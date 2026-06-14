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

from project_paths import ROOT, build_paths

ARCHIVE_SUFFIXES = {'.zip', '.tar', '.gz', '.tgz', '.bz2', '.xz', '.7z'}
DATA_SUFFIXES = {'.pkl', '.pickle', '.npy', '.npz', '.csv', '.json', '.jsonl', '.txt'} | ARCHIVE_SUFFIXES
MAX_AUTO_DOWNLOAD_BYTES = int(os.environ.get('MAX_DATA_DOWNLOAD_BYTES', str(5 * 1024 * 1024 * 1024)))


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


def run(cmd: list[str], cwd: Path = ROOT, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b'').decode('utf-8', 'ignore')
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b'').decode('utf-8', 'ignore')
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr + f'\nTIMEOUT after {timeout}s')


def choose_downloader() -> str:
    return shutil.which('curl') or shutil.which('wget') or ''


def public_url_downloadable(url: str) -> tuple[bool, str, int]:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix in DATA_SUFFIXES:
        return True, f'data/archive suffix {suffix}', 0
    curl = shutil.which('curl')
    if curl:
        proc = run([curl, '-L', '-I', '--max-time', '20', url], timeout=30)
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
            return False, 'URL appears to be an HTML landing page, not a direct dataset file', size
        if any(token in lower for token in ['application/zip', 'application/octet-stream', 'application/x-tar', 'application/gzip']):
            return True, 'HEAD content-type looks downloadable', size
        return False, 'No dataset-like suffix or downloadable content-type detected', size
    return False, 'No curl available to inspect URL headers', 0


def attempt_public_download(url: str, dest_dir: Path) -> dict:
    downloadable, reason, size = public_url_downloadable(url)
    if not downloadable:
        return {'status': 'skipped_not_direct_download', 'reason': reason, 'content_length': size}
    if size and size > MAX_AUTO_DOWNLOAD_BYTES:
        return {'status': 'skipped_too_large_without_explicit_override', 'reason': reason, 'content_length': size, 'max_auto_download_bytes': MAX_AUTO_DOWNLOAD_BYTES}
    downloader = choose_downloader()
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
        proc = run([downloader, '-L', '--fail', '--retry', '2', '-o', str(dest), url], timeout=1800)
    else:
        proc = run([downloader, '-O', str(dest), url], timeout=1800)
    ok = proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    return {
        'status': 'downloaded_unverified' if ok else 'download_failed',
        'reason': reason,
        'content_length': size,
        'path': str(dest) if dest.exists() else '',
        'bytes': dest.stat().st_size if dest.exists() else 0,
        'return_code': proc.returncode,
        'stderr_tail': (proc.stderr or '')[-1000:],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Attempt evidence-safe active-repo data acquisition and record what happened.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--repo-path', default='')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    paths = build_paths(args.project)
    req = load_json(paths.state / 'repo_data_requirements.json', {})
    plan = load_json(paths.state / 'data_acquisition_plan.json', {})
    active = load_json(paths.state / 'active_repo.json', {})
    repo_path = Path(args.repo_path or active.get('repo_path', '') or req.get('repo_path', '')).resolve()
    history_path = paths.state / 'data_acquisition_history.json'
    history = load_json(history_path, {'attempts': []})
    attempts = history.get('attempts', []) if isinstance(history, dict) else []
    attempt = {
        'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(),
        'project': args.project,
        'repo_path': str(repo_path) if repo_path else '',
        'dry_run': args.dry_run,
        'sources': [],
        'verification_rerun': {},
        'result': 'no_action',
        'guardrail': 'A download attempt is not evidence. Data is usable only after required-file checks and repo loader probe pass.',
    }

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
            downloadable, reason, size = public_url_downloadable(url)
            record.update({'status': 'dry_run_inspected', 'direct_downloadable': downloadable, 'reason': reason, 'content_length': size})
        else:
            record.update(attempt_public_download(url, downloads_dir))
        attempt['sources'].append(record)

    if not attempt['sources']:
        attempt['result'] = 'no_detected_sources'
    elif any(row.get('status') == 'downloaded_unverified' for row in attempt['sources']):
        attempt['result'] = 'downloaded_unverified_needs_manual_or_scripted_unpack_and_probe'
    elif any(row.get('status') == 'dry_run_inspected' for row in attempt['sources']):
        attempt['result'] = 'dry_run_only'
    else:
        attempt['result'] = 'blocked_or_skipped_all_sources'

    attempts.append(attempt)
    save_json(history_path, {'attempts': attempts, 'latest_result': attempt['result'], 'latest_timestamp': attempt['timestamp']})

    lines = ['# Data Acquisition Attempt\n\n']
    lines.append(f"- timestamp: {attempt['timestamp']}\n")
    lines.append(f"- result: {attempt['result']}\n")
    lines.append(f"- dry_run: {args.dry_run}\n")
    lines.append(f"- repo_path: {attempt['repo_path']}\n")
    lines.append(f"- total_attempts_recorded: {len(attempts)}\n\n")
    lines.append('## Source Outcomes\n')
    for row in attempt['sources']:
        lines.append(f"- {row.get('type','')}: {row.get('status','')} | {row.get('url','')} | reason={row.get('reason','')}\n")
    lines.append('\n## Verification Rule\n')
    lines.append('- Re-run repo data requirements and loader probe after files are placed or downloaded; do not mark claim-ready from this attempt alone.\n')
    out = paths.reports / 'data_acquisition_attempt.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
