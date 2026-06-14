#!/usr/bin/env python3
from __future__ import annotations

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
    parser.add_argument('--candidate-scope', action='store_true', help='Write candidate-scoped evidence instead of active-route repo_data_requirements.json.')
    parser.add_argument('--candidate-name', default='')
    parser.add_argument('--candidate-title', default='')
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



DATA_ROOT_NAMES = {'data', 'datasets', 'dataset', 'benchmark', 'benchmarks'}
STRUCTURED_DATA_PATTERNS = [
    '*.jsonl',
    '*.jsonl.gz',
    '*.ndjson',
    '*.ndjson.gz',
    '*.csv',
    '*.tsv',
    '*.json',
    '*.json.gz',
]


def slugify(value: str) -> str:
    text = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(value or '').strip()).strip('._-').lower()
    return text[:120] or 'candidate'


def _resolved_path(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    path = Path(text).expanduser()
    try:
        return str(path.resolve()) if path.exists() else text
    except Exception:
        return text


def candidate_identity_from_state(project: str, explicit_name: str = '', explicit_title: str = '') -> tuple[str, str]:
    if explicit_name or explicit_title:
        return str(explicit_name or '').strip(), str(explicit_title or '').strip()
    state = ROOT / 'projects' / project / 'state'
    selection = load_json(state / 'evidence_ready_repo_selection.json', {})
    if isinstance(selection, dict):
        pending = selection.get('pending_environment_candidate') if isinstance(selection.get('pending_environment_candidate'), dict) else {}
        decision = pending.get('claude_topic_decision') if isinstance(pending.get('claude_topic_decision'), dict) else selection.get('claude_topic_decision') if isinstance(selection.get('claude_topic_decision'), dict) else {}
        name = str(pending.get('name') or pending.get('repo') or decision.get('best_repo') or '').strip() if isinstance(pending, dict) else ''
        title = str(pending.get('literature_base_title') or pending.get('selected_base_title') or decision.get('literature_base_title') or '').strip() if isinstance(pending, dict) else ''
        if name or title:
            return name, title
    gate = load_json(state / 'base_switch_gate.json', {})
    candidate = gate.get('candidate_route') if isinstance(gate, dict) and isinstance(gate.get('candidate_route'), dict) else {}
    return str(candidate.get('repo') or '').strip(), str(candidate.get('title') or '').strip()


def candidate_repo_from_state(project: str, explicit: str = '') -> str:
    if explicit:
        return _resolved_path(explicit)
    state = ROOT / 'projects' / project / 'state'
    selection = load_json(state / 'evidence_ready_repo_selection.json', {})
    if isinstance(selection, dict):
        pending = selection.get('pending_environment_candidate') if isinstance(selection.get('pending_environment_candidate'), dict) else {}
        decision = pending.get('claude_topic_decision') if isinstance(pending.get('claude_topic_decision'), dict) else selection.get('claude_topic_decision') if isinstance(selection.get('claude_topic_decision'), dict) else {}
        for row in [pending, decision]:
            if not isinstance(row, dict):
                continue
            for key in ['repo_path', 'local_path', 'path']:
                value = _resolved_path(row.get(key))
                if value:
                    return value
    gate = load_json(state / 'base_switch_gate.json', {})
    candidate = gate.get('candidate_route') if isinstance(gate, dict) and isinstance(gate.get('candidate_route'), dict) else {}
    for key in ['repo_path', 'local_path', 'path']:
        value = _resolved_path(candidate.get(key)) if isinstance(candidate, dict) else ''
        if value:
            return value
    return ''


def candidate_slug(repo_path: str, candidate_name: str = '') -> str:
    return slugify(candidate_name or (Path(str(repo_path)).name if repo_path else 'candidate'))


def _data_roots(repo: Path) -> list[Path]:
    roots = [repo / name for name in DATA_ROOT_NAMES if (repo / name).is_dir()]
    return roots or [repo]


def _iter_structured_data_files(repo: Path, limit: int = 120) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for root in _data_roots(repo):
        for pattern in STRUCTURED_DATA_PATTERNS:
            for path in sorted(root.rglob(pattern)):
                if path in seen or not path.is_file():
                    continue
                if any(part in {'.git', '__pycache__', '.pytest_cache'} for part in path.parts):
                    continue
                seen.add(path)
                files.append(path)
                if len(files) >= limit:
                    return files
    return files


def _open_text(path: Path):
    if path.name.lower().endswith('.gz'):
        return gzip.open(path, 'rt', encoding='utf-8', errors='replace')
    return path.open('r', encoding='utf-8', errors='replace', newline='')


def _looks_like_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = ' '.join(value.split())
    if len(text) < 20:
        return False
    alpha = sum(1 for ch in text if ch.isalpha())
    return alpha >= 8 and any(sep in text for sep in [' ', ',', '|', ';', ':'])


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
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
            if _looks_like_text(value):
                text_fields.add(str(key))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric_fields.add(str(key))
                if lowered in {'label', 'rating', 'score', 'target', 'y'} or lowered.endswith('_label'):
                    label_fields.add(str(key))
    return {
        'field_names': sorted(fields)[:80],
        'common_field_names': sorted(common_fields or [])[:80],
        'id_fields': sorted(id_fields),
        'natural_language_text_fields': sorted(text_fields),
        'numeric_fields': sorted(numeric_fields)[:40],
        'label_fields': sorted(label_fields),
        'schema_stable': bool(common_fields),
    }


def _sample_jsonl(path: Path, limit: int) -> tuple[list[dict[str, Any]], int, str]:
    rows: list[dict[str, Any]] = []
    inspected = 0
    with _open_text(path) as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            inspected += 1
            try:
                item = json.loads(text)
            except Exception as exc:
                return rows, inspected, f'jsonl_parse_failed:{type(exc).__name__}'
            if isinstance(item, dict):
                rows.append(item)
            elif isinstance(item, list):
                rows.append({'_array_len': len(item)})
            if len(rows) >= limit:
                break
    return rows, inspected, ''


def _sample_csv(path: Path, limit: int) -> tuple[list[dict[str, Any]], int, str]:
    rows: list[dict[str, Any]] = []
    delimiter = '\t' if path.name.lower().endswith('.tsv') else ','
    with _open_text(path) as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        inspected = 0
        for row in reader:
            inspected += 1
            rows.append(dict(row))
            if len(rows) >= limit:
                break
    return rows, inspected, ''


def _sample_json(path: Path, limit: int) -> tuple[list[dict[str, Any]], int, str]:
    try:
        if path.stat().st_size > 8_000_000:
            return [], 0, 'json_file_too_large_for_safe_generic_sampling'
    except Exception:
        pass
    with _open_text(path) as handle:
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
    return rows, len(candidates), ''


def _sample_data_file(path: Path, repo: Path, limit: int = 50) -> dict[str, Any]:
    rel = str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path)
    lowered = path.name.lower()
    rows: list[dict[str, Any]] = []
    inspected = 0
    error = ''
    try:
        if lowered.endswith(('.jsonl', '.jsonl.gz', '.ndjson', '.ndjson.gz')):
            rows, inspected, error = _sample_jsonl(path, limit)
        elif lowered.endswith(('.csv', '.tsv')):
            rows, inspected, error = _sample_csv(path, limit)
        elif lowered.endswith(('.json', '.json.gz')):
            rows, inspected, error = _sample_json(path, limit)
        else:
            error = 'unsupported_structured_suffix'
    except Exception as exc:
        error = f'sample_failed:{type(exc).__name__}:{exc}'
    summary = _summarize_rows(rows) if rows else {
        'field_names': [],
        'common_field_names': [],
        'id_fields': [],
        'natural_language_text_fields': [],
        'numeric_fields': [],
        'label_fields': [],
        'schema_stable': False,
    }
    parse_success = bool(rows and not error and summary.get('field_names'))
    return {
        'path': str(path),
        'relative_path': rel,
        'format': 'jsonl' if lowered.endswith(('.jsonl', '.jsonl.gz', '.ndjson', '.ndjson.gz')) else 'csv' if lowered.endswith(('.csv', '.tsv')) else 'json' if lowered.endswith(('.json', '.json.gz')) else 'unknown',
        'parse_success': parse_success,
        'sample_rows': len(rows),
        'inspected_rows': inspected,
        'error': error,
        **summary,
    }


