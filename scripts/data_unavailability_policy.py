#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from project_paths import build_paths, load_project_config


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


def source_confidence(sources: list[dict]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    for source in sources:
        url = str(source.get('url', source.get('source', '')))
        if not url:
            continue
        low = url.lower()
        if 'pan.baidu' in low:
            score += 1
            reasons.append('baidu_pan_source_requires_manual_session')
        elif any(host in low for host in ['github.com', 'zenodo.org', 'figshare.com', 'huggingface.co', 'kaggle.com', 'dropbox.com', 'drive.google.com']):
            score += 2
            reasons.append('known_public_or_semi_public_data_host')
        else:
            score += 1
            reasons.append('generic_public_url_detected')
    return score, sorted(set(reasons))


def count_attempts(history: dict, include_dry_run: bool = False) -> int:
    attempts = history.get('attempts', []) if isinstance(history, dict) else []
    if not isinstance(attempts, list):
        return 0
    if include_dry_run:
        return len(attempts)
    return sum(1 for row in attempts if not row.get('dry_run'))


def candidate_alternatives(paths, active_repo_path: str, floor: float, require_execution_ready: bool = False) -> list[dict]:
    rows = load_json(paths.state / 'repo_candidates.json', [])
    out: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        local = str(row.get('local_path', ''))
        if local and local == active_repo_path:
            continue
        score = float(row.get('repo_reuse_score', row.get('score', 0)) or 0)
        if score < floor:
            continue
        if row.get('hard_topic_mismatch'):
            continue
        if row.get('repo_selection_bucket') == 'paused_by_veto':
            continue
        support = row.get('repo_support_signals', []) or []
        execution_ready = bool(row.get('repo_execution_ready'))
        if require_execution_ready and not execution_ready:
            continue
        out.append({
            'name': row.get('name', ''),
            'url': row.get('url', ''),
            'local_path': local,
            'score': score,
            'bucket': row.get('repo_selection_bucket', ''),
            'support_signals': support,
            'execution_ready': execution_ready,
        })
    return sorted(out, key=lambda item: (-item['score'], item['name']))[:8]


def main() -> None:
    parser = argparse.ArgumentParser(description='Evidence-safe policy for active-repo data blockers.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--max-data-attempts', type=int, default=2)
    args = parser.parse_args()

    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    active = load_json(paths.state / 'active_repo.json', {})
    req = load_json(paths.state / 'repo_data_requirements.json', {})
    acquisition = load_json(paths.state / 'data_acquisition_plan.json', {})
    history = load_json(paths.state / 'data_acquisition_history.json', {'attempts': []})
    probe = load_json(paths.state / 'real_dataset_probe.json', {})
    req_blocked = req.get('blocked_datasets', []) if isinstance(req, dict) else []
    req_ready = req.get('ready_datasets', []) if isinstance(req, dict) else []
    claim_ready_probe_rows = [
        row for row in (probe.get('probes', []) if isinstance(probe, dict) else [])
        if isinstance(row, dict) and row.get('claim_ready') and row.get('loader_probe', {}).get('success')
    ]
    claim_ready_datasets = [str(row.get('dataset')) for row in claim_ready_probe_rows if row.get('dataset')]
    ready = sorted({str(item) for item in req_ready if item} | set(claim_ready_datasets))
    ready_set = set(ready)
    blocked = [str(item) for item in req_blocked if item and str(item) not in ready_set]
    sources = req.get('download_sources', []) if isinstance(req, dict) else []
    source_score, source_reasons = source_confidence(sources)
    attempts = count_attempts(history, include_dry_run=False)
    recorded_attempts = count_attempts(history, include_dry_run=True)
    floor = float(cfg.get('literature', {}).get('repo_candidate_floor', 8.0) or 8.0)
    pool_audit = load_json(paths.state / 'repo_candidate_pool_audit.json', {})
    pool_ready = pool_audit.get('evidence_ready_candidates', []) if isinstance(pool_audit, dict) else []
    evidence_ready_alternatives = pool_ready or candidate_alternatives(paths, str(active.get('repo_path', '')), floor, require_execution_ready=True)
    alternatives = evidence_ready_alternatives or candidate_alternatives(paths, str(active.get('repo_path', '')), floor, require_execution_ready=False)

    if claim_ready_datasets:
        decision = 'continue_normal_loop_with_claim_ready_data'
        rationale = 'At least one real dataset has passed the active repo loader probe; TASTE may proceed to real smoke tests and experiments while keeping all claim evidence auditable.'
    elif ready:
        decision = 'verify_loader_then_run_real_smoke'
        rationale = 'At least one required dataset directory is present; claims remain blocked until the repo loader probe succeeds.'
    elif not blocked:
        decision = 'continue_normal_loop'
        rationale = 'No active data blocker is recorded.'
    elif attempts < args.max_data_attempts and source_score > 0:
        decision = 'attempt_acquisition_or_request_exact_user_placement'
        rationale = 'The active repo has dataset sources, but local files are missing. Try lawful acquisition if tools/session allow; otherwise ask the user to place exact files.'
    elif evidence_ready_alternatives:
        decision = 'switch_or_backtrack_to_evidence_ready_repo'
        rationale = 'The active repo remains data-blocked after bounded real acquisition attempts; preserving momentum now requires another runnable repo with auditable data.'
    elif alternatives:
        decision = 'expand_discovery_or_request_user_data_before_switching'
        rationale = 'Some alternative repos exist, but none is execution-ready enough to replace the active route without more auditing.'
    else:
        decision = 'ask_user_for_data_or_expand_discovery'
        rationale = 'No ready data and no acceptable alternative repo are currently available; the loop must not fabricate results.'

    exact_placement = []
    required = req.get('contract', {}).get('required_files_per_dataset', []) if isinstance(req, dict) else []
    statuses = req.get('local_statuses', []) if isinstance(req, dict) else []
    for row in statuses if isinstance(statuses, list) else []:
        if row.get('status') == 'ready' or str(row.get('dataset', '')) in ready_set:
            continue
        dataset = str(row.get('dataset', ''))
        active_path = str(active.get('repo_path', '')) if isinstance(active, dict) else ''
        preferred_name = dataset.upper() if dataset.lower() == 'foursquare' else dataset
        preferred = str(Path(active_path) / 'data' / 'processed' / preferred_name) if active_path and dataset else ''
        candidates = row.get('candidate_roots', []) or []
        root = preferred or (candidates[0].get('root', '') if candidates else '')
        exact_placement.append({'dataset': dataset, 'place_required_files_under': root, 'required_files': required})

    payload = {
        'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'project': args.project,
        'active_repo': active,
        'blocked_datasets': blocked,
        'ready_datasets': ready,
        'claim_ready_probe_datasets': claim_ready_datasets,
        'claim_ready_probe_details': claim_ready_probe_rows,
        'acquisition_attempt_count': attempts,
        'recorded_attempt_count_including_dry_run': recorded_attempts,
        'max_data_attempts': args.max_data_attempts,
        'source_confidence_score': source_score,
        'source_confidence_reasons': source_reasons,
        'acquisition_plan_status': acquisition.get('status', ''),
        'decision': decision,
        'rationale': rationale,
        'exact_user_data_placement_requests': exact_placement,
        'evidence_ready_alternative_repo_candidates': evidence_ready_alternatives,
        'alternative_repo_candidates': alternatives,
        'guardrails': [
            'Do not run synthetic fallback for final paper evidence when an active real repo is data-blocked.',
            'Do not mark data ready from filenames alone; require repo loader probe success.',
            'If switching repo, keep old route as a historical guardrail rather than deleting evidence.',
            'If asking user for data, provide exact dataset/file paths and never claim acquisition succeeded.',
        ],
    }
    save_json(paths.state / 'data_unavailability_policy.json', payload)

    lines = ['# Data Unavailability Policy\n\n']
    lines.append(f"- generated_at: {payload['generated_at']}\n")
    lines.append(f"- decision: {decision}\n")
    lines.append(f"- rationale: {rationale}\n")
    lines.append(f"- active_repo: {active.get('name', '')} | {active.get('repo_path', '')}\n")
    lines.append(f"- blocked_datasets: {', '.join(blocked) or 'none'}\n")
    lines.append(f"- acquisition_attempt_count: {attempts}/{args.max_data_attempts}\n")
    lines.append(f"- recorded_attempt_count_including_dry_run: {recorded_attempts}\n")
    lines.append(f"- evidence_ready_alternative_count: {len(evidence_ready_alternatives)}\n")
    lines.append(f"- source_confidence_score: {source_score}\n\n")
    lines.append('## Exact User Placement Requests\n')
    if exact_placement:
        for item in exact_placement:
            lines.append(f"- {item['dataset']}: place {', '.join(item['required_files'])} under `{item['place_required_files_under']}`\n")
    else:
        lines.append('- none\n')
    lines.append('\n## Evidence-Ready Alternative Repo Candidates\n')
    if evidence_ready_alternatives:
        for item in evidence_ready_alternatives:
            lines.append(f"- {item['name']} | score={item['score']} | ready={item['execution_ready']} | {item['url']}\n")
    else:
        lines.append('- none currently execution-ready above the evidence floor\n')
    lines.append('\n## Other Alternative Repo Candidates Needing Audit\n')
    if alternatives:
        for item in alternatives:
            lines.append(f"- {item['name']} | score={item['score']} | ready={item['execution_ready']} | {item['url']}\n")
    else:
        lines.append('- none above the current evidence floor\n')
    lines.append('\n## Guardrails\n')
    for guardrail in payload['guardrails']:
        lines.append(f'- {guardrail}\n')
    out = paths.reports / 'data_unavailability_policy.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
