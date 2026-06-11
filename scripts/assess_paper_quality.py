#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from literature_policy import build_literature_policy, now_utc, paper_sort_key, score_paper
from project_paths import build_paths, load_project_config

WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_\-/]{2,}")

NOVELTY_POSITIVE = {
    'novel', 'new', 'first', 'unified', 'generalizable', 'generalization', 'framework', 'paradigm', 'scaling', 'frontier',
}
NOVELTY_NEGATIVE = {
    'simple', 'incremental', 'efficient', 'faster', 'lightweight', 'tuning', 'adapter', 'refinement', 'engineering',
}
CLAIM_POSITIVE = {
    'ablation', 'benchmark', 'benchmarks', 'evaluation', 'compare', 'comparison', 'analysis', 'robustness', 'error',
}
COUNTEREXAMPLE_POSITIVE = {
    'failure', 'failures', 'limitation', 'limitations', 'counterexample', 'adversarial', 'stress', 'robustness', 'worst-case',
}
POSITIVE = {
    'assumption', 'generalization', 'scaling', 'long-horizon', 'reasoning', 'compositional', 'multimodal', 'search', 'agent',
}


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def save_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def tokenize(text: str) -> set[str]:
    return {token.lower() for token in WORD_RE.findall(text or '')}


def bucket(score: int) -> str:
    if score >= 4:
        return 'high'
    if score <= 1:
        return 'low'
    return 'medium'


def capped_count(tokens: set[str], keywords: set[str]) -> int:
    return sum(1 for token in tokens if token in keywords)


def heuristic_signals(meta: dict[str, object]) -> dict[str, object]:
    title = str(meta.get('title', '') or '')
    summary = str(meta.get('summary', '') or meta.get('tldr', '') or '')
    text = f"{title} {summary}".lower()
    tokens = tokenize(text)

    novelty_score = 2 + min(2, capped_count(tokens, NOVELTY_POSITIVE)) - min(2, capped_count(tokens, NOVELTY_NEGATIVE))
    claim_score = 1 + min(3, capped_count(tokens, CLAIM_POSITIVE))
    counterexample_score = min(4, capped_count(tokens, COUNTEREXAMPLE_POSITIVE))
    taste_score = 1 + min(3, capped_count(tokens, POSITIVE))

    broad_claim = any(phrase in text for phrase in ['state-of-the-art', 'sota', 'general', 'generalizable', 'frontier'])
    has_limitations = any(token in tokens for token in {'limitation', 'limitations', 'failure', 'failures'})
    if broad_claim and not has_limitations:
        counterexample_score = min(4, counterexample_score + 1)
    if 'benchmark' in text or 'benchmarks' in text:
        claim_score = min(4, claim_score + 1)
    if 'ablation' in text:
        claim_score = min(4, claim_score + 1)
    if 'new dataset' in text or 'new benchmark' in text:
        novelty_score = min(4, novelty_score + 1)

    novelty = bucket(max(0, novelty_score))
    claim_strength = bucket(max(0, claim_score))
    counterexample_pressure = bucket(counterexample_score)
    taste = bucket(max(0, taste_score))
    return {
        'title': title,
        'novelty': novelty,
        'claim_strength': claim_strength,
        'counterexample_pressure': counterexample_pressure,
        'taste': taste,
        'broad_claim': broad_claim,
    }


def venue_short_label(row: dict[str, object]) -> str:
    candidates = row.get('venue_candidates', []) or []
    if candidates:
        return ', '.join(str(value) for value in candidates[:2])
    return 'unknown'


