#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from project_paths import ROOT, build_paths, load_project_config


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


def slugify(value: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]+', '_', value.strip().lower()).strip('_') or 'repo'



def module_cmd(stage: str, action: str, *extra: str) -> list[str]:
    return [sys.executable, 'framework/scripts/run_module.py', stage, '--action', action, *extra]

def run(cmd: list[str], cwd: Path = ROOT, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b'').decode('utf-8', 'ignore')
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b'').decode('utf-8', 'ignore')
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr + f'\nTIMEOUT after {timeout}s')


def clone_or_reuse(paths, row: dict) -> tuple[Path | None, dict]:
    url = str(row.get('url', ''))
    local = str(row.get('local_path', ''))
    if local and Path(local).exists():
        return Path(local).resolve(), {'status': 'reused_existing_clone', 'path': local}
    if not url.startswith('http'):
        return None, {'status': 'not_cloneable', 'reason': 'missing http URL'}
    target = paths.repos_selected / slugify(str(row.get('name', 'candidate_repo')))
    if target.exists() and (target / '.git').exists():
        return target.resolve(), {'status': 'reused_existing_clone', 'path': str(target)}
    if target.exists() and not (target / '.git').exists():
        shutil.rmtree(target)
    proc = run(['git', 'clone', '--depth', '1', url, str(target)], ROOT, timeout=int(__import__('os').environ.get('REPO_CLONE_TIMEOUT_SEC', '45')))
    if proc.returncode != 0:
        return None, {'status': 'clone_failed', 'return_code': proc.returncode, 'stderr_tail': (proc.stderr or proc.stdout)[-1500:]}
    return target.resolve(), {'status': 'cloned', 'path': str(target)}


def has_any_file(repo: Path, names: set[str]) -> bool:
    lowered = {name.lower() for name in names}
    try:
        return any(path.name.lower() in lowered for path in repo.rglob('*') if '.git' not in path.parts)
    except Exception:
        return False


def quick_repo_signals(repo: Path) -> dict:
    top = list(repo.iterdir()) if repo.exists() else []
    readme_text = ''
    for path in top:
        if path.is_file() and path.name.lower().startswith('readme'):
            readme_text += path.read_text(encoding='utf-8', errors='ignore')[:20000]
    data_mentions = sum(1 for token in ['dataset', 'data', 'download', 'benchmark', 'processed', 'pickle', '.pkl', '.npy'] if token in readme_text.lower())
    return {
        'has_readme': bool(readme_text),
        'has_install': any((repo / name).exists() for name in ['requirements.txt', 'environment.yml', 'environment.yaml', 'setup.py', 'pyproject.toml']),
        'has_entrypoint': has_any_file(repo, {'main.py', 'train.py', 'run.py', 'eval.py'}),
        'has_data_dir': any((repo / name).exists() for name in ['data', 'dataset', 'datasets']),
        'readme_data_mentions': data_mentions,
    }


def audit_local(project: str, repo: Path, row: dict) -> dict:
    # Side-effect-free local audit for pool candidates. The main audit_local_repo.py updates
    # repo_candidates.json, which is appropriate for active selection but unsafe for exploratory
    # pool audits because it can inflate rankings before data/loader evidence exists.
    signals = quick_repo_signals(repo)
    has_tests = any(path.is_dir() and path.name.lower() in {'tests', 'test'} for path in repo.iterdir()) if repo.exists() else False
    score = 0
    support = []
    if signals.get('has_readme'):
        score += 2; support.append('readme')
    if signals.get('has_install'):
        score += 2; support.append('install')
    if signals.get('has_entrypoint'):
        score += 2; support.append('entrypoint')
    if signals.get('has_data_dir') or signals.get('readme_data_mentions', 0) >= 2:
        score += 2; support.append('dataset_docs')
    if has_tests:
        score += 1; support.append('tests')
    return {'return_code': 0, 'side_effect_free': True, 'support_score': score, 'support_signals': support, 'has_tests': has_tests}


def candidate_identity(row: dict, repo: Path) -> tuple[str, str]:
    name = str(row.get('name') or row.get('repo') or row.get('full_name') or '').strip()
    title = str(row.get('literature_base_title') or row.get('selected_base_title') or row.get('title') or '').strip()
    if not name:
        name = repo.name
    return name, title


def candidate_state_path(paths, prefix: str, repo: Path, row: dict) -> Path:
    name, _ = candidate_identity(row, repo)
    return paths.state / f'{prefix}_{slugify(name or repo.name)}.json'


