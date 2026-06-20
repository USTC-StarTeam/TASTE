#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path as _WritingDevPath
import sys as _writing_sys
_WRITING_SCRIPT_ROOT = next((p for p in _WritingDevPath(__file__).resolve().parents if p.name == "scripts"), _WritingDevPath(__file__).resolve().parent)
for _writing_path in [_WRITING_SCRIPT_ROOT, *[p for p in _WRITING_SCRIPT_ROOT.iterdir() if p.is_dir()]]:
    _writing_text = str(_writing_path)
    if _writing_text not in _writing_sys.path:
        _writing_sys.path.insert(0, _writing_text)


import argparse
from collections import Counter
from pathlib import Path

from pipeline_guard import guard_fresh_base_blocker_entry
from paper_common import (
    DEFAULT_REVIEWERS,
    count_placeholder_lines,
    ensure_paper_dirs,
    extract_summary_lines,
    list_placeholder_lines,
    load_json,
    read_text,
    summarize_experiments,
    update_pipeline_state,
    write_json,
    write_text,
)
from project_paths import build_paths


def respond_to_reviews(project: str, venue_arg: str = "") -> Path:
    guard_rc = guard_fresh_base_blocker_entry(project, venue_arg, "review_response_tools.py:respond_to_reviews", safe_unblock=False)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    paper = ensure_paper_dirs(project)
    metadata = load_json(paper["paper_metadata"], {})
    aggregate = load_json(paper["aggregate_review_json"], {})
    revised = read_text(paper["revised_md"])
    venue = venue_arg or metadata.get("target_venue", "")

    blockers = aggregate.get("top_blockers", [])
    changes = aggregate.get("required_changes", [])
    title = metadata.get("title", project)

    lines = [
        f"# Author Response: {title}\n\n",
        f'- target_venue: {venue or metadata.get("target_venue", "TBD")}\n',
        f'- review_verdict: {aggregate.get("verdict", "missing-reviews")}\n\n',
        "## Response Policy\n\n",
        "- Do not argue with missing evidence. Narrow the claim, add evidence, or admit the limitation.\n",
        "- Fatal reviewer findings should be answered with delete / downgrade / new experiment / scoped limitation.\n\n",
        "## Reviewer Concerns and Planned Responses\n\n",
    ]
    for idx, blocker in enumerate(blockers[:10], start=1):
        planned = changes[idx - 1] if idx - 1 < len(changes) else "No explicit fix written yet."
        lines.append(f"### Concern {idx}\n\n")
        lines.append(f"- concern: {blocker}\n")
        lines.append(f"- planned_response: {planned}\n")
        lines.append("- status: pending evidence or claim adjustment\n\n")
    lines.extend(["## Current Revised Draft Snapshot\n\n", revised])
    write_text(paper["author_response_md"], "".join(lines))
    update_pipeline_state(project, {"author_response_ready": True, "author_response_path": str(paper["author_response_md"])}, venue=venue)
    return paper["author_response_md"]


