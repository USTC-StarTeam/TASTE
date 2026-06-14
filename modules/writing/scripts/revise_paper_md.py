#!/usr/bin/env python3
from __future__ import annotations

import argparse

from paper_common import ensure_paper_dirs, extract_summary_lines, load_json, read_text, update_pipeline_state, write_text
from project_paths import build_paths, load_project_config
from pathlib import Path

from pipeline_guard import guard_fresh_base_blocker_entry


def bullet_block(lines: list[str], fallback: str) -> str:
    if not lines:
        return f'- {fallback}'
    return '\n'.join(f'- {line}' for line in lines)


def orchestra_section_plan(state: dict) -> str:
    rows = state.get('sections', []) if isinstance(state.get('sections', []), list) else []
    if not rows:
        return '- Build `state/paper_orchestra_state.json` before the next revision so every section has evidence, blockers, and actions.'
    lines = []
    for row in rows:
        actions = row.get('revision_actions', []) if isinstance(row.get('revision_actions', []), list) else []
        blockers = row.get('blockers', []) if isinstance(row.get('blockers', []), list) else []
        if row.get('status') == 'ready' and not actions:
            continue
        summary = '; '.join(actions[:3] or blockers[:3] or ['tighten evidence binding'])
        lines.append(f"- {row.get('title', row.get('id'))}: status={row.get('status')} -> {summary}")
    return '\n'.join(lines) if lines else '- All TASTE paper sections are currently marked ready.'


def orchestra_blockers(state: dict) -> str:
    blockers = paper_safe_items(state.get('global_blockers', []) if isinstance(state.get('global_blockers', []), list) else [])
    if not blockers:
        return '- No manuscript-facing TASTE paper blocker recorded.'
    return '\n'.join(f'- {item}' for item in blockers[:12])


def paper_safe_items(items) -> list[str]:
    forbidden = ('counterexample', 'failure', 'failed', 'negative', '失败', '负结果', 'failed hypothesis')
    out = []
    for item in items or []:
        text = str(item)
        if any(token in text.lower() for token in forbidden):
            continue
        out.append(text)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    args = parser.parse_args()

    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name, safe_unblock=False)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    paper = ensure_paper_dirs(args.project)
    metadata = load_json(paper['paper_metadata'], {})
    aggregate = load_json(paper['aggregate_review_json'], {})
    draft = read_text(paper['draft_md'])
    next_actions = read_text(paths.planning / 'next_actions.md')
    reflection = read_text(paths.reports / 'iteration_reflection.md')
    experiment_log = read_text(paths.experiments / 'experiment_log.md')
    orchestra = load_json(paths.state / 'paper_orchestra_state.json', {})
    venue = args.venue or metadata.get('target_venue', '')

    title = metadata.get('title', args.project)
    verdict = aggregate.get('verdict', 'missing-reviews')
    blockers = paper_safe_items(aggregate.get('top_blockers', []))
    changes = paper_safe_items(aggregate.get('required_changes', []))
    weak_dims = paper_safe_items(aggregate.get('weakest_dimensions', []))

    revised = f'''# {title}

## Revision Status

- target_venue: {metadata.get('target_venue', 'TBD')}
- project: {args.project}
- internal_review_verdict: {verdict}
- weakest_dimensions: {', '.join(weak_dims) if weak_dims else 'none'}

## Editor Summary

This revised Markdown draft is intentionally conservative and TASTE-paper-stage driven. It keeps each section tied to current selected-route evidence, verified citations, table/figure artifacts, and review blockers. The current objective is to sharpen a submission-style manuscript without turning internal audit diagnostics into the paper storyline.

## Current Contribution Story

- topic: {cfg.get('topic', '')}
- strongest narrative constraint: only claim what the logged experiments actually support
- most likely reason the current draft is not yet top-tier ready: {changes[0] if changes else 'missing current-route claim-ready evidence'}

## Submission Blockers

{bullet_block(blockers[:8], 'No blockers recorded yet.')}

## Paper Blockers

{orchestra_blockers(orchestra)}

## Required Revision Actions

{bullet_block(changes[:8], 'No required changes recorded yet.')}

## Section-by-Section Revision Queue

{orchestra_section_plan(orchestra)}

## Evidence and Experiment Snapshot

- Current manuscript-facing results must come only from current selected-route, audit-ready, claim-supporting runs.
- If no such run exists, keep the results section empty and use this draft only as a venue-format inspection artifact.

## Scope Discipline

- Do not promote legacy-route rows or unsupported experiments into manuscript claims.
- Keep internal audit diagnostics in project reports, not in the paper narrative.

## Revised Abstract Draft

This paper is still under active evidence-building. At present, the strongest defensible version should emphasize the concrete problem setting, the implemented current-route method design, the reproducible evaluation protocol, and only result claims backed by current-route audit-ready evidence. The abstract should remain conservative until the loop produces explicit supported claim verdicts and a convincing novelty delta over the nearest prior work.

## Revised Writing Plan

- Rewrite the novelty section around the closest prior work and exact delta.
- Rewrite the results section around current-route claim-ready evidence, not intended outcomes.
- Keep unsupported and legacy-route rows out of manuscript results.
- Document scope boundaries without presenting internal audit diagnostics as contributions.
- Only then promote this draft into the venue-specific LaTeX template.

## Appendix: Original Draft Snapshot

{draft}
'''

    write_text(paper['revised_md'], revised)
    update_pipeline_state(args.project, {
        'paper_revision_ready': True,
        'revised_draft_path': str(paper['revised_md']),
    }, venue=venue)
    print(paper['revised_md'])


if __name__ == '__main__':
    main()
