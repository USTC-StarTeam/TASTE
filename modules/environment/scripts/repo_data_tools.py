#!/usr/bin/env python3
from __future__ import annotations

# Consolidated Environment repo/data backend tools. Public access goes through
# modules/environment/main.py; project-local adapters keep their legacy names.


import argparse
import csv
import datetime as dt
import gzip
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

def dr__repo_root_from_script() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / 'framework').is_dir() and (candidate / 'modules').is_dir() and (candidate / 'web').is_dir():
            return candidate
    return current.parents[1]
ROOT = dr__repo_root_from_script()

def dr_project_from_args(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--project', default='')
    ns, _ = parser.parse_known_args(argv)
    if ns.project:
        return ns.project
    return os.environ.get('PROJECT_ID', '')

def dr_parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build a project repo-data requirement contract, with a safe generic fallback.')
    parser.add_argument('--project', default='')
    parser.add_argument('--repo-path', default='')
    parser.add_argument('--candidate-scope', action='store_true', help='Write candidate-scoped evidence instead of active-route repo_data_requirements.json.')
    parser.add_argument('--candidate-name', default='')
    parser.add_argument('--candidate-title', default='')
    return parser.parse_known_args(argv)[0]

def dr_load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default

def dr_save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

def dr__repo_from_state(project: str, explicit: str='') -> str:
    if explicit:
        return explicit
    state = ROOT / 'projects' / project / 'state'
    for name in ['evidence_ready_repo_selection.json', 'active_repo.json', 'fresh_base_implementation_plan.json']:
        payload = dr_load_json(state / name, {})
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

def dr__dataset_names_from_repo(repo_path: str) -> list[str]:
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
DATA_ROOT_NAMES = {'data', 'datasets', 'dataset', 'benchmark', 'benchmarks'}
STRUCTURED_DATA_PATTERNS = ['*.jsonl', '*.jsonl.gz', '*.ndjson', '*.ndjson.gz', '*.csv', '*.tsv', '*.json', '*.json.gz']

def dr_slugify(value: str) -> str:
    text = re.sub('[^A-Za-z0-9_.-]+', '_', str(value or '').strip()).strip('._-').lower()
    return text[:120] or 'candidate'

def dr__resolved_path(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    path = Path(text).expanduser()
    try:
        return str(path.resolve()) if path.exists() else text
    except Exception:
        return text

def dr_candidate_identity_from_state(project: str, explicit_name: str='', explicit_title: str='') -> tuple[str, str]:
    if explicit_name or explicit_title:
        return (str(explicit_name or '').strip(), str(explicit_title or '').strip())
    state = ROOT / 'projects' / project / 'state'
    selection = dr_load_json(state / 'evidence_ready_repo_selection.json', {})
    if isinstance(selection, dict):
        pending = selection.get('pending_environment_candidate') if isinstance(selection.get('pending_environment_candidate'), dict) else {}
        decision = pending.get('claude_topic_decision') if isinstance(pending.get('claude_topic_decision'), dict) else selection.get('claude_topic_decision') if isinstance(selection.get('claude_topic_decision'), dict) else {}
        name = str(pending.get('name') or pending.get('repo') or decision.get('best_repo') or '').strip() if isinstance(pending, dict) else ''
        title = str(pending.get('literature_base_title') or pending.get('selected_base_title') or decision.get('literature_base_title') or '').strip() if isinstance(pending, dict) else ''
        if name or title:
            return (name, title)
    gate = dr_load_json(state / 'base_switch_gate.json', {})
    candidate = gate.get('candidate_route') if isinstance(gate, dict) and isinstance(gate.get('candidate_route'), dict) else {}
    return (str(candidate.get('repo') or '').strip(), str(candidate.get('title') or '').strip())

def dr_candidate_repo_from_state(project: str, explicit: str='') -> str:
    if explicit:
        return dr__resolved_path(explicit)
    state = ROOT / 'projects' / project / 'state'
    selection = dr_load_json(state / 'evidence_ready_repo_selection.json', {})
    if isinstance(selection, dict):
        pending = selection.get('pending_environment_candidate') if isinstance(selection.get('pending_environment_candidate'), dict) else {}
        decision = pending.get('claude_topic_decision') if isinstance(pending.get('claude_topic_decision'), dict) else selection.get('claude_topic_decision') if isinstance(selection.get('claude_topic_decision'), dict) else {}
        for row in [pending, decision]:
            if not isinstance(row, dict):
                continue
            for key in ['repo_path', 'local_path', 'path']:
                value = dr__resolved_path(row.get(key))
                if value:
                    return value
    gate = dr_load_json(state / 'base_switch_gate.json', {})
    candidate = gate.get('candidate_route') if isinstance(gate, dict) and isinstance(gate.get('candidate_route'), dict) else {}
    for key in ['repo_path', 'local_path', 'path']:
        value = dr__resolved_path(candidate.get(key)) if isinstance(candidate, dict) else ''
        if value:
            return value
    return ''

def dr_candidate_slug(repo_path: str, candidate_name: str='') -> str:
    return dr_slugify(candidate_name or (Path(str(repo_path)).name if repo_path else 'candidate'))

def dr__data_roots(repo: Path) -> list[Path]:
    roots = [repo / name for name in DATA_ROOT_NAMES if (repo / name).is_dir()]
    return roots or [repo]

def dr__iter_structured_data_files(repo: Path, limit: int=120) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for root in dr__data_roots(repo):
        for pattern in STRUCTURED_DATA_PATTERNS:
            for path in sorted(root.rglob(pattern)):
                if path in seen or not path.is_file():
                    continue
                if any((part in {'.git', '__pycache__', '.pytest_cache'} for part in path.parts)):
                    continue
                seen.add(path)
                files.append(path)
                if len(files) >= limit:
                    return files
    return files

def dr__open_text(path: Path):
    if path.name.lower().endswith('.gz'):
        return gzip.open(path, 'rt', encoding='utf-8', errors='replace')
    return path.open('r', encoding='utf-8', errors='replace', newline='')

def dr__looks_like_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = ' '.join(value.split())
    if len(text) < 20:
        return False
    alpha = sum((1 for ch in text if ch.isalpha()))
    return alpha >= 8 and any((sep in text for sep in [' ', ',', '|', ';', ':']))

def dr__summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields: set[str] = set()
    common_fields: set[str] | None = None
    id_fields: set[str] = set()
    text_fields: set[str] = set()
    numeric_fields: set[str] = set()
    label_fields: set[str] = set()
    for row in rows:
        row_fields = {str(key) for key in row.keys()}
        fields.update(row_fields)
        common_fields = set(row_fields) if common_fields is None else common_fields & row_fields
        for key, value in row.items():
            lowered = str(key).strip().lower()
            if lowered == 'id' or lowered.endswith('_id') or lowered.endswith('id'):
                id_fields.add(str(key))
            if dr__looks_like_text(value):
                text_fields.add(str(key))
            if isinstance(value, (int, float)) and (not isinstance(value, bool)):
                numeric_fields.add(str(key))
                if lowered in {'label', 'rating', 'score', 'target', 'y'} or lowered.endswith('_label'):
                    label_fields.add(str(key))
    return {'field_names': sorted(fields)[:80], 'common_field_names': sorted(common_fields or [])[:80], 'id_fields': sorted(id_fields), 'natural_language_text_fields': sorted(text_fields), 'numeric_fields': sorted(numeric_fields)[:40], 'label_fields': sorted(label_fields), 'schema_stable': bool(common_fields)}

def dr__sample_jsonl(path: Path, limit: int) -> tuple[list[dict[str, Any]], int, str]:
    rows: list[dict[str, Any]] = []
    inspected = 0
    with dr__open_text(path) as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            inspected += 1
            try:
                item = json.loads(text)
            except Exception as exc:
                return (rows, inspected, f'jsonl_parse_failed:{type(exc).__name__}')
            if isinstance(item, dict):
                rows.append(item)
            elif isinstance(item, list):
                rows.append({'_array_len': len(item)})
            if len(rows) >= limit:
                break
    return (rows, inspected, '')

def dr__sample_csv(path: Path, limit: int) -> tuple[list[dict[str, Any]], int, str]:
    rows: list[dict[str, Any]] = []
    delimiter = '\t' if path.name.lower().endswith('.tsv') else ','
    with dr__open_text(path) as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        inspected = 0
        for row in reader:
            inspected += 1
            rows.append(dict(row))
            if len(rows) >= limit:
                break
    return (rows, inspected, '')

def dr__sample_json(path: Path, limit: int) -> tuple[list[dict[str, Any]], int, str]:
    try:
        if path.stat().st_size > 8000000:
            return ([], 0, 'json_file_too_large_for_safe_generic_sampling')
    except Exception:
        pass
    with dr__open_text(path) as handle:
        payload = json.load(handle)
    rows: list[dict[str, Any]] = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        candidates = []
        for value in payload.values():
            if isinstance(value, list):
                candidates = value
                break
        if not candidates:
            candidates = [payload]
    else:
        candidates = []
    for item in candidates[:limit]:
        if isinstance(item, dict):
            rows.append(item)
    return (rows, len(candidates), '')

def dr__sample_data_file(path: Path, repo: Path, limit: int=50) -> dict[str, Any]:
    rel = str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path)
    lowered = path.name.lower()
    rows: list[dict[str, Any]] = []
    inspected = 0
    error = ''
    try:
        if lowered.endswith(('.jsonl', '.jsonl.gz', '.ndjson', '.ndjson.gz')):
            rows, inspected, error = dr__sample_jsonl(path, limit)
        elif lowered.endswith(('.csv', '.tsv')):
            rows, inspected, error = dr__sample_csv(path, limit)
        elif lowered.endswith(('.json', '.json.gz')):
            rows, inspected, error = dr__sample_json(path, limit)
        else:
            error = 'unsupported_structured_suffix'
    except Exception as exc:
        error = f'sample_failed:{type(exc).__name__}:{exc}'
    summary = dr__summarize_rows(rows) if rows else {'field_names': [], 'common_field_names': [], 'id_fields': [], 'natural_language_text_fields': [], 'numeric_fields': [], 'label_fields': [], 'schema_stable': False}
    parse_success = bool(rows and (not error) and summary.get('field_names'))
    return {'path': str(path), 'relative_path': rel, 'format': 'jsonl' if lowered.endswith(('.jsonl', '.jsonl.gz', '.ndjson', '.ndjson.gz')) else 'csv' if lowered.endswith(('.csv', '.tsv')) else 'json' if lowered.endswith(('.json', '.json.gz')) else 'unknown', 'parse_success': parse_success, 'sample_rows': len(rows), 'inspected_rows': inspected, 'error': error, **summary}

def dr__dataset_name_for(path: Path, repo: Path) -> str:
    try:
        parts = path.relative_to(repo).parts
    except Exception:
        return path.parent.name or 'dataset'
    for anchor in DATA_ROOT_NAMES:
        if anchor not in parts:
            continue
        index = parts.index(anchor)
        if index + 1 < len(parts) - 1:
            return str(parts[index + 1])
        return anchor
    return path.parent.name or 'dataset'

def dr_discover_generic_data_contract(project: str, repo_path: str, candidate_name: str='', candidate_title: str='') -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    repo = Path(repo_path).expanduser() if repo_path else Path()
    if not repo_path or not repo.exists() or (not repo.is_dir()):
        return {'generated_at': now, 'project': project, 'candidate_scope': True, 'candidate_repo': candidate_name, 'candidate_title': candidate_title, 'repo_path': repo_path, 'status': 'blocked_candidate_repo_missing', 'decision': 'candidate_repo_path_required', 'ready_datasets': [], 'blocked_datasets': ['candidate_repo_path'], 'local_statuses': [], 'sampled_files': [], 'blocker_reasons': ['candidate repo_path is missing or does not exist']}
    repo = repo.resolve()
    samples = [dr__sample_data_file(path, repo) for path in dr__iter_structured_data_files(repo)]
    grouped: dict[str, dict[str, Any]] = {}
    for sample in samples:
        dataset = dr__dataset_name_for(Path(sample['path']), repo)
        row = grouped.setdefault(dataset, {'dataset': dataset, 'status': 'blocked', 'candidate_roots': [], 'sampled_files': [], 'parseable_file_count': 0, 'sample_rows': 0, 'field_names': set(), 'id_fields': set(), 'natural_language_text_fields': set(), 'label_fields': set(), 'split_files': []})
        row['sampled_files'].append(sample)
        row['sample_rows'] += int(sample.get('sample_rows') or 0)
        row['field_names'].update(sample.get('field_names') or [])
        row['id_fields'].update(sample.get('id_fields') or [])
        row['natural_language_text_fields'].update(sample.get('natural_language_text_fields') or [])
        row['label_fields'].update(sample.get('label_fields') or [])
        stem = Path(sample.get('relative_path') or '').stem.lower()
        if any((token in stem for token in ['train', 'valid', 'validation', 'dev', 'test'])):
            row['split_files'].append(sample.get('relative_path'))
        if sample.get('parse_success'):
            row['parseable_file_count'] += 1
    local_statuses: list[dict[str, Any]] = []
    ready: list[str] = []
    for dataset, row in sorted(grouped.items()):
        ok = bool(row['parseable_file_count'] and row['sample_rows'] and row['field_names'])
        if ok:
            ready.append(dataset)
        local_statuses.append({'dataset': dataset, 'status': 'ready' if ok else 'blocked', 'reason': 'structured local data files parse with stable sample schema' if ok else 'no parseable structured data sample with fields', 'ready_root': str(repo / 'data' / dataset) if ok and (repo / 'data' / dataset).exists() else '', 'candidate_roots': sorted({str(Path(sample['path']).parent) for sample in row['sampled_files']})[:12], 'sampled_file_count': len(row['sampled_files']), 'parseable_file_count': row['parseable_file_count'], 'sample_rows': row['sample_rows'], 'field_names': sorted(row['field_names'])[:80], 'id_fields': sorted(row['id_fields']), 'natural_language_text_fields': sorted(row['natural_language_text_fields']), 'label_fields': sorted(row['label_fields']), 'split_files': sorted({str(item) for item in row['split_files']})[:20]})
    blocked = [row['dataset'] for row in local_statuses if row['dataset'] not in ready]
    if not local_statuses:
        blocked = ['candidate_structured_data_contract']
    status = 'ready' if ready else 'blocked_no_parseable_structured_candidate_data'
    return {'generated_at': now, 'project': project, 'candidate_scope': True, 'candidate_repo': candidate_name, 'candidate_title': candidate_title, 'repo_path': str(repo), 'status': status, 'decision': 'ready_for_loader_probe' if ready else 'candidate_data_contract_blocked', 'adapter_missing': False, 'adapter_path': '', 'datasets': [row['dataset'] for row in local_statuses] or blocked, 'ready_datasets': ready, 'blocked_datasets': blocked, 'download_sources': [{'dataset': name, 'source': 'candidate_repo_local_files', 'path': str(repo)} for name in ready], 'contract': {'status': 'candidate_data_contract_ready' if ready else 'blocked', 'scope': 'candidate_base_switch_proposal', 'required_files_per_dataset': [], 'loader_probe_required': True, 'claim_rule': 'Generic parsing proves only local structured data presence; repo loader/import, reference protocol, smoke, full reproduction, and artifact-local audit gates remain separate.'}, 'local_statuses': local_statuses, 'sampled_files': samples[:40], 'blocker_reasons': [] if ready else ['No parseable structured local data files were found under data/datasets/benchmark roots.'], 'guardrails': ['Candidate-scoped evidence must not overwrite active-route repo_data_requirements.json or real_dataset_probe.json.', 'Generic structured-data parsing is not a repo loader/import probe and does not authorize route switching.', 'Reference protocol, bounded smoke, full reference reproduction, and artifact-local audit gates remain mandatory.']}


def dr__paths_equal(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return str(Path(left).expanduser().resolve()) == str(Path(right).expanduser().resolve())
    except Exception:
        return str(left).strip() == str(right).strip()


def dr__list_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or '').strip()]
    return []