def quality_row(meta: dict[str, object], cfg: dict[str, object], reference_time) -> dict[str, object]:
    scored = score_paper(meta, cfg, reference_time=reference_time)
    base = heuristic_signals(meta)
    top_tier_ready = 'watch'
    if scored.get('selection_bucket') == 'recent_high_priority' and base['novelty'] != 'low' and base['claim_strength'] != 'low' and base['taste'] != 'low':
        top_tier_ready = 'promising'
    elif scored.get('selection_bucket') == 'deprioritized' and not scored.get('foundational_keep'):
        top_tier_ready = 'weak'
    elif base['novelty'] == 'low' and scored.get('selection_bucket') != 'older_foundational':
        top_tier_ready = 'weak'

    concerns: list[str] = []
    if scored.get('selection_bucket') == 'deprioritized' and not scored.get('foundational_keep'):
        concerns.append('This paper falls outside the preferred recent literature window and does not look strong enough to anchor the loop.')
    if scored.get('stale_penalty_active') and not scored.get('foundational_keep'):
        concerns.append('The paper is aging relative to the current field and should not dominate initialization unless it is uniquely relevant.')
    if base['novelty'] == 'low':
        concerns.append('The apparent contribution looks incremental relative to nearby work.')
    if base['claim_strength'] == 'low':
        concerns.append('The summary does not yet signal decisive evaluation, ablations, or strong comparison discipline.')
    if base['counterexample_pressure'] == 'high':
        concerns.append('The paper appears to make broad claims without enough visible falsification pressure.')
    if base['taste'] == 'low':
        concerns.append('The framing does not obviously move a field-level assumption or central bottleneck.')
    if scored.get('actionability_score', 0) == 0:
        concerns.append('The paper currently looks hard to borrow from directly because code/reproducibility cues are weak at abstract level.')
    if not concerns:
        concerns.append('The current signals make this worth deeper reading, but abstract-level screening is still only a first pass.')

    next_checks: list[str] = []
    if scored.get('selection_bucket') in {'recent_candidate', 'deprioritized'}:
        next_checks.append('Verify that a newer, higher-tier paper has not already dominated this angle in the last 6-12 months.')
    if base['novelty'] != 'high':
        next_checks.append('Map the nearest-neighbor papers and force a precise delta claim before investing execution budget.')
    if base['claim_strength'] != 'high':
        next_checks.append('Inspect whether the paper really includes decisive baselines, ablations, and stress tests.')
    if base['counterexample_pressure'] != 'low':
        next_checks.append("List what evidence would falsify the paper's core claim and whether the paper already covers it.")
    if base['taste'] != 'high':
        next_checks.append('Check whether success would change a meaningful community assumption or only polish a known recipe.')
    if scored.get('actionability_score', 0) < 2:
        next_checks.append('Search for linked code, reproducibility notes, or companion repos before allocating large implementation budget.')
    if scored.get('selection_bucket') == 'older_foundational':
        next_checks.append('Use this as a foundation or control, not as proof that the current frontier is still open.')
    if not next_checks:
        next_checks.append('Translate the central claim into a reproducible benchmark-level experiment contract.')

    return {
        'paper_id': meta.get('paper_id', ''),
        'title': meta.get('title', ''),
        'source': meta.get('source', ''),
        **base,
        **scored,
        'claim_ready_anchor': bool(meta.get('claim_ready_anchor', False)),
        'top_tier_readiness': top_tier_ready,
        'concerns': concerns,
        'next_checks': next_checks,
    }