def _dataset_name_for(path: Path, repo: Path) -> str:
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


def discover_generic_data_contract(project: str, repo_path: str, candidate_name: str = '', candidate_title: str = '') -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    repo = Path(repo_path).expanduser() if repo_path else Path()
    if not repo_path or not repo.exists() or not repo.is_dir():
        return {
            'generated_at': now,
            'project': project,
            'candidate_scope': True,
            'candidate_repo': candidate_name,
            'candidate_title': candidate_title,
            'repo_path': repo_path,
            'status': 'blocked_candidate_repo_missing',
            'decision': 'candidate_repo_path_required',
            'ready_datasets': [],
            'blocked_datasets': ['candidate_repo_path'],
            'local_statuses': [],
            'sampled_files': [],
            'blocker_reasons': ['candidate repo_path is missing or does not exist'],
        }
    repo = repo.resolve()
    samples = [_sample_data_file(path, repo) for path in _iter_structured_data_files(repo)]
    grouped: dict[str, dict[str, Any]] = {}
    for sample in samples:
        dataset = _dataset_name_for(Path(sample['path']), repo)
        row = grouped.setdefault(dataset, {
            'dataset': dataset,
            'status': 'blocked',
            'candidate_roots': [],
            'sampled_files': [],
            'parseable_file_count': 0,
            'sample_rows': 0,
            'field_names': set(),
            'id_fields': set(),
            'natural_language_text_fields': set(),
            'label_fields': set(),
            'split_files': [],
        })
        row['sampled_files'].append(sample)
        row['sample_rows'] += int(sample.get('sample_rows') or 0)
        row['field_names'].update(sample.get('field_names') or [])
        row['id_fields'].update(sample.get('id_fields') or [])
        row['natural_language_text_fields'].update(sample.get('natural_language_text_fields') or [])
        row['label_fields'].update(sample.get('label_fields') or [])
        stem = Path(sample.get('relative_path') or '').stem.lower()
        if any(token in stem for token in ['train', 'valid', 'validation', 'dev', 'test']):
            row['split_files'].append(sample.get('relative_path'))
        if sample.get('parse_success'):
            row['parseable_file_count'] += 1
    local_statuses: list[dict[str, Any]] = []
    ready: list[str] = []
    for dataset, row in sorted(grouped.items()):
        ok = bool(row['parseable_file_count'] and row['sample_rows'] and row['field_names'])
        if ok:
            ready.append(dataset)
        local_statuses.append({
            'dataset': dataset,
            'status': 'ready' if ok else 'blocked',
            'reason': 'structured local data files parse with stable sample schema' if ok else 'no parseable structured data sample with fields',
            'ready_root': str(repo / 'data' / dataset) if ok and (repo / 'data' / dataset).exists() else '',
            'candidate_roots': sorted({str(Path(sample['path']).parent) for sample in row['sampled_files']})[:12],
            'sampled_file_count': len(row['sampled_files']),
            'parseable_file_count': row['parseable_file_count'],
            'sample_rows': row['sample_rows'],
            'field_names': sorted(row['field_names'])[:80],
            'id_fields': sorted(row['id_fields']),
            'natural_language_text_fields': sorted(row['natural_language_text_fields']),
            'label_fields': sorted(row['label_fields']),
            'split_files': sorted({str(item) for item in row['split_files']})[:20],
        })
    blocked = [row['dataset'] for row in local_statuses if row['dataset'] not in ready]
    if not local_statuses:
        blocked = ['candidate_structured_data_contract']
    status = 'ready' if ready else 'blocked_no_parseable_structured_candidate_data'
    return {
        'generated_at': now,
        'project': project,
        'candidate_scope': True,
        'candidate_repo': candidate_name,
        'candidate_title': candidate_title,
        'repo_path': str(repo),
        'status': status,
        'decision': 'ready_for_loader_probe' if ready else 'candidate_data_contract_blocked',
        'adapter_missing': False,
        'adapter_path': '',
        'datasets': [row['dataset'] for row in local_statuses] or blocked,
        'ready_datasets': ready,
        'blocked_datasets': blocked,
        'download_sources': [{'dataset': name, 'source': 'candidate_repo_local_files', 'path': str(repo)} for name in ready],
        'contract': {
            'status': 'candidate_data_contract_ready' if ready else 'blocked',
            'scope': 'candidate_base_switch_proposal',
            'required_files_per_dataset': [],
            'loader_probe_required': True,
            'claim_rule': 'Generic parsing proves only local structured data presence; repo loader/import, reference protocol, smoke, full reproduction, and artifact-local audit gates remain separate.',
        },
        'local_statuses': local_statuses,
        'sampled_files': samples[:40],
        'blocker_reasons': [] if ready else ['No parseable structured local data files were found under data/datasets/benchmark roots.'],
        'guardrails': [
            'Candidate-scoped evidence must not overwrite active-route repo_data_requirements.json or real_dataset_probe.json.',
            'Generic structured-data parsing is not a repo loader/import probe and does not authorize route switching.',
            'Reference protocol, bounded smoke, full reference reproduction, and artifact-local audit gates remain mandatory.',
        ],
    }


