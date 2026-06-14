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


def _project_from_args(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--project', default='')
    ns, _ = parser.parse_known_args(argv)
    if ns.project:
        return ns.project
    return os.environ.get('PROJECT_ID', '')


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Probe repo datasets, with a safe generic blocker when no adapter exists.')
    parser.add_argument('--project', default='')
    parser.add_argument('--repo-path', default='')
    parser.add_argument('--env-name', default='')
    parser.add_argument('--timeout-sec', default='')
    return parser.parse_known_args(argv)[0]


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def _repo_from_state(project: str, explicit: str = '') -> str:
    if explicit:
        return explicit
    state = ROOT / 'projects' / project / 'state'
    for name in ['evidence_ready_repo_selection.json', 'active_repo.json', 'fresh_base_implementation_plan.json']:
        payload = load_json(state / name, {})
        rows: list[Any] = []
        if isinstance(payload, dict):
            rows.extend([payload.get('selected'), payload.get('repo'), payload])
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ['repo_path', 'local_path', 'path']:
                value = str(row.get(key) or '').strip()
                if value:
                    return value
    return ''


def write_generic_probe(project: str, repo_path: str, env_name: str, adapter: Path) -> int:
    state = ROOT / 'projects' / project / 'state'
    reports = ROOT / 'projects' / project / 'reports'
    req = load_json(state / 'repo_data_requirements.json', {})
    datasets = req.get('blocked_datasets') if isinstance(req, dict) and isinstance(req.get('blocked_datasets'), list) else []
    if not datasets:
        datasets = ['project_specific_dataset_contract']
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    reason = 'No project-local dataset probe adapter is available; no dataset can be claim-ready until a repo-specific loader probe is implemented.'
    probes = [
        {
            'dataset': str(name),
            'claim_ready': False,
            'loader_probe_success': False,
            'loader_probe': {'success': False, 'return_code': 2, 'reason': reason},
            'required_files_ok': False,
            'missing_required_files': [],
            'reason': reason,
        }
        for name in datasets
    ]
    payload = {
        'generated_at': now,
        'project': project,
        'repo_path': repo_path,
        'env_name': env_name,
        'status': 'blocked_missing_project_dataset_probe_adapter',
        'decision': 'project_dataset_loader_probe_required',
        'adapter_missing': True,
        'adapter_path': str(adapter),
        'probes': probes,
        'ready_datasets': [],
        'blocked_datasets': [str(name) for name in datasets],
        'blocker_reasons': [reason],
        'probe_return_code': 0,
    }
    save_json(state / 'real_dataset_probe.json', payload)
    reports.mkdir(parents=True, exist_ok=True)
    lines = ['# Real Dataset Probe\n\n']
    lines.append(f"- generated_at: {now}\n")
    lines.append('- status: blocked_missing_project_dataset_probe_adapter\n')
    lines.append(f"- repo_path: {repo_path or 'not selected'}\n")
    lines.append(f"- env_name: {env_name or 'not set'}\n")
    lines.append(f"- adapter_path: {adapter}\n")
    lines.append('\n## Probe Rows\n')
    for row in probes:
        lines.append(f"- {row['dataset']}: claim_ready=false; {reason}\n")
    (reports / 'real_dataset_probe.md').write_text(''.join(lines), encoding='utf-8')
    print(json.dumps({'status': payload['status'], 'project': project, 'repo_path': repo_path, 'probe_count': len(probes)}, ensure_ascii=False))
    return 0


def main() -> int:
    args = parse_args(sys.argv[1:])
    project = args.project or _project_from_args(sys.argv[1:])
    if not project:
        print(json.dumps({
            'status': 'blocked',
            'decision': 'project_required_for_project_adapter',
            'message': 'This framework entrypoint dispatches to a project-local adapter; pass --project or set PROJECT_ID.',
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    adapter = ROOT / 'projects' / project / 'scripts' / 'adapters' / Path(__file__).name
    if adapter.exists():
        proc = subprocess.run([sys.executable, str(adapter), *sys.argv[1:]], cwd=ROOT)
        return int(proc.returncode)
    repo_path = _repo_from_state(project, args.repo_path)
    return write_generic_probe(project, repo_path, args.env_name, adapter)


if __name__ == '__main__':
    raise SystemExit(main())
