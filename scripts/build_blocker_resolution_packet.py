#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from project_paths import build_paths


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description='Build an explicit evidence-safe resolution packet for the current blocker.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    args = parser.parse_args()

    paths = build_paths(args.project)
    active = load_json(paths.state / 'active_repo.json', {})
    req = load_json(paths.state / 'repo_data_requirements.json', {})
    policy = load_json(paths.state / 'data_unavailability_policy.json', {})
    pool = load_json(paths.state / 'repo_candidate_pool_audit.json', {})
    acquisition = load_json(paths.state / 'data_acquisition_history.json', {})
    audit = (paths.reports / 'paper_evidence_audit.md').read_text(encoding='utf-8') if (paths.reports / 'paper_evidence_audit.md').exists() else ''
    placement = policy.get('exact_user_data_placement_requests', []) if isinstance(policy, dict) else []
    evidence_ready = pool.get('evidence_ready_candidates', []) if isinstance(pool, dict) else []
    audited = pool.get('audited_candidates', []) if isinstance(pool, dict) else []
    attempts = acquisition.get('attempts', []) if isinstance(acquisition, dict) else []
    ready_datasets = req.get('ready_datasets', []) if isinstance(req.get('ready_datasets', []), list) else []
    missing_datasets = req.get('blocked_datasets', []) if isinstance(req.get('blocked_datasets', []), list) else []
    hard_real_data_blocker = not bool(ready_datasets)
    allowed_paths = [
        'place_active_repo_data_then_verify_loader',
        'continue_pool_audit_until_evidence_ready_candidate_exists',
        'expand_discovery_with_data_ready_repo_queries',
    ]
    blocked_actions = [
        'do_not_write_or_strengthen_final_claims',
        'do_not_run_synthetic_results_as_paper_evidence',
        'do_not_switch_to_needs_audit_repo',
        'do_not_mark_download_attempt_as_data_ready',
    ]
    payload = {
        'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'project': args.project,
        'venue_snapshot': args.venue,
        'blocker_type': 'active_repo_real_data_missing' if hard_real_data_blocker else 'none',
        'active_repo': active,
        'ready_datasets': ready_datasets,
        'blocked_datasets': missing_datasets if hard_real_data_blocker else [],
        'non_ready_optional_datasets': missing_datasets if not hard_real_data_blocker else [],
        'required_files_per_dataset': req.get('contract', {}).get('required_files_per_dataset', []),
        'data_policy_decision': policy.get('decision', ''),
        'data_acquisition_attempts': len([row for row in attempts if not row.get('dry_run')]),
        'recorded_attempts_including_dry_run': len(attempts),
        'evidence_ready_candidate_count': len(evidence_ready),
        'audited_candidate_count': len(audited),
        'allowed_resolution_paths': allowed_paths,
        'blocked_actions': blocked_actions,
        'exact_user_data_placement_requests': placement,
        'paper_gate_summary': 'hold-markdown-only' if 'promotion_gate_recommendation: hold-markdown-only' in audit else 'unknown_or_missing',
        'verification_commands': [
            f"python3 scripts/build_repo_data_requirements.py --project {args.project} --repo-path {active.get('repo_path', '<active_repo>')}",
            f"python3 scripts/probe_repo_dataset.py --project {args.project} --repo-path {active.get('repo_path', '<active_repo>')} --env-name <active_env>",
            f"python3 scripts/audit_paper_evidence.py --project {args.project}" + (f" --venue \"{args.venue}\"" if args.venue else ''),
            f"python3 scripts/report_status.py --project {args.project}" + (f" --venue \"{args.venue}\"" if args.venue else ''),
        ],
        'completion_condition': 'At least one real dataset must be present, loader-probed, and used in an audit-ready active-repo experiment before paper claims can advance.' if hard_real_data_blocker else 'Active repo has ready real dataset evidence; do not treat other missing optional datasets as the active blocker. Paper promotion remains controlled by the evidence audit gate.',
    }
    save_json(paths.state / 'blocker_resolution_packet.json', payload)
    lines = ['# Blocker Resolution Packet\n\n']
    for key in ['generated_at', 'project', 'venue_snapshot', 'blocker_type', 'ready_datasets', 'data_policy_decision', 'data_acquisition_attempts', 'recorded_attempts_including_dry_run', 'evidence_ready_candidate_count', 'audited_candidate_count', 'paper_gate_summary', 'completion_condition']:
        lines.append(f"- {key}: {payload.get(key)}\n")
    lines.append('\n## Non-Ready Optional Datasets\n')
    for item in payload.get('non_ready_optional_datasets', []):
        lines.append(f'- {item}\n')
    if not payload.get('non_ready_optional_datasets'):
        lines.append('- none\n')
    lines.append('\n## Exact User Data Placement Requests\n')
    if placement:
        for item in placement:
            lines.append(f"- {item.get('dataset')}: place {', '.join(item.get('required_files', []))} under `{item.get('place_required_files_under')}`\n")
    else:
        lines.append('- none\n')
    lines.append('\n## Allowed Resolution Paths\n')
    for item in allowed_paths:
        lines.append(f'- {item}\n')
    lines.append('\n## Blocked Actions\n')
    for item in blocked_actions:
        lines.append(f'- {item}\n')
    lines.append('\n## Verification Commands\n')
    for cmd in payload['verification_commands']:
        lines.append(f'- `{cmd}`\n')
    out = paths.reports / 'blocker_resolution_packet.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