def dr__normalize_candidate_contract_payload(project: str, repo_path: str, candidate_name: str, candidate_title: str, source: dict[str, Any], source_path: Path, source_kind: str) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    local_statuses = source.get('local_statuses') if isinstance(source.get('local_statuses'), list) else []
    if not local_statuses and isinstance(source.get('dataset_statuses'), list):
        local_statuses = source.get('dataset_statuses') or []
    ready = dr__list_text(source.get('ready_datasets'))
    if not ready:
        for row in local_statuses:
            if isinstance(row, dict) and str(row.get('status') or '').lower() == 'ready':
                dataset = str(row.get('dataset') or row.get('id') or '').strip()
                if dataset and dataset not in ready:
                    ready.append(dataset)
    blocked = dr__list_text(source.get('blocked_datasets'))
    if not blocked:
        for row in local_statuses:
            if isinstance(row, dict) and str(row.get('status') or '').lower() != 'ready':
                dataset = str(row.get('dataset') or row.get('id') or '').strip()
                if dataset and dataset not in ready and dataset not in blocked:
                    blocked.append(dataset)
    datasets = dr__list_text(source.get('datasets')) or ready + [item for item in blocked if item not in ready]
    if not datasets and local_statuses:
        for row in local_statuses:
            if isinstance(row, dict):
                dataset = str(row.get('dataset') or row.get('id') or '').strip()
                if dataset and dataset not in datasets:
                    datasets.append(dataset)
    contract = source.get('contract') if isinstance(source.get('contract'), dict) else source.get('dataset_contract') if isinstance(source.get('dataset_contract'), dict) else {}
    contract = dict(contract) if isinstance(contract, dict) else {}
    if ready:
        contract.setdefault('scope', 'candidate_base_switch_proposal')
        contract['status'] = 'candidate_data_contract_ready'
        status = 'ready'
        decision = 'ready_for_loader_probe'
    else:
        contract.setdefault('status', 'blocked')
        contract.setdefault('scope', 'candidate_base_switch_proposal')
        status = str(source.get('status') or 'blocked_candidate_data_contract_required')
        if status in {'ready', 'passed'}:
            status = 'ready'
            decision = 'ready_for_loader_probe'
        else:
            decision = str(source.get('decision') or 'candidate_data_contract_blocked')
    blocker_reasons = dr__list_text(source.get('blocker_reasons'))
    if not ready and not blocker_reasons:
        blocker_reasons = [str(source.get('reason') or 'Candidate data contract is not ready.').strip()]
    download_sources = source.get('download_sources') if isinstance(source.get('download_sources'), list) else []
    payload = {
        'generated_at': now,
        'project': project,
        'candidate_scope': True,
        'candidate_repo': candidate_name,
        'candidate_title': candidate_title,
        'repo_path': str(Path(repo_path).expanduser().resolve() if repo_path and Path(repo_path).expanduser().exists() else repo_path),
        'status': status,
        'decision': decision,
        'adapter_missing': False,
        'adapter_path': str(source_path),
        'datasets': datasets or ['candidate_structured_data_contract'],
        'ready_datasets': ready,
        'blocked_datasets': [item for item in blocked if item not in ready] if ready else blocked or ['candidate_structured_data_contract'],
        'download_sources': download_sources,
        'contract': contract,
        'local_statuses': local_statuses,
        'sampled_files': source.get('sampled_files', []) if isinstance(source.get('sampled_files'), list) else [],
        'blocker_reasons': [] if ready else blocker_reasons,
        'source_kind': source_kind,
        'source_path': str(source_path),
        'guardrails': [
            'Candidate-scoped evidence must not overwrite active-route repo_data_requirements.json or real_dataset_probe.json.',
            'A ready candidate data contract only authorizes loader/import probing; it does not authorize route switching by itself.',
            'Reference protocol, bounded smoke, full reproduction, and artifact-local audit gates remain mandatory.',
        ],
    }
    if isinstance(source.get('dataset_statuses'), list):
        payload['dataset_statuses'] = source.get('dataset_statuses')
    return payload


def dr__candidate_contract_from_fresh_acquisition(project: str, repo_path: str, candidate_name: str, candidate_title: str) -> dict[str, Any]:
    state = ROOT / 'projects' / project / 'state'
    path = state / 'fresh_base_data_acquisition.json'
    source = dr_load_json(path, {})
    if not isinstance(source, dict) or not source:
        return {}
    source_repo = str(source.get('repo_path') or '').strip()
    if source_repo and not dr__paths_equal(source_repo, repo_path):
        return {}
    if str(source.get('status') or '').lower() != 'ready' and str(source.get('decision') or '') != 'ready_for_loader_probe':
        return {}
    payload = dr__normalize_candidate_contract_payload(project, repo_path, candidate_name, candidate_title, source, path, 'fresh_base_data_acquisition')
    return payload if payload.get('ready_datasets') else {}


def dr__write_candidate_requirement_payload(project: str, payload: dict[str, Any], repo_path: str, candidate_name: str='') -> int:
    state = ROOT / 'projects' / project / 'state'
    reports = ROOT / 'projects' / project / 'reports'
    slug = dr_candidate_slug(payload.get('repo_path') or repo_path, candidate_name)
    state_path = state / f'candidate_data_contract_{slug}.json'
    dr_save_json(state_path, payload)
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f'candidate_data_contract_{slug}.md'
    lines = ['# Candidate Data Contract\n\n']
    lines.append(f"- generated_at: {payload.get('generated_at', '')}\n")
    lines.append(f"- status: {payload.get('status', '')}\n")
    lines.append(f"- decision: {payload.get('decision', '')}\n")
    lines.append(f"- source_kind: {payload.get('source_kind', 'generic')}\n")
    lines.append(f"- candidate_repo: {candidate_name or payload.get('candidate_repo', '')}\n")
    lines.append(f"- repo_path: {payload.get('repo_path') or repo_path or 'not selected'}\n")
    lines.append(f"- ready_datasets: {', '.join(payload.get('ready_datasets') or []) or 'none'}\n")
    lines.append(f"- blocked_datasets: {', '.join(payload.get('blocked_datasets') or []) or 'none'}\n")
    lines.append('\n## Dataset Evidence\n')
    for row in payload.get('local_statuses') or payload.get('dataset_statuses') or []:
        if not isinstance(row, dict):
            continue
        dataset = row.get('dataset') or row.get('id') or 'dataset'
        status = row.get('status') or ('ready' if dataset in (payload.get('ready_datasets') or []) else 'blocked')
        root = row.get('ready_root') or row.get('root') or ''
        present = row.get('present_required_files') if isinstance(row.get('present_required_files'), list) else []
        lines.append(f"- {dataset}: {status}; root={root or 'n/a'}; present={', '.join(str(x) for x in present) or 'n/a'}\n")
    for reason in payload.get('blocker_reasons') or []:
        lines.append(f'- blocker: {reason}\n')
    report_path.write_text(''.join(lines), encoding='utf-8')
    print(json.dumps({'status': payload.get('status'), 'project': project, 'candidate_scope': True, 'repo_path': payload.get('repo_path') or repo_path, 'ready_datasets': payload.get('ready_datasets', []), 'state_path': str(state_path)}, ensure_ascii=False))
    return 0 if payload.get('status') == 'ready' and payload.get('ready_datasets') else 2


def dr_write_candidate_requirement_from_adapter(project: str, repo_path: str, candidate_name: str, candidate_title: str, adapter: Path, argv: list[str]) -> int:
    state = ROOT / 'projects' / project / 'state'
    proc = subprocess.run([sys.executable, str(adapter), *argv], cwd=ROOT, text=True)
    source = dr_load_json(state / 'repo_data_requirements.json', {})
    payload: dict[str, Any] = {}
    if isinstance(source, dict) and source:
        source_repo = str(source.get('repo_path') or repo_path).strip()
        if not source_repo or dr__paths_equal(source_repo, repo_path):
            payload = dr__normalize_candidate_contract_payload(project, repo_path, candidate_name, candidate_title, source, adapter, 'project_adapter')
    fresh_payload = dr__candidate_contract_from_fresh_acquisition(project, repo_path, candidate_name, candidate_title)
    if fresh_payload and (not payload or payload.get('status') != 'ready'):
        payload = fresh_payload
    if not payload:
        payload = dr_discover_generic_data_contract(project, repo_path, candidate_name, candidate_title)
    rc = dr__write_candidate_requirement_payload(project, payload, repo_path, candidate_name)
    return 0 if rc == 0 else int(proc.returncode or rc or 2)

