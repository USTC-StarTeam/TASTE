#!/usr/bin/env python3
from __future__ import annotations

import argparse

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

    return {
        'reviewer': name,
        'focus': reviewer['focus'],
        'score': score,
        'verdict': verdict_from_score(score),
        'findings': findings,
        'required_changes': required_changes,
        'positives': positives,
        'placeholder_examples': placeholder_lines,
    }


def render_markdown(title: str, review: dict[str, object]) -> str:
    findings = '\n'.join(f"- {item}" for item in review['findings']) or '- No critical findings.'
    changes = '\n'.join(f"- {item}" for item in review['required_changes']) or '- No required changes.'
    positives = '\n'.join(f"- {item}" for item in review['positives']) or '- No positive signals yet.'
    placeholders = '\n'.join(f"- {item}" for item in review['placeholder_examples']) or '- None logged.'
    return f'''# Internal Review: {title}

## Reviewer

- reviewer: {review['reviewer']}
- focus: {review['focus']}
- score: {review['score']}
- verdict: {review['verdict']}

## Positive Signals

{positives}

## Findings

{findings}

## Required Changes

{changes}

## Placeholder Examples

{placeholders}
'''


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--reviewer', action='append', default=[])
    parser.add_argument('--venue', default='')
    args = parser.parse_args()

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

    update_pipeline_state(args.project, {
        'internal_reviewers': selected,
        'internal_reviews_ready': True,
        'internal_review_count': len(selected),
        'internal_review_index': review_index,
    }, venue=venue)
    for path in created:
        print(path)


if __name__ == '__main__':
    main()