def re_review_paper(project: str, venue_arg: str = "") -> Path:
    guard_rc = guard_fresh_base_blocker_entry(project, venue_arg, "review_response_tools.py:re_review_paper", safe_unblock=False)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    paper = ensure_paper_dirs(project)
    paths = build_paths(project)
    metadata = load_json(paper["paper_metadata"], {})
    aggregate = load_json(paper["aggregate_review_json"], {})
    author_response = read_text(paper["author_response_md"])
    revised = read_text(paper["revised_md"])
    evidence_audit = read_text(paths.reports / "paper_evidence_audit.md")
    venue = venue_arg or metadata.get("target_venue", "")

    blockers = aggregate.get("top_blockers", [])
    evidence_issues = aggregate.get("evidence_issues", [])
    unresolved = []
    for blocker in blockers:
        key = blocker.lower().split(",")[0][:80]
        if key and key not in author_response.lower() and key not in revised.lower():
            unresolved.append(blocker)
    for issue in evidence_issues:
        key = issue.lower().split(",")[0][:80]
        if key and key not in author_response.lower() and key not in revised.lower():
            unresolved.append(issue)
    unresolved = list(dict.fromkeys(unresolved))

    if aggregate.get("verdict") in {"blocked", "evidence-blocked"}:
        verdict = "still-blocked"
    elif unresolved:
        verdict = "needs-more-evidence"
    elif "## Issues" in evidence_audit:
        verdict = "needs-more-evidence"
    else:
        verdict = "ready-for-template"

    summary = {
        "verdict": verdict,
        "unresolved_blockers": unresolved[:12],
        "resolved_by_response": max(0, len(blockers) + len(evidence_issues) - len(unresolved)),
        "original_blocker_count": len(blockers),
        "evidence_issue_count": len(evidence_issues),
    }
    write_json(paper["re_review_json"], summary)
    lines = [
        "# Re-Review Summary\n\n",
        f"- verdict: {verdict}\n",
        f"- original_blocker_count: {len(blockers)}\n",
        f'- evidence_issue_count: {len(evidence_issues)}\n',
        f'- resolved_by_response: {summary["resolved_by_response"]}\n',
        f"- unresolved_count: {len(unresolved)}\n\n",
        "## Unresolved Blockers\n\n",
    ]
    if unresolved:
        for blocker in unresolved[:12]:
            lines.append(f"- {blocker}\n")
    else:
        lines.append("- No unresolved blocker detected in this re-review.\n")
    write_text(paper["re_review_md"], "".join(lines))
    update_pipeline_state(project, {
        "re_review_ready": True,
        "re_review_verdict": verdict,
        "re_review_path": str(paper["re_review_md"]),
        "promotion_gate": "allow-template" if verdict == "ready-for-template" else "hold-markdown-only",
    }, venue=venue)
    return paper["re_review_md"]


def write_comparison(project: str, topic: str, content: str) -> Path:
    paths = build_paths(project)
    slug = topic.lower().replace(" ", "-").replace("/", "-")
    out = paths.wiki_comparisons / f"{slug}-comparison.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"# {topic} Comparison\n\n{content}\n", encoding="utf-8")
    return out


# ---- internal paper review ----
def verdict_from_score(score: int) -> str:
    if score <= 2:
        return 'block'
    if score == 3:
        return 'major-revision'
    if score == 4:
        return 'minor-revision'
    return 'pass'

def build_review(reviewer: dict[str, str], draft_text: str, experiments: list[dict], reflection: str, paper_quality: str) -> dict[str, object]:
    summary = summarize_experiments(experiments)
    placeholders = count_placeholder_lines(draft_text)
    placeholder_lines = list_placeholder_lines(draft_text, limit=8)
    findings: list[str] = []
    required_changes: list[str] = []
    positives: list[str] = []
    score = 5
    name = reviewer['name']
    if summary['completed_count'] == 0:
        findings.append('No completed run exists, so the paper currently has no executed evidence behind its central story.')
        required_changes.append('Execute at least one complete baseline run before strengthening the paper narrative.')
        score = min(score, 2)
    else:
        positives.append(f"There is at least one completed run in the registry ({summary['completed_count']} total).")
    if placeholders:
        findings.append(f'The draft still contains {placeholders} placeholder lines, which means several sections are structurally unfinished.')
        required_changes.append('Replace placeholder bullets with concrete, evidence-backed prose or explicit scoped TODOs tied to experiments.')
        score = min(score, 3)
    if name == 'novelty_reviewer':
        if 'Closest prior work' in draft_text or 'closest prior work' in draft_text:
            positives.append('The draft reserves a dedicated novelty-delta section instead of hiding novelty in vague language.')
        findings.append('The novelty section is not yet anchored to a named nearest-neighbor paper or strongest baseline family.')
        required_changes.append('Name the exact closest prior work and state the narrowest defensible delta over it.')
        score = min(score, 3)
    elif name == 'claim_reviewer':
        best = summary.get('best')
        if not best or float(best.get('metric_value', 0.0) or 0.0) <= 0.0:
            findings.append('The strongest logged metric is absent or weak, so the draft cannot support a strong performance claim yet.')
            required_changes.append('Either improve the strongest run or narrow the claim to match the existing evidence.')
            score = min(score, 2)
        if summary['claim_checked_count'] == 0:
            findings.append('No run has an explicit claim verdict, so the loop is not yet testing whether the paper claim is actually true.')
            required_changes.append('Attach claim verdicts to executed runs and use them to decide whether the abstract should be weakened.')
            score = min(score, 2)
    elif name == 'counterexample_reviewer':
        if 'counterexample' not in draft_text.lower():
            findings.append('The paper does not name a concrete counterexample or stress setting that would most damage the central claim.')
            required_changes.append('Write one concrete counterexample and explain what outcome would falsify the claim.')
            score = min(score, 2)
        if 'counterexample_pressure: low' in paper_quality.lower():
            findings.append('Upstream quality analysis already says counterexample pressure is low, which is a serious top-tier weakness.')
            required_changes.append('Increase falsification pressure with stress tests, scope limits, and negative evidence.')
            score = min(score, 2)
    elif name == 'bad_case_reviewer':
        if summary['bad_case_count'] == 0:
            findings.append('No executed run logged a machine-readable bad-case artifact, so error analysis is too shallow.')
            required_changes.append('Emit bad_cases.json or an equivalent artifact and analyze the worst slice explicitly.')
            score = min(score, 2)
        if not extract_summary_lines(reflection, limit=20):
            findings.append('The reflection record is too thin to explain what the loop learned from failures.')
            required_changes.append('Strengthen failure reflection so the next iteration is driven by actual weak slices.')
            score = min(score, 3)
    elif name == 'taste_reviewer':
        if 'automatically assembled' in draft_text.lower():
            findings.append('The prose still reads like a scaffold rather than a deliberate top-tier paper narrative.')
            required_changes.append('Replace scaffold language with a sharper story about the changed assumption, bottleneck, or capability.')
            score = min(score, 3)
        if 'prune' not in draft_text.lower():
            findings.append('The draft does not visibly show that weak directions were pruned, which makes the loop look under-disciplined.')
            required_changes.append('Document one real prune or pivot decision and the evidence that triggered it.')
            score = min(score, 3)
    if not positives:
        positives.append('The pipeline already supports a structured Markdown draft and internal review pass, which is a solid starting point.')
    return {'reviewer': name, 'focus': reviewer['focus'], 'score': score, 'verdict': verdict_from_score(score), 'findings': findings, 'required_changes': required_changes, 'positives': positives, 'placeholder_examples': placeholder_lines}

