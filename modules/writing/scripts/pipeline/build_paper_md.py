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
import re
from pathlib import Path

from paper_common import (
    DEFAULT_REVIEWERS,
    compact_bullets,
    draft_title_from_config,
    ensure_paper_dirs,
    extract_summary_lines,
    load_json,
    read_text,
    summarize_experiments,
    update_pipeline_state,
    write_json,
    write_text,
)
from project_paths import build_paths, load_project_config
from pipeline_guard import guard_fresh_base_blocker_entry
from experiment_contracts import SUPPORTIVE_CLAIM_VERDICTS, row_promotion_blockers


def clean_block(text: str, limit: int = 8, fallback: str = 'No evidence logged yet.') -> str:
    lines = extract_summary_lines(text, limit=limit)
    return compact_bullets(lines) if lines else f'- {fallback}'


def norm_path(value: object) -> str:
    return str(value or '').rstrip('/')


def current_route_row(row: dict, active_repo: dict) -> bool:
    active_path = norm_path(active_repo.get('repo_path') or active_repo.get('local_path'))
    active_name = str(active_repo.get('name') or active_repo.get('repo_name') or '').strip().lower()
    row_path = norm_path(row.get('repo_path') or row.get('active_repo_path') or row.get('local_path'))
    row_name = str(row.get('repo_name') or row.get('repo') or row.get('base_repo') or '').strip().lower()
    if active_path and row_path:
        return row_path == active_path
    if active_name and row_name:
        return row_name == active_name
    return False


def claim_ready_positive_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        if str(row.get('status', '')).lower() not in {'completed', 'success'}:
            continue
        if not row.get('audit_ready') or row_promotion_blockers(row):
            continue
        if str(row.get('claim_verdict') or '').strip().lower() not in SUPPORTIVE_CLAIM_VERDICTS:
            continue
        out.append(row)
    return out


def render_experiment_table(rows: list[dict], limit: int = 8) -> str:
    claim_ready = claim_ready_positive_rows(rows)
    if not claim_ready:
        return '- No claim-ready positive experiment is available yet; result tables must stay empty until TASTE evidence improves.'
    header = '| Method | Dataset | Metric | Value | Audit | Claim |\n|---|---|---:|---:|---|---|'
    lines = [header]
    for row in claim_ready[-limit:]:
        method = str(row.get('method') or row.get('experiment_id') or '')
        dataset = str(row.get('dataset') or '')
        metric = str(row.get('metric_name') or row.get('metric') or '')
        value = str(row.get('metric_value') if row.get('metric_value') is not None else row.get('result', ''))
        audit = 'ready' if row.get('audit_ready') else 'missing'
        claim = str(row.get('claim_verdict') or '')
        lines.append(f'| {method} | {dataset} | {metric} | {value} | {audit} | {claim} |')
    return '\n'.join(lines)


def render_citation_candidates(paths, limit: int = 8) -> str:
    ranking = load_json(paths.state / 'ingest_ranking.json', {})
    rows = []
    if isinstance(ranking, dict):
        rows = list(ranking.get('ingested', []) or []) + list(ranking.get('already_ingested', []) or [])
    if not rows:
        return '- No citation candidates have been ingested yet.'
    out = []
    for row in rows[:limit]:
        title = row.get('title', '')
        bucket = row.get('selection_bucket', '')
        score = row.get('discovery_priority_score', '')
        out.append(f'- {title} ({bucket}, score={score})')
    return '\n'.join(out)


def load_orchestra_state(paths) -> dict:
    state = load_json(paths.state / 'paper_orchestra_state.json', {})
    return state if isinstance(state, dict) else {}


def render_section_ledger(state: dict, limit: int = 12) -> str:
    rows = state.get('sections', []) if isinstance(state.get('sections', []), list) else []
    if not rows:
        return '- TASTE paper section ledger has not been built yet.'
    out = ['| Section | Status | Blockers | Evidence |\n|---|---|---|---|']
    for row in rows[:limit]:
        blockers = '; '.join(str(item) for item in row.get('blockers', [])[:3]) if isinstance(row.get('blockers', []), list) else ''
        evidence_rows = row.get('evidence', []) if isinstance(row.get('evidence', []), list) else []
        evidence = '; '.join(str(item.get('label', 'evidence')) for item in evidence_rows[:3] if isinstance(item, dict))
        out.append(f"| {row.get('title', row.get('id', ''))} | {row.get('status', '')} | {blockers or 'none'} | {evidence or 'none'} |")
    return '\n'.join(out)


def render_global_blockers(state: dict) -> str:
    blockers = state.get('global_blockers', []) if isinstance(state.get('global_blockers', []), list) else []
    if not blockers:
        return '- No global TASTE paper blocker is recorded.'
    return '\n'.join(f'- {item}' for item in blockers[:12])


