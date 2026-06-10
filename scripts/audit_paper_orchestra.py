#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from paper_common import ensure_paper_dirs, load_json, read_text, update_pipeline_state, write_json, write_text
from project_paths import build_paths

REQUIRED_SECTIONS = ['abstract', 'introduction', 'related work', 'method', 'experimental setup', 'experiments', 'limitations']


def latest_markdown(*paths: Path) -> Path | None:
    existing = [path for path in paths if path.exists() and path.read_text(encoding='utf-8', errors='replace').strip()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def section_hits(text: str) -> dict[str, bool]:
    low = text.lower()
    return {section: bool(re.search(r'^#{1,3}\s+.*' + re.escape(section), low, flags=re.MULTILINE)) or section in low for section in REQUIRED_SECTIONS}


def count_citation_like(text: str) -> int:
    return len(re.findall(r'\[[^\]]+\]|\([A-Z][A-Za-z]+\s+et al\.|\\cite\{', text))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    args = parser.parse_args()
    paths = build_paths(args.project)
    paper = ensure_paper_dirs(args.project)
    state = load_json(paper['pipeline_state'], {})
    venue = args.venue or state.get('venue', '')
    draft_source = latest_markdown(paper['revised_md'], paper['draft_md'])
    draft = read_text(draft_source) if draft_source else ''
    claim_ledger = read_text(paths.planning / 'claim_ledger.md')
    claim_ledger_json = load_json(paths.state / 'claim_ledger.json', {'claims': []})
    evidence_audit = read_text(paths.reports / 'paper_evidence_audit.md')
    aris_board = read_text(paths.reports / 'aris_review_board.md')
    experiments = load_json(paths.state / 'experiment_registry.json', [])
    discovery = load_json(paths.state / 'ingest_ranking.json', {})
    orchestra_state = load_json(paths.state / 'paper_orchestra_state.json', {})
    submission_readiness = load_json(paths.state / 'submission_readiness.json', {})
    sections = section_hits(draft)
    missing_sections = [name for name, ok in sections.items() if not ok]
    citation_count = count_citation_like(draft)
    experiment_tables = '|' in draft and ('metric' in draft.lower() or 'ndcg' in draft.lower())
    has_real_dataset = any(str(row.get('status','')).lower() in {'completed','success'} and row.get('audit_ready') and not str(row.get('dataset','')).startswith('synthetic') for row in experiments if isinstance(row, dict))
    citation_candidates = len(discovery.get('ingested', []) or []) + len(discovery.get('already_ingested', []) or [])
    section_rows = orchestra_state.get('sections', []) if isinstance(orchestra_state.get('sections', []), list) else []
    blocked_section_rows = [row for row in section_rows if row.get('status') == 'blocked']
    revision_section_rows = [row for row in section_rows if row.get('status') == 'needs_revision']
    weak_claim_rows = [row for row in claim_ledger_json.get('claims', []) if isinstance(row, dict) and str(row.get('status', '')).lower() in {'weak', 'unsupported'}] if isinstance(claim_ledger_json, dict) else []
    bad_case_runs = sum(1 for row in experiments if isinstance(row, dict) and row.get('audit_ready') and (row.get('bad_case_path') or row.get('bad_case_slices')))
    counterexample_runs = sum(1 for row in experiments if isinstance(row, dict) and row.get('audit_ready') and row.get('counterexample_outcome'))
    claim_verdict_runs = sum(1 for row in experiments if isinstance(row, dict) and row.get('audit_ready') and row.get('claim_verdict'))
    issues = []
    if missing_sections:
        issues.append('missing sections: ' + ', '.join(missing_sections))
    if blocked_section_rows:
        issues.append('writing section ledger has blocked sections: ' + ', '.join(str(row.get('id')) for row in blocked_section_rows[:8]))
    if revision_section_rows:
        issues.append('writing section ledger still requires revision: ' + ', '.join(str(row.get('id')) for row in revision_section_rows[:8]))
    if citation_count == 0 and citation_candidates == 0:
        issues.append('no citation-like text and no ingested citation candidates')
    elif citation_count == 0:
        issues.append('citations not surfaced in draft despite available candidates')
    if not experiment_tables:
        issues.append('experiment table/metric presentation missing')
    if weak_claim_rows:
        issues.append('claim ledger still has weak/unsupported claims')
    if 'promotion_gate_recommendation: hold-markdown-only' in evidence_audit:
        issues.append('paper evidence audit still blocks promotion')
    if 'real-dataset evidence' in aris_board.lower() or not has_real_dataset:
        issues.append('no real-dataset evidence for final paper claims')
    if bad_case_runs == 0:
        issues.append('no audit-ready bad-case evidence for final paper analysis')
    if counterexample_runs == 0:
        issues.append('no audit-ready counterexample evidence for final paper analysis')
    if claim_verdict_runs == 0:
        issues.append('no audit-ready claim verdict evidence for final paper claims')
    readiness_status = submission_readiness.get('status', '') if isinstance(submission_readiness, dict) else ''
    if readiness_status == 'submission_ready':
        status = 'submission_ready' if not issues else 'hold'
    elif issues:
        status = 'hold'
    else:
        status = 'pass'
    payload = {
        'project': args.project,
        'venue': venue,
        'status': status,
        'sections': sections,
        'missing_sections': missing_sections,
        'citation_like_count': citation_count,
        'citation_candidate_count': citation_candidates,
        'experiment_table_present': experiment_tables,
        'has_real_dataset_experiment': has_real_dataset,
        'bad_case_runs': bad_case_runs,
        'counterexample_runs': counterexample_runs,
        'claim_verdict_runs': claim_verdict_runs,
        'writing_state_status': orchestra_state.get('status', '') if isinstance(orchestra_state, dict) else '',
        'blocked_section_count': len(blocked_section_rows),
        'revision_section_count': len(revision_section_rows),
        'submission_readiness_status': readiness_status,
        'draft_source': str(draft_source) if draft_source else '',
        'issues': issues,
    }
    write_json(paths.state / 'paper_orchestra_audit.json', payload)
    lines = ['# Writing Audit\n\n', f'- status: {status}\n', f'- draft_source: {draft_source or "missing"}\n', f'- writing_state_status: {payload["writing_state_status"]}\n', f'- submission_readiness_status: {readiness_status}\n', f'- citation_like_count: {citation_count}\n', f'- citation_candidate_count: {citation_candidates}\n', f'- experiment_table_present: {experiment_tables}\n', f'- has_real_dataset_experiment: {has_real_dataset}\n', f'- bad_case_runs: {bad_case_runs}\n', f'- counterexample_runs: {counterexample_runs}\n', f'- claim_verdict_runs: {claim_verdict_runs}\n\n', '## Sections\n']
    for name, ok in sections.items():
        lines.append(f'- {name}: {ok}\n')
    lines.append('\n## Issues\n')
    if issues:
        for item in issues:
            lines.append(f'- {item}\n')
    else:
        lines.append('- No orchestration issue detected.\n')
    out = paths.reports / 'paper_orchestra_audit.md'
    write_text(out, ''.join(lines))
    update_pipeline_state(args.project, {'paper_orchestra_audit': str(out), 'paper_orchestra_status': status, 'paper_orchestra_issues': issues}, venue=venue, promote_to_top=True)
    print(out)


if __name__ == '__main__':
    main()
