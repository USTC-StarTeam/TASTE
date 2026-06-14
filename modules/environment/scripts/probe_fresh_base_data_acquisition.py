#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from project_paths import build_paths

REQUIRED_DEFAULT: list[str] = []
ARCHIVE_NAME = 'fresh_base_dataset_download.bin'


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def run(cmd: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    started = now_iso()
    try:
        proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
        return {
            'command': cmd,
            'started_at': started,
            'finished_at': now_iso(),
            'return_code': proc.returncode,
            'timed_out': False,
            'stdout_tail': (proc.stdout or '')[-3000:],
            'stderr_tail': (proc.stderr or '')[-3000:],
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b'').decode('utf-8', 'ignore')
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b'').decode('utf-8', 'ignore')
        return {
            'command': cmd,
            'started_at': started,
            'finished_at': now_iso(),
            'return_code': 124,
            'timed_out': True,
            'stdout_tail': (stdout or '')[-3000:],
            'stderr_tail': ((stderr or '') + f'\nTIMEOUT after {timeout}s')[-3000:],
        }


def dataset_roots(repo: Path, dataset: str, root_hints: list[str] | None = None) -> list[Path]:
    hints = root_hints or []
    roots: list[Path] = []
    for hint in hints:
        raw = Path(str(hint))
        base = raw if raw.is_absolute() else repo / raw
        roots.extend([base / dataset, base / dataset.upper(), base])
    roots.extend([
        repo / 'data' / dataset,
        repo / 'data' / dataset.upper(),
        repo / 'data',
        repo / 'datasets' / dataset,
        repo / 'datasets' / dataset.upper(),
    ])
    out: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        resolved = root.resolve()
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            out.append(resolved)
    return out




def safe_extract_official_zip(archive: Path, repo: Path) -> dict[str, Any]:
    started = now_iso()
    result: dict[str, Any] = {
        "kind": "extract_official_zip",
        "command": ["python_zipfile_extract", str(archive), str(repo)],
        "started_at": started,
        "finished_at": "",
        "return_code": 1,
        "timed_out": False,
        "stdout_tail": "",
        "stderr_tail": "",
        "archive_path": str(archive),
        "archive_exists": archive.exists(),
        "extracted_members": 0,
    }
    if not archive.exists():
        result["finished_at"] = now_iso()
        result["stderr_tail"] = "archive not found"
        return result
    try:
        if not zipfile.is_zipfile(archive):
            result["finished_at"] = now_iso()
            result["stderr_tail"] = "downloaded file is not a zip archive"
            return result
        repo_resolved = repo.resolve()
        extracted = 0
        with zipfile.ZipFile(archive) as zf:
            members = zf.infolist()
            unsafe = []
            for member in members:
                target = (repo_resolved / member.filename).resolve()
                if target != repo_resolved and repo_resolved not in target.parents:
                    unsafe.append(member.filename)
            if unsafe:
                result["finished_at"] = now_iso()
                result["stderr_tail"] = "unsafe zip paths: " + ", ".join(unsafe[:8])
                return result
            for member in members:
                zf.extract(member, repo_resolved)
                if not member.is_dir():
                    extracted += 1
        result.update({
            "finished_at": now_iso(),
            "return_code": 0,
            "extracted_members": extracted,
            "stdout_tail": f"extracted {extracted} files from fresh-base data archive into {repo_resolved}",
        })
        return result
    except Exception as exc:
        result["finished_at"] = now_iso()
        result["stderr_tail"] = f"{type(exc).__name__}: {exc}"
        return result