def append_group(lines: list[str], title: str, rows: list[dict[str, object]]) -> None:
    lines.append(f'## {title}\n\n')
    if not rows:
        lines.append('- None yet.\n\n')
        return
    for row in rows:
        lines.append(
            f"- `{row['paper_id']}` | source={row.get('source', '')} | score={row.get('discovery_priority_score', 0)} | idea={row.get('idea_worthiness_score', 0)} | "
            f"bucket={row.get('selection_bucket', '')} | recency={row.get('recency_bucket', '')} | age_days={row.get('paper_age_days', '')} | venue={venue_short_label(row)} | title={row.get('title', '')}\n"
        )
    lines.append('\n')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    reference_time = now_utc()
    policy = build_literature_policy(cfg)
    assessments: list[dict[str, object]] = []
    paper_dirs = sorted([p for p in paths.raw_papers.iterdir() if p.is_dir()]) if paths.raw_papers.exists() else []
    for paper_dir in paper_dirs:
        metadata_path = paper_dir / 'metadata.json'
        meta = load_json(metadata_path)
        row = quality_row(meta, cfg, reference_time=reference_time)
        assessments.append(row)
        updated_meta = dict(meta)
        for key in [
            'published_at',
            'paper_age_days',
            'recency_bucket',
            'recency_score',
            'within_primary_window',
            'within_secondary_window',
            'required_topic_groups',
            'topic_group_hits',
            'missing_topic_groups',
            'hard_topic_mismatch',
            'hard_missing_topic_groups',
            'venue_candidates',
            'venue_matches',
            'journal_matches',
            'venue_quality',
            'venue_score',
            'topic_match_score',
            'hard_mismatch_penalty',
            'missing_soft_axis_penalty',
            'citation_signal',
            'actionability_score',
            'foundational_keep',
            'not_positive_support',
            'selection_bucket',
            'discovery_priority_score',
            'idea_worthiness_score',
            'high_quality_recent',
            'top_tier_readiness',
        ]:
            updated_meta[key] = row.get(key)
        if row.get('not_positive_support'):
            updated_meta['weak_candidate_for_critique'] = True
            updated_meta['guardrail'] = updated_meta.get('guardrail') or (
                'This item is retained for critique/search expansion only, not as positive paper support.'
            )
        save_json(metadata_path, updated_meta)

    assessments = sorted(assessments, key=paper_sort_key)
    summary = {
        'paper_count': len(assessments),
        'recent_high_priority_count': sum(1 for row in assessments if row.get('selection_bucket') == 'recent_high_priority'),
        'recent_candidate_count': sum(1 for row in assessments if row.get('selection_bucket') == 'recent_candidate'),
        'older_foundational_count': sum(1 for row in assessments if row.get('selection_bucket') == 'older_foundational'),
        'deprioritized_count': sum(1 for row in assessments if row.get('selection_bucket') == 'deprioritized'),
        'promising_count': sum(1 for row in assessments if row.get('top_tier_readiness') == 'promising'),
    }
    payload = {
        'generated_at': reference_time.isoformat(),
        'reference_time': reference_time.isoformat(),
        'literature_policy': policy,
        'summary': summary,
        'papers': assessments,
    }
    (paths.state / 'paper_quality.json').write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + '\n',
        encoding='utf-8',
    )

    recent_high = [row for row in assessments if row.get('selection_bucket') == 'recent_high_priority']
    recent_candidates = [row for row in assessments if row.get('selection_bucket') == 'recent_candidate']
    older_foundational = [row for row in assessments if row.get('selection_bucket') == 'older_foundational']

    lines = ['# Paper Quality Assessment\n\n']
    lines.extend([
        f"- reference_time_utc: {reference_time.isoformat()}\n",
        f"- primary_window_days: {policy.get('primary_window_days', 180)}\n",
        f"- secondary_window_days: {policy.get('secondary_window_days', 365)}\n",
        f"- deprioritize_older_than_days: {policy.get('deprioritize_older_than_days', 730)}\n",
        f"- paper_count: {summary['paper_count']}\n",
        f"- recent_high_priority_count: {summary['recent_high_priority_count']}\n",
        f"- recent_candidate_count: {summary['recent_candidate_count']}\n",
        f"- older_foundational_count: {summary['older_foundational_count']}\n",
        f"- deprioritized_count: {summary['deprioritized_count']}\n\n",
        '## Screening Rule\n\n',
        '- Default reading priority should emphasize papers from roughly the last 6 months, then the last year, with explicit preference for top AI venues and top journals relevant to the topic.\n',
        '- arXiv and other fresh sources are valuable, but they should be weighted by topic fit, venue/journal quality when known, citation signal, and borrowability into real experiments.\n',
        '- Older papers should only survive as foundations, controls, or still-unbeaten references.\n\n',
    ])

    append_group(lines, 'Recent High-Priority Papers', recent_high)
    append_group(lines, 'Recent Candidate Papers', recent_candidates)
    append_group(lines, 'Older Foundational Keepers', older_foundational)

    if not assessments:
        lines.append('- No imported papers available yet.\n')
    for row in assessments:
        lines.extend([
            f"## {row['paper_id']}\n\n",
            f"- title: {row['title']}\n",
            f"- source: {row.get('source', '')}\n",
            f"- top_tier_readiness: {row['top_tier_readiness']}\n",
            f"- selection_bucket: {row.get('selection_bucket', '')}\n",
            f"- discovery_priority_score: {row.get('discovery_priority_score', 0)}\n",
            f"- idea_worthiness_score: {row.get('idea_worthiness_score', 0)}\n",
            f"- recency_bucket: {row.get('recency_bucket', '')}\n",
            f"- paper_age_days: {row.get('paper_age_days', '')}\n",
            f"- venue_quality: {row.get('venue_quality', '')}\n",
            f"- venue_candidates: {', '.join(row.get('venue_candidates', []) or [])}\n",
            f"- novelty: {row['novelty']}\n",
            f"- claim_strength: {row['claim_strength']}\n",
            f"- counterexample_pressure: {row['counterexample_pressure']}\n",
            f"- taste: {row['taste']}\n",
            f"- topic_match_score: {row.get('topic_match_score', 0)}\n",
            f"- citation_signal: {row.get('citation_signal', 0)}\n",
            f"- actionability_score: {row.get('actionability_score', 0)}\n",
            f"- foundational_keep: {row.get('foundational_keep', False)}\n",
            f"- broad_claim_present: {row['broad_claim']}\n",
            '\n### Key Concerns\n',
        ])
        for concern in row['concerns']:
            lines.append(f'- {concern}\n')
        lines.append('\n### Required Follow-up Checks\n')
        for item in row['next_checks']:
            lines.append(f'- {item}\n')
        lines.append('\n')
    lines.append('## Usage Note\n')
    lines.append('- These are screening and prioritization signals for the literature loop. They sharpen search and selection, but they do not replace full-paper reading or direct evidence checks.\n')
    out = paths.planning / 'paper_quality.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
