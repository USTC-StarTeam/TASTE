#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from literature_policy import now_utc, repo_sort_key, score_repo_candidate
from project_paths import ROOT, build_paths, load_project_config, management_python


# ---- assess_repo tool, from assess_repo_candidates.py ----
def _assess_repo_load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []

def run_assess_repo(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args(argv)
    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    registry = paths.state / 'repo_candidates.json'
    rows = _assess_repo_load_json(registry)
    overrides = _assess_repo_load_json(paths.state / 'method_overrides.json') if (paths.state / 'method_overrides.json').exists() else {'repos': {}}
    paused_repos = overrides.get('repos', {}) if isinstance(overrides, dict) else {}
    refreshed = []
    for row in rows:
        item = dict(row)
        item.update(score_repo_candidate(item, cfg, reference_time=now_utc()))
        local_path = str(item.get('local_path', ''))
        if local_path and paused_repos.get(local_path, {}).get('status') in {'paused_or_abandoned', 'abandoned'}:
            item['repo_selection_bucket'] = 'paused_by_veto'
            item['score'] = -100.0
            item['repo_reuse_score'] = -100.0
            item['notes'] = (str(item.get('notes', '')) + ' | paused_by_research_veto').strip(' |')
        else:
            item['score'] = item.get('repo_reuse_score', item.get('score', 0))
        refreshed.append(item)
    refreshed.sort(key=repo_sort_key)
    registry.write_text(json.dumps(refreshed, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    md = ['# Repo Candidates\n\n', '| Score | Name | URL | Fit | Activity | Install | Entrypoint | Notes |\n', '| --- | --- | --- | --- | --- | --- | --- | --- |\n']
    for row in refreshed:
        md.append(f"| {row.get('score', 0)} | {row.get('name', '')} | {row.get('url', '')} | {row.get('task_fit', False)} | {row.get('activity_bucket', row.get('recent_activity', False))} | {row.get('has_install', False)} | {row.get('has_entrypoint', False)} | {row.get('notes', '')} |\n")
    (paths.reports / 'repo_candidates.md').write_text(''.join(md), encoding='utf-8')
    print(paths.reports / 'repo_candidates.md')

# ---- select_repo_candidate tool, from select_repo_candidate.py ----
def _select_repo_candidate_load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []

def run_select_repo_candidate(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--name')
    args = parser.parse_args(argv)
    paths = build_paths(args.project)
    rows = _select_repo_candidate_load_json(paths.state / 'repo_candidates.json')
    if not rows:
        raise SystemExit('No repo candidates registered')
    if args.name:
        matches = [row for row in rows if row.get('name') == args.name]
        if not matches:
            raise SystemExit(f'No repo candidate named {args.name}')
        winner = matches[0]
    else:
        winner = sorted(rows, key=lambda x: (-int(x.get('score', 0)), x.get('name', '')))[0]
    out = paths.repos_selected / 'selected_repo.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"# Selected Repo\n\n- name: {winner.get('name', '')}\n- url: {winner.get('url', '')}\n- score: {winner.get('score', 0)}\n- summary: {winner.get('summary', '')}\n- notes: {winner.get('notes', '')}\n", encoding='utf-8')
    print(out)

# ---- register_repo_candidate tool, from register_repo_candidate.py ----
def _register_repo_candidate_load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []

def _register_repo_candidate_save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

def run_register_repo_candidate(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--summary', default='')
    parser.add_argument('--task-fit', action='store_true')
    parser.add_argument('--recent-activity', action='store_true')
    parser.add_argument('--has-readme', action='store_true')
    parser.add_argument('--has-license', action='store_true')
    parser.add_argument('--has-install', action='store_true')
    parser.add_argument('--has-entrypoint', action='store_true')
    parser.add_argument('--has-tests', action='store_true')
    parser.add_argument('--has-dataset-docs', action='store_true')
    parser.add_argument('--stars', type=int, default=0)
    parser.add_argument('--forks', type=int, default=0)
    parser.add_argument('--last-pushed-at', default='')
    parser.add_argument('--topics', default='')
    parser.add_argument('--language', default='')
    parser.add_argument('--notes', default='')
    args = parser.parse_args(argv)
    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    registry_path = paths.state / 'repo_candidates.json'
    rows = _register_repo_candidate_load_json(registry_path)
    item = {'name': args.name, 'url': args.url, 'summary': args.summary, 'task_fit': args.task_fit, 'recent_activity': args.recent_activity, 'has_readme': args.has_readme, 'has_license': args.has_license, 'has_install': args.has_install, 'has_entrypoint': args.has_entrypoint, 'has_tests': args.has_tests, 'has_dataset_docs': args.has_dataset_docs, 'stars': args.stars, 'forks': args.forks, 'last_pushed_at': args.last_pushed_at, 'topics': [x.strip() for x in args.topics.split(',') if x.strip()], 'language': args.language, 'notes': args.notes, 'updated_from': 'manual_registration'}
    item.update(score_repo_candidate(item, cfg, reference_time=now_utc()))
    item['score'] = item.get('repo_reuse_score', 0)
    rows = [row for row in rows if row.get('name') != args.name]
    rows.append(item)
    rows.sort(key=repo_sort_key)
    _register_repo_candidate_save_json(registry_path, rows)
    md = ['# Repo Candidates\n\n', '| Score | Name | URL | Fit | Activity | Install | Entrypoint | Notes |\n', '| --- | --- | --- | --- | --- | --- | --- | --- |\n']
    for row in rows:
        md.append(f"| {row.get('score', 0)} | {row.get('name', '')} | {row.get('url', '')} | {row.get('task_fit', False)} | {row.get('activity_bucket', row.get('recent_activity', False))} | {row.get('has_install', False)} | {row.get('has_entrypoint', False)} | {row.get('notes', '')} |\n")
    (paths.reports / 'repo_candidates.md').write_text(''.join(md), encoding='utf-8')
    print(paths.reports / 'repo_candidates.md')

# ---- register_dataset tool, from register_dataset.py ----
def _register_dataset_load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []

def _register_dataset_save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

def _register_dataset_readiness_score(item: dict[str, object]) -> int:
    score = 0
    score += 2 if item.get('access') in {'public', 'requestable'} else 0
    score += 2 if item.get('format') else 0
    score += 2 if item.get('split') else 0
    score += 2 if item.get('metric') else 0
    score += 2 if item.get('available') else 0
    score += 1 if item.get('download_tested') else 0
    score += 1 if item.get('notes') else 0
    return score

def run_register_dataset(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--access', default='unknown')
    parser.add_argument('--format', default='')
    parser.add_argument('--split', default='')
    parser.add_argument('--metric', default='')
    parser.add_argument('--url', default='')
    parser.add_argument('--notes', default='')
    parser.add_argument('--available', action='store_true')
    parser.add_argument('--download-tested', action='store_true')
    args = parser.parse_args(argv)
    paths = build_paths(args.project)
    registry_path = paths.state / 'dataset_registry.json'
    rows = _register_dataset_load_json(registry_path)
    item = {'name': args.name, 'task': args.task, 'access': args.access, 'format': args.format, 'split': args.split, 'metric': args.metric, 'url': args.url, 'notes': args.notes, 'available': args.available, 'download_tested': args.download_tested}
    item['readiness_score'] = _register_dataset_readiness_score(item)
    rows = [row for row in rows if row.get('name') != args.name]
    rows.append(item)
    rows.sort(key=lambda x: (-int(x.get('readiness_score', 0)), x.get('name', '')))
    _register_dataset_save_json(registry_path, rows)
    md = ['# Dataset Registry\n\n', '| Score | Name | Task | Access | Format | Split | Metric | Available | Notes |\n', '| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n']
    for row in rows:
        md.append(f"| {row.get('readiness_score', 0)} | {row.get('name', '')} | {row.get('task', '')} | {row.get('access', '')} | {row.get('format', '')} | {row.get('split', '')} | {row.get('metric', '')} | {row.get('available', False)} | {row.get('notes', '')} |\n")
    (paths.reports / 'dataset_registry.md').write_text(''.join(md), encoding='utf-8')
    print(paths.reports / 'dataset_registry.md')

# ---- reconcile_candidates tool, from reconcile_active_and_pool_candidates.py ----
def _reconcile_candidates_load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default

def _reconcile_candidates_save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

def run_reconcile_candidates(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Keep active repo evidence and exploratory pool audit evidence separate.')
    parser.add_argument('--project', required=True)
    args = parser.parse_args(argv)
    paths = build_paths(args.project)
    active = _reconcile_candidates_load_json(paths.state / 'active_repo.json', {})
    pool = _reconcile_candidates_load_json(paths.state / 'repo_candidate_pool_audit.json', {})
    rows = _reconcile_candidates_load_json(paths.state / 'repo_candidates.json', [])
    active_path = str(active.get('repo_path', ''))
    active_name = str(active.get('name', ''))
    pool_by_name = {item.get('name'): item for item in pool.get('audited_candidates', [])} if isinstance(pool, dict) else {}
    changes = []
    reconciled = []
    for row in rows if isinstance(rows, list) else []:
        row = dict(row)
        name = row.get('name', '')
        local_path = str(row.get('local_path', ''))
        if name == active_name or (active_path and local_path == active_path):
            row['repo_execution_ready'] = bool(active.get('repo_execution_ready', row.get('repo_execution_ready')))
            row['repo_support_signals'] = active.get('repo_support_signals', row.get('repo_support_signals', []))
            if row.get('repo_execution_ready'):
                base = float(row.get('repo_reuse_score', row.get('score', 0)) or 0)
                row['repo_reuse_score'] = max(base, float(active.get('repo_reuse_score', base) or base))
                row['score'] = row['repo_reuse_score']
            row['notes'] = 'active repo; local audit evidence stored in active_repo.json'
        elif name in pool_by_name and row.get('notes') == 'auto-audited local repo':
            original = float(row.get('repo_reuse_score', row.get('score', 0)) or 0)
            penalty = 3.0 if not pool_by_name[name].get('execution_ready_after_audit') else 0.0
            row['repo_reuse_score'] = max(0.0, original - penalty)
            row['score'] = row['repo_reuse_score']
            row['repo_execution_ready'] = False
            row['repo_support_signals'] = []
            row['repo_selection_bucket'] = 'needs_audit'
            row['notes'] = 'pool-audited candidate; not switch-ready until repo_candidate_pool_audit shows execution/data evidence'
            changes.append({'name': name, 'old_score': original, 'new_score': row['score'], 'reason': 'pool audit separated from active selection'})
        reconciled.append(row)
    reconciled = sorted(reconciled, key=repo_sort_key)
    _reconcile_candidates_save_json(paths.state / 'repo_candidates.json', reconciled)
    payload = {'project': args.project, 'changes': changes, 'active_repo': active, 'guardrail': 'Only active repo audits may promote active selection; pool audits must prove execution/data evidence before switching.'}
    _reconcile_candidates_save_json(paths.state / 'candidate_reconciliation.json', payload)
    lines = ['# Candidate Reconciliation\n\n', f'- changes: {len(changes)}\n', f"- guardrail: {payload['guardrail']}\n\n"]
    for item in changes:
        lines.append(f"- {item['name']}: {item['old_score']} -> {item['new_score']} | {item['reason']}\n")
    out = paths.reports / 'candidate_reconciliation.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)

# ---- plan_data tool, from plan_data_acquisition.py ----
def _plan_data_load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default

def _plan_data_save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

def run_plan_data(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Create an evidence-safe data acquisition plan for the active repo.')
    parser.add_argument('--project', required=True)
    args = parser.parse_args(argv)
    paths = build_paths(args.project)
    req = _plan_data_load_json(paths.state / 'repo_data_requirements.json', {})
    active = _plan_data_load_json(paths.state / 'active_repo.json', {})
    tools = {'curl': bool(shutil.which('curl')), 'wget': bool(shutil.which('wget')), 'unzip': bool(shutil.which('unzip')), 'tar': bool(shutil.which('tar')), 'BaiduPCS-Go': bool(shutil.which('BaiduPCS-Go')), 'bypy': bool(shutil.which('bypy'))}
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
    payload = {'project': args.project, 'active_repo': active, 'repo_path': req.get('repo_path', ''), 'blocked_datasets': blocked, 'required_files_per_dataset': req.get('contract', {}).get('required_files_per_dataset', []), 'expected_roots': req.get('contract', {}).get('expected_roots', []), 'tools': tools, 'acquisition_steps': acquisition_steps, 'status': 'blocked_waiting_for_data' if blocked else 'ready_or_not_needed', 'guardrail': 'Only mark a dataset ready after files exist locally and the repo loader probe succeeds.'}
    _plan_data_save_json(paths.state / 'data_acquisition_plan.json', payload)
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
    lines.append('- After data is placed, run `{management_python()} framework/scripts/run_module.py environment --action data_requirements --project <project>` and `{management_python()} framework/scripts/run_module.py environment --action probe_repo --project <project> --repo-path <active_repo>` again.\n')
    lines.append('- Do not log a real experiment until `claim_ready=True` appears for at least one active-repo dataset.\n')
    (paths.reports / 'data_acquisition_plan.md').write_text(''.join(lines), encoding='utf-8')
    print(paths.reports / 'data_acquisition_plan.md')

# ---- attempt_data tool, from attempt_data_acquisition.py ----
ARCHIVE_SUFFIXES = {'.zip', '.tar', '.gz', '.tgz', '.bz2', '.xz', '.7z'}
DATA_SUFFIXES = {'.pkl', '.pickle', '.npy', '.npz', '.csv', '.json', '.jsonl', '.txt'} | ARCHIVE_SUFFIXES
MAX_AUTO_DOWNLOAD_BYTES = int(os.environ.get('MAX_DATA_DOWNLOAD_BYTES', str(5 * 1024 * 1024 * 1024)))

def _attempt_data_load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default

def _attempt_data_save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

def _attempt_data_run(cmd: list[str], cwd: Path=ROOT, timeout: int=120) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b'').decode('utf-8', 'ignore')
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b'').decode('utf-8', 'ignore')
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr + f'\nTIMEOUT after {timeout}s')

def _attempt_data_choose_downloader() -> str:
    return shutil.which('curl') or shutil.which('wget') or ''

def _attempt_data_public_url_downloadable(url: str) -> tuple[bool, str, int]:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix in DATA_SUFFIXES:
        return (True, f'data/archive suffix {suffix}', 0)
    curl = shutil.which('curl')
    if curl:
        proc = _attempt_data_run([curl, '-L', '-I', '--max-time', '20', url], timeout=30)
        headers = (proc.stdout or '') + '\n' + (proc.stderr or '')
        lower = headers.lower()
        size = 0
        for line in headers.splitlines():
            if line.lower().startswith('content-length:'):
                try:
                    size = int(line.split(':', 1)[1].strip())
                except Exception:
                    size = 0
        if 'text/html' in lower and suffix not in DATA_SUFFIXES:
            return (False, 'URL appears to be an HTML landing page, not a direct dataset file', size)
        if any((token in lower for token in ['application/zip', 'application/octet-stream', 'application/x-tar', 'application/gzip'])):
            return (True, 'HEAD content-type looks downloadable', size)
        return (False, 'No dataset-like suffix or downloadable content-type detected', size)
    return (False, 'No curl available to inspect URL headers', 0)

def _attempt_data_attempt_public_download(url: str, dest_dir: Path) -> dict:
    downloadable, reason, size = _attempt_data_public_url_downloadable(url)
    if not downloadable:
        return {'status': 'skipped_not_direct_download', 'reason': reason, 'content_length': size}
    if size and size > MAX_AUTO_DOWNLOAD_BYTES:
        return {'status': 'skipped_too_large_without_explicit_override', 'reason': reason, 'content_length': size, 'max_auto_download_bytes': MAX_AUTO_DOWNLOAD_BYTES}
    downloader = _attempt_data_choose_downloader()
    if not downloader:
        return {'status': 'blocked_missing_downloader', 'reason': 'curl/wget unavailable'}
    dest_dir.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    name = Path(parsed.path).name or 'downloaded_dataset'
    suffix = Path(name).suffix.lower()
    if not suffix:
        guessed = mimetypes.guess_extension('application/octet-stream') or '.bin'
        name += guessed
    dest = dest_dir / name
    if Path(downloader).name == 'curl':
        proc = _attempt_data_run([downloader, '-L', '--fail', '--retry', '2', '-o', str(dest), url], timeout=1800)
    else:
        proc = _attempt_data_run([downloader, '-O', str(dest), url], timeout=1800)
    ok = proc.returncode == 0 and dest.exists() and (dest.stat().st_size > 0)
    return {'status': 'downloaded_unverified' if ok else 'download_failed', 'reason': reason, 'content_length': size, 'path': str(dest) if dest.exists() else '', 'bytes': dest.stat().st_size if dest.exists() else 0, 'return_code': proc.returncode, 'stderr_tail': (proc.stderr or '')[-1000:]}

def run_attempt_data(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Attempt evidence-safe active-repo data acquisition and record what happened.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--repo-path', default='')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    paths = build_paths(args.project)
    req = _attempt_data_load_json(paths.state / 'repo_data_requirements.json', {})
    plan = _attempt_data_load_json(paths.state / 'data_acquisition_plan.json', {})
    active = _attempt_data_load_json(paths.state / 'active_repo.json', {})
    repo_path = Path(args.repo_path or active.get('repo_path', '') or req.get('repo_path', '')).resolve()
    history_path = paths.state / 'data_acquisition_history.json'
    history = _attempt_data_load_json(history_path, {'attempts': []})
    attempts = history.get('attempts', []) if isinstance(history, dict) else []
    attempt = {'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(), 'project': args.project, 'repo_path': str(repo_path) if repo_path else '', 'dry_run': args.dry_run, 'sources': [], 'verification_rerun': {}, 'result': 'no_action', 'guardrail': 'A download attempt is not evidence. Data is usable only after required-file checks and repo loader probe pass.'}
    sources = plan.get('acquisition_steps', []) if isinstance(plan, dict) else []
    downloads_dir = paths.root / 'datasets' / 'downloads'
    for source in sources:
        url = str(source.get('source', ''))
        kind = str(source.get('type', ''))
        if not url:
            continue
        record = {'url': url, 'type': kind, 'planned_action': source.get('action', '')}
        if 'baidu' in kind or 'pan.baidu' in url.lower():
            record.update({'status': 'blocked_manual_session_required', 'reason': 'Baidu Pan requires authenticated client/session; AutoScientist must not claim success without verified local files.'})
        elif args.dry_run:
            downloadable, reason, size = _attempt_data_public_url_downloadable(url)
            record.update({'status': 'dry_run_inspected', 'direct_downloadable': downloadable, 'reason': reason, 'content_length': size})
        else:
            record.update(_attempt_data_attempt_public_download(url, downloads_dir))
        attempt['sources'].append(record)
    if not attempt['sources']:
        attempt['result'] = 'no_detected_sources'
    elif any((row.get('status') == 'downloaded_unverified' for row in attempt['sources'])):
        attempt['result'] = 'downloaded_unverified_needs_manual_or_scripted_unpack_and_probe'
    elif any((row.get('status') == 'dry_run_inspected' for row in attempt['sources'])):
        attempt['result'] = 'dry_run_only'
    else:
        attempt['result'] = 'blocked_or_skipped_all_sources'
    attempts.append(attempt)
    _attempt_data_save_json(history_path, {'attempts': attempts, 'latest_result': attempt['result'], 'latest_timestamp': attempt['timestamp']})
    lines = ['# Data Acquisition Attempt\n\n']
    lines.append(f"- timestamp: {attempt['timestamp']}\n")
    lines.append(f"- result: {attempt['result']}\n")
    lines.append(f'- dry_run: {args.dry_run}\n')
    lines.append(f"- repo_path: {attempt['repo_path']}\n")
    lines.append(f'- total_attempts_recorded: {len(attempts)}\n\n')
    lines.append('## Source Outcomes\n')
    for row in attempt['sources']:
        lines.append(f"- {row.get('type', '')}: {row.get('status', '')} | {row.get('url', '')} | reason={row.get('reason', '')}\n")
    lines.append('\n## Verification Rule\n')
    lines.append('- Re-run repo data requirements and loader probe after files are placed or downloaded; do not mark claim-ready from this attempt alone.\n')
    out = paths.reports / 'data_acquisition_attempt.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)

# ---- data_policy tool, from data_unavailability_policy.py ----
def _data_policy_load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default

def _data_policy_save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

def _data_policy_source_confidence(sources: list[dict]) -> tuple[int, list[str]]:
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
        elif any((host in low for host in ['github.com', 'zenodo.org', 'figshare.com', 'huggingface.co', 'kaggle.com', 'dropbox.com', 'drive.google.com'])):
            score += 2
            reasons.append('known_public_or_semi_public_data_host')
        else:
            score += 1
            reasons.append('generic_public_url_detected')
    return (score, sorted(set(reasons)))

def _data_policy_count_attempts(history: dict, include_dry_run: bool=False) -> int:
    attempts = history.get('attempts', []) if isinstance(history, dict) else []
    if not isinstance(attempts, list):
        return 0
    if include_dry_run:
        return len(attempts)
    return sum((1 for row in attempts if not row.get('dry_run')))

def _data_policy_candidate_alternatives(paths, active_repo_path: str, floor: float, require_execution_ready: bool=False) -> list[dict]:
    rows = _data_policy_load_json(paths.state / 'repo_candidates.json', [])
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
        if require_execution_ready and (not execution_ready):
            continue
        out.append({'name': row.get('name', ''), 'url': row.get('url', ''), 'local_path': local, 'score': score, 'bucket': row.get('repo_selection_bucket', ''), 'support_signals': support, 'execution_ready': execution_ready})
    return sorted(out, key=lambda item: (-item['score'], item['name']))[:8]

def _data_policy_normalize_repo_candidate(row: dict) -> dict:
    """Return the small candidate contract used by data policy reports."""
    data_requirements = row.get('data_requirements') if isinstance(row.get('data_requirements'), dict) else {}
    ready_datasets = data_requirements.get('ready_datasets', []) if isinstance(data_requirements, dict) else []
    loader_probe = row.get('loader_probe') if isinstance(row.get('loader_probe'), dict) else {}
    execution_ready = bool(row.get('execution_ready') or row.get('repo_execution_ready') or row.get('execution_ready_after_audit') or ready_datasets or loader_probe.get('success'))
    return {'name': str(row.get('name') or row.get('repo') or row.get('full_name') or ''), 'url': str(row.get('url') or row.get('html_url') or row.get('repo_url') or ''), 'local_path': str(row.get('local_path') or row.get('repo_path') or row.get('path') or ''), 'score': float(row.get('score', row.get('repo_reuse_score', row.get('selection_score', 0))) or 0), 'bucket': str(row.get('bucket') or row.get('repo_selection_bucket') or row.get('decision') or ''), 'support_signals': row.get('support_signals') or row.get('repo_support_signals') or [], 'execution_ready': execution_ready, 'ready_datasets': [str(item) for item in ready_datasets if item]}

def _data_policy_normalize_repo_candidates(rows) -> list[dict]:
    if not isinstance(rows, list):
        return []
    return [_data_policy_normalize_repo_candidate(row) for row in rows if isinstance(row, dict)]

def run_data_policy(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Evidence-safe policy for active-repo data blockers.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--max-data-attempts', type=int, default=2)
    args = parser.parse_args(argv)
    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    active = _data_policy_load_json(paths.state / 'active_repo.json', {})
    req = _data_policy_load_json(paths.state / 'repo_data_requirements.json', {})
    acquisition = _data_policy_load_json(paths.state / 'data_acquisition_plan.json', {})
    history = _data_policy_load_json(paths.state / 'data_acquisition_history.json', {'attempts': []})
    probe = _data_policy_load_json(paths.state / 'real_dataset_probe.json', {})
    req_blocked = req.get('blocked_datasets', []) if isinstance(req, dict) else []
    req_ready = req.get('ready_datasets', []) if isinstance(req, dict) else []
    claim_ready_probe_rows = [row for row in (probe.get('probes', []) if isinstance(probe, dict) else []) if isinstance(row, dict) and row.get('claim_ready') and row.get('loader_probe', {}).get('success')]
    claim_ready_datasets = [str(row.get('dataset')) for row in claim_ready_probe_rows if row.get('dataset')]
    ready = sorted({str(item) for item in req_ready if item} | set(claim_ready_datasets))
    ready_set = set(ready)
    blocked = [str(item) for item in req_blocked if item and str(item) not in ready_set]
    sources = req.get('download_sources', []) if isinstance(req, dict) else []
    source_score, source_reasons = _data_policy_source_confidence(sources)
    attempts = _data_policy_count_attempts(history, include_dry_run=False)
    recorded_attempts = _data_policy_count_attempts(history, include_dry_run=True)
    floor = float(cfg.get('literature', {}).get('repo_candidate_floor', 8.0) or 8.0)
    pool_audit = _data_policy_load_json(paths.state / 'repo_candidate_pool_audit.json', {})
    pool_ready = pool_audit.get('evidence_ready_candidates', []) if isinstance(pool_audit, dict) else []
    evidence_ready_alternatives = _data_policy_normalize_repo_candidates(pool_ready) or _data_policy_candidate_alternatives(paths, str(active.get('repo_path', '')), floor, require_execution_ready=True)
    alternatives = evidence_ready_alternatives or _data_policy_candidate_alternatives(paths, str(active.get('repo_path', '')), floor, require_execution_ready=False)
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
    payload = {'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'project': args.project, 'active_repo': active, 'blocked_datasets': blocked, 'ready_datasets': ready, 'claim_ready_probe_datasets': claim_ready_datasets, 'claim_ready_probe_details': claim_ready_probe_rows, 'acquisition_attempt_count': attempts, 'recorded_attempt_count_including_dry_run': recorded_attempts, 'max_data_attempts': args.max_data_attempts, 'source_confidence_score': source_score, 'source_confidence_reasons': source_reasons, 'acquisition_plan_status': acquisition.get('status', ''), 'decision': decision, 'rationale': rationale, 'exact_user_data_placement_requests': exact_placement, 'evidence_ready_alternative_repo_candidates': evidence_ready_alternatives, 'alternative_repo_candidates': alternatives, 'guardrails': ['Do not run synthetic fallback for final paper evidence when an active real repo is data-blocked.', 'Do not mark data ready from filenames alone; require repo loader probe success.', 'If switching repo, keep old route as a historical guardrail rather than deleting evidence.', 'If asking user for data, provide exact dataset/file paths and never claim acquisition succeeded.']}
    _data_policy_save_json(paths.state / 'data_unavailability_policy.json', payload)
    lines = ['# Data Unavailability Policy\n\n']
    lines.append(f"- generated_at: {payload['generated_at']}\n")
    lines.append(f'- decision: {decision}\n')
    lines.append(f'- rationale: {rationale}\n')
    lines.append(f"- active_repo: {active.get('name', '')} | {active.get('repo_path', '')}\n")
    lines.append(f"- blocked_datasets: {', '.join(blocked) or 'none'}\n")
    lines.append(f'- acquisition_attempt_count: {attempts}/{args.max_data_attempts}\n')
    lines.append(f'- recorded_attempt_count_including_dry_run: {recorded_attempts}\n')
    lines.append(f'- evidence_ready_alternative_count: {len(evidence_ready_alternatives)}\n')
    lines.append(f'- source_confidence_score: {source_score}\n\n')
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

TOOL_ACTIONS = {
    "assess_repo": run_assess_repo,
    "select_repo_candidate": run_select_repo_candidate,
    "register_repo_candidate": run_register_repo_candidate,
    "register_dataset": run_register_dataset,
    "reconcile_candidates": run_reconcile_candidates,
    "plan_data": run_plan_data,
    "attempt_data": run_attempt_data,
    "data_policy": run_data_policy,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Environment module data, registry, and candidate utility tools.")
    parser.add_argument("--tool-action", required=True, choices=sorted(TOOL_ACTIONS))
    ns, rest = parser.parse_known_args(argv)
    TOOL_ACTIONS[ns.tool_action](rest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