def inspect_datasets(repo: Path, datasets: list[str], required: list[str], root_hints: list[str] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ds in datasets:
        roots = []
        seen: set[str] = set()
        for root in dataset_roots(repo, ds, root_hints):
            key = str(root)
            if key in seen:
                continue
            seen.add(key)
            present = []
            if root.exists():
                for name in required:
                    if '<' in name:
                        if list(root.glob('*_emb.pickle')):
                            present.append(name)
                    elif (root / name).exists():
                        present.append(name)
            roots.append({
                'root': str(root),
                'exists': root.exists(),
                'present_required_files': present,
                'missing_required_files': [name for name in required if name not in present],
            })
        ready = next((row for row in roots if row['exists'] and not row['missing_required_files']), None)
        rows.append({'dataset': ds, 'status': 'ready' if ready else 'missing', 'ready_root': ready['root'] if ready else '', 'candidate_roots': roots})
    return rows


def infer_datasets(plan: dict[str, Any]) -> list[str]:
    impl = plan.get('implementation_evidence', {}) if isinstance(plan.get('implementation_evidence'), dict) else {}
    contract = impl.get('dataset_contract', {}) if isinstance(impl.get('dataset_contract'), dict) else {}
    datasets = contract.get('datasets', []) if isinstance(contract, dict) else []
    out = []
    for row in datasets:
        ds = str(row.get('id') if isinstance(row, dict) else row).strip()
        if ds and ds not in out:
            out.append(ds)
    return out




def data_contract(plan: dict[str, Any]) -> dict[str, Any]:
    impl = plan.get('implementation_evidence', {}) if isinstance(plan.get('implementation_evidence'), dict) else {}
    contract = impl.get('dataset_contract', {}) if isinstance(impl.get('dataset_contract'), dict) else {}
    if not contract and isinstance(plan.get('dataset_contract'), dict):
        contract = plan.get('dataset_contract', {})
    return contract if isinstance(contract, dict) else {}


def required_files(plan: dict[str, Any]) -> list[str]:
    contract = data_contract(plan)
    files = contract.get('required_files_per_dataset') if isinstance(contract.get('required_files_per_dataset'), list) else []
    return [str(item) for item in files if str(item).strip()]


def root_hints(plan: dict[str, Any]) -> list[str]:
    contract = data_contract(plan)
    hints = contract.get('expected_roots') if isinstance(contract.get('expected_roots'), list) else []
    primary = contract.get('expected_primary_root')
    secondary = contract.get('secondary_root_hint')
    out = []
    for item in [primary, secondary, *hints]:
        text = str(item or '').strip()
        if text and '<dataset>' not in text and text not in out:
            out.append(text)
    return out


def download_sources(plan: dict[str, Any]) -> list[dict[str, str]]:
    impl = plan.get('implementation_evidence', {}) if isinstance(plan.get('implementation_evidence'), dict) else {}
    rows = impl.get('download_sources') if isinstance(impl.get('download_sources'), list) else []
    out: list[dict[str, str]] = []
    for row in rows:
        if isinstance(row, dict):
            url = str(row.get('url') or '').strip()
            if url:
                out.append({'url': url, 'kind': str(row.get('kind') or 'public_data_url')})
        elif str(row).strip():
            out.append({'url': str(row).strip(), 'kind': 'public_data_url'})
    return out


def google_drive_file_id(url: str) -> str:
    text = str(url or '')
    if '/d/' in text:
        return text.split('/d/', 1)[1].split('/', 1)[0].split('?', 1)[0]
    if 'id=' in text:
        return text.split('id=', 1)[1].split('&', 1)[0]
    return ''

def write_report(paths, payload: dict[str, Any]) -> None:
    lines = ['# Fresh Base Data Acquisition Probe\n\n']
    for key in ['status', 'decision', 'repo_path', 'official_data_url', 'timeout_sec']:
        lines.append(f'- {key}: {payload.get(key, "")}\n')
    lines.append('\n## Dataset Status\n')
    for row in payload.get('dataset_statuses', []):
        lines.append(f"- {row.get('dataset')}: {row.get('status')} | ready_root={row.get('ready_root','')}\n")
    lines.append('\n## Attempts\n')
    for row in payload.get('attempts', []):
        lines.append(f"- {row.get('kind')}: rc={row.get('return_code')} timeout={row.get('timed_out')} cmd={' '.join(row.get('command', []))}\n")
        tail = (row.get('stderr_tail') or row.get('stdout_tail') or '').strip().replace('\n', ' ')[:600]
        if tail:
            lines.append(f'  - tail: {tail}\n')
    lines.append('\n## Guardrail\n')
    lines.append('- A successful download attempt is not scientific evidence. TASTE may continue only after required files exist and loader/import probes pass.\n')
    lines.append('- If this probe times out or cannot access Google Drive non-interactively, keep blocked_fresh_base_data_required visible in the web UI.\n')
    out = paths.reports / 'fresh_base_data_acquisition.md'
    out.write_text(''.join(lines), encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description='Bounded data-acquisition probe for the current Find-selected fresh base.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--timeout-sec', type=int, default=int(os.environ.get('FRESH_BASE_DATA_PROBE_TIMEOUT_SEC', '45')))
    parser.add_argument('--attempt-download', action='store_true')
    args = parser.parse_args()

    paths = build_paths(args.project)
    plan = load_json(paths.state / 'fresh_base_implementation_plan.json', {})
    repo_path = ''
    if isinstance(plan, dict) and isinstance(plan.get('repo'), dict):
        repo_path = str(plan['repo'].get('repo_path') or '')
    repo = Path(repo_path).resolve() if repo_path else Path('')
    plan = plan if isinstance(plan, dict) else {}
    datasets = infer_datasets(plan)
    required = required_files(plan)
    roots = root_hints(plan)
    sources = download_sources(plan)
    before = inspect_datasets(repo, datasets, required, roots) if repo.exists() and required else []
    attempts: list[dict[str, Any]] = []
    contract_missing = not bool(required)

    archive = repo / "data" / ARCHIVE_NAME if repo.exists() else Path("")
    if repo.exists() and archive.exists() and not any(row.get("status") == "ready" for row in before):
        attempts.append(safe_extract_official_zip(archive, repo))
    mid = inspect_datasets(repo, datasets, required, roots) if repo.exists() and required else before

    if args.attempt_download and repo.exists() and required and not any(row.get("status") == "ready" for row in mid):
        gdown = shutil.which("gdown") or ""
        data_dir = repo / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        source = next((row for row in sources if google_drive_file_id(row.get('url', ''))), {})
        file_id = google_drive_file_id(source.get('url', ''))
        if file_id and gdown:
            attempts.append({"kind": "gdown_fresh_base_data", **run([gdown, file_id, "-O", str(data_dir / ARCHIVE_NAME)], repo, max(5, args.timeout_sec))})
            attempts.append(safe_extract_official_zip(data_dir / ARCHIVE_NAME, repo))
        elif file_id:
            attempts.append({"kind": "gdown_fresh_base_data", "return_code": 127, "timed_out": False, "command": ["gdown", file_id], "stderr_tail": "gdown not found"})
        else:
            attempts.append({"kind": "fresh_base_data_download", "return_code": 2, "timed_out": False, "command": ["download_sources_from_fresh_base_implementation_plan"], "stderr_tail": "no supported non-interactive download source found in current fresh-base plan"})
    after = inspect_datasets(repo, datasets, required, roots) if repo.exists() and required else []
    ready = [row['dataset'] for row in after if row.get('status') == 'ready']
    status = 'ready' if ready else 'blocked_missing_dataset_contract' if contract_missing else 'blocked_data_acquisition'
    decision = 'ready_for_loader_probe' if ready else 'blocked_dataset_contract_required' if contract_missing else 'blocked_external_data_required'
    payload = {
        'project': args.project,
        'updated_at': now_iso(),
        'status': status,
        'decision': decision,
        'repo_path': str(repo) if repo.exists() else repo_path,
        'download_sources': sources,
        'timeout_sec': args.timeout_sec,
        'attempt_download': args.attempt_download,
        'dataset_statuses_before': before,
        'dataset_statuses': after,
        'ready_datasets': ready,
        'blocked_datasets': [row['dataset'] for row in after if row.get('status') != 'ready'],
        'attempts': attempts,
        'blocker_reasons': [] if ready else ([
            'current fresh-base plan has no repo-specific dataset_contract.required_files_per_dataset; project Claude Code must infer and record the real loader contract before TASTE probes data roots',
        ] if contract_missing else [
            'current fresh-base download sources have not yielded any loader-ready dataset roots yet',
            'repo-specific required dataset files are still missing after bounded download/extract probe',
        ]),
        'required_files_per_dataset': required,
        'guardrail': 'Do not run selected-base training, legacy-route fallback, paper writing, or claim promotion until ready_datasets is non-empty and loader/import probes pass.',
    }
    save_json(paths.state / 'fresh_base_data_acquisition.json', payload)
    write_report(paths, payload)
    print(paths.reports / 'fresh_base_data_acquisition.md')
    return 0 if ready else 2


if __name__ == '__main__':
    raise SystemExit(main())
