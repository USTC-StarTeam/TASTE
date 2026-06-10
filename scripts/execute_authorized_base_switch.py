#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any

from project_paths import build_paths


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def backup(path: Path) -> str:
    if not path.exists():
        return ''
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = path.with_name(path.name + f'.bak_authorized_base_switch_{stamp}')
    shutil.copy2(path, target)
    return str(target)


def safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def current_find_run_id(paths) -> str:
    for path in [paths.planning / 'finding' / 'find_progress.json', paths.state / 'current_find_research_plan.json', paths.state / 'evidence_ready_repo_selection.json']:
        payload = load_json(path, {})
        if isinstance(payload, dict):
            for key in ['run_id', 'source_run_id', 'find_run_id', 'fresh_find_run_id', 'current_find_run_id']:
                value = str(payload.get(key) or '').strip()
                if value:
                    return value
    return ''


def metric_from_audit(audit: dict[str, Any]) -> dict[str, Any]:
    metrics = audit.get('metrics') if isinstance(audit.get('metrics'), dict) else {}
    out: dict[str, Any] = {}
    for key in ['ndcg_at_10', 'best_ndcg_at_10', 'recall_at_10', 'hr_at_10']:
        if key in metrics:
            out[key] = metrics.get(key)
        elif key in audit:
            out[key] = audit.get(key)
    return out


def load_candidate_audit(paths, repo_path: str) -> dict[str, Any]:
    for path in sorted(paths.state.glob('*reference_reproduction_audit.json')):
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            continue
        if str(payload.get('repo_path') or payload.get('active_repo_path') or '').strip() == repo_path:
            if payload.get('status') == 'completed_reference_reproduction' and int(payload.get('return_code') or 0) == 0:
                return {**payload, '_path': str(path)}
    return {}


def build_selected_payload(paths, gate: dict[str, Any], audit: dict[str, Any], run_id: str) -> dict[str, Any]:
    candidate = safe_dict(gate.get('candidate_route'))
    repo_name = str(candidate.get('repo') or audit.get('repo_name') or '').strip()
    repo_path = str(candidate.get('repo_path') or audit.get('repo_path') or '').strip()
    title = str(candidate.get('title') or audit.get('paper_title') or audit.get('base_title') or repo_name).strip()
    dataset = str(candidate.get('dataset') or audit.get('dataset') or '').strip()
    selected = {
        'name': repo_name,
        'url': candidate.get('url') or audit.get('repo_url') or '',
        'repo_path': repo_path,
        'local_path': repo_path,
        'source': 'deterministic_base_switch_gate',
        'fresh_find_run_id': run_id,
        'literature_base_title': title,
        'selected_base_title': title,
        'selection_stage': 'environment_claude_code',
        'selected_by_stage': 'environment_claude_code',
        'selection_gate': 'accepted_by_deterministic_base_switch_gate',
        'decision': 'selected_by_authorized_base_switch_gate',
        'claim_ready_dataset': dataset,
        'claim_ready_datasets': [dataset] if dataset else [],
        'probe_summary': {'claim_ready_datasets': [dataset] if dataset else [], 'probe_count': 0},
        'reference_reproduction': {
            'status': 'pass',
            'decision': 'continue_base',
            'audit_path': audit.get('_path') or '',
            'artifact_dir': audit.get('artifact_dir') or audit.get('artifact_path') or '',
            'metrics': metric_from_audit(audit),
        },
        'base_switch_gate': {
            'status': gate.get('status'),
            'decision': gate.get('decision'),
            'switch_authorized': gate.get('switch_authorized'),
            'gate_path': str(paths.state / 'base_switch_gate.json'),
        },
        'anchor_selection_policy': 'Environment-stage route updated by deterministic base-switch gate after selected-base viability exhaustion; Find/Read/Idea/Plan remain candidate inputs, not direct route execution authority.',
    }
    return selected


def gate_authorized(gate: dict[str, Any]) -> bool:
    return bool(
        isinstance(gate, dict)
        and gate.get('status') == 'pass'
        and gate.get('decision') == 'authorize_base_switch'
        and gate.get('switch_authorized') is True
    )