def dr_write_candidate_requirement(project: str, repo_path: str, candidate_name: str='', candidate_title: str='') -> int:
    payload = dr__candidate_contract_from_fresh_acquisition(project, repo_path, candidate_name, candidate_title)
    if not payload:
        payload = dr_discover_generic_data_contract(project, repo_path, candidate_name, candidate_title)
    return dr__write_candidate_requirement_payload(project, payload, repo_path, candidate_name)

def dr_write_generic_requirement(project: str, repo_path: str, adapter: Path) -> int:
    state = ROOT / 'projects' / project / 'state'
    reports = ROOT / 'projects' / project / 'reports'
    datasets = dr__dataset_names_from_repo(repo_path) or ['project_specific_dataset_contract']
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    reason = 'No project-local data adapter is available, so TASTE cannot prove a repo-specific real dataset contract yet.'
    payload = {'generated_at': now, 'project': project, 'repo_path': repo_path, 'status': 'blocked_missing_project_data_adapter', 'decision': 'project_data_contract_required', 'adapter_missing': True, 'adapter_path': str(adapter), 'datasets': datasets, 'ready_datasets': [], 'blocked_datasets': datasets, 'download_sources': [], 'contract': {'status': 'project_adapter_required', 'required_files_per_dataset': [], 'loader_probe_required': True}, 'local_statuses': [{'dataset': name, 'status': 'blocked_project_adapter_required', 'reason': reason, 'candidate_roots': [], 'missing_required_files': []} for name in datasets], 'blocker_reasons': [reason], 'guardrails': ['Do not mark a dataset ready without a project-specific loader probe.', 'A missing adapter is a real blocker, not an installation failure.']}
    dr_save_json(state / 'repo_data_requirements.json', payload)
    reports.mkdir(parents=True, exist_ok=True)
    lines = ['# Repo Data Requirements\n\n']
    lines.append(f'- generated_at: {now}\n')
    lines.append('- status: blocked_missing_project_data_adapter\n')
    lines.append(f"- repo_path: {repo_path or 'not selected'}\n")
    lines.append(f'- adapter_path: {adapter}\n')
    lines.append('- decision: project_data_contract_required\n')
    lines.append('\n## Blocked Datasets\n')
    for name in datasets:
        lines.append(f'- {name}: {reason}\n')
    (reports / 'repo_data_requirements.md').write_text(''.join(lines), encoding='utf-8')
    print(json.dumps({'status': payload['status'], 'project': project, 'repo_path': repo_path, 'blocked_datasets': datasets}, ensure_ascii=False))
    return 0

def dr_main() -> int:
    args = dr_parse_args(sys.argv[1:])
    project = args.project or dr_project_from_args(sys.argv[1:])
    if not project:
        print(json.dumps({'status': 'blocked', 'decision': 'project_required_for_project_adapter'}, ensure_ascii=False), file=sys.stderr)
        return 2
    adapter = ROOT / 'projects' / project / 'scripts' / 'adapters' / Path(__file__).name
    if args.candidate_scope:
        repo_path = dr_candidate_repo_from_state(project, args.repo_path)
        candidate_name, candidate_title = dr_candidate_identity_from_state(project, args.candidate_name, args.candidate_title)
        if adapter.exists() and _adapter_supports_candidate(adapter, repo_path, candidate_name, candidate_title):
            return dr_write_candidate_requirement_from_adapter(project, repo_path, candidate_name, candidate_title, adapter, argv)
        return dr_write_candidate_requirement(project, repo_path, candidate_name, candidate_title)
    if adapter.exists():
        proc = subprocess.run([sys.executable, str(adapter), *sys.argv[1:]], cwd=ROOT)
        return int(proc.returncode)
    repo_path = dr__repo_from_state(project, args.repo_path)
    return dr_write_generic_requirement(project, repo_path, adapter)



import types as _taste_types

candidate_data = _taste_types.SimpleNamespace(
    ROOT=ROOT,
    candidate_slug=dr_candidate_slug,
    candidate_repo_from_state=dr_candidate_repo_from_state,
    candidate_identity_from_state=dr_candidate_identity_from_state,
    discover_generic_data_contract=dr_discover_generic_data_contract,
)


import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

def probe__repo_root_from_script() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / 'framework').is_dir() and (candidate / 'modules').is_dir() and (candidate / 'web').is_dir():
            return candidate
    return current.parents[1]
ROOT = probe__repo_root_from_script()

