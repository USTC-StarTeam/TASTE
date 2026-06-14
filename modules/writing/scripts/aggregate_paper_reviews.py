#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter

from paper_common import ensure_paper_dirs, load_json, update_pipeline_state, write_json, write_text
from project_paths import build_paths

PRUNE_RECOMMENDATIONS = {'compare_then_prune_or_pause', 'pause_or_prune'}


def verdict_from_scores(scores: list[int]) -> str:
    if not scores:
        return 'missing-reviews'
    if min(scores) <= 2:
        return 'blocked'
    if min(scores) == 3:
        return 'major-revision'
    if sum(scores) / len(scores) < 4.5:
        return 'minor-revision'
    return 'ready-for-template'


def build_evidence_issues(paths) -> list[str]:
    experiments = load_json(paths.state / 'experiment_registry.json', [])
    next_actions = load_json(paths.state / 'next_actions.json', {'method_summaries': []})
    claim_ledger = load_json(paths.state / 'claim_ledger.json', {'claims': []})
    methods = next_actions.get('method_summaries', []) if isinstance(next_actions, dict) else []
    issues = []
    if sum(1 for row in experiments if str(row.get('status', '')).lower() in {'completed', 'success'}) == 0:
        issues.append('No completed experiment exists.')
    if sum(1 for row in experiments if row.get('audit_ready')) == 0:
        issues.append('No audit-ready experiment exists.')
    if sum(1 for row in experiments if row.get('bad_case_slices')) == 0:
        issues.append('No bad-case slice evidence exists.')
    if sum(1 for row in experiments if row.get('claim_verdict')) == 0:
        issues.append('No claim verdict evidence exists.')
    if sum(1 for row in experiments if row.get('counterexample_outcome')) == 0:
        issues.append('No counterexample evidence exists.')
    if len(methods) >= 2 and not any(row.get('recommendation') in PRUNE_RECOMMENDATIONS for row in methods):
        issues.append('No method has been pruned or paused yet.')
    for claim in claim_ledger.get('claims', []):
        if claim.get('status') in {'unsupported', 'weak'}:
            issues.append(f"Claim {claim.get('claim_type')} is still unsupported.")
    return issues


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    args = parser.parse_args()

    paper = ensure_paper_dirs(args.project)
    paths = build_paths(args.project)
    metadata = load_json(paper['paper_metadata'], {})
    venue = args.venue or metadata.get('target_venue', '')
    reviews = []
    for path in sorted(paper['review_internal_dir'].glob('*.json')):
        reviews.append(load_json(path, {}))
    scores = [int(review.get('score', 0)) for review in reviews if review]
    verdict = verdict_from_scores(scores)
    blockers: list[str] = []
    positives: list[str] = []
    required_changes: list[str] = []
    reviewer_scores = []
    weakness_counter = Counter()
    fatal_reviewers = []
    for review in reviews:
        reviewer_scores.append({'reviewer': review.get('reviewer', ''), 'score': review.get('score', 0), 'verdict': review.get('verdict', '')})
        if review.get('verdict') == 'block':
            fatal_reviewers.append(review.get('reviewer', ''))
        for item in review.get('findings', []):
            blockers.append(item)
            lowered = str(item).lower()
            if 'novelty' in lowered or 'prior work' in lowered or 'baseline' in lowered:
                weakness_counter['novelty'] += 1
            if 'claim' in lowered or 'metric' in lowered or 'evidence' in lowered:
                weakness_counter['claim_strength'] += 1
            if 'counterexample' in lowered or 'falsif' in lowered or 'stress' in lowered:
                weakness_counter['counterexample'] += 1
            if 'bad-case' in lowered or 'error analysis' in lowered or 'slice' in lowered:
                weakness_counter['bad_case'] += 1
            if 'prune' in lowered or 'narrative' in lowered or 'top-tier' in lowered:
                weakness_counter['taste_and_prune'] += 1
        positives.extend(review.get('positives', []))
        required_changes.extend(review.get('required_changes', []))

    evidence_issues = build_evidence_issues(paths)
    dedup_blockers = list(dict.fromkeys(blockers))
    dedup_changes = list(dict.fromkeys(required_changes + evidence_issues))
    dedup_positives = list(dict.fromkeys(positives))
    weakest_dimensions = [name for name, _ in weakness_counter.most_common(5)]
    if evidence_issues and verdict == 'ready-for-template':
        verdict = 'evidence-blocked'
    promotion_gate = 'allow-template' if verdict == 'ready-for-template' and not evidence_issues else 'hold-markdown-only'
    aggregate = {
        'review_count': len(reviews),
        'reviewer_scores': reviewer_scores,
        'verdict': verdict,
        'fatal_reviewers': fatal_reviewers,
        'weakest_dimensions': weakest_dimensions,
        'top_blockers': dedup_blockers[:10],
        'required_changes': dedup_changes[:12],
        'positive_signals': dedup_positives[:8],
        'evidence_issues': evidence_issues,
        'evidence_gate': 'pass' if not evidence_issues else 'fail',
        'promotion_gate': promotion_gate,
    }
    write_json(paper['aggregate_review_json'], aggregate)
    md = [
        '# Aggregated Internal Review\n\n',
        f"- verdict: {aggregate['verdict']}\n",
        f"- review_count: {aggregate['review_count']}\n",
        f"- fatal_reviewers: {', '.join(fatal_reviewers) if fatal_reviewers else 'none'}\n",
        f"- weakest_dimensions: {', '.join(weakest_dimensions) if weakest_dimensions else 'none'}\n",
        f"- evidence_gate: {aggregate['evidence_gate']}\n",
        f"- promotion_gate: {aggregate['promotion_gate']}\n\n",
        '## Reviewer Scores\n\n',
    ]
    for row in reviewer_scores:
        md.append(f"- {row['reviewer']}: score={row['score']}, verdict={row['verdict']}\n")
    md.append('\n## Evidence Gate Issues\n\n')
    if evidence_issues:
        for item in evidence_issues:
            md.append(f'- {item}\n')
    else:
        md.append('- No evidence-gate issue recorded.\n')
    md.append('\n## Top Blockers\n\n')
    if dedup_blockers:
        for item in dedup_blockers[:10]:
            md.append(f'- {item}\n')
    else:
        md.append('- No blocker recorded.\n')
    md.append('\n## Required Changes\n\n')
    if dedup_changes:
        for item in dedup_changes[:12]:
            md.append(f'- {item}\n')
    else:
        md.append('- No required change recorded.\n')
    md.append('\n## Positive Signals\n\n')
    if dedup_positives:
        for item in dedup_positives[:8]:
            md.append(f'- {item}\n')
    else:
        md.append('- No positive signal recorded.\n')
    write_text(paper['aggregate_review_md'], ''.join(md))

    update_pipeline_state(args.project, {
        'paper_reviews_ready': len(reviews) > 0,
        'paper_review_count': len(reviews),
        'paper_review_verdict': verdict,
        'aggregated_review_path': str(paper['aggregate_review_md']),
        'aggregated_review_json': str(paper['aggregate_review_json']),
        'fatal_reviewers': fatal_reviewers,
        'promotion_gate': promotion_gate,
    }, venue=venue)
    print(paper['aggregate_review_md'])


if __name__ == '__main__':
    main()