def render_claims_from_orchestra(state: dict) -> str:
    claims = state.get('claims', {}) if isinstance(state.get('claims', {}), dict) else {}
    rows = claims.get('claims', []) if isinstance(claims.get('claims', []), list) else []
    if not rows:
        return (
            '- Headline claim under test: missing; must be generated from current selected-route evidence, not aspiration.\n'
            '- Scope boundary: missing; define the exact settings where the claim should stop.\n'
            '- Minimal evidence contract: missing; require current-route real-data audit-ready support before results claims.'
        )
    out = []
    by_type = {row.get('claim_type'): row for row in rows if isinstance(row, dict)}
    defaults = [
        ('headline_claim', 'Headline claim under test'),
        ('scope_boundary', 'Scope boundary'),
        ('minimal_evidence_contract', 'Minimal evidence contract'),
    ]
    for key, label in defaults:
        row = by_type.get(key, {})
        text = row.get('text') or 'missing'
        text = re.sub(r'\s*\(status=[^)]+support_count=\d+\)\s*$', '', str(text)).strip()
        status = row.get('status') or 'unscored'
        support = row.get('support_count', 0)
        out.append(f'- {label}: {text}')
        out.append(f'- {label} evidence status: status={status}, support_count={support}')
    return '\n'.join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    parser.add_argument('--title', default='')
    args = parser.parse_args()

    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name, safe_unblock=False)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    paper = ensure_paper_dirs(args.project)

    init_brief = read_text(paths.planning / 'init_brief.md')
    next_actions = read_text(paths.planning / 'next_actions.md')
    quality = read_text(paths.planning / 'paper_quality.md')
    reflection = read_text(paths.reports / 'iteration_reflection.md')
    health = read_text(paths.reports / 'healthcheck.md')
    repo_candidates = read_text(paths.reports / 'repo_candidates.md')
    dataset_registry = read_text(paths.reports / 'dataset_registry.md')
    aris_review = read_text(paths.reports / 'aris_review_board.md')
    paper_orchestra = read_text(paths.reports / 'paper_orchestra_audit.md')
    orchestra_state = load_orchestra_state(paths)
    all_experiment_rows = load_json(paths.state / 'experiment_registry.json', [])
    active_repo = load_json(paths.state / 'active_repo.json', {})
    if not isinstance(active_repo, dict):
        active_repo = {}
    experiment_rows = [row for row in all_experiment_rows if isinstance(row, dict) and current_route_row(row, active_repo)] if isinstance(all_experiment_rows, list) else []

    title = args.title or draft_title_from_config(cfg)
    venue = args.venue or cfg.get('paper', {}).get('target_venue', '')
    experiment_summary = summarize_experiments(experiment_rows)
    claim_ready_rows = claim_ready_positive_rows(experiment_rows)
    best = None
    if claim_ready_rows:
        def _metric(row):
            try:
                return float(row.get('metric_value') if row.get('metric_value') is not None else row.get('result'))
            except Exception:
                return float('-inf')
        best = max(claim_ready_rows, key=_metric)
    if best:
        best_line = (
            f"Best current-route claim-ready run is `{best.get('name', '') or best.get('experiment_id', '')}` with "
            f"`{best.get('metric_name', '')}` = `{best.get('metric_value', '')}` on `{best.get('dataset', '')}`."
        )
    else:
        best_line = 'No current selected-route experiment has produced a claim-ready positive result yet.'

    claim_status = 'not yet credible' if not claim_ready_rows else 'current-route claim evidence available'

    reviewer_lines = '\n'.join(
        f"- {row['name']}: {row['focus']}" for row in DEFAULT_REVIEWERS
    )

    draft = f'''# {title}

## Submission Status

- target_venue: {venue or 'TBD'}
- project: {args.project}
- topic: {cfg.get('topic', '')}
- current_claim_status: {claim_status}
- strongest_result_snapshot: {best_line}
- paper_stage_state: {orchestra_state.get('status', 'not-built')}
- promotion_gate_recommendation: {orchestra_state.get('promotion_gate_recommendation', 'unknown')}

## Abstract

This workspace has not yet earned a submission-ready headline claim. The current manuscript draft should focus on the motivated problem, current selected-base method design, reproducible protocol, verified citations, and only those result claims that have current-route audit-ready support.

## Introduction

{clean_block(init_brief, limit=6, fallback='Add a sharper field-level problem statement tied to a real assumption or bottleneck.')}

The draft must not convert internal audit observations into a paper storyline. Until current-route evidence supports a positive claim, keep the manuscript conservative and keep unsupported claims out of the abstract, introduction, and results.

## Why This Project Could Matter

{clean_block(quality, limit=8, fallback='Add evidence that success would change a meaningful field-level assumption.')}

## Exact Claim To Validate

{render_claims_from_orchestra(orchestra_state)}

## TASTE Paper Section Ledger

{render_section_ledger(orchestra_state)}

## Related Work

The following candidates are available to ground related work and nearest-neighbor novelty checks. They are candidates, not automatic citations; each must be verified before final submission.

{render_citation_candidates(paths, limit=8)}

## Novelty Delta

- Closest prior work and baseline family: name them explicitly.
- Real delta over that prior work: specify what is genuinely different.
- Why this is not routine recombination: tie it to a concrete modeling or evaluation gap verified by current literature and local artifacts.

## Method

## Method Snapshot

- Core idea: summarize the smallest nontrivial intervention.
- Module coordination risk: identify which components must work together.
- Repo adaptation note: explain what was modified in the selected codebase instead of rebuilding from scratch.

## Experimental Setup

{clean_block(dataset_registry, limit=10, fallback='Add dataset, split, benchmark, and metric details.')}

## Experiments

{render_experiment_table(experiment_rows, limit=8)}

## Evidence Snapshot

- {best_line}
- completed_runs: {experiment_summary['completed_count']}
- claim_checked_runs: {experiment_summary['claim_checked_count']}

## Evidence Scope

Only current selected-route, audit-ready, claim-supporting results may enter the manuscript as results. Internal audit records remain outside manuscript prose.

## Limitations

- Scope remains limited to verified current-route artifacts and citations.
- Result claims stay omitted until a current selected-route run becomes claim-ready.

## Reproducibility and Environment

{clean_block(health, limit=8, fallback='Add runtime readiness, dependency gaps, and reproducibility notes.')}

## Writing Blockers Before Submission

- Replace placeholder claim bullets with evidence-backed prose from the current selected route.
- Keep result tables empty unless a current-route run is audit-ready and claim-supporting.
- Keep internal audit diagnostics out of manuscript prose.
- Do not promote legacy-route claims or unsupported rows.
'''

    review_packet = f'''# Review Packet: {title}

## Target Venue

- target_venue: {venue or 'TBD'}
- project: {args.project}

## Reviewer Roles

{reviewer_lines}

## What Reviewers Should Stress-Test

1. Is the novelty delta over the nearest strong paper or baseline explicit and field-relevant?
2. Does the current evidence really support the headline claim, or is the paper overclaiming?
3. Does each result claim come from the current selected route and an audit-ready artifact?
4. Are unsupported rows excluded from the manuscript rather than polished into claims?
5. Did the research loop keep legacy-route claims out of the current paper?

## Evidence Reviewers Should Read

- state/paper_orchestra_state.json
- planning/paper_quality.md
- planning/init_brief.md
- planning/next_actions.md
- reports/iteration_reflection.md
- reports/healthcheck.md
- experiments/experiment_log.md
- paper/drafts/paper_draft.md

## Current Quality Signals

{clean_block(quality, limit=12, fallback='No paper quality signal yet.')}

## Current Reproducibility Signals

{clean_block(health, limit=10, fallback='No health summary yet.')}

## Repo and Dataset Context

{clean_block(repo_candidates, limit=8, fallback='No repo candidate summary yet.')}

{clean_block(dataset_registry, limit=8, fallback='No dataset summary yet.')}

## Immediate Red Flags

- Placeholder lines in draft should count as real blockers.
- Claim verdicts and bad-case outputs must be grounded in logged runs.
- If the strongest result is weak, the paper must narrow the claim instead of polishing the prose.
- If no venue template or LaTeX runtime is available, stay in Markdown and keep improving the content first.

## Required Reviewer Output

- overall_verdict:
- score_1_to_5:
- top_blockers:
- strongest_positive_signal:
- strongest_scope_risk:
- required_changes:
'''

    write_text(paper['draft_md'], draft)
    write_text(paper['review_md'], review_packet)
    metadata = {
        'project': args.project,
        'title': title,
        'target_venue': venue,
        'draft_path': str(paper['draft_md']),
        'review_packet_path': str(paper['review_md']),
    }
    write_json(paper['paper_metadata'], metadata)
    update_pipeline_state(args.project, {
        'project': args.project,
        'title': title,
        'target_venue': venue,
        'draft_path': str(paper['draft_md']),
        'review_packet_path': str(paper['review_md']),
        'draft_ready': True,
        'review_packet_ready': True,
    }, venue=venue)
    print(paper['draft_md'])
    print(paper['review_md'])


if __name__ == '__main__':
    main()