def execute(project: str, venue: str = '') -> dict[str, Any]:
    paths = build_paths(project)
    gate = load_json(paths.state / 'base_switch_gate.json', {})
    candidate = safe_dict(gate.get('candidate_route')) if isinstance(gate, dict) else {}
    repo_path = str(candidate.get('repo_path') or '').strip()
    executed_at = now_iso()
    if not gate_authorized(gate):
        payload = {
            'project': project,
            'venue': venue,
            'executed_at': executed_at,
            'status': 'blocked_gate_not_authorized',
            'decision': 'not_executed',
            'switch_authorized': False,
            'authorization_status': 'not_authorized',
            'gate_status': gate.get('status') if isinstance(gate, dict) else '',
            'gate_decision': gate.get('decision') if isinstance(gate, dict) else '',
            'summary': 'base-switch execution requires state/base_switch_gate.json status=pass, decision=authorize_base_switch, switch_authorized=true.',
            'summary_zh': 'base-switch 执行需要确定性门控先通过；当前不切换 selected-base。',
        }
        save_json(paths.state / 'base_switch_execution.json', payload)
        return payload
    if not repo_path:
        payload = {
            'project': project,
            'venue': venue,
            'executed_at': executed_at,
            'status': 'blocked_missing_candidate_repo_path',
            'decision': 'not_executed',
            'switch_authorized': False,
            'authorization_status': 'not_authorized',
            'summary': 'authorized base-switch gate has no candidate_route.repo_path.',
            'summary_zh': 'base-switch gate 虽通过但缺少候选仓库路径；不执行切换。',
        }
        save_json(paths.state / 'base_switch_execution.json', payload)
        return payload
    audit = load_candidate_audit(paths, repo_path)
    audit_ready = bool(
        audit
        and audit.get('audit_ready') is True
        and audit.get('paper_level_reproduction_passed') is True
        and (audit.get('artifact_dir') or audit.get('artifact_path'))
    )
    if not audit_ready:
        payload = {
            'project': project,
            'venue': venue,
            'executed_at': executed_at,
            'status': 'blocked_missing_candidate_audit_ready_reference',
            'decision': 'not_executed',
            'switch_authorized': False,
            'authorization_status': 'not_authorized',
            'candidate_repo_path': repo_path,
            'summary': 'candidate route is gate-authorized but lacks artifact-local audit-ready full reference reproduction evidence.',
            'summary_zh': '候选路线虽已被 gate 授权，但缺少 artifact-local、audit-ready 的完整参考复现证据；不执行切换。',
        }
        save_json(paths.state / 'base_switch_execution.json', payload)
        return payload

    run_id = current_find_run_id(paths)
    selected = build_selected_payload(paths, gate, audit, run_id)
    selection_path = paths.state / 'evidence_ready_repo_selection.json'
    active_path = paths.state / 'active_repo.json'
    selection_backup = backup(selection_path)
    active_backup = backup(active_path)
    previous_selection = load_json(selection_path, {})
    previous_active = load_json(active_path, {})
    if not isinstance(previous_selection, dict):
        previous_selection = {}
    if not isinstance(previous_active, dict):
        previous_active = {}

    new_selection = dict(previous_selection)
    new_selection.update({
        'generated_at': executed_at,
        'project': project,
        'fresh_find_run_id': run_id,
        'selection_stage': 'environment_claude_code',
        'selected_by_stage': 'environment_claude_code',
        'selection_gate': 'accepted_by_deterministic_base_switch_gate',
        'accepted_by_claude': True,
        'selected': selected,
        'base_switch_execution': {
            'status': 'authorized_by_deterministic_base_switch_gate',
            'execution_path': str(paths.state / 'base_switch_execution.json'),
            'gate_path': str(paths.state / 'base_switch_gate.json'),
            'audit_path': audit.get('_path') or '',
        },
    })
    new_active = dict(previous_active)
    new_active.update({
        'project': project,
        'updated_at': executed_at,
        'name': selected.get('name') or '',
        'url': selected.get('url') or '',
        'repo_path': selected.get('repo_path') or '',
        'local_path': selected.get('local_path') or selected.get('repo_path') or '',
        'selected_at': executed_at,
        'selected_by': run_id,
        'fresh_find_run_id': run_id,
        'selection_stage': 'environment_claude_code',
        'selected_by_stage': 'environment_claude_code',
        'selection_gate': 'accepted_by_deterministic_base_switch_gate',
        'selected_base_title': selected.get('literature_base_title') or selected.get('selected_base_title') or '',
        'claim_ready_dataset': selected.get('claim_ready_dataset') or '',
        'claim_ready_datasets': selected.get('claim_ready_datasets') or [],
        'reference_reproduction': selected.get('reference_reproduction') or {},
        'anchor_selection_policy': selected.get('anchor_selection_policy') or '',
    })
    save_json(selection_path, new_selection)
    save_json(active_path, new_active)
    payload = {
        'project': project,
        'venue': venue,
        'executed_at': executed_at,
        'status': 'authorized_by_deterministic_base_switch_gate',
        'decision': 'route_switch_executed',
        'switch_authorized': True,
        'authorization_status': 'authorized',
        'authorized_by': str(paths.state / 'base_switch_gate.json'),
        'audit_path': audit.get('_path') or '',
        'new_route': selected,
        'backups': {
            'evidence_ready_repo_selection': selection_backup,
            'active_repo': active_backup,
        },
        'guardrail': 'This execution changes only current route identity after deterministic gate pass. It does not promote paper claims, import experiments, or clean obsolete project files; cleanup remains project-Claude-owned and separately audited.',
        'summary': 'base-switch executed after deterministic gate pass and artifact-local audit-ready candidate reference reproduction.',
        'summary_zh': 'deterministic base-switch gate 通过且候选路线具备 artifact-local 审计就绪参考复现后，TASTE 已受控切换当前基底；论文结论和旧文件清理仍需后续门控。',
    }
    save_json(paths.state / 'base_switch_execution.json', payload)
    report = paths.reports / 'base_switch_execution.md'
    report.write_text(
        '# Base-Switch Execution\n\n'
        f"- status: {payload['status']}\n"
        f"- decision: {payload['decision']}\n"
        f"- repo_path: {selected.get('repo_path') or ''}\n"
        f"- audit_path: {payload['audit_path']}\n"
        f"- summary_zh: {payload['summary_zh']}\n",
        encoding='utf-8',
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description='Execute a route switch only after deterministic base-switch gate pass.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    args = parser.parse_args()
    payload = execute(args.project, args.venue)
    paths = build_paths(args.project)
    print(json.dumps(payload, ensure_ascii=False))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