def write_candidate_requirement(project: str, repo_path: str, candidate_name: str = '', candidate_title: str = '') -> int:
    state = ROOT / 'projects' / project / 'state'
    reports = ROOT / 'projects' / project / 'reports'
    payload = discover_generic_data_contract(project, repo_path, candidate_name, candidate_title)
    slug = candidate_slug(payload.get('repo_path') or repo_path, candidate_name)
    state_path = state / f'candidate_data_contract_{slug}.json'
    save_json(state_path, payload)
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f'candidate_data_contract_{slug}.md'
    lines = ['# Candidate Data Contract\n\n']
    lines.append(f"- generated_at: {payload.get('generated_at', '')}\n")
    lines.append(f"- status: {payload.get('status', '')}\n")
    lines.append(f"- decision: {payload.get('decision', '')}\n")
    lines.append(f"- candidate_repo: {candidate_name or payload.get('candidate_repo', '')}\n")
    lines.append(f"- repo_path: {payload.get('repo_path') or repo_path or 'not selected'}\n")
    lines.append(f"- ready_datasets: {', '.join(payload.get('ready_datasets') or []) or 'none'}\n")
    lines.append(f"- blocked_datasets: {', '.join(payload.get('blocked_datasets') or []) or 'none'}\n")
    lines.append('\n## Dataset Samples\n')
    for row in payload.get('local_statuses') or []:
        lines.append(f"- {row.get('dataset')}: {row.get('status')}; files={row.get('sampled_file_count', 0)}; rows={row.get('sample_rows', 0)}; text_fields={', '.join(row.get('natural_language_text_fields') or []) or 'none'}\n")
    for reason in payload.get('blocker_reasons') or []:
        lines.append(f"- blocker: {reason}\n")
    report_path.write_text(''.join(lines), encoding='utf-8')
    print(json.dumps({'status': payload['status'], 'project': project, 'candidate_scope': True, 'repo_path': payload.get('repo_path') or repo_path, 'ready_datasets': payload.get('ready_datasets', []), 'state_path': str(state_path)}, ensure_ascii=False))
    return 0 if payload.get('status') == 'ready' else 2


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
    if args.candidate_scope:
        repo_path = candidate_repo_from_state(project, args.repo_path)
        candidate_name, candidate_title = candidate_identity_from_state(project, args.candidate_name, args.candidate_title)
        return write_candidate_requirement(project, repo_path, candidate_name, candidate_title)
    if adapter.exists():
        proc = subprocess.run([sys.executable, str(adapter), *sys.argv[1:]], cwd=ROOT)
        return int(proc.returncode)
    repo_path = _repo_from_state(project, args.repo_path)
    return write_generic_requirement(project, repo_path, adapter)


if __name__ == '__main__':
    raise SystemExit(main())