def extract_data_requirements(project: str, repo: Path, row: dict | None = None, *, candidate_scope: bool = False) -> dict:
    cmd = module_cmd('environment', 'data_requirements', '--project', project, '--repo-path', str(repo))
    paths = build_paths(project)
    state_path = paths.state / 'repo_data_requirements.json'
    if candidate_scope:
        row = row or {}
        name, title = candidate_identity(row, repo)
        cmd.extend(['--candidate-scope', '--candidate-name', name, '--candidate-title', title])
        state_path = candidate_state_path(paths, 'candidate_data_contract', repo, row)
    proc = run(cmd, ROOT, timeout=120)
    req = load_json(state_path, {})
    expected_repo = str(repo.resolve())
    req_repo = str(req.get('repo_path') or '') if isinstance(req, dict) else ''
    if not isinstance(req, dict) or req_repo != expected_repo:
        req = {}
    return {'return_code': proc.returncode, 'requirements': req, 'state_path': str(state_path), 'stderr_tail': (proc.stderr or '')[-1000:]}


def loader_success(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    return bool(
        payload.get('loader_probe_success') is True
        or payload.get('decision') == 'loader_contract_passed'
        or any(isinstance(row, dict) and row.get('loader_probe_success') for row in payload.get('probes', []) or [])
    )


def extract_candidate_loader_probe(project: str, repo: Path, row: dict) -> dict:
    paths = build_paths(project)
    name, title = candidate_identity(row, repo)
    state_path = candidate_state_path(paths, 'candidate_loader_probe', repo, row)
    existing = load_json(state_path, {})
    if loader_success(existing):
        return {'return_code': int(existing.get('probe_return_code') or 0), 'probe': existing, 'state_path': str(state_path), 'reused_existing_loader_success': True}
    proc = run(module_cmd(
        'environment', 'probe_repo',
        '--project', project,
        '--repo-path', str(repo),
        '--candidate-scope',
        '--candidate-name', name,
        '--candidate-title', title,
    ), ROOT, timeout=120)
    probe = load_json(state_path, {})
    return {'return_code': proc.returncode, 'probe': probe if isinstance(probe, dict) else {}, 'state_path': str(state_path), 'stderr_tail': (proc.stderr or '')[-1000:]}


def main() -> None:
    parser = argparse.ArgumentParser(description='Deep-audit non-active repo candidates before switching away from a data-blocked active repo.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--min-score', type=float, default=0.0)
    parser.add_argument('--include-watch', action='store_true', help='When stalled, also audit lower-scored watch candidates without allowing route switching unless evidence-ready.')
    parser.add_argument('--use-cursor', action='store_true', help='Rotate through eligible candidates across repeated audit cycles.')
    args = parser.parse_args()

    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    active = load_json(paths.state / 'active_repo.json', {})
    active_path = str(active.get('repo_path', ''))
    floor = args.min_score or (0.0 if args.include_watch else float(cfg.get('literature', {}).get('repo_candidate_floor', 8.0) or 8.0))
    rows = load_json(paths.state / 'repo_candidates.json', [])
    candidates = []
    for row in rows if isinstance(rows, list) else []:
        if str(row.get('local_path', '')) == active_path:
            continue
        if row.get('hard_topic_mismatch') or row.get('repo_selection_bucket') == 'paused_by_veto':
            continue
        score = float(row.get('repo_reuse_score', row.get('score', 0)) or 0)
        if score >= floor:
            candidates.append(row)
    candidates = sorted(candidates, key=lambda r: (-float(r.get('repo_reuse_score', r.get('score', 0)) or 0), r.get('name', '')))
    cursor_payload = {'next_index': 0, 'history': []}
    if args.use_cursor and candidates:
        cursor_path = paths.state / 'repo_candidate_pool_audit_cursor.json'
        cursor_payload = load_json(cursor_path, cursor_payload)
        total = len(candidates)
        start = int(cursor_payload.get('next_index', 0) or 0) % total
        limit = max(1, min(args.limit, total))
        candidates = [candidates[(start + offset) % total] for offset in range(limit)]
        cursor_payload.update({
            'total_candidates': total,
            'last_start_index': start,
            'next_index': (start + limit) % max(1, total),
            'last_candidates': [row.get('name', '') for row in candidates],
            'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        })
        hist = cursor_payload.get('history', []) if isinstance(cursor_payload.get('history', []), list) else []
        hist.append({'timestamp': cursor_payload['updated_at'], 'start_index': start, 'candidates': cursor_payload['last_candidates']})
        cursor_payload['history'] = hist[-50:]
        save_json(cursor_path, cursor_payload)
    else:
        candidates = candidates[:args.limit]

    audited = []
    evidence_ready = []
    snapshots = {}
    original_req = load_json(paths.state / 'repo_data_requirements.json', {})
    for row in candidates:
        item = {'name': row.get('name', ''), 'url': row.get('url', ''), 'score': row.get('repo_reuse_score', row.get('score', 0))}
        repo, clone_info = clone_or_reuse(paths, row)
        item['clone'] = clone_info
        if repo is None:
            item['decision'] = 'reject_clone_unavailable'
            audited.append(item)
            continue
        item['repo_path'] = str(repo)
        item['signals'] = quick_repo_signals(repo)
        candidate_scope = str(repo) != active_path
        item['local_audit'] = audit_local(args.project, repo, row)
        data_req = extract_data_requirements(args.project, repo, row, candidate_scope=candidate_scope)
        item['data_requirements'] = data_req.get('requirements', {})
        item['data_requirements_scope'] = 'candidate' if candidate_scope else 'active'
        item['data_requirements_path'] = data_req.get('state_path', '')
        req = item['data_requirements'] if isinstance(item.get('data_requirements'), dict) else {}
        ready = req.get('ready_datasets', []) if isinstance(req, dict) else []
        blocked = req.get('blocked_datasets', []) if isinstance(req, dict) else []
        candidate_contract_ready = bool(candidate_scope and req.get('status') == 'ready' and ready)
        if candidate_contract_ready:
            loader_req = extract_candidate_loader_probe(args.project, repo, row)
            item['candidate_loader_probe'] = loader_req.get('probe', {})
            item['candidate_loader_probe_path'] = loader_req.get('state_path', '')
            item['candidate_loader_probe_return_code'] = loader_req.get('return_code')
            item['candidate_loader_import_probe_passed'] = loader_success(item['candidate_loader_probe'])
        execution_ready = item['local_audit'].get('return_code') == 0 and bool(item['signals'].get('has_entrypoint')) and bool(item['signals'].get('has_install') or item['signals'].get('has_readme'))
        has_data_contract = bool(req.get('datasets') or req.get('contract') or item['signals'].get('has_data_dir') or item['signals'].get('readme_data_mentions'))
        item['execution_ready_after_audit'] = execution_ready
        item['has_data_contract_after_audit'] = has_data_contract
        item['candidate_data_contract_ready_after_audit'] = candidate_contract_ready
        item['ready_datasets_after_audit'] = ready
        item['blocked_datasets_after_audit'] = blocked
        if execution_ready and ready and not candidate_scope:
            item['decision'] = 'candidate_evidence_ready_requires_loader_probe'
            evidence_ready.append(item)
        elif execution_ready and candidate_contract_ready:
            item['decision'] = 'candidate_data_contract_ready_loader_probe_required'
        elif execution_ready and has_data_contract:
            item['decision'] = 'candidate_promising_but_data_blocked'
        else:
            item['decision'] = 'candidate_not_execution_ready'
        audited.append(item)
        snapshots[str(repo)] = req

    # Restore active repo data requirement snapshot after auditing candidates so active-state reports stay coherent.
    if isinstance(original_req, dict) and original_req:
        save_json(paths.state / 'repo_data_requirements.json', original_req)

    payload = {
        'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'project': args.project,
        'active_repo_path': active_path,
        'floor': floor,
        'include_watch': bool(args.include_watch),
        'use_cursor': bool(args.use_cursor),
        'cursor': cursor_payload,
        'audited_count': len(audited),
        'evidence_ready_count': len(evidence_ready),
        'audited_candidates': audited,
        'evidence_ready_candidates': evidence_ready,
        'guardrail': 'Do not switch active repo unless a candidate has execution evidence and at least one dataset/loader path that can be verified without fabrication.',
    }
    save_json(paths.state / 'repo_candidate_pool_audit.json', payload)
    lines = ['# Repo Candidate Pool Audit\n\n']
    lines.append(f"- generated_at: {payload['generated_at']}\n")
    lines.append(f"- audited_count: {payload['audited_count']}\n")
    lines.append(f"- evidence_ready_count: {payload['evidence_ready_count']}\n")
    lines.append(f"- include_watch: {payload.get('include_watch', False)}\n")
    lines.append(f"- cursor_last_candidates: {', '.join(payload.get('cursor', {}).get('last_candidates', [])) if isinstance(payload.get('cursor', {}), dict) else ''}\n")
    lines.append(f"- guardrail: {payload['guardrail']}\n\n")
    lines.append('## Candidates\n')
    for item in audited:
        lines.append(f"- {item.get('name')} | score={item.get('score')} | decision={item.get('decision')} | repo={item.get('repo_path','')}\n")
        if item.get('candidate_data_contract_ready_after_audit'):
            lines.append(f"  candidate_data_contract: ready; loader_import_probe={str(item.get('candidate_loader_import_probe_passed', False)).lower()}; ready_data={', '.join(item.get('ready_datasets_after_audit', [])[:5])}\n")
        if item.get('blocked_datasets_after_audit'):
            lines.append(f"  blocked_data: {', '.join(item.get('blocked_datasets_after_audit', [])[:5])}\n")
    out = paths.reports / 'repo_candidate_pool_audit.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