def render_markdown(title: str, review: dict[str, object]) -> str:
    findings = '\n'.join((f'- {item}' for item in review['findings'])) or '- No critical findings.'
    changes = '\n'.join((f'- {item}' for item in review['required_changes'])) or '- No required changes.'
    positives = '\n'.join((f'- {item}' for item in review['positives'])) or '- No positive signals yet.'
    placeholders = '\n'.join((f'- {item}' for item in review['placeholder_examples'])) or '- None logged.'
    return f"# Internal Review: {title}\n\n## Reviewer\n\n- reviewer: {review['reviewer']}\n- focus: {review['focus']}\n- score: {review['score']}\n- verdict: {review['verdict']}\n\n## Positive Signals\n\n{positives}\n\n## Findings\n\n{findings}\n\n## Required Changes\n\n{changes}\n\n## Placeholder Examples\n\n{placeholders}\n"

def run_review_paper(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--reviewer', action='append', default=[])
    parser.add_argument('--venue', default='')
    args = parser.parse_args(argv)
    paths = build_paths(args.project)
    paper = ensure_paper_dirs(args.project)
    metadata = load_json(paper['paper_metadata'], {})
    title = metadata.get('title', args.project)
    venue = args.venue or metadata.get('target_venue', '')
    draft_text = read_text(paper['draft_md'])
    reflection = read_text(paths.reports / 'iteration_reflection.md')
    paper_quality = read_text(paths.planning / 'paper_quality.md')
    experiments = load_json(paths.state / 'experiment_registry.json', [])
    selected = args.reviewer or [row['name'] for row in DEFAULT_REVIEWERS]
    created = []
    review_index = []
    reviewer_map = {row['name']: row for row in DEFAULT_REVIEWERS}
    for name in selected:
        reviewer = reviewer_map.get(name)
        if reviewer is None:
            raise SystemExit(f'Unknown reviewer: {name}')
        review = build_review(reviewer, draft_text, experiments, reflection, paper_quality)
        md_path = paper['review_internal_dir'] / f'{name}.md'
        json_path = paper['review_internal_dir'] / f'{name}.json'
        write_text(md_path, render_markdown(title, review))
        write_json(json_path, review)
        created.append(str(md_path))
        review_index.append({'reviewer': name, 'markdown_path': str(md_path), 'json_path': str(json_path)})
    update_pipeline_state(args.project, {'internal_reviewers': selected, 'internal_reviews_ready': True, 'internal_review_count': len(selected), 'internal_review_index': review_index}, venue=venue)
    for path in created:
        print(path)


# ---- aggregated internal review ----
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
    if sum((1 for row in experiments if str(row.get('status', '')).lower() in {'completed', 'success'})) == 0:
        issues.append('No completed experiment exists.')
    if sum((1 for row in experiments if row.get('audit_ready'))) == 0:
        issues.append('No audit-ready experiment exists.')
    if sum((1 for row in experiments if row.get('bad_case_slices'))) == 0:
        issues.append('No bad-case slice evidence exists.')
    if sum((1 for row in experiments if row.get('claim_verdict'))) == 0:
        issues.append('No claim verdict evidence exists.')
    if sum((1 for row in experiments if row.get('counterexample_outcome'))) == 0:
        issues.append('No counterexample evidence exists.')
    if len(methods) >= 2 and (not any((row.get('recommendation') in PRUNE_RECOMMENDATIONS for row in methods))):
        issues.append('No method has been pruned or paused yet.')
    for claim in claim_ledger.get('claims', []):
        if claim.get('status') in {'unsupported', 'weak'}:
            issues.append(f"Claim {claim.get('claim_type')} is still unsupported.")
    return issues

def run_aggregate_reviews(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    args = parser.parse_args(argv)
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
    promotion_gate = 'allow-template' if verdict == 'ready-for-template' and (not evidence_issues) else 'hold-markdown-only'
    aggregate = {'review_count': len(reviews), 'reviewer_scores': reviewer_scores, 'verdict': verdict, 'fatal_reviewers': fatal_reviewers, 'weakest_dimensions': weakest_dimensions, 'top_blockers': dedup_blockers[:10], 'required_changes': dedup_changes[:12], 'positive_signals': dedup_positives[:8], 'evidence_issues': evidence_issues, 'evidence_gate': 'pass' if not evidence_issues else 'fail', 'promotion_gate': promotion_gate}
    write_json(paper['aggregate_review_json'], aggregate)
    md = ['# Aggregated Internal Review\n\n', f"- verdict: {aggregate['verdict']}\n", f"- review_count: {aggregate['review_count']}\n", f"- fatal_reviewers: {(', '.join(fatal_reviewers) if fatal_reviewers else 'none')}\n", f"- weakest_dimensions: {(', '.join(weakest_dimensions) if weakest_dimensions else 'none')}\n", f"- evidence_gate: {aggregate['evidence_gate']}\n", f"- promotion_gate: {aggregate['promotion_gate']}\n\n", '## Reviewer Scores\n\n']
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
    update_pipeline_state(args.project, {'paper_reviews_ready': len(reviews) > 0, 'paper_review_count': len(reviews), 'paper_review_verdict': verdict, 'aggregated_review_path': str(paper['aggregate_review_md']), 'aggregated_review_json': str(paper['aggregate_review_json']), 'fatal_reviewers': fatal_reviewers, 'promotion_gate': promotion_gate}, venue=venue)
    print(paper['aggregate_review_md'])


def main() -> int:
    parser = argparse.ArgumentParser(description="Writing review/response helper actions.")
    parser.add_argument("--tool-action", required=True, choices=["respond", "re_review", "comparison", "review_paper", "aggregate_reviews"])
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    parser.add_argument("--topic", default="")
    parser.add_argument("--content", default="")
    parser.add_argument("--reviewer", action="append", default=[])
    args = parser.parse_args()
    if args.tool_action == "respond":
        print(respond_to_reviews(args.project, args.venue))
    elif args.tool_action == "re_review":
        print(re_review_paper(args.project, args.venue))
    elif args.tool_action == "review_paper":
        review_args = ["--project", args.project]
        for reviewer in args.reviewer:
            review_args.extend(["--reviewer", reviewer])
        if args.venue:
            review_args.extend(["--venue", args.venue])
        run_review_paper(review_args)
    elif args.tool_action == "aggregate_reviews":
        aggregate_args = ["--project", args.project]
        if args.venue:
            aggregate_args.extend(["--venue", args.venue])
        run_aggregate_reviews(aggregate_args)
    else:
        if not args.topic or not args.content:
            raise SystemExit("--topic and --content are required for --tool-action comparison")
        print(write_comparison(args.project, args.topic, args.content))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
