#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from project_paths import build_paths, load_project_config, project_experiment_python_from_config


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


def one_line(value: Any, limit: int = 320) -> str:
    text = ' '.join(str(value or '').replace('\n', ' ').split())
    return text[:limit] + ('...' if len(text) > limit else '')


def slugify(value: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]+', '_', str(value or '').strip().lower()).strip('_') or 'candidate'


def sha256_file(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return ''


def safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def repo_path_from(row: Any) -> str:
    if not isinstance(row, dict):
        return ''
    for key in ['repo_path', 'local_path', 'path', 'proposed_path_hint']:
        value = str(row.get(key) or '').strip()
        if value:
            return value
    return ''


def candidate_from_state(paths, explicit_repo: str = '', explicit_name: str = '', explicit_title: str = '', explicit_dataset: str = '') -> dict[str, Any]:
    selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    pending = safe_dict(selection.get('pending_environment_candidate')) if isinstance(selection, dict) else {}
    gate = load_json(paths.state / 'base_switch_gate.json', {})
    gate_candidate = safe_dict(gate.get('candidate_route')) if isinstance(gate, dict) else {}
    row = dict(pending or gate_candidate)
    if explicit_repo:
        row['repo_path'] = explicit_repo
    if explicit_name:
        row['name'] = explicit_name
        row['repo'] = explicit_name
    if explicit_title:
        row['literature_base_title'] = explicit_title
        row['title'] = explicit_title
    if explicit_dataset:
        row['claim_ready_dataset'] = explicit_dataset
        row['dataset'] = explicit_dataset
    if not row.get('repo') and row.get('name'):
        row['repo'] = row.get('name')
    if not row.get('name') and row.get('repo'):
        row['name'] = row.get('repo')
    return row


def matches_candidate(payload: Any, repo_path: str, name: str) -> bool:
    if not isinstance(payload, dict):
        return False
    payload_path = repo_path_from(payload)
    if repo_path and payload_path and payload_path == repo_path:
        return True
    payload_name = str(payload.get('candidate_repo') or payload.get('repo_name') or payload.get('name') or payload.get('repo') or '').strip()
    return bool(name and payload_name and payload_name == name)


def find_matching_payload(paths, patterns: list[str], repo_path: str, name: str) -> tuple[Path | None, dict[str, Any]]:
    for pattern in patterns:
        for path in sorted(paths.state.glob(pattern)):
            payload = load_json(path, {})
            if matches_candidate(payload, repo_path, name):
                return path, payload
    return None, {}


def claim_ready_datasets(*payloads: Any) -> list[str]:
    out: list[str] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in ['claim_ready_dataset', 'dataset']:
            value = str(payload.get(key) or '').strip()
            if value and value not in out:
                out.append(value)
        for key in ['claim_ready_datasets', 'ready_datasets']:
            values = payload.get(key)
            if isinstance(values, str):
                values = [values]
            for item in values if isinstance(values, list) else []:
                value = str(item or '').strip()
                if value and value not in out:
                    out.append(value)
        for row in payload.get('probes', []) if isinstance(payload.get('probes'), list) else []:
            if isinstance(row, dict) and row.get('claim_ready'):
                value = str(row.get('dataset') or '').strip()
                if value and value not in out:
                    out.append(value)
    return out


def loader_success(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return bool(
        payload.get('loader_probe_success') is True
        or payload.get('status') == 'passed'
        or payload.get('decision') in {'candidate_loader_import_probe_passed', 'loader_contract_passed'}
        or any(isinstance(row, dict) and row.get('loader_probe_success') for row in safe_list(payload.get('probes')))
    )


def contract_ready(payload: Any) -> bool:
    return bool(isinstance(payload, dict) and payload.get('status') == 'ready' and payload.get('ready_datasets'))


def read_text(path: Path, limit: int = 120000) -> str:
    try:
        return path.read_text(encoding='utf-8', errors='replace')[:limit] if path.exists() else ''
    except Exception:
        return ''


def readme_text(repo: Path) -> str:
    for name in ['README.md', 'README.rst', 'README.txt', 'readme.md']:
        text = read_text(repo / name)
        if text:
            return text
    return ''


def extract_reference_commands(readme: str) -> list[str]:
    commands: list[str] = []
    for raw in readme.splitlines():
        line = raw.strip().strip('`')
        if not line or line.startswith('#'):
            continue
        lowered = line.lower()
        if any(token in lowered for token in ['python ', 'accelerate launch', 'bash ']) and any(token in lowered for token in ['train', 'infer', 'preprocess', 'generate', 'eval', 'main']):
            if line not in commands:
                commands.append(line[:400])
    return commands[:12]


def sample_files_from_contract(contract: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for status in safe_list(contract.get('local_statuses')):
        if not isinstance(status, dict):
            continue
        for key in ['split_files', 'sample_files']:
            for rel in safe_list(status.get(key)):
                text = str(rel or '').strip()
                if text and text not in out:
                    out.append(text)
    for row in safe_list(contract.get('sampled_files')):
        if not isinstance(row, dict):
            continue
        rel = str(row.get('relative_path') or row.get('path') or '').strip()
        if rel and rel not in out:
            out.append(rel)
    return out[:12]


def parse_sample(path: Path, max_rows: int = 8) -> dict[str, Any]:
    suffixes = ''.join(path.suffixes).lower()
    row_count = 0
    fields: list[str] = []
    try:
        if suffixes.endswith('.jsonl.gz') or suffixes.endswith('.json.gz'):
            handle_ctx = gzip.open(path, 'rt', encoding='utf-8', errors='replace')
        else:
            handle_ctx = path.open('r', encoding='utf-8', errors='replace')
        if suffixes.endswith(('.jsonl', '.jsonl.gz')):
            with handle_ctx as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    row_count += 1
                    if isinstance(obj, dict):
                        for key in obj:
                            if key not in fields:
                                fields.append(str(key))
                    if row_count >= max_rows:
                        break
        elif suffixes.endswith('.csv'):
            with handle_ctx as handle:
                reader = csv.DictReader(handle)
                fields = list(reader.fieldnames or [])
                for _ in reader:
                    row_count += 1
                    if row_count >= max_rows:
                        break
        elif suffixes.endswith('.json') or suffixes.endswith('.json.gz'):
            with handle_ctx as handle:
                obj = json.load(handle)
            if isinstance(obj, list):
                row_count = min(len(obj), max_rows)
                if obj and isinstance(obj[0], dict):
                    fields = list(obj[0])
            elif isinstance(obj, dict):
                row_count = 1
                fields = list(obj)
        elif suffixes.endswith('.parquet'):
            import pandas as pd
            df = pd.read_parquet(path)
            row_count = min(len(df), max_rows)
            fields = [str(col) for col in df.columns]
        else:
            return {'success': False, 'reason': 'unsupported_sample_format', 'path': str(path)}
    except Exception as exc:
        return {'success': False, 'reason': f'{type(exc).__name__}: {exc}', 'path': str(path)}
    return {'success': row_count > 0, 'path': str(path), 'sample_rows': row_count, 'fields': fields[:40]}


def experiment_python(project: str) -> str:
    cfg = load_project_config(project)
    return project_experiment_python_from_config(cfg if isinstance(cfg, dict) else {}, fallback_to_current=True)


def state_names(slug: str, mode: str) -> tuple[str, str]:
    if mode == 'protocol':
        return f'candidate_{slug}_reference_protocol_probe.json', f'candidate_{slug}_reference_protocol_probe.md'
    if mode == 'smoke':
        return f'candidate_{slug}_reference_smoke.json', f'candidate_{slug}_reference_smoke.md'
    return f'candidate_{slug}_reference_reproduction_audit.json', f'candidate_{slug}_reference_reproduction_audit.md'


def write_report(paths, report_name: str, payload: dict[str, Any]) -> None:
    lines = [f"# {report_name[:-3]}\n\n"]
    for key in ['status', 'decision', 'repo_name', 'repo_path', 'dataset', 'mode', 'return_code']:
        lines.append(f"- {key}: {payload.get(key, '')}\n")
    blockers = safe_list(payload.get('blockers'))
    if blockers:
        lines.append('\n## Blockers\n')
        for blocker in blockers:
            lines.append(f'- {blocker}\n')
    lines.append('\n## JSON\n\n```json\n')
    lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
    lines.append('\n```\n')
    (paths.reports / report_name).write_text(''.join(lines), encoding='utf-8')


def build_payload(args: argparse.Namespace) -> tuple[int, dict[str, Any], str, str]:
    paths = build_paths(args.project)
    candidate = candidate_from_state(paths, args.repo_path, args.candidate_name, args.candidate_title, args.dataset)
    repo_path = repo_path_from(candidate)
    repo = Path(repo_path).resolve() if repo_path else Path('')
    name = str(candidate.get('name') or candidate.get('repo') or repo.name or 'candidate').strip()
    slug = slugify(name.replace('/', '_') or repo.name)
    loader_path, loader = find_matching_payload(paths, ['candidate_*loader*probe*.json', '*loader*probe*.json', 'real_dataset_probe.json'], repo_path, name)
    contract_path, contract = find_matching_payload(paths, ['candidate_*data*contract*.json', '*data*contract*.json'], repo_path, name)
    datasets = claim_ready_datasets(candidate, loader, contract)
    dataset = args.dataset or (datasets[0] if datasets else '')
    readme = readme_text(repo) if repo.exists() else ''
    commands = extract_reference_commands(readme)
    blockers: list[str] = []
    if not repo_path or not repo.exists():
        blockers.append('candidate repo_path is missing or does not exist')
    if not contract_ready(contract):
        blockers.append('candidate data contract is not ready')
    if not loader_success(loader):
        blockers.append('candidate repo-defined loader/import probe has not passed')
    if not commands:
        blockers.append('README/metadata does not expose an auditable reference command')

    artifact_dir = paths.artifacts / 'candidate_base_switch_evidence' / f'{slug}_{args.mode}_{dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")}'
    artifact_dir.mkdir(parents=True, exist_ok=True)
    protocol_ok = not blockers
    rc = 0 if protocol_ok else 2
    status = 'reference_protocol_probe_passed' if protocol_ok else 'blocked'
    decision = 'ready_for_bounded_reference_smoke' if protocol_ok else 'candidate_reference_protocol_blocked'
    extra: dict[str, Any] = {}
    if args.mode == 'smoke':
        protocol_state = load_json(paths.state / state_names(slug, 'protocol')[0], {})
        protocol_ok = bool(protocol_ok or (isinstance(protocol_state, dict) and protocol_state.get('status') == 'reference_protocol_probe_passed'))
        sample_results = []
        for rel in sample_files_from_contract(contract):
            sample_path = Path(rel)
            if not sample_path.is_absolute():
                sample_path = repo / rel
            if sample_path.exists():
                sample_results.append(parse_sample(sample_path))
        smoke_ok = bool(protocol_ok and (any(row.get('success') for row in sample_results) or loader_success(loader)))
        if not protocol_ok:
            blockers.append('candidate reference protocol probe has not passed')
        if not smoke_ok and protocol_ok:
            blockers.append('candidate bounded sample parse/loader smoke did not pass')
        rc = 0 if smoke_ok else 2
        status = 'reference_smoke_passed' if smoke_ok else 'blocked'
        decision = 'ready_for_reference_reproduction_audit' if smoke_ok else 'candidate_reference_smoke_blocked'
        extra['sample_parse_results'] = sample_results
    elif args.mode == 'full':
        smoke_state = load_json(paths.state / state_names(slug, 'smoke')[0], {})
        smoke_ok = bool(isinstance(smoke_state, dict) and smoke_state.get('status') == 'reference_smoke_passed')
        rc = 2
        status = 'blocked_reference_reproduction_audit'
        decision = 'candidate_full_reference_reproduction_requires_official_training_or_model_artifacts'
        blockers = [] if smoke_ok else ['candidate bounded reference smoke has not passed']
        blockers.append('full candidate reference reproduction is not auto-executed by this generic probe because the official commands require model/checkpoint/training resources; keep the route proposal-only and continue search or provide a dedicated audited reproduction adapter')
        extra.update({
            'official_reference_commands': commands,
            'execute_requested': bool(args.execute),
            'paper_level_reproduction_passed': False,
            'audit_ready': False,
        })
    payload = {
        'project': args.project,
        'generated_at': now_iso(),
        'status': status,
        'decision': decision,
        'mode': args.mode,
        'repo_name': name,
        'repo_path': repo_path,
        'candidate_scope': True,
        'dataset': dataset,
        'ready_datasets': datasets,
        'python_executable': experiment_python(args.project),
        'env_name': args.env_name,
        'return_code': rc,
        'artifact_dir': str(artifact_dir),
        'loader_probe_path': str(loader_path) if loader_path else '',
        'data_contract_path': str(contract_path) if contract_path else '',
        'loader_probe_success': loader_success(loader),
        'data_contract_ready': contract_ready(contract),
        'reference_commands': commands,
        'blockers': blockers,
        'hashes': {
            'readme_sha256': sha256_file(repo / 'README.md') if repo.exists() else '',
        },
        'guardrail': 'Candidate base-switch evidence remains non-authoritative: it cannot edit active_repo/evidence_ready_repo_selection, launch main-route experiments, or support paper claims before deterministic base-switch execution.',
        **extra,
    }
    if args.mode != 'full':
        payload['audit_ready'] = bool(rc == 0)
        payload['paper_level_reproduction_passed'] = False
    return rc, payload, slug, state_names(slug, args.mode)[1]


def main() -> int:
    parser = argparse.ArgumentParser(description='Collect candidate-scoped reference evidence for deterministic base-switch gates.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--repo-path', default='')
    parser.add_argument('--candidate-name', default='')
    parser.add_argument('--candidate-title', default='')
    parser.add_argument('--dataset', default='')
    parser.add_argument('--env-name', default='')
    parser.add_argument('--mode', choices=['protocol', 'smoke', 'full'], default='protocol')
    parser.add_argument('--timeout-sec', type=int, default=300)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    paths = build_paths(args.project)
    rc, payload, slug, report_name = build_payload(args)
    state_name, report_name = state_names(slug, args.mode)
    save_json(paths.state / state_name, payload)
    write_report(paths, report_name, payload)
    print(json.dumps({'status': payload.get('status'), 'decision': payload.get('decision'), 'repo_path': payload.get('repo_path'), 'return_code': rc, 'state_path': str(paths.state / state_name)}, ensure_ascii=False))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
