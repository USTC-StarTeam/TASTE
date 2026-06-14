#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from project_paths import build_paths, management_python


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description='Create an evidence-safe data acquisition plan for the active repo.')
    parser.add_argument('--project', required=True)
    args = parser.parse_args()
    paths = build_paths(args.project)
    req = load_json(paths.state / 'repo_data_requirements.json', {})
    active = load_json(paths.state / 'active_repo.json', {})
    tools = {
        'curl': bool(shutil.which('curl')),
        'wget': bool(shutil.which('wget')),
        'unzip': bool(shutil.which('unzip')),
        'tar': bool(shutil.which('tar')),
        'BaiduPCS-Go': bool(shutil.which('BaiduPCS-Go')),
        'bypy': bool(shutil.which('bypy')),
    }
    blocked = req.get('blocked_datasets', []) if isinstance(req, dict) else []
    sources = req.get('download_sources', []) if isinstance(req, dict) else []
    acquisition_steps = []
    for source in sources:
        url = source.get('url', '')
        manual = bool(source.get('requires_manual_or_external_tool'))
        codes = source.get('passwords_or_codes_found', []) or []
        if 'pan.baidu' in url.lower():
            action = 'manual_or_baidu_client_required'
            if tools.get('BaiduPCS-Go'):
                action = 'BaiduPCS-Go_available_but_manual_login_may_be_required'
            elif tools.get('bypy'):
                action = 'bypy_available_but_manual_auth_may_be_required'
            acquisition_steps.append({'source': url, 'type': 'baidu_pan', 'code': codes, 'action': action, 'can_auto_download_now': False, 'reason': 'Baidu links usually require account/session or a specialized client; do not pretend download success.'})
        elif url:
            acquisition_steps.append({'source': url, 'type': 'public_url', 'code': codes, 'action': 'inspect_and_download_if_license_allows', 'can_auto_download_now': tools.get('curl') or tools.get('wget'), 'reason': 'Public URL detected; the loop should verify license/format and checksum after download.'})
    payload = {
        'project': args.project,
        'active_repo': active,
        'repo_path': req.get('repo_path', ''),
        'blocked_datasets': blocked,
        'required_files_per_dataset': req.get('contract', {}).get('required_files_per_dataset', []),
        'expected_roots': req.get('contract', {}).get('expected_roots', []),
        'tools': tools,
        'acquisition_steps': acquisition_steps,
        'status': 'blocked_waiting_for_data' if blocked else 'ready_or_not_needed',
        'guardrail': 'Only mark a dataset ready after files exist locally and the repo loader probe succeeds.',
    }
    save_json(paths.state / 'data_acquisition_plan.json', payload)
    lines = ['# Data Acquisition Plan\n\n']
    lines.append(f"- status: {payload['status']}\n")
    lines.append(f"- active_repo: {active.get('name', '')} | {active.get('repo_path', '')}\n")
    lines.append(f"- blocked_datasets: {', '.join(blocked) or 'none'}\n")
    lines.append(f"- required_files_per_dataset: {', '.join(payload['required_files_per_dataset'])}\n")
    lines.append(f"- expected_roots: {', '.join(payload['expected_roots'])}\n\n")
    lines.append('## Available Tools\n')
    for name, ok in tools.items():
        lines.append(f'- {name}: {ok}\n')
    lines.append('\n## Acquisition Steps\n')
    if acquisition_steps:
        for step in acquisition_steps:
            lines.append(f"- source: {step['source']} | type: {step['type']} | action: {step['action']} | can_auto_download_now: {step['can_auto_download_now']} | reason: {step['reason']}\n")
    else:
        lines.append('- no source detected; choose another repo or use literature/backtracking to locate official data.\n')
    lines.append('\n## Verification Contract\n')
    lines.append('- After data is placed, run `{management_python()} modules/environment/scripts/build_repo_data_requirements.py --project <project>` and `{management_python()} modules/environment/scripts/probe_repo_dataset.py --project <project> --repo-path <active_repo>` again.\n')
    lines.append('- Do not log a real experiment until `claim_ready=True` appears for at least one active-repo dataset.\n')
    (paths.reports / 'data_acquisition_plan.md').write_text(''.join(lines), encoding='utf-8')
    print(paths.reports / 'data_acquisition_plan.md')


if __name__ == '__main__':
    main()
