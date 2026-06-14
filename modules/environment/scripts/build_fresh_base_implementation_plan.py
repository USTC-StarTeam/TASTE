#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

def _repo_root_from_script() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "framework").is_dir() and (candidate / "modules").is_dir() and (candidate / "web").is_dir():
            return candidate
    return current.parents[1]

ROOT = _repo_root_from_script()


def project_from_args(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--project', default='')
    ns, _ = parser.parse_known_args(argv)
    if ns.project:
        return ns.project
    return os.environ.get('PROJECT_ID', '')


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def _current_find_run_id(project: str) -> str:
    root = ROOT / 'projects' / project
    for path in [root / 'planning' / 'finding' / 'find_progress.json', root / 'planning' / 'finding' / 'find_results.json', root / 'state' / 'current_find_research_plan.json']:
        payload = load_json(path, {})
        if isinstance(payload, dict):
            run_id = str(payload.get('run_id') or payload.get('find_run_id') or payload.get('source_run_id') or '').strip()
            if run_id:
                return run_id
    return ''


def _repo_path(row: Any) -> str:
    if not isinstance(row, dict):
        return ''
    for key in ['repo_path', 'local_path', 'path']:
        value = str(row.get(key) or '').strip()
        if value:
            return value
    return ''


def _selected_repo(selection: dict[str, Any], active: dict[str, Any]) -> dict[str, Any]:
    selected = selection.get('selected') if isinstance(selection.get('selected'), dict) else {}
    if selected:
        return dict(selected)
    pending = selection.get('pending_environment_candidate') if isinstance(selection.get('pending_environment_candidate'), dict) else {}
    if pending:
        return dict(pending)
    if isinstance(active, dict) and active:
        return dict(active)
    return {}


def _ready_datasets(selected: dict[str, Any], req: dict[str, Any], probe: dict[str, Any]) -> list[str]:
    ready: set[str] = set()
    for value in [selected.get('claim_ready_dataset'), selected.get('dataset')]:
        if str(value or '').strip():
            ready.add(str(value).strip())
    for pool in [selected.get('claim_ready_datasets'), selected.get('ready_datasets'), selected.get('probe_summary', {}).get('claim_ready_datasets') if isinstance(selected.get('probe_summary'), dict) else [], req.get('ready_datasets') if isinstance(req, dict) else []]:
        if isinstance(pool, str):
            pool = [pool]
        for item in pool if isinstance(pool, list) else []:
            if str(item or '').strip():
                ready.add(str(item).strip())
    for row in probe.get('probes', []) if isinstance(probe, dict) and isinstance(probe.get('probes'), list) else []:
        if not isinstance(row, dict):
            continue
        loader = row.get('loader_probe') if isinstance(row.get('loader_probe'), dict) else {}
        if row.get('claim_ready') and (row.get('loader_probe_success') or loader.get('success')) and str(row.get('dataset') or '').strip():
            ready.add(str(row.get('dataset')).strip())
    return sorted(ready)


def _blocked_datasets(req: dict[str, Any], probe: dict[str, Any], selected: dict[str, Any], ready: list[str]) -> list[str]:
    ready_set = set(ready)
    blocked: list[str] = []
    for pool in [req.get('blocked_datasets') if isinstance(req, dict) else [], probe.get('blocked_datasets') if isinstance(probe, dict) else []]:
        if isinstance(pool, str):
            pool = [pool]
        for item in pool if isinstance(pool, list) else []:
            text = str(item or '').strip()
            if text and text not in ready_set and text not in blocked:
                blocked.append(text)
    for row in probe.get('probes', []) if isinstance(probe, dict) and isinstance(probe.get('probes'), list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get('dataset') or '').strip()
        if name and name not in ready_set and name not in blocked:
            blocked.append(name)
    if not ready and not blocked:
        if selected.get('pending_loader_bootstrap') or selected.get('pending_reason'):
            blocked.append('real_data_loader_contract')
        else:
            blocked.append('project_specific_dataset_contract')
    return blocked[:20]


def _repo_payload(selected: dict[str, Any]) -> dict[str, Any]:
    keys = [
        'name', 'repo', 'url', 'repo_url', 'repo_path', 'local_path', 'path', 'source',
        'fresh_find_run_id', 'selected_plan_id', 'selected_idea_id', 'literature_base_title', 'selected_base_title', 'selection_stage',
        'selected_by_stage', 'selection_gate', 'decision', 'selection_score', 'pending_loader_bootstrap',
        'pending_reason', 'anchor_selection_policy',
    ]
    repo = {key: selected.get(key) for key in keys if selected.get(key) not in (None, '', [])}
    if isinstance(selected.get('signals'), dict):
        repo['signals'] = selected.get('signals')
    if isinstance(selected.get('probe_summary'), dict):
        repo['probe_summary'] = selected.get('probe_summary')
    if isinstance(selected.get('claude_topic_decision'), dict):
        decision = selected.get('claude_topic_decision')
        repo['claude_topic_decision'] = {
            key: decision.get(key)
            for key in ['decision', 'confidence', 'repo_action', 'env_action', 'data_action', 'data_action_reason', 'data_action_reason_zh', 'recommended_env_name']
            if decision.get(key) not in (None, '', [])
        }
    return repo


def write_generic_plan(project: str, adapter: Path) -> int:
    root = ROOT / 'projects' / project
    state = root / 'state'
    reports = root / 'reports'
    selection = load_json(state / 'evidence_ready_repo_selection.json', {})
    active = load_json(state / 'active_repo.json', {})
    req = load_json(state / 'repo_data_requirements.json', {})
    probe = load_json(state / 'real_dataset_probe.json', {})
    selected = _selected_repo(selection if isinstance(selection, dict) else {}, active if isinstance(active, dict) else {})
    run_id = str((selection.get('fresh_find_run_id') if isinstance(selection, dict) else '') or _current_find_run_id(project)).strip()
    selected_plan_id = str((selection.get('selected_plan_id') if isinstance(selection, dict) else '') or selected.get('selected_plan_id') or '').strip()
    selected_idea_id = str((selection.get('selected_idea_id') if isinstance(selection, dict) else '') or selected.get('selected_idea_id') or '').strip()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if not selected or not _repo_path(selected):
        reason = 'No current environment-stage repo has been selected for this Find run.'
        payload = {
            'updated_at': now,
            'project': project,
            'fresh_find_run_id': run_id,
            'selected_plan_id': selected_plan_id,
            'selected_idea_id': selected_idea_id,
            'status': 'blocked_environment_repo_selection_required',
            'reason': reason,
            'repo': {},
            'ready_datasets': [],
            'blocked_datasets': [],
            'blocker_reasons': [reason],
            'adapter_missing': not adapter.exists(),
            'adapter_path': str(adapter),
        }
        save_json(state / 'fresh_base_implementation_plan.json', payload)
        print(json.dumps({'status': payload['status'], 'project': project, 'reason': reason}, ensure_ascii=False))
        return 2
    ready = _ready_datasets(selected, req if isinstance(req, dict) else {}, probe if isinstance(probe, dict) else {})
    blocked = _blocked_datasets(req if isinstance(req, dict) else {}, probe if isinstance(probe, dict) else {}, selected, ready)
    repo = _repo_payload(selected)
    no_adapter = not adapter.exists()
    if ready:
        status = 'implementation_ready_for_reference_probe'
        reason = 'Environment-stage repo has at least one claim-ready real dataset with loader evidence.'
    else:
        status = 'blocked_fresh_base_data_required'
        reason = 'Environment-stage Claude Code selected the current transformable repo, but no real dataset/loader evidence is claim-ready yet.'
        if no_adapter:
            reason += ' A project-local data adapter is still required to define and probe the dataset contract.'
    payload = {
        'updated_at': now,
        'project': project,
        'fresh_find_run_id': run_id,
        'selected_plan_id': selected_plan_id,
        'selected_idea_id': selected_idea_id,
        'status': status,
        'reason': reason,
        'repo': repo,
        'ready_datasets': ready,
        'blocked_datasets': blocked,
        'blocker_reasons': [] if ready else [reason],
        'selection_gate': selection.get('selection_gate', '') if isinstance(selection, dict) else '',
        'selection_stage': selection.get('selection_stage', '') if isinstance(selection, dict) else '',
        'evidence_ready_count': selection.get('evidence_ready_count', 0) if isinstance(selection, dict) else 0,
        'adapter_missing': no_adapter,
        'adapter_path': str(adapter),
        'policy': 'Experiments and paper claims remain blocked until ready_datasets is non-empty and repo loader evidence passes.',
    }
    save_json(state / 'fresh_base_implementation_plan.json', payload)
    reports.mkdir(parents=True, exist_ok=True)
    lines = ['# Fresh Base Implementation Plan\n\n']
    lines.append(f"- updated_at: {now}\n")
    lines.append(f"- status: {status}\n")
    lines.append(f"- repo: {repo.get('name') or repo.get('repo_path') or 'none'}\n")
    lines.append(f"- ready_datasets: {', '.join(ready) or 'none'}\n")
    lines.append(f"- blocked_datasets: {', '.join(blocked) or 'none'}\n")
    lines.append(f"- reason: {reason}\n")
    (reports / 'fresh_base_implementation_plan.md').write_text(''.join(lines), encoding='utf-8')
    print(json.dumps({'status': status, 'project': project, 'repo': repo.get('name') or repo.get('repo_path'), 'ready_datasets': ready, 'blocked_datasets': blocked}, ensure_ascii=False))
    return 0


def main() -> int:
    project = project_from_args(sys.argv[1:])
    if not project:
        print(json.dumps({'status': 'blocked', 'decision': 'project_required_for_project_adapter'}, ensure_ascii=False), file=sys.stderr)
        return 2
    adapter = ROOT / 'projects' / project / 'scripts' / 'adapters' / Path(__file__).name
    if adapter.exists():
        proc = subprocess.run([sys.executable, str(adapter), *sys.argv[1:]], cwd=ROOT)
        return int(proc.returncode)
    return write_generic_plan(project, adapter)


if __name__ == '__main__':
    raise SystemExit(main())
