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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build a project repo-data requirement contract, with a safe generic fallback.')
    parser.add_argument('--project', default='')
    parser.add_argument('--repo-path', default='')
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


def _dataset_names_from_repo(repo_path: str) -> list[str]:
    repo = Path(repo_path) if repo_path else None
    if not repo or not repo.exists():
        return []
    names: list[str] = []
    for rel in ['data', 'datasets', 'benchmark', 'benchmarks']:
        root = repo / rel
        if not root.exists() or not root.is_dir():
            continue
        children = [p.name for p in root.iterdir() if not p.name.startswith('.')][:12]
        if children:
            names.extend(children)
        else:
            names.append(rel)
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        text = str(name).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out[:12]


def write_generic_requirement(project: str, repo_path: str, adapter: Path) -> int:
    state = ROOT / 'projects' / project / 'state'
    reports = ROOT / 'projects' / project / 'reports'
    datasets = _dataset_names_from_repo(repo_path) or ['project_specific_dataset_contract']
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    reason = 'No project-local data adapter is available, so TASTE cannot prove a repo-specific real dataset contract yet.'
    payload = {
        'generated_at': now,
        'project': project,
        'repo_path': repo_path,
        'status': 'blocked_missing_project_data_adapter',
        'decision': 'project_data_contract_required',
        'adapter_missing': True,
        'adapter_path': str(adapter),
        'datasets': datasets,
        'ready_datasets': [],
        'blocked_datasets': datasets,
        'download_sources': [],
        'contract': {
            'status': 'project_adapter_required',
            'required_files_per_dataset': [],
            'loader_probe_required': True,
        },
        'local_statuses': [
            {
                'dataset': name,
                'status': 'blocked_project_adapter_required',
                'reason': reason,
                'candidate_roots': [],
                'missing_required_files': [],
            }
            for name in datasets
        ],
        'blocker_reasons': [reason],
        'guardrails': [
            'Do not mark a dataset ready without a project-specific loader probe.',
            'A missing adapter is a real blocker, not an installation failure.',
        ],
    }
    save_json(state / 'repo_data_requirements.json', payload)
    reports.mkdir(parents=True, exist_ok=True)
    lines = ['# Repo Data Requirements\n\n']
    lines.append(f"- generated_at: {now}\n")
    lines.append('- status: blocked_missing_project_data_adapter\n')
    lines.append(f"- repo_path: {repo_path or 'not selected'}\n")
    lines.append(f"- adapter_path: {adapter}\n")
    lines.append('- decision: project_data_contract_required\n')
    lines.append('\n## Blocked Datasets\n')
    for name in datasets:
        lines.append(f"- {name}: {reason}\n")
    (reports / 'repo_data_requirements.md').write_text(''.join(lines), encoding='utf-8')
    print(json.dumps({'status': payload['status'], 'project': project, 'repo_path': repo_path, 'blocked_datasets': datasets}, ensure_ascii=False))
    return 0


def main() -> int:
    args = parse_args(sys.argv[1:])
    project = args.project or project_from_args(sys.argv[1:])
    if not project:
        print(json.dumps({'status': 'blocked', 'decision': 'project_required_for_project_adapter'}, ensure_ascii=False), file=sys.stderr)
        return 2
    adapter = ROOT / 'projects' / project / 'scripts' / 'adapters' / Path(__file__).name
    if adapter.exists():
        proc = subprocess.run([sys.executable, str(adapter), *sys.argv[1:]], cwd=ROOT)
        return int(proc.returncode)
    repo_path = _repo_from_state(project, args.repo_path)
    return write_generic_requirement(project, repo_path, adapter)


if __name__ == '__main__':
    raise SystemExit(main())