def probe__project_from_args(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--project', default='')
    ns, _ = parser.parse_known_args(argv)
    if ns.project:
        return ns.project
    return os.environ.get('PROJECT_ID', '')

def probe_parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Probe repo datasets, with a safe generic blocker when no adapter exists.')
    parser.add_argument('--project', default='')
    parser.add_argument('--repo-path', default='')
    parser.add_argument('--env-name', default='')
    parser.add_argument('--timeout-sec', default='')
    parser.add_argument('--candidate-scope', action='store_true', help='Write candidate-scoped loader evidence without touching active-route real_dataset_probe.json.')
    parser.add_argument('--candidate-name', default='')
    parser.add_argument('--candidate-title', default='')
    return parser.parse_known_args(argv)[0]

def probe_load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default

def probe_save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
BASE_SWITCH_SELECTION_GATE = 'blocked_candidate_base_switch_gate_required'
PENDING_LOADER_SELECTION_GATE = 'blocked_pending_data_loader_for_claude_best_candidate'

def probe__same_text(left: Any, right: Any) -> bool:
    return bool(str(left or '').strip() and str(left or '').strip() == str(right or '').strip())

def probe__candidate_matches_pending(payload: dict[str, Any], pending: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or not isinstance(pending, dict):
        return False
    for key in ['repo_path', 'local_path', 'path']:
        if probe__same_text(payload.get('repo_path'), pending.get(key)):
            return True
    if probe__same_text(payload.get('candidate_repo'), pending.get('name') or pending.get('repo') or pending.get('full_name')):
        return True
    return False

def probe__claim_ready_datasets(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for row in payload.get('probes', []) if isinstance(payload.get('probes'), list) else []:
        if not isinstance(row, dict) or not row.get('claim_ready'):
            continue
        dataset = str(row.get('dataset') or '').strip()
        if dataset and dataset not in out:
            out.append(dataset)
    if not out and payload.get('loader_probe_success') is True:
        for item in payload.get('ready_datasets', []) if isinstance(payload.get('ready_datasets'), list) else []:
            dataset = str(item or '').strip()
            if dataset and dataset not in out:
                out.append(dataset)
    return out

def probe__failed_check_ids(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    failed = payload.get('failed_checks') if isinstance(payload.get('failed_checks'), list) else []
    if not failed and isinstance(payload.get('checks'), list):
        failed = [row for row in payload.get('checks', []) if isinstance(row, dict) and row.get('status') != 'pass']
    out: list[str] = []
    for row in failed:
        if isinstance(row, dict):
            check_id = str(row.get('id') or '').strip()
        else:
            check_id = str(row or '').strip()
        if check_id and check_id not in out:
            out.append(check_id)
    return out

def probe__default_base_switch_failed_checks() -> list[str]:
    return ['candidate_reference_protocol_passed', 'candidate_reference_smoke_passed', 'candidate_full_reference_reproduction_passed', 'candidate_artifact_local_audit_ready']

def probe__base_switch_pending_topic_decision(existing: Any, pending: dict[str, Any], claim_ready: list[str], failed: list[str]) -> dict[str, Any]:
    decision = dict(existing) if isinstance(existing, dict) else {}
    repo_name = str(pending.get('name') or pending.get('repo') or pending.get('full_name') or 'candidate repo').strip()
    repo_path = str(pending.get('repo_path') or pending.get('local_path') or pending.get('path') or '').strip()
    dataset_text = ', '.join(claim_ready) if claim_ready else 'candidate data'
    failed_text = ', '.join(failed or probe__default_base_switch_failed_checks())
    rationale_en = f'{repo_name} has passed candidate-scoped repo-defined loader/import probing and has claim-ready data ({dataset_text}), but it remains non-authoritative because deterministic base-switch gates are still blocked: {failed_text}. The next required work is reference protocol, bounded smoke, full reference reproduction, and artifact-local audit evidence; this is not a pending loader problem.'
    rationale_zh = f'{repo_name} 已通过候选仓库定义的 loader/import 探测，并已有可声明数据（{dataset_text}），但确定性 base-switch 门控仍阻塞：{failed_text}。下一步必须补参考协议、有界 smoke、完整参考复现和 artifact-local audit 证据；这不再是等待 loader 的问题。'
    data_reason_en = f'Candidate data/loader evidence is ready for {dataset_text}; do not request another loader probe. Keep the candidate proposal-only until deterministic reference/audit gates authorize the switch.'
    data_reason_zh = f'{dataset_text} 的候选数据/loader 证据已就绪；不要再要求 loader probe。确定性参考复现/audit 门控授权前，该候选只能作为 proposal。'
    required_en = [f'Pass deterministic base-switch checks before selecting this candidate: {failed_text}.', 'Run/record the candidate reference protocol, bounded smoke test, full reference reproduction, and artifact-local audit.']
    required_zh = [f'选中该候选前必须通过确定性 base-switch checks：{failed_text}。', '运行并记录候选参考协议、有界 smoke、完整参考复现和 artifact-local audit。']
    evidence_en = [f'candidate_loader_probe_status=passed for {repo_name}', f'claim_ready_datasets={dataset_text}', f'blocked_base_switch_checks={failed_text}']
    evidence_zh = [f'{repo_name} 的 candidate_loader_probe_status=passed', f'可声明数据集={dataset_text}', f'仍阻塞的 base-switch checks={failed_text}']
    risks_en = ['Reference protocol, bounded smoke, full reproduction, or artifact-local audit may still fail; do not claim this route until those gates pass.']
    risks_zh = ['参考协议、有界 smoke、完整复现或 artifact-local audit 仍可能失败；这些门控通过前不能使用该路线生成论文结论。']
    stewardship_en = f'{repo_name} has cleared candidate loader/data evidence for {dataset_text}. Next work must target deterministic base-switch checks: {failed_text}. Do not run ordinary main-route experiments or write claims until those reference/audit gates pass.'
    stewardship_zh = f'{repo_name} 已通过 {dataset_text} 的候选 loader/data 证据。下一步必须处理确定性 base-switch checks：{failed_text}。参考复现/audit 门控通过前，不得启动普通主线实验或写 claim。'
    for stale_key in ['raw_output_tail']:
        decision.pop(stale_key, None)
    decision.update({'decision': str(decision.get('decision') or 'accept-with-modifications'), 'rationale': rationale_en, 'rationale_en': rationale_en, 'rationale_zh': rationale_zh, 'repo_action': str(decision.get('repo_action') or 'switch_to_best_repo'), 'repo_action_reason': rationale_en, 'repo_action_reason_en': rationale_en, 'repo_action_reason_zh': rationale_zh, 'data_action': 'use_claim_ready_dataset', 'data_action_reason': data_reason_en, 'data_action_reason_en': data_reason_en, 'data_action_reason_zh': data_reason_zh, 'best_repo': str(decision.get('best_repo') or repo_name), 'repo_path': str(decision.get('repo_path') or repo_path), 'dataset': str(decision.get('dataset') or (claim_ready[0] if claim_ready else '')), 'required_modifications': required_en, 'required_modifications_en': required_en, 'required_modifications_zh': required_zh, 'risks': risks_en, 'risks_en': risks_en, 'risks_zh': risks_zh, 'evidence': evidence_en, 'evidence_en': evidence_en, 'evidence_zh': evidence_zh, 'repo_action_reason_i18n': {'zh': rationale_zh, 'en': rationale_en}, 'data_action_reason_i18n': {'zh': data_reason_zh, 'en': data_reason_en}, 'stewardship_memory': stewardship_en, 'stewardship_memory_en': stewardship_en, 'stewardship_memory_zh': stewardship_zh, 'stewardship_memory_i18n': {'zh': stewardship_zh, 'en': stewardship_en}})
    return decision

def probe__write_topic_decision_report(project: str, decision: dict[str, Any]) -> None:
    reports = ROOT / 'projects' / project / 'reports'
    reports.mkdir(parents=True, exist_ok=True)
    probe_save_json(reports / 'repo_topic_fit_decision.json', decision)

def probe__write_repo_env_strategy_report(project: str, strategy: dict[str, Any]) -> None:
    reports = ROOT / 'projects' / project / 'reports'
    reports.mkdir(parents=True, exist_ok=True)
    lines = ['# Claude Repo/Data/Env Stewardship Strategy\n\n', f"- generated_at: {strategy.get('generated_at', '')}\n", f"- repo_action: {strategy.get('repo_action', '')}\n", f"- repo_action_reason: {strategy.get('repo_action_reason', '')}\n", f"- repo_action_reason_zh: {strategy.get('repo_action_reason_zh', '')}\n", f"- env_action: {strategy.get('env_action', '')}\n", f"- env_action_reason: {strategy.get('env_action_reason', '')}\n", f"- env_action_reason_zh: {strategy.get('env_action_reason_zh', '')}\n", f"- recommended_env_name: {strategy.get('recommended_env_name', '')}\n", f"- data_action: {strategy.get('data_action', '')}\n", f"- data_action_reason: {strategy.get('data_action_reason', '')}\n", f"- data_action_reason_zh: {strategy.get('data_action_reason_zh', '')}\n", f"- stewardship_memory: {strategy.get('stewardship_memory', '')}\n", f"- stewardship_memory_zh: {strategy.get('stewardship_memory_zh', '')}\n"]
    (reports / 'repo_env_strategy.md').write_text(''.join(lines), encoding='utf-8')

def probe__sync_pending_environment_candidate(project: str, payload: dict[str, Any], contract: dict[str, Any], state_path: Path, contract_path: Path) -> None:
    state = ROOT / 'projects' / project / 'state'
    selection_path = state / 'evidence_ready_repo_selection.json'
    selection = probe_load_json(selection_path, {})
    if not isinstance(selection, dict):
        return
    pending = selection.get('pending_environment_candidate') if isinstance(selection.get('pending_environment_candidate'), dict) else {}
    if not probe__candidate_matches_pending(payload, pending):
        return
    summary = pending.get('probe_summary') if isinstance(pending.get('probe_summary'), dict) else {}
    summary = dict(summary)
    claim_ready = probe__claim_ready_datasets(payload)
    summary.update({'probe_return_code': payload.get('probe_return_code'), 'probe_count': len(payload.get('probes', []) if isinstance(payload.get('probes'), list) else []), 'claim_ready_datasets': claim_ready, 'candidate_data_contract_status': contract.get('status', ''), 'candidate_data_ready_datasets': contract.get('ready_datasets', []) if isinstance(contract.get('ready_datasets'), list) else [], 'candidate_data_contract_path': str(contract_path), 'generic_data_parse_probe_success': bool(payload.get('generic_data_parse_probe_success')), 'candidate_loader_probe_status': payload.get('status', ''), 'candidate_loader_probe_decision': payload.get('decision', ''), 'candidate_loader_probe_path': str(state_path)})
    pending = dict(pending)
    pending['probe_summary'] = summary
    failed: list[str] = []
    if payload.get('loader_probe_success') is True:
        gate = probe_load_json(state / 'base_switch_gate.json', {})
        failed = probe__failed_check_ids(gate) or probe__default_base_switch_failed_checks()
        topic_decision = probe__base_switch_pending_topic_decision(selection.get('claude_topic_decision'), pending, claim_ready, failed)
        pending['pending_loader_bootstrap'] = False
        pending['decision'] = 'candidate_loader_import_probe_passed_reference_checks_required'
        pending['pending_reason'] = 'Candidate loader/import probe passed, but deterministic base-switch reference protocol, smoke, full reproduction, and artifact-local audit remain mandatory.'
        pending['anchor_selection_policy'] = 'Loader/data evidence is ready, but this candidate remains proposal-only until deterministic base-switch reference/audit gates authorize it.'
        pending['base_switch_failed_checks'] = failed
        pending['claude_topic_decision'] = topic_decision
        selection['claude_topic_decision'] = topic_decision
        probe__write_topic_decision_report(project, topic_decision)
        strategy = probe_load_json(state / 'repo_env_strategy.json', {})
        if not isinstance(strategy, dict):
            strategy = {}
        strategy.update({'repo_action': topic_decision.get('repo_action', 'switch_to_best_repo'), 'repo_action_reason': topic_decision.get('repo_action_reason_en') or topic_decision.get('repo_action_reason') or '', 'repo_action_reason_en': topic_decision.get('repo_action_reason_en') or topic_decision.get('repo_action_reason') or '', 'repo_action_reason_zh': topic_decision.get('repo_action_reason_zh') or '', 'data_action': topic_decision.get('data_action', 'use_claim_ready_dataset'), 'data_action_reason': topic_decision.get('data_action_reason_en') or topic_decision.get('data_action_reason') or '', 'data_action_reason_en': topic_decision.get('data_action_reason_en') or topic_decision.get('data_action_reason') or '', 'data_action_reason_zh': topic_decision.get('data_action_reason_zh') or '', 'stewardship_memory': topic_decision.get('stewardship_memory_en') or topic_decision.get('stewardship_memory') or '', 'stewardship_memory_en': topic_decision.get('stewardship_memory_en') or topic_decision.get('stewardship_memory') or '', 'stewardship_memory_zh': topic_decision.get('stewardship_memory_zh') or '', 'selected_repo': {}, 'pending_environment_candidate': {'name': pending.get('name') or pending.get('repo') or '', 'repo_path': pending.get('repo_path') or pending.get('local_path') or '', 'claim_ready_datasets': claim_ready, 'selection_gate': BASE_SWITCH_SELECTION_GATE}})
        probe_save_json(state / 'repo_env_strategy.json', strategy)
        probe__write_repo_env_strategy_report(project, strategy)
        selection['repo_env_strategy'] = strategy
        if str(selection.get('selection_gate') or '') == PENDING_LOADER_SELECTION_GATE:
            selection['selection_gate'] = BASE_SWITCH_SELECTION_GATE
            selection['blocker'] = pending['pending_reason']
            selection['base_switch_failed_checks'] = failed
            selection['selected'] = {}
            selection['selected_by_stage'] = ''
    selection['pending_environment_candidate'] = pending
    audited = selection.get('audited_candidates') if isinstance(selection.get('audited_candidates'), list) else []
    for row in audited:
        if not isinstance(row, dict) or not probe__candidate_matches_pending(payload, row):
            continue
        row['probe_summary'] = summary
        if payload.get('loader_probe_success') is True:
            row['decision'] = 'candidate_loader_import_probe_passed_reference_checks_required'
            row['base_switch_failed_checks'] = failed
            row['claude_topic_decision'] = selection.get('claude_topic_decision')
    probe_save_json(selection_path, selection)
    probe__write_selection_report(project, selection)

def probe__write_selection_report(project: str, selection: dict[str, Any]) -> None:
    reports = ROOT / 'projects' / project / 'reports'
    reports.mkdir(parents=True, exist_ok=True)
    selected = selection.get('selected') if isinstance(selection.get('selected'), dict) else {}
    lines = ['# Evidence-Ready Repo Selection\n\n']
    lines.append(f"- generated_at: {selection.get('generated_at', '')}\n")
    lines.append(f"- audited_count: {selection.get('audited_count', 0)}\n")
    lines.append(f"- evidence_ready_count: {selection.get('evidence_ready_count', 0)}\n")
    lines.append(f"- selection_gate: {selection.get('selection_gate', '')}\n")
    if selected:
        lines.append(f"- selected_repo: {selected.get('name')}\n")
        lines.append(f"- selected_path: {selected.get('repo_path')}\n")
        lines.append(f"- selected_dataset: {selected.get('claim_ready_dataset')}\n")
        lines.append(f"- selection_score: {selected.get('selection_score')}\n")
    else:
        lines.append('- selected_repo: none\n')
        pending = selection.get('pending_environment_candidate') if isinstance(selection.get('pending_environment_candidate'), dict) else {}
        if pending:
            lines.append(f"- pending_environment_candidate: {pending.get('name')}\n")
            lines.append(f"- pending_repo_path: {pending.get('repo_path')}\n")
            lines.append(f"- pending_reason: {pending.get('pending_reason', '')}\n")
            summary = pending.get('probe_summary') if isinstance(pending.get('probe_summary'), dict) else {}
            lines.append(f"- pending_claim_ready: {', '.join(summary.get('claim_ready_datasets', []) or []) or 'none'}\n")
    topic_decision = selection.get('claude_topic_decision') if isinstance(selection.get('claude_topic_decision'), dict) else {}
    if topic_decision:
        lines.append(f"- claude_decision: {topic_decision.get('decision', '')} confidence={topic_decision.get('confidence', '')}\n")
        lines.append(f"- claude_rationale: {topic_decision.get('rationale_en') or topic_decision.get('rationale', '')}\n")
        if topic_decision.get('rationale_zh'):
            lines.append(f"- claude_rationale_zh: {topic_decision.get('rationale_zh', '')}\n")
    lines.append('\n## Audited Candidates\n')
    for item in selection.get('audited_candidates', []) if isinstance(selection.get('audited_candidates'), list) else []:
        if not isinstance(item, dict):
            continue
        summary = item.get('probe_summary') if isinstance(item.get('probe_summary'), dict) else {}
        claim_ready_text = ', '.join(summary.get('claim_ready_datasets', []) or []) or 'none'
        generic_ready_text = ', '.join(summary.get('candidate_data_ready_datasets', []) or []) or 'none'
        loader_status = summary.get('candidate_loader_probe_status', '') or 'n/a'
        contract_status = summary.get('candidate_data_contract_status', '') or 'n/a'
        lines.append(f"- {item.get('name')} | decision={item.get('decision')} | claim_ready={claim_ready_text} | candidate_data={contract_status}:{generic_ready_text} | loader_probe={loader_status} | score={item.get('selection_score', '')}\n")
    (reports / 'evidence_ready_repo_selection.md').write_text(''.join(lines), encoding='utf-8')

def probe__project_config(project: str) -> dict[str, Any]:
    payload = probe_load_json(ROOT / 'projects' / project / 'project.json', {})
    return payload if isinstance(payload, dict) else {}

def probe__existing_file(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        path = Path(text).expanduser()
        return str(path.resolve()) if path.is_file() else ''
    except Exception:
        return ''

def probe__candidate_probe_python(project: str, env_name: str='') -> str:
    cfg = probe__project_config(project)
    runtime = cfg.get('runtime') if isinstance(cfg.get('runtime'), dict) else {}
    env_cfg = cfg.get('environment') if isinstance(cfg.get('environment'), dict) else {}
    for key in ['EXPERIMENT_PYTHON', 'PROJECT_PYTHON']:
        explicit = probe__existing_file(os.environ.get(key))
        if explicit:
            return explicit
    for value in [runtime.get('experiment_python'), env_cfg.get('experiment_python')]:
        explicit = probe__existing_file(value)
        if explicit:
            return explicit
    selected_env = str(env_name or cfg.get('conda_env') or '').strip()
    conda_base = str(runtime.get('conda_base') or env_cfg.get('conda_base_hint') or os.environ.get('CONDA_BASE') or '').strip()
    if selected_env and conda_base:
        explicit = probe__existing_file(Path(conda_base) / 'envs' / selected_env / 'bin' / 'python')
        if explicit:
            return explicit
    return str(Path(sys.executable).resolve())

def probe__pyproject_package_names(repo: Path) -> list[str]:
    pyproject = repo / 'pyproject.toml'
    if not pyproject.exists():
        return []
    try:
        payload = tomllib.loads(pyproject.read_text(encoding='utf-8'))
    except Exception:
        return []
    project = payload.get('project') if isinstance(payload.get('project'), dict) else {}
    name = str(project.get('name') or '').strip().replace('-', '_')
    return [name] if name else []

def probe__candidate_package_names(repo: Path) -> list[str]:
    names: list[str] = []
    for name in probe__pyproject_package_names(repo):
        if name and name not in names:
            names.append(name)
    for child in sorted(repo.iterdir()) if repo.exists() else []:
        if child.is_dir() and (child / '__init__.py').exists() and (not child.name.startswith(('.', '_'))):
            if child.name not in {'tests', 'test', 'docs', 'examples'} and child.name not in names:
                names.append(child.name)
    return names[:8]

def probe__candidate_script_paths(repo: Path) -> list[str]:
    if not repo.exists():
        return []
    priority_tokens = ('loader', 'dataset', 'data', 'preprocess', 'prepare')
    fallback_tokens = ('train', 'infer', 'eval')
    ignored_parts = {'.git', '__pycache__', '.pytest_cache', 'data', 'datasets', 'dataset', 'docs', 'tests', 'test', 'outputs', 'output', 'logs', 'wandb', 'checkpoints'}
    ranked: list[tuple[int, int, str, Path]] = []
    for path in repo.rglob('*.py'):
        if not path.is_file():
            continue
        rel = path.relative_to(repo)
        rel_parts = [part.lower() for part in rel.parts]
        if any((part in ignored_parts for part in rel_parts[:-1])):
            continue
        haystack = '/'.join(rel_parts)
        if any((token in haystack for token in priority_tokens)):
            rank = 0
        elif any((token in rel.name.lower() for token in fallback_tokens)):
            rank = 1
        else:
            continue
        ranked.append((rank, len(rel.parts), str(rel), path))
    ranked.sort()
    return [str(path.relative_to(repo)) for _, _, _, path in ranked[:16]]

def probe__split_file_priority(rel: str) -> tuple[int, str]:
    name = Path(rel).name.lower()
    if 'train' in name:
        return (0, rel)
    if 'valid' in name or 'validation' in name or 'dev' in name:
        return (1, rel)
    if 'test' in name:
        return (2, rel)
    return (3, rel)

def probe__contract_sample_files(contract: dict[str, Any]) -> list[str]:
    out: list[str] = []
    split_files: list[str] = []
    for status in contract.get('local_statuses', []) if isinstance(contract.get('local_statuses'), list) else []:
        if not isinstance(status, dict):
            continue
        for rel in status.get('split_files', []) if isinstance(status.get('split_files'), list) else []:
            rel_text = str(rel or '').strip()
            if rel_text and rel_text not in split_files:
                split_files.append(rel_text)
    for rel in sorted(split_files, key=probe__split_file_priority):
        if rel not in out:
            out.append(rel)
    for row in contract.get('sampled_files', []) if isinstance(contract.get('sampled_files'), list) else []:
        if not isinstance(row, dict) or row.get('parse_success') is not True:
            continue
        rel = str(row.get('relative_path') or '').strip()
        if rel and rel not in out:
            out.append(rel)
    return out[:16]

def probe__bounded_candidate_repo_probe(project: str, repo_path: str, env_name: str, contract: dict[str, Any], timeout_sec: int=120) -> dict[str, Any]:
    repo = Path(repo_path).expanduser().resolve()
    python_executable = probe__candidate_probe_python(project, env_name)
    packages = probe__candidate_package_names(repo)
    scripts = probe__candidate_script_paths(repo)
    sample_files = probe__contract_sample_files(contract)
    probe_code = "\nimport importlib\nimport importlib.util\nimport inspect\nimport json\nimport os\nimport sys\nimport traceback\nfrom pathlib import Path\n\nrepo = Path(os.environ['TASTE_CANDIDATE_REPO']).resolve()\npackages = json.loads(os.environ.get('TASTE_CANDIDATE_PACKAGES') or '[]')\nscripts = json.loads(os.environ.get('TASTE_CANDIDATE_SCRIPTS') or '[]')\nsample_files = json.loads(os.environ.get('TASTE_CANDIDATE_SAMPLE_FILES') or '[]')\nsys.path.insert(0, str(repo))\nout = {'package_imports': [], 'script_imports': [], 'loader_calls': []}\n\nout['package_imports_skipped'] = 'script_level_loader_probe_preferred_to_avoid_heavy_package_import_side_effects'\n\ndef split_name(path: Path) -> str:\n    name = path.name\n    for suffix in ['.jsonl.gz', '.json.gz', '.jsonl', '.json', '.csv', '.parquet']:\n        if name.endswith(suffix):\n            return name[:-len(suffix)]\n    return path.stem\n\ndef candidate_loader_functions(module):\n    funcs = []\n    for name, obj in inspect.getmembers(module, inspect.isfunction):\n        lowered = name.lower()\n        if not any(token in lowered for token in ['load', 'read']):\n            continue\n        if not any(token in lowered for token in ['data', 'dataset', 'split', 'jsonl', 'parquet', 'csv']):\n            continue\n        try:\n            source_file = inspect.getsourcefile(obj) or ''\n            source_path = Path(source_file).resolve()\n            if repo not in source_path.parents and source_path != repo:\n                continue\n        except Exception:\n            continue\n        try:\n            sig = inspect.signature(obj)\n        except Exception:\n            continue\n        params = list(sig.parameters.values())\n        required = [p for p in params if p.default is inspect._empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]\n        if 1 <= len(required) <= 2:\n            funcs.append((name, obj, required))\n    return funcs\n\nfor index, rel in enumerate(scripts):\n    path = repo / rel\n    row = {'path': rel, 'success': False}\n    module = None\n    try:\n        spec = importlib.util.spec_from_file_location(f'_taste_candidate_probe_{index}_{path.stem}', path)\n        if spec is None or spec.loader is None:\n            raise RuntimeError('spec loader unavailable')\n        module = importlib.util.module_from_spec(spec)\n        spec.loader.exec_module(module)\n        row['success'] = True\n    except BaseException as exc:\n        row.update({'error': f'{exc.__class__.__name__}: {exc}', 'traceback_tail': traceback.format_exc()[-1200:]})\n    out['script_imports'].append(row)\n    if module is None:\n        continue\n    for func_name, func, required in candidate_loader_functions(module):\n        for sample in sample_files[:8]:\n            sample_path = repo / sample\n            call = {'script': rel, 'function': func_name, 'sample_file': sample, 'success': False}\n            if not sample_path.exists():\n                call['error'] = 'sample file missing'\n                out['loader_calls'].append(call)\n                continue\n            try:\n                if len(required) == 1:\n                    result = func(str(sample_path))\n                else:\n                    result = func(str(sample_path), split_name(sample_path))\n                try:\n                    result_len = len(result)\n                except Exception:\n                    result_len = None\n                call.update({'success': True, 'result_type': type(result).__name__, 'result_len': result_len, 'shape': list(getattr(result, 'shape', []) or [])})\n            except BaseException as exc:\n                call.update({'error': f'{exc.__class__.__name__}: {exc}', 'traceback_tail': traceback.format_exc()[-1200:]})\n            out['loader_calls'].append(call)\n            if call['success']:\n                break\n        if any(item.get('success') for item in out['loader_calls']):\n            break\n    if any(item.get('success') for item in out['loader_calls']):\n        break\n\npackage_success = any(row.get('success') for row in out['package_imports'])\nscript_success = any(row.get('success') for row in out['script_imports'])\nloader_success = any(row.get('success') for row in out['loader_calls'])\nout.update({'package_import_success': package_success, 'script_import_success': script_success, 'loader_function_success': loader_success, 'loader_probe_success': bool(loader_success and (package_success or script_success))})\nprint(json.dumps(out, ensure_ascii=False))\n"
    env = os.environ.copy()
    env['TASTE_CANDIDATE_REPO'] = str(repo)
    env['TASTE_CANDIDATE_PACKAGES'] = json.dumps(packages, ensure_ascii=False)
    env['TASTE_CANDIDATE_SCRIPTS'] = json.dumps(scripts, ensure_ascii=False)
    env['TASTE_CANDIDATE_SAMPLE_FILES'] = json.dumps(sample_files, ensure_ascii=False)
    env['PYTHONPATH'] = str(repo) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    try:
        proc = subprocess.run([python_executable, '-c', probe_code], cwd=repo, text=True, capture_output=True, timeout=max(10, int(timeout_sec or 120)), env=env)
    except subprocess.TimeoutExpired as exc:
        return {'python_executable': python_executable, 'packages': packages, 'scripts': scripts, 'sample_files': sample_files, 'loader_probe_success': False, 'return_code': 124, 'stderr_tail': str(exc)[-1200:]}
    try:
        payload = json.loads((proc.stdout or '').strip().splitlines()[-1]) if (proc.stdout or '').strip() else {}
    except Exception:
        payload = {}
    payload.update({'python_executable': python_executable, 'packages': packages, 'scripts': scripts, 'sample_files': sample_files, 'return_code': proc.returncode, 'stderr_tail': (proc.stderr or '')[-2000:], 'stdout_tail': (proc.stdout or '')[-2000:]})
    payload['loader_probe_success'] = bool(proc.returncode == 0 and payload.get('loader_probe_success'))
    return payload

def probe__repo_from_state(project: str, explicit: str='') -> str:
    if explicit:
        return explicit
    state = ROOT / 'projects' / project / 'state'
    for name in ['evidence_ready_repo_selection.json', 'active_repo.json', 'fresh_base_implementation_plan.json']:
        payload = probe_load_json(state / name, {})
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


def probe_write_candidate_probe_from_adapter(project: str, repo_path: str, env_name: str, candidate_name: str, candidate_title: str, timeout_sec: int, adapter: Path, argv: list[str]) -> int:
    state = ROOT / 'projects' / project / 'state'
    reports = ROOT / 'projects' / project / 'reports'
    slug = candidate_data.candidate_slug(repo_path, candidate_name)
    contract_path = state / f'candidate_data_contract_{slug}.json'
    contract = probe_load_json(contract_path, {})
    proc = subprocess.run([sys.executable, str(adapter), *argv], cwd=ROOT, text=True, timeout=max(30, int(timeout_sec or 120) + 60))
    source = probe_load_json(state / 'real_dataset_probe.json', {})
    if not isinstance(source, dict) or not source:
        source = {}
    if not isinstance(contract, dict) or str(contract.get('repo_path') or '') != str(Path(repo_path).expanduser().resolve() if repo_path and Path(repo_path).expanduser().exists() else repo_path):
        fresh_contract = candidate_data.dr__candidate_contract_from_fresh_acquisition(project, repo_path, candidate_name, candidate_title)
        contract = fresh_contract or candidate_data.discover_generic_data_contract(project, repo_path, candidate_name, candidate_title)
        probe_save_json(contract_path, contract)
    ready_contract = [str(item) for item in contract.get('ready_datasets', [])] if isinstance(contract.get('ready_datasets'), list) else []
    raw_probes = source.get('probes') if isinstance(source.get('probes'), list) else []
    loader_ok = bool(str(source.get('status') or '') in {'passed', 'ready'} or source.get('loader_probe_success') or any(isinstance(row, dict) and row.get('claim_ready') and row.get('loader_probe_success') for row in raw_probes))
    generic_parse_ok = bool(ready_contract or source.get('generic_data_parse_probe_success') or any(isinstance(row, dict) and row.get('generic_data_parse_probe_success') for row in raw_probes))
    ready = [str(item) for item in source.get('ready_datasets', [])] if isinstance(source.get('ready_datasets'), list) else []
    if not ready and loader_ok:
        ready = [str(row.get('dataset')) for row in raw_probes if isinstance(row, dict) and row.get('claim_ready') and str(row.get('dataset') or '').strip()]
    blocked = [str(item) for item in source.get('blocked_datasets', [])] if isinstance(source.get('blocked_datasets'), list) else []
    if not blocked:
        blocked = [name for name in ready_contract if name not in ready] or ([] if loader_ok else ready_contract or ['candidate_structured_data_contract'])
    reason = 'Project adapter repo loader/import probe passed on real candidate data; reference protocol, smoke, full reproduction, and artifact-local audit remain mandatory before switch authorization.' if loader_ok else str((source.get('blocker_reasons') or [''])[0] if isinstance(source.get('blocker_reasons'), list) and source.get('blocker_reasons') else source.get('reason') or 'Candidate data is present, but repo loader/import probe has not passed yet.')
    probes = raw_probes if raw_probes else [{'dataset': name, 'claim_ready': loader_ok, 'loader_probe_success': loader_ok, 'generic_data_parse_probe_success': generic_parse_ok, 'loader_probe': {'success': loader_ok, 'return_code': 0 if loader_ok else 2, 'reason': reason}, 'required_files_ok': generic_parse_ok, 'missing_required_files': [], 'reason': reason} for name in ready or blocked or ready_contract or ['candidate_structured_data_contract']]
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    payload = {
        'generated_at': now,
        'project': project,
        'candidate_scope': True,
        'candidate_repo': candidate_name,
        'candidate_title': candidate_title,
        'repo_path': contract.get('repo_path') or repo_path,
        'env_name': env_name,
        'status': 'passed' if loader_ok else 'blocked_candidate_repo_loader_import_probe_required' if generic_parse_ok else 'blocked_candidate_data_contract_required',
        'decision': 'candidate_loader_import_probe_passed' if loader_ok else 'candidate_repo_loader_import_probe_required' if generic_parse_ok else 'candidate_data_contract_required',
        'loader_probe_success': loader_ok,
        'generic_data_parse_probe_success': generic_parse_ok,
        'adapter_missing': False,
        'adapter_path': str(adapter),
        'data_contract_path': str(contract_path),
        'candidate_data_contract': contract,
        'repo_import_probe': source.get('import_probe_summary', {}) if isinstance(source.get('import_probe_summary'), dict) else {},
        'probes': probes,
        'ready_datasets': ready if loader_ok else [],
        'blocked_datasets': [] if loader_ok else blocked,
        'blocker_reasons': [] if loader_ok else [reason],
        'probe_return_code': 0 if loader_ok else int(source.get('probe_return_code') or proc.returncode or 2),
        'source_kind': 'project_adapter',
        'source_path': str(state / 'real_dataset_probe.json'),
        'guardrails': [
            'Candidate-scoped loader evidence must not overwrite active-route real_dataset_probe.json.',
            'Project adapters may define repo-specific loader probes, but promotion still requires reference/smoke/reproduction/audit gates.',
            'Reference protocol, bounded smoke, full reproduction, and artifact-local audit gates remain mandatory.',
        ],
    }
    state_path = state / f'candidate_loader_probe_{slug}.json'
    probe_save_json(state_path, payload)
    probe__sync_pending_environment_candidate(project, payload, contract, state_path, contract_path)
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f'candidate_loader_probe_{slug}.md'
    lines = ['# Candidate Loader Probe\n\n']
    lines.append(f'- generated_at: {now}\n')
    lines.append(f"- status: {payload['status']}\n")
    lines.append(f"- decision: {payload['decision']}\n")
    lines.append('- source_kind: project_adapter\n')
    lines.append(f'- candidate_repo: {candidate_name}\n')
    lines.append(f"- repo_path: {payload.get('repo_path') or 'not selected'}\n")
    lines.append(f'- generic_data_parse_probe_success: {str(generic_parse_ok).lower()}\n')
    lines.append(f'- loader_probe_success: {str(loader_ok).lower()}\n')
    lines.append(f'- data_contract_path: {contract_path}\n')
    for row in probes:
        if isinstance(row, dict):
            lines.append(f"- {row.get('dataset', 'dataset')}: claim_ready={str(row.get('claim_ready')).lower()}; loader_probe_success={str(row.get('loader_probe_success')).lower()}; {row.get('reason') or reason}\n")
    report_path.write_text(''.join(lines), encoding='utf-8')
    print(json.dumps({'status': payload['status'], 'project': project, 'candidate_scope': True, 'repo_path': payload.get('repo_path'), 'loader_probe_success': loader_ok, 'ready_datasets': payload.get('ready_datasets', []), 'state_path': str(state_path)}, ensure_ascii=False))
    return 0 if loader_ok else int(payload['probe_return_code'] or 2)

def probe_write_candidate_probe(project: str, repo_path: str, env_name: str, candidate_name: str='', candidate_title: str='', timeout_sec: int=120) -> int:
    state = ROOT / 'projects' / project / 'state'
    reports = ROOT / 'projects' / project / 'reports'
    slug = candidate_data.candidate_slug(repo_path, candidate_name)
    contract_path = state / f'candidate_data_contract_{slug}.json'
    contract = probe_load_json(contract_path, {})
    if not isinstance(contract, dict) or str(contract.get('repo_path') or '') != str(Path(repo_path).expanduser().resolve() if repo_path and Path(repo_path).expanduser().exists() else repo_path):
        contract = candidate_data.discover_generic_data_contract(project, repo_path, candidate_name, candidate_title)
        probe_save_json(contract_path, contract)
    ready_datasets = [str(item) for item in contract.get('ready_datasets', [])] if isinstance(contract.get('ready_datasets'), list) else []
    blocked_datasets = [str(item) for item in contract.get('blocked_datasets', [])] if isinstance(contract.get('blocked_datasets'), list) else []
    generic_parse_ok = bool(contract.get('status') == 'ready' and ready_datasets)
    repo_import_probe = probe__bounded_candidate_repo_probe(project, str(contract.get('repo_path') or repo_path), env_name, contract, timeout_sec) if generic_parse_ok else {}
    loader_ok = bool(repo_import_probe.get('loader_probe_success'))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    reason = 'Candidate repo import and bounded loader function probe passed on local candidate data; reference protocol, smoke, full reproduction, and artifact-local audit remain mandatory before switch authorization.' if loader_ok else 'Candidate structured data parses locally, but no repo loader/import probe has passed yet; install/repair repo dependencies or provide a repo loader entrypoint before authorizing a base switch.' if generic_parse_ok else 'Candidate data contract is not ready; no repo loader/import probe can be credited yet.'
    probes = [{'dataset': name, 'claim_ready': loader_ok, 'loader_probe_success': loader_ok, 'generic_data_parse_probe_success': generic_parse_ok, 'loader_probe': {'success': loader_ok, 'return_code': 0 if loader_ok else 2, 'reason': reason}, 'required_files_ok': generic_parse_ok, 'missing_required_files': [], 'reason': reason} for name in ready_datasets or blocked_datasets or ['candidate_structured_data_contract']]
    payload = {'generated_at': now, 'project': project, 'candidate_scope': True, 'candidate_repo': candidate_name, 'candidate_title': candidate_title, 'repo_path': contract.get('repo_path') or repo_path, 'env_name': env_name, 'status': 'passed' if loader_ok else 'blocked_candidate_repo_loader_import_probe_required' if generic_parse_ok else 'blocked_candidate_data_contract_required', 'decision': 'candidate_loader_import_probe_passed' if loader_ok else 'candidate_repo_loader_import_probe_required' if generic_parse_ok else 'candidate_data_contract_required', 'loader_probe_success': loader_ok, 'generic_data_parse_probe_success': generic_parse_ok, 'adapter_missing': False, 'adapter_path': '', 'data_contract_path': str(contract_path), 'repo_import_probe': repo_import_probe, 'probes': probes, 'ready_datasets': [row['dataset'] for row in probes] if loader_ok else [], 'blocked_datasets': [] if loader_ok else [row['dataset'] for row in probes], 'blocker_reasons': [] if loader_ok else [reason], 'probe_return_code': 0 if loader_ok else 2, 'guardrails': ['Candidate-scoped loader evidence must not overwrite active-route real_dataset_probe.json.', 'Generic structured-data parsing cannot be credited as repo loader/import compatibility.', 'Reference protocol, bounded smoke, full reference reproduction, and artifact-local audit gates remain mandatory.']}
    state_path = state / f'candidate_loader_probe_{slug}.json'
    probe_save_json(state_path, payload)
    probe__sync_pending_environment_candidate(project, payload, contract, state_path, contract_path)
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f'candidate_loader_probe_{slug}.md'
    lines = ['# Candidate Loader Probe\n\n']
    lines.append(f'- generated_at: {now}\n')
    lines.append(f"- status: {payload['status']}\n")
    lines.append(f"- decision: {payload['decision']}\n")
    lines.append(f'- candidate_repo: {candidate_name}\n')
    lines.append(f"- repo_path: {payload.get('repo_path') or 'not selected'}\n")
    lines.append(f'- generic_data_parse_probe_success: {str(generic_parse_ok).lower()}\n')
    lines.append(f'- loader_probe_success: {str(loader_ok).lower()}\n')
    lines.append(f'- data_contract_path: {contract_path}\n')
    lines.append(f"- probe_python: {(repo_import_probe.get('python_executable', '') if isinstance(repo_import_probe, dict) else '')}\n")
    lines.append('\n## Probe Rows\n')
    for row in probes:
        lines.append(f"- {row['dataset']}: loader_probe_success={str(row.get('loader_probe_success')).lower()}; generic_data_parse_probe_success={str(row.get('generic_data_parse_probe_success')).lower()}; {row.get('reason', '')}\n")
    if isinstance(repo_import_probe, dict) and repo_import_probe:
        lines.append('\n## Repo Import / Loader Function Probe\n')
        for row in repo_import_probe.get('package_imports', []) if isinstance(repo_import_probe.get('package_imports'), list) else []:
            lines.append(f"- package {row.get('name')}: success={str(row.get('success')).lower()}\n")
        for row in repo_import_probe.get('script_imports', []) if isinstance(repo_import_probe.get('script_imports'), list) else []:
            lines.append(f"- script {row.get('path')}: success={str(row.get('success')).lower()}\n")
        for row in repo_import_probe.get('loader_calls', []) if isinstance(repo_import_probe.get('loader_calls'), list) else []:
            lines.append(f"- loader {row.get('script')}::{row.get('function')} on {row.get('sample_file')}: success={str(row.get('success')).lower()} result_len={row.get('result_len', '')}\n")
    report_path.write_text(''.join(lines), encoding='utf-8')
    print(json.dumps({'status': payload['status'], 'project': project, 'candidate_scope': True, 'repo_path': payload.get('repo_path') or repo_path, 'generic_data_parse_probe_success': generic_parse_ok, 'loader_probe_success': loader_ok, 'state_path': str(state_path)}, ensure_ascii=False))
    return 0 if loader_ok else 2

def probe_write_generic_probe(project: str, repo_path: str, env_name: str, adapter: Path) -> int:
    state = ROOT / 'projects' / project / 'state'
    reports = ROOT / 'projects' / project / 'reports'
    req = probe_load_json(state / 'repo_data_requirements.json', {})
    datasets = req.get('blocked_datasets') if isinstance(req, dict) and isinstance(req.get('blocked_datasets'), list) else []
    if not datasets:
        datasets = ['project_specific_dataset_contract']
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    reason = 'No project-local dataset probe adapter is available; no dataset can be claim-ready until a repo-specific loader probe is implemented.'
    probes = [{'dataset': str(name), 'claim_ready': False, 'loader_probe_success': False, 'loader_probe': {'success': False, 'return_code': 2, 'reason': reason}, 'required_files_ok': False, 'missing_required_files': [], 'reason': reason} for name in datasets]
    payload = {'generated_at': now, 'project': project, 'repo_path': repo_path, 'env_name': env_name, 'status': 'blocked_missing_project_dataset_probe_adapter', 'decision': 'project_dataset_loader_probe_required', 'adapter_missing': True, 'adapter_path': str(adapter), 'probes': probes, 'ready_datasets': [], 'blocked_datasets': [str(name) for name in datasets], 'blocker_reasons': [reason], 'probe_return_code': 0}
    probe_save_json(state / 'real_dataset_probe.json', payload)
    reports.mkdir(parents=True, exist_ok=True)
    lines = ['# Real Dataset Probe\n\n']
    lines.append(f'- generated_at: {now}\n')
    lines.append('- status: blocked_missing_project_dataset_probe_adapter\n')
    lines.append(f"- repo_path: {repo_path or 'not selected'}\n")
    lines.append(f"- env_name: {env_name or 'not set'}\n")
    lines.append(f'- adapter_path: {adapter}\n')
    lines.append('\n## Probe Rows\n')
    for row in probes:
        lines.append(f"- {row['dataset']}: claim_ready=false; {reason}\n")
    (reports / 'real_dataset_probe.md').write_text(''.join(lines), encoding='utf-8')
    print(json.dumps({'status': payload['status'], 'project': project, 'repo_path': repo_path, 'probe_count': len(probes)}, ensure_ascii=False))
    return 0

def probe_main() -> int:
    args = probe_parse_args(sys.argv[1:])
    project = args.project or probe__project_from_args(sys.argv[1:])
    if not project:
        print(json.dumps({'status': 'blocked', 'decision': 'project_required_for_project_adapter', 'message': 'This framework entrypoint dispatches to a project-local adapter; pass --project or set PROJECT_ID.'}, ensure_ascii=False), file=sys.stderr)
        return 2
    adapter = ROOT / 'projects' / project / 'scripts' / 'adapters' / Path(__file__).name
    if args.candidate_scope:
        repo_path = candidate_data.candidate_repo_from_state(project, args.repo_path)
        candidate_name, candidate_title = candidate_data.candidate_identity_from_state(project, args.candidate_name, args.candidate_title)
        timeout_sec = int(args.timeout_sec or 120)
        return probe_write_candidate_probe(project, repo_path, args.env_name, candidate_name, candidate_title, timeout_sec=timeout_sec)
    if adapter.exists():
        proc = subprocess.run([sys.executable, str(adapter), *sys.argv[1:]], cwd=ROOT)
        return int(proc.returncode)
    repo_path = probe__repo_from_state(project, args.repo_path)
    return probe_write_generic_probe(project, repo_path, args.env_name, adapter)

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

def plan__repo_root_from_script() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / 'framework').is_dir() and (candidate / 'modules').is_dir() and (candidate / 'web').is_dir():
            return candidate
    return current.parents[1]
ROOT = plan__repo_root_from_script()

def plan_project_from_args(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--project', default='')
    ns, _ = parser.parse_known_args(argv)
    if ns.project:
        return ns.project
    return os.environ.get('PROJECT_ID', '')

def plan_load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default

def plan_save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

def plan__current_find_run_id(project: str) -> str:
    root = ROOT / 'projects' / project
    for path in [root / 'planning' / 'finding' / 'find_progress.json', root / 'planning' / 'finding' / 'find_results.json', root / 'state' / 'current_find_research_plan.json']:
        payload = plan_load_json(path, {})
        if isinstance(payload, dict):
            run_id = str(payload.get('run_id') or payload.get('find_run_id') or payload.get('source_run_id') or '').strip()
            if run_id:
                return run_id
    return ''

def plan__current_selected_execution_ids(project: str) -> tuple[str, str]:
    root = ROOT / 'projects' / project
    for path in [root / 'state' / 'current_find_research_plan.json', root / 'planning' / 'finding' / 'plans.json']:
        payload = plan_load_json(path, {})
        if isinstance(payload, dict):
            plan_id = str(payload.get('selected_plan_id') or '').strip()
            idea_id = str(payload.get('selected_idea_id') or '').strip()
            if plan_id or idea_id:
                return (plan_id, idea_id)
    return ('', '')

def plan__repo_path(row: Any) -> str:
    if not isinstance(row, dict):
        return ''
    for key in ['repo_path', 'local_path', 'path']:
        value = str(row.get(key) or '').strip()
        if value:
            return value
    return ''

def plan__selected_repo(selection: dict[str, Any], active: dict[str, Any]) -> dict[str, Any]:
    selected = selection.get('selected') if isinstance(selection.get('selected'), dict) else {}
    if selected:
        return dict(selected)
    pending = selection.get('pending_environment_candidate') if isinstance(selection.get('pending_environment_candidate'), dict) else {}
    if pending:
        return dict(pending)
    if isinstance(active, dict) and active:
        return dict(active)
    return {}

def plan__ready_datasets(selected: dict[str, Any], req: dict[str, Any], probe: dict[str, Any]) -> list[str]:
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

def plan__blocked_datasets(req: dict[str, Any], probe: dict[str, Any], selected: dict[str, Any], ready: list[str]) -> list[str]:
    ready_set = set(ready)
    blocked: list[str] = []
    for pool in [req.get('blocked_datasets') if isinstance(req, dict) else [], probe.get('blocked_datasets') if isinstance(probe, dict) else []]:
        if isinstance(pool, str):
            pool = [pool]
        for item in pool if isinstance(pool, list) else []:
            text = str(item or '').strip()
            if text and text not in ready_set and (text not in blocked):
                blocked.append(text)
    for row in probe.get('probes', []) if isinstance(probe, dict) and isinstance(probe.get('probes'), list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get('dataset') or '').strip()
        if name and name not in ready_set and (name not in blocked):
            blocked.append(name)
    if not ready and (not blocked):
        if selected.get('pending_loader_bootstrap') or selected.get('pending_reason'):
            blocked.append('real_data_loader_contract')
        else:
            blocked.append('project_specific_dataset_contract')
    return blocked[:20]

def plan__repo_payload(selected: dict[str, Any]) -> dict[str, Any]:
    keys = ['name', 'repo', 'url', 'repo_url', 'repo_path', 'local_path', 'path', 'source', 'fresh_find_run_id', 'selected_plan_id', 'selected_idea_id', 'literature_base_title', 'selected_base_title', 'selection_stage', 'selected_by_stage', 'selection_gate', 'decision', 'selection_score', 'pending_loader_bootstrap', 'pending_reason', 'anchor_selection_policy']
    repo = {key: selected.get(key) for key in keys if selected.get(key) not in (None, '', [])}
    if isinstance(selected.get('signals'), dict):
        repo['signals'] = selected.get('signals')
    if isinstance(selected.get('probe_summary'), dict):
        repo['probe_summary'] = selected.get('probe_summary')
    if isinstance(selected.get('claude_topic_decision'), dict):
        decision = selected.get('claude_topic_decision')
        repo['claude_topic_decision'] = {key: decision.get(key) for key in ['decision', 'confidence', 'repo_action', 'env_action', 'data_action', 'data_action_reason', 'data_action_reason_zh', 'recommended_env_name'] if decision.get(key) not in (None, '', [])}
    return repo


def plan__strip_archive_suffix(name: str) -> str:
    text = str(name or '').strip()
    for suffix in ['.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.zip']:
        if text.lower().endswith(suffix):
            return text[:-len(suffix)]
    return Path(text).stem


def plan__read_repo_text(repo_path: str, selected: dict[str, Any]) -> str:
    parts: list[str] = []
    signals = selected.get('signals') if isinstance(selected.get('signals'), dict) else {}
    for key in ['readme_evidence', 'readme_topic_evidence']:
        value = str(signals.get(key) or '').strip()
        if value:
            parts.append(value)
    repo = Path(repo_path) if repo_path else Path('')
    if repo.exists():
        for readme in list(repo.glob('README*'))[:3]:
            try:
                parts.append(readme.read_text(encoding='utf-8', errors='ignore')[:50000])
            except Exception:
                pass
        for script in list(repo.glob('**/*.py'))[:80]:
            if '.git' in script.parts:
                continue
            try:
                chunk = script.read_text(encoding='utf-8', errors='ignore')[:12000]
            except Exception:
                continue
            if any(token in chunk for token in ['processed_file_names', 'raw_file_names', 'InMemoryDataset', 'Dataset']):
                parts.append(chunk)
    return '\n'.join(parts)


def plan__infer_public_data_evidence(selected: dict[str, Any], repo: dict[str, Any]) -> dict[str, Any]:
    repo_path = str(repo.get('repo_path') or repo.get('local_path') or repo.get('path') or '').strip()
    text = plan__read_repo_text(repo_path, selected)
    if not text:
        return {}
    hf_ids: list[str] = []
    for match in re.finditer(r'https?://huggingface\.co/datasets/([^\s)\]"\']+/[^\s)\]"\']+)', text):
        repo_id = match.group(1).split('/tree/', 1)[0].split('/blob/', 1)[0].strip().strip('.,;')
        if repo_id and repo_id not in hf_ids:
            hf_ids.append(repo_id)
    archives: list[str] = []
    for match in re.finditer(r'\b([A-Za-z0-9][A-Za-z0-9_.-]*\.(?:tar\.gz|tar\.bz2|tar\.xz|tgz|zip))\b', text):
        name = match.group(1).strip()
        if name and name not in archives:
            archives.append(name)
    env_roots: list[str] = []
    for match in re.finditer(r'export\s+[A-Za-z0-9_]*DATA[A-Za-z0-9_]*\s*=\s*["\']([^"\']+)["\']', text):
        value = match.group(1).strip()
        if value and not value.startswith('/path/to/') and value not in env_roots:
            env_roots.append(value)
    processed_names: list[str] = []
    for match in re.finditer(r'processed_file_names[^\n]{0,200}?return\s+["\']([^"\']+)["\']', text, flags=re.S):
        name = match.group(1).strip()
        if name and name not in processed_names:
            processed_names.append(name)
    if not processed_names and any(token in text for token in ['torch.load', 'torch.save', 'InMemoryDataset']):
        processed_names.append('**/data.pt')
    datasets = []
    roots: list[str] = ['data']
    for archive in archives:
        ds = plan__strip_archive_suffix(archive)
        if ds and ds not in [row['id'] for row in datasets]:
            datasets.append({'id': ds, 'source_archive': archive})
        root = f'data/{ds}' if ds else ''
        if root and root not in roots:
            roots.append(root)
    for root in env_roots:
        if root not in roots:
            roots.append(root)
    sources = []
    for repo_id in hf_ids:
        row = {'kind': 'huggingface_dataset', 'url': f'https://huggingface.co/datasets/{repo_id}', 'repo_id': repo_id}
        if archives:
            row['files'] = list(archives)
        sources.append(row)
    if not datasets and sources:
        datasets = [{'id': Path(item).stem if item else 'huggingface_dataset'} for item in archives] or [{'id': hf_ids[0].split('/')[-1]}]
    if not sources and not datasets:
        return {}
    contract = {
        'status': 'inferred_from_repo_readme',
        'datasets': datasets,
        'expected_roots': roots,
        'expected_primary_root': 'data',
        'required_files_per_dataset': processed_names,
        'loader_probe_required': True,
        'claim_rule': 'README/download-source inference is only a data acquisition contract; claims require repo loader/import probes and reference reproduction gates.',
    }
    return {'dataset_contract': contract, 'download_sources': sources, 'inference_source': 'repo_readme_and_loader_code'}

def plan_write_generic_plan(project: str, adapter: Path) -> int:
    root = ROOT / 'projects' / project
    state = root / 'state'
    reports = root / 'reports'
    selection = plan_load_json(state / 'evidence_ready_repo_selection.json', {})
    active = plan_load_json(state / 'active_repo.json', {})
    req = plan_load_json(state / 'repo_data_requirements.json', {})
    probe = plan_load_json(state / 'real_dataset_probe.json', {})
    selected = plan__selected_repo(selection if isinstance(selection, dict) else {}, active if isinstance(active, dict) else {})
    current_run_id = plan__current_find_run_id(project)
    current_plan_id, current_idea_id = plan__current_selected_execution_ids(project)
    selection_run_id = str((selection.get('fresh_find_run_id') if isinstance(selection, dict) else '') or '').strip()
    selected_run_id = str(selected.get('fresh_find_run_id') or '').strip()
    selection_plan_id = str((selection.get('selected_plan_id') if isinstance(selection, dict) else '') or '').strip()
    selection_idea_id = str((selection.get('selected_idea_id') if isinstance(selection, dict) else '') or '').strip()
    selected_route_plan_id = str(selected.get('selected_plan_id') or '').strip()
    selected_plan_id = selection_plan_id or selected_route_plan_id
    selected_idea_id = selection_idea_id or str(selected.get('selected_idea_id') or '').strip()
    selected_contract_run_id = selected_run_id or selection_run_id
    selected_contract_plan_id = selected_route_plan_id or selection_plan_id
    stale_for_current_find = bool(selected and current_run_id and (not selection_run_id or selection_run_id != current_run_id or (not selected_contract_run_id) or (selected_contract_run_id != current_run_id)))
    stale_for_current_plan = bool(selected and current_plan_id and (not selection_plan_id or selection_plan_id != current_plan_id or (not selected_contract_plan_id) or (selected_contract_plan_id != current_plan_id)))
    if stale_for_current_find or stale_for_current_plan:
        selected = {}
        selected_plan_id = current_plan_id or selected_plan_id
        selected_idea_id = current_idea_id or selected_idea_id
    run_id = str((selection_run_id if selected else '') or current_run_id or selection_run_id).strip()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if not selected or not plan__repo_path(selected):
        reason = 'No current environment-stage repo has been selected for this Find run.'
        payload = {'updated_at': now, 'project': project, 'fresh_find_run_id': run_id, 'selected_plan_id': selected_plan_id, 'selected_idea_id': selected_idea_id, 'status': 'blocked_environment_repo_selection_required', 'reason': reason, 'repo': {}, 'ready_datasets': [], 'blocked_datasets': [], 'blocker_reasons': [reason], 'adapter_missing': not adapter.exists(), 'adapter_path': str(adapter)}
        plan_save_json(state / 'fresh_base_implementation_plan.json', payload)
        print(json.dumps({'status': payload['status'], 'project': project, 'reason': reason}, ensure_ascii=False))
        return 2
    ready = plan__ready_datasets(selected, req if isinstance(req, dict) else {}, probe if isinstance(probe, dict) else {})
    blocked = plan__blocked_datasets(req if isinstance(req, dict) else {}, probe if isinstance(probe, dict) else {}, selected, ready)
    repo = plan__repo_payload(selected)
    inferred_public_data = plan__infer_public_data_evidence(selected, repo)
    no_adapter = not adapter.exists()
    selection_gate = str(selection.get('selection_gate') or '') if isinstance(selection, dict) else ''
    base_switch_failed = []
    if isinstance(selection, dict):
        raw_failed = selection.get('base_switch_failed_checks') or selected.get('base_switch_failed_checks')
        if isinstance(raw_failed, str):
            raw_failed = [raw_failed]
        for item in raw_failed if isinstance(raw_failed, list) else []:
            if isinstance(item, dict):
                item = item.get('id') or item.get('check') or item.get('name')
            text = str(item or '').strip()
            if text and text not in base_switch_failed:
                base_switch_failed.append(text)
    if selection_gate == 'blocked_candidate_base_switch_gate_required':
        status = 'blocked_candidate_base_switch_gate_required'
        failed_text = ', '.join(base_switch_failed) if base_switch_failed else 'candidate reference protocol, smoke, full reproduction, and artifact-local audit gates'
        reason = f'Environment-stage Claude Code identified a loader/data-ready candidate, but it remains non-authoritative until deterministic base-switch evidence passes: {failed_text}.'
    elif ready:
        status = 'implementation_ready_for_reference_probe'
        reason = 'Environment-stage repo has at least one claim-ready real dataset with loader evidence.'
    else:
        status = 'blocked_fresh_base_data_required'
        reason = 'Environment-stage Claude Code selected the current transformable repo, but no real dataset/loader evidence is claim-ready yet.'
        if no_adapter:
            reason += ' A project-local data adapter is still required to define and probe the dataset contract.'
    payload = {'updated_at': now, 'project': project, 'fresh_find_run_id': run_id, 'selected_plan_id': selected_plan_id, 'selected_idea_id': selected_idea_id, 'status': status, 'reason': reason, 'repo': repo, 'ready_datasets': ready, 'blocked_datasets': blocked, 'blocker_reasons': [reason, *base_switch_failed] if status == 'blocked_candidate_base_switch_gate_required' else [] if ready else [reason], 'base_switch_failed_checks': base_switch_failed, 'selection_gate': selection.get('selection_gate', '') if isinstance(selection, dict) else '', 'selection_stage': selection.get('selection_stage', '') if isinstance(selection, dict) else '', 'evidence_ready_count': selection.get('evidence_ready_count', 0) if isinstance(selection, dict) else 0, 'adapter_missing': no_adapter, 'adapter_path': str(adapter), 'policy': 'Experiments and paper claims remain blocked until ready_datasets is non-empty and repo loader evidence passes.'}
    if inferred_public_data:
        payload['implementation_evidence'] = inferred_public_data
        payload['dataset_contract'] = inferred_public_data.get('dataset_contract', {})
        payload['download_sources'] = inferred_public_data.get('download_sources', [])
    plan_save_json(state / 'fresh_base_implementation_plan.json', payload)
    reports.mkdir(parents=True, exist_ok=True)
    lines = ['# Fresh Base Implementation Plan\n\n']
    lines.append(f'- updated_at: {now}\n')
    lines.append(f'- status: {status}\n')
    lines.append(f"- repo: {repo.get('name') or repo.get('repo_path') or 'none'}\n")
    lines.append(f"- ready_datasets: {', '.join(ready) or 'none'}\n")
    lines.append(f"- blocked_datasets: {', '.join(blocked) or 'none'}\n")
    if inferred_public_data.get('download_sources'):
        lines.append(f"- inferred_download_sources: {len(inferred_public_data.get('download_sources') or [])}\n")
    if inferred_public_data.get('dataset_contract'):
        ds_rows = inferred_public_data['dataset_contract'].get('datasets') or []
        lines.append(f"- inferred_dataset_contract: {', '.join(str(row.get('id') if isinstance(row, dict) else row) for row in ds_rows) or 'none'}\n")
    lines.append(f'- reason: {reason}\n')
    (reports / 'fresh_base_implementation_plan.md').write_text(''.join(lines), encoding='utf-8')
    print(json.dumps({'status': status, 'project': project, 'repo': repo.get('name') or repo.get('repo_path'), 'ready_datasets': ready, 'blocked_datasets': blocked}, ensure_ascii=False))
    return 0

def plan_main() -> int:
    project = plan_project_from_args(sys.argv[1:])
    if not project:
        print(json.dumps({'status': 'blocked', 'decision': 'project_required_for_project_adapter'}, ensure_ascii=False), file=sys.stderr)
        return 2
    adapter = ROOT / 'projects' / project / 'scripts' / 'adapters' / Path(__file__).name
    if adapter.exists():
        proc = subprocess.run([sys.executable, str(adapter), *sys.argv[1:]], cwd=ROOT)
        return int(proc.returncode)
    return plan_write_generic_plan(project, adapter)



# Public aliases kept for tests and module-internal reuse.
candidate_slug = dr_candidate_slug
candidate_repo_from_state = dr_candidate_repo_from_state
candidate_identity_from_state = dr_candidate_identity_from_state
discover_generic_data_contract = dr_discover_generic_data_contract
write_candidate_requirement = dr_write_candidate_requirement
write_generic_requirement = dr_write_generic_requirement
write_candidate_probe = probe_write_candidate_probe
write_generic_probe = probe_write_generic_probe
write_generic_plan = plan_write_generic_plan


def _legacy_project_adapter(project: str, legacy_filename: str) -> Path:
    return ROOT / 'projects' / project / 'scripts' / 'adapters' / legacy_filename


def _norm_scope_text(value: Any) -> str:
    return str(value or '').strip().lower()


def _adapter_scope_entries(adapter: Path) -> list[dict[str, Any]]:
    """Return explicit candidate-scope declarations for project adapters.

    Project adapters are active-route code by default. Candidate probing may use one
    only when a project Claude-written sidecar explicitly binds that adapter to the
    candidate repo/name/title; otherwise a repo-specific adapter can silently poison
    unrelated candidates.
    """
    candidates = [
        adapter.with_name(f'{adapter.stem}.scope.json'),
        adapter.with_name(f'{adapter.name}.scope.json'),
        adapter.parent / 'adapter_scope.json',
    ]
    entries: list[dict[str, Any]] = []
    for meta_path in candidates:
        payload = dr_load_json(meta_path, {})
        if not isinstance(payload, dict) or not payload:
            continue
        raw = payload.get('candidate_adapters') or payload.get('adapters') or payload.get('entries')
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    item = dict(item)
                    item.setdefault('_scope_path', str(meta_path))
                    entries.append(item)
        else:
            item = dict(payload)
            item.setdefault('_scope_path', str(meta_path))
            entries.append(item)
    return entries


def _adapter_supports_candidate(adapter: Path, repo_path: str, candidate_name: str = '', candidate_title: str = '') -> bool:
    repo_resolved = ''
    if repo_path:
        try:
            repo_resolved = str(Path(repo_path).expanduser().resolve())
        except Exception:
            repo_resolved = str(repo_path)
    name = _norm_scope_text(candidate_name)
    title = _norm_scope_text(candidate_title)
    adapter_name = adapter.name
    for entry in _adapter_scope_entries(adapter):
        declared_adapter = str(entry.get('adapter') or entry.get('adapter_file') or entry.get('filename') or '').strip()
        if declared_adapter and declared_adapter != adapter_name:
            continue
        paths = dr__list_text(entry.get('repo_path')) + dr__list_text(entry.get('repo_paths')) + dr__list_text(entry.get('local_path'))
        names = dr__list_text(entry.get('candidate_repo')) + dr__list_text(entry.get('repo_name')) + dr__list_text(entry.get('repo_names')) + dr__list_text(entry.get('name'))
        titles = dr__list_text(entry.get('candidate_title')) + dr__list_text(entry.get('paper_title')) + dr__list_text(entry.get('title'))
        if repo_resolved and any(dr__paths_equal(path, repo_resolved) for path in paths):
            return True
        if name and any(_norm_scope_text(item) == name for item in names):
            return True
        if title and any(_norm_scope_text(item) == title for item in titles):
            return True
    return False


def run_data_requirements(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    args = dr_parse_args(argv)
    project = args.project or dr_project_from_args(argv)
    if not project:
        print(json.dumps({'status': 'blocked', 'decision': 'project_required_for_project_adapter'}, ensure_ascii=False), file=sys.stderr)
        return 2
    adapter = _legacy_project_adapter(project, 'build_repo_data_requirements.py')
    if args.candidate_scope:
        repo_path = dr_candidate_repo_from_state(project, args.repo_path)
        candidate_name, candidate_title = dr_candidate_identity_from_state(project, args.candidate_name, args.candidate_title)
        if adapter.exists() and _adapter_supports_candidate(adapter, repo_path, candidate_name, candidate_title):
            return dr_write_candidate_requirement_from_adapter(project, repo_path, candidate_name, candidate_title, adapter, argv)
        return dr_write_candidate_requirement(project, repo_path, candidate_name, candidate_title)
    if adapter.exists():
        proc = subprocess.run([sys.executable, str(adapter), *argv], cwd=ROOT)
        return int(proc.returncode)
    repo_path = dr__repo_from_state(project, args.repo_path)
    return dr_write_generic_requirement(project, repo_path, adapter)


def run_dataset_probe(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    args = probe_parse_args(argv)
    project = args.project or probe__project_from_args(argv)
    if not project:
        print(json.dumps({
            'status': 'blocked',
            'decision': 'project_required_for_project_adapter',
            'message': 'This framework entrypoint dispatches to a project-local adapter; pass --project or set PROJECT_ID.',
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    adapter = _legacy_project_adapter(project, 'probe_repo_dataset.py')
    if args.candidate_scope:
        repo_path = dr_candidate_repo_from_state(project, args.repo_path)
        candidate_name, candidate_title = dr_candidate_identity_from_state(project, args.candidate_name, args.candidate_title)
        timeout_sec = int(args.timeout_sec or 120)
        if adapter.exists() and _adapter_supports_candidate(adapter, repo_path, candidate_name, candidate_title):
            return probe_write_candidate_probe_from_adapter(project, repo_path, args.env_name, candidate_name, candidate_title, timeout_sec, adapter, argv)
        return probe_write_candidate_probe(project, repo_path, args.env_name, candidate_name=candidate_name, candidate_title=candidate_title, timeout_sec=timeout_sec)
    if adapter.exists():
        proc = subprocess.run([sys.executable, str(adapter), *argv], cwd=ROOT)
        return int(proc.returncode)
    repo_path = probe__repo_from_state(project, args.repo_path)
    return probe_write_generic_probe(project, repo_path, args.env_name, adapter)


def run_fresh_base_plan(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    project = plan_project_from_args(argv)
    if not project:
        print(json.dumps({'status': 'blocked', 'decision': 'project_required_for_project_adapter'}, ensure_ascii=False), file=sys.stderr)
        return 2
    adapter = _legacy_project_adapter(project, 'build_fresh_base_implementation_plan.py')
    if adapter.exists():
        proc = subprocess.run([sys.executable, str(adapter), *argv], cwd=ROOT)
        return int(proc.returncode)
    return plan_write_generic_plan(project, adapter)


TOOL_ACTIONS = {
    'data_requirements': run_data_requirements,
    'repo_data_requirements': run_data_requirements,
    'build_repo_data_requirements': run_data_requirements,
    'dataset_probe': run_dataset_probe,
    'probe_repo': run_dataset_probe,
    'probe_repo_dataset': run_dataset_probe,
    'fresh_base_plan': run_fresh_base_plan,
    'build_fresh_base_implementation_plan': run_fresh_base_plan,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Environment repo/data backend tools.')
    parser.add_argument('--tool-action', default='data_requirements')
    ns, rest = parser.parse_known_args(argv)
    action = str(ns.tool_action or '').strip().replace('-', '_')
    runner = TOOL_ACTIONS.get(action)
    if runner is None:
        print(json.dumps({'status': 'blocked', 'decision': 'unknown_environment_repo_data_tool_action', 'action': action}, ensure_ascii=False), file=sys.stderr)
        return 2
    return runner(rest)


if __name__ == '__main__':
    raise SystemExit(main())

