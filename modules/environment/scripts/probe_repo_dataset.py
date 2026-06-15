#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

def _repo_root_from_script() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "framework").is_dir() and (candidate / "modules").is_dir() and (candidate / "web").is_dir():
            return candidate
    return current.parents[1]

ROOT = _repo_root_from_script()

import build_repo_data_requirements as candidate_data


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
    parser.add_argument('--candidate-scope', action='store_true', help='Write candidate-scoped loader evidence without touching active-route real_dataset_probe.json.')
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


BASE_SWITCH_SELECTION_GATE = 'blocked_candidate_base_switch_gate_required'
PENDING_LOADER_SELECTION_GATE = 'blocked_pending_data_loader_for_claude_best_candidate'


def _same_text(left: Any, right: Any) -> bool:
    return bool(str(left or '').strip() and str(left or '').strip() == str(right or '').strip())


def _candidate_matches_pending(payload: dict[str, Any], pending: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or not isinstance(pending, dict):
        return False
    for key in ['repo_path', 'local_path', 'path']:
        if _same_text(payload.get('repo_path'), pending.get(key)):
            return True
    if _same_text(payload.get('candidate_repo'), pending.get('name') or pending.get('repo') or pending.get('full_name')):
        return True
    return False


def _claim_ready_datasets(payload: dict[str, Any]) -> list[str]:
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


def _failed_check_ids(payload: Any) -> list[str]:
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


def _default_base_switch_failed_checks() -> list[str]:
    return [
        'candidate_reference_protocol_passed',
        'candidate_reference_smoke_passed',
        'candidate_full_reference_reproduction_passed',
        'candidate_artifact_local_audit_ready',
    ]


def _base_switch_pending_topic_decision(existing: Any, pending: dict[str, Any], claim_ready: list[str], failed: list[str]) -> dict[str, Any]:
    decision = dict(existing) if isinstance(existing, dict) else {}
    repo_name = str(pending.get('name') or pending.get('repo') or pending.get('full_name') or 'candidate repo').strip()
    repo_path = str(pending.get('repo_path') or pending.get('local_path') or pending.get('path') or '').strip()
    dataset_text = ', '.join(claim_ready) if claim_ready else 'candidate data'
    failed_text = ', '.join(failed or _default_base_switch_failed_checks())
    rationale_en = (
        f'{repo_name} has passed candidate-scoped repo-defined loader/import probing and has claim-ready data '
        f'({dataset_text}), but it remains non-authoritative because deterministic base-switch gates are still blocked: '
        f'{failed_text}. The next required work is reference protocol, bounded smoke, full reference reproduction, '
        'and artifact-local audit evidence; this is not a pending loader problem.'
    )
    rationale_zh = (
        f'{repo_name} 已通过候选仓库定义的 loader/import 探测，并已有可声明数据（{dataset_text}），'
        f'但确定性 base-switch 门控仍阻塞：{failed_text}。下一步必须补参考协议、有界 smoke、完整参考复现和 artifact-local audit 证据；'
        '这不再是等待 loader 的问题。'
    )
    data_reason_en = (
        f'Candidate data/loader evidence is ready for {dataset_text}; do not request another loader probe. '
        'Keep the candidate proposal-only until deterministic reference/audit gates authorize the switch.'
    )
    data_reason_zh = f'{dataset_text} 的候选数据/loader 证据已就绪；不要再要求 loader probe。确定性参考复现/audit 门控授权前，该候选只能作为 proposal。'
    required_en = [
        f'Pass deterministic base-switch checks before selecting this candidate: {failed_text}.',
        'Run/record the candidate reference protocol, bounded smoke test, full reference reproduction, and artifact-local audit.',
    ]
    required_zh = [
        f'选中该候选前必须通过确定性 base-switch checks：{failed_text}。',
        '运行并记录候选参考协议、有界 smoke、完整参考复现和 artifact-local audit。',
    ]
    evidence_en = [
        f'candidate_loader_probe_status=passed for {repo_name}',
        f'claim_ready_datasets={dataset_text}',
        f'blocked_base_switch_checks={failed_text}',
    ]
    evidence_zh = [
        f'{repo_name} 的 candidate_loader_probe_status=passed',
        f'可声明数据集={dataset_text}',
        f'仍阻塞的 base-switch checks={failed_text}',
    ]
    risks_en = ['Reference protocol, bounded smoke, full reproduction, or artifact-local audit may still fail; do not claim this route until those gates pass.']
    risks_zh = ['参考协议、有界 smoke、完整复现或 artifact-local audit 仍可能失败；这些门控通过前不能使用该路线生成论文结论。']
    stewardship_en = (
        f'{repo_name} has cleared candidate loader/data evidence for {dataset_text}. Next work must target deterministic base-switch checks: {failed_text}. '
        'Do not run ordinary main-route experiments or write claims until those reference/audit gates pass.'
    )
    stewardship_zh = f'{repo_name} 已通过 {dataset_text} 的候选 loader/data 证据。下一步必须处理确定性 base-switch checks：{failed_text}。参考复现/audit 门控通过前，不得启动普通主线实验或写 claim。'
    for stale_key in ['raw_output_tail']:
        decision.pop(stale_key, None)
    decision.update({
        'decision': str(decision.get('decision') or 'accept-with-modifications'),
        'rationale': rationale_en,
        'rationale_en': rationale_en,
        'rationale_zh': rationale_zh,
        'repo_action': str(decision.get('repo_action') or 'switch_to_best_repo'),
        'repo_action_reason': rationale_en,
        'repo_action_reason_en': rationale_en,
        'repo_action_reason_zh': rationale_zh,
        'data_action': 'use_claim_ready_dataset',
        'data_action_reason': data_reason_en,
        'data_action_reason_en': data_reason_en,
        'data_action_reason_zh': data_reason_zh,
        'best_repo': str(decision.get('best_repo') or repo_name),
        'repo_path': str(decision.get('repo_path') or repo_path),
        'dataset': str(decision.get('dataset') or (claim_ready[0] if claim_ready else '')),
        'required_modifications': required_en,
        'required_modifications_en': required_en,
        'required_modifications_zh': required_zh,
        'risks': risks_en,
        'risks_en': risks_en,
        'risks_zh': risks_zh,
        'evidence': evidence_en,
        'evidence_en': evidence_en,
        'evidence_zh': evidence_zh,
        'repo_action_reason_i18n': {'zh': rationale_zh, 'en': rationale_en},
        'data_action_reason_i18n': {'zh': data_reason_zh, 'en': data_reason_en},
        'stewardship_memory': stewardship_en,
        'stewardship_memory_en': stewardship_en,
        'stewardship_memory_zh': stewardship_zh,
        'stewardship_memory_i18n': {'zh': stewardship_zh, 'en': stewardship_en},
    })
    return decision


def _sync_pending_environment_candidate(project: str, payload: dict[str, Any], contract: dict[str, Any], state_path: Path, contract_path: Path) -> None:
    state = ROOT / 'projects' / project / 'state'
    selection_path = state / 'evidence_ready_repo_selection.json'
    selection = load_json(selection_path, {})
    if not isinstance(selection, dict):
        return
    pending = selection.get('pending_environment_candidate') if isinstance(selection.get('pending_environment_candidate'), dict) else {}
    if not _candidate_matches_pending(payload, pending):
        return
    summary = pending.get('probe_summary') if isinstance(pending.get('probe_summary'), dict) else {}
    summary = dict(summary)
    claim_ready = _claim_ready_datasets(payload)
    summary.update({
        'probe_return_code': payload.get('probe_return_code'),
        'probe_count': len(payload.get('probes', []) if isinstance(payload.get('probes'), list) else []),
        'claim_ready_datasets': claim_ready,
        'candidate_data_contract_status': contract.get('status', ''),
        'candidate_data_ready_datasets': contract.get('ready_datasets', []) if isinstance(contract.get('ready_datasets'), list) else [],
        'candidate_data_contract_path': str(contract_path),
        'generic_data_parse_probe_success': bool(payload.get('generic_data_parse_probe_success')),
        'candidate_loader_probe_status': payload.get('status', ''),
        'candidate_loader_probe_decision': payload.get('decision', ''),
        'candidate_loader_probe_path': str(state_path),
    })
    pending = dict(pending)
    pending['probe_summary'] = summary
    failed: list[str] = []
    if payload.get('loader_probe_success') is True:
        gate = load_json(state / 'base_switch_gate.json', {})
        failed = _failed_check_ids(gate) or _default_base_switch_failed_checks()
        topic_decision = _base_switch_pending_topic_decision(selection.get('claude_topic_decision'), pending, claim_ready, failed)
        pending['pending_loader_bootstrap'] = False
        pending['decision'] = 'candidate_loader_import_probe_passed_reference_checks_required'
        pending['pending_reason'] = 'Candidate loader/import probe passed, but deterministic base-switch reference protocol, smoke, full reproduction, and artifact-local audit remain mandatory.'
        pending['anchor_selection_policy'] = 'Loader/data evidence is ready, but this candidate remains proposal-only until deterministic base-switch reference/audit gates authorize it.'
        pending['base_switch_failed_checks'] = failed
        pending['claude_topic_decision'] = topic_decision
        selection['claude_topic_decision'] = topic_decision
        strategy = load_json(state / 'repo_env_strategy.json', {})
        if not isinstance(strategy, dict):
            strategy = {}
        strategy.update({
            'repo_action': topic_decision.get('repo_action', 'switch_to_best_repo'),
            'repo_action_reason': topic_decision.get('repo_action_reason_en') or topic_decision.get('repo_action_reason') or '',
            'repo_action_reason_en': topic_decision.get('repo_action_reason_en') or topic_decision.get('repo_action_reason') or '',
            'repo_action_reason_zh': topic_decision.get('repo_action_reason_zh') or '',
            'data_action': topic_decision.get('data_action', 'use_claim_ready_dataset'),
            'data_action_reason': topic_decision.get('data_action_reason_en') or topic_decision.get('data_action_reason') or '',
            'data_action_reason_en': topic_decision.get('data_action_reason_en') or topic_decision.get('data_action_reason') or '',
            'data_action_reason_zh': topic_decision.get('data_action_reason_zh') or '',
            'stewardship_memory': topic_decision.get('stewardship_memory_en') or topic_decision.get('stewardship_memory') or '',
            'stewardship_memory_en': topic_decision.get('stewardship_memory_en') or topic_decision.get('stewardship_memory') or '',
            'stewardship_memory_zh': topic_decision.get('stewardship_memory_zh') or '',
            'selected_repo': {},
            'pending_environment_candidate': {
                'name': pending.get('name') or pending.get('repo') or '',
                'repo_path': pending.get('repo_path') or pending.get('local_path') or '',
                'claim_ready_datasets': claim_ready,
                'selection_gate': BASE_SWITCH_SELECTION_GATE,
            },
        })
        save_json(state / 'repo_env_strategy.json', strategy)
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
        if not isinstance(row, dict) or not _candidate_matches_pending(payload, row):
            continue
        row['probe_summary'] = summary
        if payload.get('loader_probe_success') is True:
            row['decision'] = 'candidate_loader_import_probe_passed_reference_checks_required'
            row['base_switch_failed_checks'] = failed
            row['claude_topic_decision'] = selection.get('claude_topic_decision')
    save_json(selection_path, selection)
    _write_selection_report(project, selection)


def _write_selection_report(project: str, selection: dict[str, Any]) -> None:
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



def _project_config(project: str) -> dict[str, Any]:
    payload = load_json(ROOT / 'projects' / project / 'project.json', {})
    return payload if isinstance(payload, dict) else {}


def _existing_file(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        path = Path(text).expanduser()
        return str(path.resolve()) if path.is_file() else ''
    except Exception:
        return ''


def _candidate_probe_python(project: str, env_name: str = '') -> str:
    cfg = _project_config(project)
    runtime = cfg.get('runtime') if isinstance(cfg.get('runtime'), dict) else {}
    env_cfg = cfg.get('environment') if isinstance(cfg.get('environment'), dict) else {}
    for key in ['EXPERIMENT_PYTHON', 'PROJECT_PYTHON']:
        explicit = _existing_file(os.environ.get(key))
        if explicit:
            return explicit
    for value in [runtime.get('experiment_python'), env_cfg.get('experiment_python')]:
        explicit = _existing_file(value)
        if explicit:
            return explicit
    selected_env = str(env_name or cfg.get('conda_env') or '').strip()
    conda_base = str(runtime.get('conda_base') or env_cfg.get('conda_base_hint') or os.environ.get('CONDA_BASE') or '').strip()
    if selected_env and conda_base:
        explicit = _existing_file(Path(conda_base) / 'envs' / selected_env / 'bin' / 'python')
        if explicit:
            return explicit
    return str(Path(sys.executable).resolve())


def _pyproject_package_names(repo: Path) -> list[str]:
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


def _candidate_package_names(repo: Path) -> list[str]:
    names: list[str] = []
    for name in _pyproject_package_names(repo):
        if name and name not in names:
            names.append(name)
    for child in sorted(repo.iterdir()) if repo.exists() else []:
        if child.is_dir() and (child / '__init__.py').exists() and not child.name.startswith(('.', '_')):
            if child.name not in {'tests', 'test', 'docs', 'examples'} and child.name not in names:
                names.append(child.name)
    return names[:8]


def _candidate_script_paths(repo: Path) -> list[str]:
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
        if any(part in ignored_parts for part in rel_parts[:-1]):
            continue
        haystack = '/'.join(rel_parts)
        if any(token in haystack for token in priority_tokens):
            rank = 0
        elif any(token in rel.name.lower() for token in fallback_tokens):
            rank = 1
        else:
            continue
        ranked.append((rank, len(rel.parts), str(rel), path))
    ranked.sort()
    return [str(path.relative_to(repo)) for _, _, _, path in ranked[:16]]


def _split_file_priority(rel: str) -> tuple[int, str]:
    name = Path(rel).name.lower()
    if 'train' in name:
        return (0, rel)
    if 'valid' in name or 'validation' in name or 'dev' in name:
        return (1, rel)
    if 'test' in name:
        return (2, rel)
    return (3, rel)


def _contract_sample_files(contract: dict[str, Any]) -> list[str]:
    out: list[str] = []
    split_files: list[str] = []
    for status in contract.get('local_statuses', []) if isinstance(contract.get('local_statuses'), list) else []:
        if not isinstance(status, dict):
            continue
        for rel in status.get('split_files', []) if isinstance(status.get('split_files'), list) else []:
            rel_text = str(rel or '').strip()
            if rel_text and rel_text not in split_files:
                split_files.append(rel_text)
    for rel in sorted(split_files, key=_split_file_priority):
        if rel not in out:
            out.append(rel)
    for row in contract.get('sampled_files', []) if isinstance(contract.get('sampled_files'), list) else []:
        if not isinstance(row, dict) or row.get('parse_success') is not True:
            continue
        rel = str(row.get('relative_path') or '').strip()
        if rel and rel not in out:
            out.append(rel)
    return out[:16]


def _bounded_candidate_repo_probe(project: str, repo_path: str, env_name: str, contract: dict[str, Any], timeout_sec: int = 120) -> dict[str, Any]:
    repo = Path(repo_path).expanduser().resolve()
    python_executable = _candidate_probe_python(project, env_name)
    packages = _candidate_package_names(repo)
    scripts = _candidate_script_paths(repo)
    sample_files = _contract_sample_files(contract)
    probe_code = r'''
import importlib
import importlib.util
import inspect
import json
import os
import sys
import traceback
from pathlib import Path

repo = Path(os.environ['TASTE_CANDIDATE_REPO']).resolve()
packages = json.loads(os.environ.get('TASTE_CANDIDATE_PACKAGES') or '[]')
scripts = json.loads(os.environ.get('TASTE_CANDIDATE_SCRIPTS') or '[]')
sample_files = json.loads(os.environ.get('TASTE_CANDIDATE_SAMPLE_FILES') or '[]')
sys.path.insert(0, str(repo))
out = {'package_imports': [], 'script_imports': [], 'loader_calls': []}

out['package_imports_skipped'] = 'script_level_loader_probe_preferred_to_avoid_heavy_package_import_side_effects'

def split_name(path: Path) -> str:
    name = path.name
    for suffix in ['.jsonl.gz', '.json.gz', '.jsonl', '.json', '.csv', '.parquet']:
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return path.stem

def candidate_loader_functions(module):
    funcs = []
    for name, obj in inspect.getmembers(module, inspect.isfunction):
        lowered = name.lower()
        if not any(token in lowered for token in ['load', 'read']):
            continue
        if not any(token in lowered for token in ['data', 'dataset', 'split', 'jsonl', 'parquet', 'csv']):
            continue
        try:
            source_file = inspect.getsourcefile(obj) or ''
            source_path = Path(source_file).resolve()
            if repo not in source_path.parents and source_path != repo:
                continue
        except Exception:
            continue
        try:
            sig = inspect.signature(obj)
        except Exception:
            continue
        params = list(sig.parameters.values())
        required = [p for p in params if p.default is inspect._empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        if 1 <= len(required) <= 2:
            funcs.append((name, obj, required))
    return funcs

for index, rel in enumerate(scripts):
    path = repo / rel
    row = {'path': rel, 'success': False}
    module = None
    try:
        spec = importlib.util.spec_from_file_location(f'_taste_candidate_probe_{index}_{path.stem}', path)
        if spec is None or spec.loader is None:
            raise RuntimeError('spec loader unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        row['success'] = True
    except BaseException as exc:
        row.update({'error': f'{exc.__class__.__name__}: {exc}', 'traceback_tail': traceback.format_exc()[-1200:]})
    out['script_imports'].append(row)
    if module is None:
        continue
    for func_name, func, required in candidate_loader_functions(module):
        for sample in sample_files[:8]:
            sample_path = repo / sample
            call = {'script': rel, 'function': func_name, 'sample_file': sample, 'success': False}
            if not sample_path.exists():
                call['error'] = 'sample file missing'
                out['loader_calls'].append(call)
                continue
            try:
                if len(required) == 1:
                    result = func(str(sample_path))
                else:
                    result = func(str(sample_path), split_name(sample_path))
                try:
                    result_len = len(result)
                except Exception:
                    result_len = None
                call.update({'success': True, 'result_type': type(result).__name__, 'result_len': result_len, 'shape': list(getattr(result, 'shape', []) or [])})
            except BaseException as exc:
                call.update({'error': f'{exc.__class__.__name__}: {exc}', 'traceback_tail': traceback.format_exc()[-1200:]})
            out['loader_calls'].append(call)
            if call['success']:
                break
        if any(item.get('success') for item in out['loader_calls']):
            break
    if any(item.get('success') for item in out['loader_calls']):
        break

package_success = any(row.get('success') for row in out['package_imports'])
script_success = any(row.get('success') for row in out['script_imports'])
loader_success = any(row.get('success') for row in out['loader_calls'])
out.update({'package_import_success': package_success, 'script_import_success': script_success, 'loader_function_success': loader_success, 'loader_probe_success': bool(loader_success and (package_success or script_success))})
print(json.dumps(out, ensure_ascii=False))
'''
    env = os.environ.copy()
    env['TASTE_CANDIDATE_REPO'] = str(repo)
    env['TASTE_CANDIDATE_PACKAGES'] = json.dumps(packages, ensure_ascii=False)
    env['TASTE_CANDIDATE_SCRIPTS'] = json.dumps(scripts, ensure_ascii=False)
    env['TASTE_CANDIDATE_SAMPLE_FILES'] = json.dumps(sample_files, ensure_ascii=False)
    env['PYTHONPATH'] = str(repo) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    try:
        proc = subprocess.run([python_executable, '-c', probe_code], cwd=repo, text=True, capture_output=True, timeout=max(10, int(timeout_sec or 120)), env=env)
    except subprocess.TimeoutExpired as exc:
        return {
            'python_executable': python_executable,
            'packages': packages,
            'scripts': scripts,
            'sample_files': sample_files,
            'loader_probe_success': False,
            'return_code': 124,
            'stderr_tail': str(exc)[-1200:],
        }
    try:
        payload = json.loads((proc.stdout or '').strip().splitlines()[-1]) if (proc.stdout or '').strip() else {}
    except Exception:
        payload = {}
    payload.update({
        'python_executable': python_executable,
        'packages': packages,
        'scripts': scripts,
        'sample_files': sample_files,
        'return_code': proc.returncode,
        'stderr_tail': (proc.stderr or '')[-2000:],
        'stdout_tail': (proc.stdout or '')[-2000:],
    })
    payload['loader_probe_success'] = bool(proc.returncode == 0 and payload.get('loader_probe_success'))
    return payload


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


def write_candidate_probe(project: str, repo_path: str, env_name: str, candidate_name: str = '', candidate_title: str = '', timeout_sec: int = 120) -> int:
    state = ROOT / 'projects' / project / 'state'
    reports = ROOT / 'projects' / project / 'reports'
    slug = candidate_data.candidate_slug(repo_path, candidate_name)
    contract_path = state / f'candidate_data_contract_{slug}.json'
    contract = load_json(contract_path, {})
    if not isinstance(contract, dict) or str(contract.get('repo_path') or '') != str(Path(repo_path).expanduser().resolve() if repo_path and Path(repo_path).expanduser().exists() else repo_path):
        contract = candidate_data.discover_generic_data_contract(project, repo_path, candidate_name, candidate_title)
        save_json(contract_path, contract)
    ready_datasets = [str(item) for item in contract.get('ready_datasets', [])] if isinstance(contract.get('ready_datasets'), list) else []
    blocked_datasets = [str(item) for item in contract.get('blocked_datasets', [])] if isinstance(contract.get('blocked_datasets'), list) else []
    generic_parse_ok = bool(contract.get('status') == 'ready' and ready_datasets)
    repo_import_probe = _bounded_candidate_repo_probe(project, str(contract.get('repo_path') or repo_path), env_name, contract, timeout_sec) if generic_parse_ok else {}
    loader_ok = bool(repo_import_probe.get('loader_probe_success'))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    reason = (
        'Candidate repo import and bounded loader function probe passed on local candidate data; reference protocol, smoke, full reproduction, and artifact-local audit remain mandatory before switch authorization.'
        if loader_ok else
        'Candidate structured data parses locally, but no repo loader/import probe has passed yet; install/repair repo dependencies or provide a repo loader entrypoint before authorizing a base switch.'
        if generic_parse_ok else
        'Candidate data contract is not ready; no repo loader/import probe can be credited yet.'
    )
    probes = [
        {
            'dataset': name,
            'claim_ready': loader_ok,
            'loader_probe_success': loader_ok,
            'generic_data_parse_probe_success': generic_parse_ok,
            'loader_probe': {'success': loader_ok, 'return_code': 0 if loader_ok else 2, 'reason': reason},
            'required_files_ok': generic_parse_ok,
            'missing_required_files': [],
            'reason': reason,
        }
        for name in (ready_datasets or blocked_datasets or ['candidate_structured_data_contract'])
    ]
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
        'adapter_path': '',
        'data_contract_path': str(contract_path),
        'repo_import_probe': repo_import_probe,
        'probes': probes,
        'ready_datasets': [row['dataset'] for row in probes] if loader_ok else [],
        'blocked_datasets': [] if loader_ok else [row['dataset'] for row in probes],
        'blocker_reasons': [] if loader_ok else [reason],
        'probe_return_code': 0 if loader_ok else 2,
        'guardrails': [
            'Candidate-scoped loader evidence must not overwrite active-route real_dataset_probe.json.',
            'Generic structured-data parsing cannot be credited as repo loader/import compatibility.',
            'Reference protocol, bounded smoke, full reference reproduction, and artifact-local audit gates remain mandatory.',
        ],
    }
    state_path = state / f'candidate_loader_probe_{slug}.json'
    save_json(state_path, payload)
    _sync_pending_environment_candidate(project, payload, contract, state_path, contract_path)
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f'candidate_loader_probe_{slug}.md'
    lines = ['# Candidate Loader Probe\n\n']
    lines.append(f"- generated_at: {now}\n")
    lines.append(f"- status: {payload['status']}\n")
    lines.append(f"- decision: {payload['decision']}\n")
    lines.append(f"- candidate_repo: {candidate_name}\n")
    lines.append(f"- repo_path: {payload.get('repo_path') or 'not selected'}\n")
    lines.append(f"- generic_data_parse_probe_success: {str(generic_parse_ok).lower()}\n")
    lines.append(f"- loader_probe_success: {str(loader_ok).lower()}\n")
    lines.append(f"- data_contract_path: {contract_path}\n")
    lines.append(f"- probe_python: {repo_import_probe.get('python_executable', '') if isinstance(repo_import_probe, dict) else ''}\n")
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
    if args.candidate_scope:
        repo_path = candidate_data.candidate_repo_from_state(project, args.repo_path)
        candidate_name, candidate_title = candidate_data.candidate_identity_from_state(project, args.candidate_name, args.candidate_title)
        timeout_sec = int(args.timeout_sec or 120)
        return write_candidate_probe(project, repo_path, args.env_name, candidate_name, candidate_title, timeout_sec=timeout_sec)
    if adapter.exists():
        proc = subprocess.run([sys.executable, str(adapter), *sys.argv[1:]], cwd=ROOT)
        return int(proc.returncode)
    repo_path = _repo_from_state(project, args.repo_path)
    return write_generic_probe(project, repo_path, args.env_name, adapter)


if __name__ == '__main__':
    raise SystemExit(main())
