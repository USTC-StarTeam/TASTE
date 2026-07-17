#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt

from project.project_paths import build_paths
from runtime.framework_io import read_json as load_json
from runtime.framework_io import write_json_raw as save_json


def main() -> None:
    parser = argparse.ArgumentParser(description='Build an evidence-safe stagnation and escalation report for AutoScientist.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    parser.add_argument('--min-cycles', type=int, default=6)
    parser.add_argument('--min-query-coverage', type=float, default=0.35)
    args = parser.parse_args()

    paths = build_paths(args.project)
    continuous = load_json(paths.state / 'autoscientist_continuous.json', {})
    query_cursor = load_json(paths.state / 'data_blocker_restart_cursor.json', {})
    repo_cursor = load_json(paths.state / 'repo_candidate_pool_audit_cursor.json', {})
    blocker = load_json(paths.state / 'blocker_resolution_packet.json', {})
    pool = load_json(paths.state / 'repo_candidate_pool_audit.json', {})
    data_policy = load_json(paths.state / 'data_unavailability_policy.json', {})
    cycles = continuous.get('cycles', []) if isinstance(continuous, dict) else []
    query_hist = query_cursor.get('history', []) if isinstance(query_cursor, dict) else []
    repo_hist = repo_cursor.get('history', []) if isinstance(repo_cursor, dict) else []
    queried = []
    for row in query_hist if isinstance(query_hist, list) else []:
        queried.extend(row.get('queries', []) or [])
    audited_names = []
    for row in repo_hist if isinstance(repo_hist, list) else []:
        audited_names.extend(row.get('candidates', []) or [])
    total_queries = int(query_cursor.get('total_queries', 0) or 0)
    total_candidates = int(repo_cursor.get('total_candidates', 0) or 0)
    unique_queries = sorted(set(str(q) for q in queried if q))
    unique_audited = sorted(set(str(x) for x in audited_names if x))
    query_coverage = (len(unique_queries) / total_queries) if total_queries else 0.0
    candidate_coverage = (len(unique_audited) / total_candidates) if total_candidates else 0.0
    latest_status = continuous.get('latest', {}) if isinstance(continuous, dict) else {}
    evidence_ready = bool(latest_status.get('real_evidence_ready'))
    still_blocked = blocker.get('blocker_type') not in {'', 'none'} or blocker.get('paper_gate_summary') == 'hold-markdown-only'
    evidence_ready_candidates = int(pool.get('evidence_ready_count', blocker.get('evidence_ready_candidate_count', 0)) or 0)
    stagnated = bool(
        len(cycles) >= args.min_cycles
        and not evidence_ready
        and still_blocked
        and evidence_ready_candidates == 0
        and (query_coverage >= args.min_query_coverage or candidate_coverage >= 1.0)
    )
    escalation = []
    if blocker.get('blocked_datasets'):
        escalation.append('request_or_place_active_repo_real_data_at_exact_paths')
    if query_coverage < 1.0:
        escalation.append('continue_rotating_data_ready_repo_queries')
    else:
        escalation.append('expand_query_set_with_broader_recommender_and_dataset_terms')
    if candidate_coverage >= 1.0 and evidence_ready_candidates == 0:
        escalation.append('increase_github_limit_and_discover_new_candidates_before_more_pool_audits')
    else:
        escalation.append('continue_cursor_based_pool_audit')
    if not latest_status.get('llm_live_ok'):
        escalation.append('treat_llm_api_as_optional_until_live_check_succeeds')
    payload = {
        'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'project': args.project,
        'venue': args.venue,
        'cycles_completed': len(cycles),
        'stagnated': stagnated,
        'real_evidence_ready': evidence_ready,
        'blocker_type': blocker.get('blocker_type', ''),
        'paper_gate_summary': blocker.get('paper_gate_summary', ''),
        'data_policy_decision': data_policy.get('decision', ''),
        'evidence_ready_candidate_count': evidence_ready_candidates,
        'query_coverage': round(query_coverage, 4),
        'unique_queries_tried': unique_queries,
        'total_query_count': total_queries,
        'candidate_coverage': round(candidate_coverage, 4),
        'unique_candidates_audited': unique_audited,
        'total_candidate_count': total_candidates,
        'blocked_datasets': blocker.get('blocked_datasets', []),
        'exact_user_data_placement_requests': blocker.get('exact_user_data_placement_requests', []),
        'recommended_escalations': escalation,
        'guardrail': 'Stagnation is not scientific failure evidence. It is a routing/blocker state; do not write final claims until real-data experiments pass evidence gates.',
    }
    save_json(paths.state / 'stagnation_report.json', payload)
    lines = ['# AutoScientist Stagnation Report\n\n']
    for key in ['generated_at', 'project', 'venue', 'cycles_completed', 'stagnated', 'real_evidence_ready', 'blocker_type', 'paper_gate_summary', 'data_policy_decision', 'evidence_ready_candidate_count', 'query_coverage', 'candidate_coverage']:
        lines.append(f'- {key}: {payload.get(key)}\n')
    lines.append('\n## Recommended Escalations\n')
    for item in escalation:
        lines.append(f'- {item}\n')
    lines.append('\n## Queries Tried\n')
    for item in unique_queries:
        lines.append(f'- {item}\n')
    lines.append('\n## Candidates Audited\n')
    for item in unique_audited:
        lines.append(f'- {item}\n')
    lines.append('\n## Exact Data Placement Requests\n')
    for item in payload['exact_user_data_placement_requests']:
        lines.append(f"- {item.get('dataset')}: {', '.join(item.get('required_files', []))} under `{item.get('place_required_files_under')}`\n")
    lines.append('\n## Guardrail\n')
    lines.append(f"- {payload['guardrail']}\n")
    out = paths.reports / 'stagnation_report.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
