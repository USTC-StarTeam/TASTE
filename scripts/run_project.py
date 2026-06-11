#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import selectors
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from experiment_contracts import load_audit_payload
from llm_client import llm_available, llm_disabled_reason
from project_paths import ROOT, build_paths, load_project_config
from pipeline_guard import guard_fresh_base_blocker_entry

WORKSPACE_ROOT = ROOT / "modules" / "taste"
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
from auto_research.source_selection import canonical_source_selection


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def runtime_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if extra:
        env.update(extra)
    return env


def run(cmd: list[str], cwd: Path, log_path: Path, timeout: int | None = None) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        env=runtime_env(),
    )
    start = time.time()
    timed_out = False
    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            for key, _ in selector.select(timeout=0.25):
                line = key.fileobj.readline()
                if line:
                    chunks.append(line)
                    print(line, end='', flush=True)
            if proc.poll() is not None:
                rest = proc.stdout.read()
                if rest:
                    chunks.append(rest)
                    print(rest, end='', flush=True)
                break
            if timeout is not None and time.time() - start > timeout:
                timed_out = True
                message = f'\nTIMEOUT: command exceeded {timeout}s and was skipped so the autonomous loop can continue.\n'
                chunks.append(message)
                print(message, end='', file=sys.stderr, flush=True)
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                break
    finally:
        selector.close()
    returncode = 124 if timed_out else int(proc.returncode or 0)
    log_path.write_text(''.join(chunks) + '\n--- STDERR MERGED INTO STDOUT ---\n', encoding='utf-8')
    return returncode

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--topic')
    parser.add_argument('--max-results', type=int)
    parser.add_argument('--discover-retries', type=int)
    parser.add_argument('--skip-llm', action='store_true')
    parser.add_argument('--skip-semantic-scholar', action='store_true')
    parser.add_argument('--skip-arxiv', action='store_true')
    parser.add_argument('--skip-github', action='store_true')
    parser.add_argument('--skip-initialization', action='store_true')
    parser.add_argument('--skip-discovery', action='store_true')
    parser.add_argument('--parallel-method', action='append', default=[])
    parser.add_argument('--benchmark')
    parser.add_argument('--metric')
    parser.add_argument('--dataset')
    parser.add_argument('--repo-name')
    parser.add_argument('--repo-path')
    parser.add_argument('--command-template')
    parser.add_argument('--execute-plan', action='store_true')
    parser.add_argument('--prepare-env', action='store_true')
    parser.add_argument('--real-bootstrap-env', action='store_true')
    parser.add_argument('--max-launches', type=int)
    parser.add_argument('--coding-backend', default='')
    parser.add_argument('--venue', default='')
    parser.add_argument('--deep-literature-survey', action='store_true', help='Run TASTE in full survey mode instead of fast initialization mode.')
    return parser.parse_args()


def load_literature_plan(paths) -> dict:
    plan_path = paths.state / 'literature_review_plan.json'
    if not plan_path.exists():
        return {}
    try:
        return json.loads(plan_path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _query_placeholder_key(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').lower())


def _query_looks_like_project_id(value: str, project: str) -> bool:
    text = str(value or '').strip()
    if not text:
        return True
    project_key = _query_placeholder_key(project)
    text_key = _query_placeholder_key(text)
    return bool(project_key and text_key == project_key)


def selected_plan_topic(paths) -> str:
    plans_path = paths.planning / 'finding' / 'plans.json'
    state_path = paths.state / 'current_find_research_plan.json'
    try:
        plans_payload = json.loads(plans_path.read_text(encoding='utf-8')) if plans_path.exists() else {}
    except Exception:
        plans_payload = {}
    try:
        state_payload = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    except Exception:
        state_payload = {}
    selected_id = str(
        (plans_payload.get('selected_plan_id') if isinstance(plans_payload, dict) else '')
        or (state_payload.get('selected_plan_id') if isinstance(state_payload, dict) else '')
        or ''
    ).strip()
    for row in (plans_payload.get('plans') if isinstance(plans_payload, dict) else []) or []:
        if not isinstance(row, dict):
            continue
        plan_id = str(row.get('plan_id') or row.get('id') or '').strip()
        if selected_id and plan_id != selected_id:
            continue
        for key in ['title', 'idea_title', 'hypothesis', 'experiment_name', 'summary']:
            value = str(row.get(key) or '').strip()
            if value:
                return value
    return ''


def effective_project_topic(project: str, args_topic: str | None, cfg: dict, paths) -> str:
    candidates = [
        args_topic,
        cfg.get('topic') if isinstance(cfg, dict) else '',
        cfg.get('title') if isinstance(cfg, dict) else '',
        cfg.get('research_interest') if isinstance(cfg, dict) else '',
        cfg.get('user_prompt') if isinstance(cfg, dict) else '',
        selected_plan_topic(paths),
        'research',
    ]
    for value in candidates:
        text = str(value or '').strip()
        if text and not _query_looks_like_project_id(text, project):
            return text
    return 'research'


def planned_discovery_queries(cfg: dict, paths, fallback_topic: str, max_queries: int = 6, project: str = '') -> list[str]:
    plan = load_literature_plan(paths)
    values = list(plan.get('queries', []) or []) + list(cfg.get('queries', []) or []) + [fallback_topic]
    out = []
    seen = set()
    for value in values:
        text = str(value or '').strip()
        key = text.lower()
        if not text or key in seen or _query_looks_like_project_id(text, project):
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= max_queries:
            break
    return out or [fallback_topic or 'research']


def read_parallel_methods(paths) -> list[dict]:
    plan_path = paths.state / 'parallel_plan.json'
    if not plan_path.exists():
        return []
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    return plan.get('methods', []) if isinstance(plan, dict) else plan


CURRENT_FIND_SELECTION_FIELD_KEYS = ['selected_idea_id', 'selected_plan_id', 'selected_idea', 'selected_plan', 'selected_by', 'execution_policy']


def _first_dict(*values) -> dict:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _truthy_selected(value) -> bool:
    if value is True:
        return True
    if value in (False, None, ''):
        return False
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'selected', 'select', 'execute', 'execute_next', 'primary', 'best', 'best_idea', 'best_plan'}


def _row_selected_for_execution(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    if _truthy_selected(row.get('selected_for_execution')) or _truthy_selected(row.get('execute_next')):
        return True
    for key in ('execution_selection', 'execution_policy'):
        nested = row.get(key)
        if isinstance(nested, dict) and _truthy_selected(nested.get('selected')):
            return True
    return False


def _payload_run_id(payload) -> str:
    if not isinstance(payload, dict):
        return ''
    return str(payload.get('run_id') or payload.get('source_run_id') or payload.get('find_run_id') or payload.get('current_find_run_id') or '').strip()


def _selected_summary(row: dict, keys: list[str]) -> dict:
    if not isinstance(row, dict):
        return {}
    return {key: row.get(key) for key in keys if key in row and row.get(key) not in (None, '')}


def _current_find_payloads(paths) -> list[dict]:
    payloads: list[dict] = []
    for source_path in [
        paths.state / 'current_find_research_plan.json',
        paths.state / 'taste_plan_bridge.json',
        paths.state / 'experiment_plan.json',
        paths.state / 'literature_tool_packet.json',
        paths.planning / 'finding' / 'plans.json',
        paths.planning / 'finding' / 'ideas.json',
    ]:
        data = load_json(source_path)
        if isinstance(data, dict):
            payloads.append(data)
            nested = data.get('plans_json')
            if isinstance(nested, dict):
                payloads.append(nested)
    return payloads


def current_find_execution_contract(paths) -> dict:
    all_payloads = _current_find_payloads(paths)
    find_results_payload = load_json(paths.planning / "finding" / "find_results.json")
    find_progress_payload = load_json(paths.planning / "finding" / "find_progress.json")
    planning_plan_payload = load_json(paths.planning / "finding" / "plans.json")
    planning_idea_payload = load_json(paths.planning / "finding" / "ideas.json")
    state_plan_payload = load_json(paths.state / "current_find_research_plan.json")
    experiment_plan_payload = load_json(paths.state / "experiment_plan.json")
    primary_sources = [
        find_results_payload,
        find_progress_payload,
        planning_plan_payload,
        planning_idea_payload,
        state_plan_payload,
        experiment_plan_payload,
    ]
    primary_payloads = [payload for payload in primary_sources if isinstance(payload, dict)]
    run_id = ''
    for payload in primary_payloads + [payload for payload in all_payloads if isinstance(payload, dict)]:
        run_id = _payload_run_id(payload)
        if run_id:
            break

    def payload_matches_current_run(payload: dict) -> bool:
        payload_run_id = _payload_run_id(payload)
        return bool(not run_id or not payload_run_id or payload_run_id == run_id)

    primary_payloads = [payload for payload in primary_payloads if payload_matches_current_run(payload)]
    planning_plan_authoritative = (
        isinstance(planning_plan_payload, dict)
        and payload_matches_current_run(planning_plan_payload)
        and isinstance(planning_plan_payload.get('plans'), list)
    )
    if planning_plan_authoritative:
        payloads = [planning_plan_payload]
        if isinstance(planning_idea_payload, dict) and payload_matches_current_run(planning_idea_payload):
            payloads.append(planning_idea_payload)
        if not any(isinstance(payload.get('ideas'), list) for payload in payloads):
            for fallback in [state_plan_payload] + [payload for payload in all_payloads if isinstance(payload, dict)]:
                if isinstance(fallback, dict) and payload_matches_current_run(fallback) and isinstance(fallback.get('ideas'), list):
                    payloads.append({'run_id': _payload_run_id(fallback) or run_id, 'ideas': fallback.get('ideas')})
                    break
    else:
        primary_has_rows = any(isinstance(payload.get('ideas'), list) or isinstance(payload.get('plans'), list) for payload in primary_payloads)
        payloads = primary_payloads if primary_has_rows else [payload for payload in all_payloads if isinstance(payload, dict) and payload_matches_current_run(payload)]

    contract: dict = {
        'required': False,
        'selected_idea_id': '',
        'selected_plan_id': '',
        'selected_idea': {},
        'selected_plan': {},
        'selected_by': '',
        'execution_policy': {},
        'source': 'current_find_execution_contract',
        'run_id': run_id,
    }
    ideas: list[dict] = []
    plans: list[dict] = []
    for payload in payloads:
        payload_run_id = _payload_run_id(payload)
        if payload_run_id and not contract['run_id']:
            contract['run_id'] = payload_run_id
        for key in CURRENT_FIND_SELECTION_FIELD_KEYS:
            value = payload.get(key)
            if value not in (None, '', [], {}) and not contract.get(key):
                contract[key] = value
        rows = payload.get('ideas')
        if isinstance(rows, list):
            ideas.extend(row for row in rows if isinstance(row, dict))
        rows = payload.get('plans')
        if isinstance(rows, list):
            plans.extend(row for row in rows if isinstance(row, dict))

    def unique_rows(rows: list[dict], fallback_prefix: str) -> list[dict]:
        unique: dict[str, dict] = {}
        order: list[str] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            key = str(row.get('plan_id') or row.get('id') or row.get('idea_id') or row.get('title') or f'{fallback_prefix}-{index}').strip()
            if key not in unique:
                order.append(key)
            unique[key] = row
        return [unique[key] for key in order]

    ideas = unique_rows(ideas, 'idea')
    plans = unique_rows(plans, 'plan')
    contract['required'] = bool(plans or ideas or contract.get('selected_plan_id'))
    if contract.get('selected_plan_id') and plans:
        selected_plan_id = str(contract.get('selected_plan_id') or '').strip()
        matching_plans = [row for row in plans if str(row.get('plan_id') or row.get('id') or '').strip() == selected_plan_id]
        if len(matching_plans) != 1:
            contract['selected_plan_id'] = ''
            contract['selected_plan'] = {}
            contract['selection_issue'] = 'selected_plan_id_missing' if not matching_plans else 'ambiguous_selected_plan'
            contract['reason'] = 'persisted selected_plan_id does not match exactly one current-Find plan artifact'
    if not contract.get('selected_plan_id'):
        selected_plans = [row for row in plans if _row_selected_for_execution(row)]
        if len(selected_plans) == 1:
            selected_plan = selected_plans[0]
            contract['selected_plan_id'] = str(selected_plan.get('plan_id') or selected_plan.get('id') or '').strip()
            contract['selected_idea_id'] = contract.get('selected_idea_id') or str(selected_plan.get('idea_id') or '').strip()
            contract['selected_plan'] = contract.get('selected_plan') or _selected_summary(selected_plan, ['plan_id', 'id', 'idea_id', 'title', 'new_method', 'initial_experiment', 'status'])
            contract['selected_by'] = contract.get('selected_by') or 'selected_for_execution_plan_flag'
        elif len(selected_plans) > 1:
            contract['selection_issue'] = 'ambiguous_selected_plan'
            contract['ambiguous_selected_plan_ids'] = [str(row.get('plan_id') or row.get('id') or '').strip() for row in selected_plans]
    if not contract.get('selected_idea_id'):
        selected_ideas = [row for row in ideas if _row_selected_for_execution(row)]
        if selected_ideas:
            selected_idea = selected_ideas[0]
            contract['selected_idea_id'] = str(selected_idea.get('id') or selected_idea.get('idea_id') or '').strip()
            contract['selected_idea'] = contract.get('selected_idea') or _selected_summary(selected_idea, ['id', 'idea_id', 'title', 'new_method', 'initial_experiment', 'status'])
            contract['selected_by'] = contract.get('selected_by') or 'selected_for_execution_idea_flag'
    if not isinstance(contract.get('selected_plan'), dict):
        contract['selected_plan'] = {}
    if not isinstance(contract.get('selected_idea'), dict):
        contract['selected_idea'] = {}
    if not isinstance(contract.get('execution_policy'), dict):
        contract['execution_policy'] = {}
    if contract['required'] and not contract.get('selected_plan_id'):
        ambiguous = contract.get('selection_issue') == 'ambiguous_selected_plan'
        contract['selection_issue'] = 'ambiguous_selected_plan' if ambiguous else 'missing_selected_plan'
        contract['status'] = 'blocked_ambiguous_selected_plan' if ambiguous else 'blocked_missing_selected_plan'
        contract['reason'] = 'current Find produced multiple explicit selected plans; rerun current-Find Claude selection so exactly one selected_plan_id exists before executing experiments' if ambiguous else 'current Find produced idea/plan candidates but no selected_plan_id contract exists; rerun ensure_current_find_research_plan.py or project Claude selection before executing experiments'
        contract['execution_policy'] = {
            **contract.get('execution_policy', {}),
            'status': 'no_selected_plan',
            'downstream_consumes': 'selected_plan_id',
            'candidate_backlog_policy': 'Non-selected ideas/plans are visible for supervision only and must not drive environment, experiment, paper, or claim execution.',
        }
    elif contract.get('selected_plan_id'):
        contract['selection_issue'] = ''
        contract['status'] = 'selected_plan_ready'
        contract['reason'] = 'downstream execution is restricted to selected_plan_id'
        contract['execution_policy'] = {
            **contract.get('execution_policy', {}),
            'status': contract.get('execution_policy', {}).get('status') or 'selected_plan_only',
            'downstream_consumes': 'selected_plan_id',
            'candidate_backlog_policy': 'Non-selected ideas/plans are visible for supervision only and must not drive environment, experiment, paper, or claim execution.',
        }
    else:
        contract['selection_issue'] = ''
        contract['status'] = 'not_required'
        contract['reason'] = 'no current Find idea/plan contract is present for this project'
    contract['candidate_counts'] = {'ideas': len(ideas), 'plans': len(plans)}
    return contract


def _method_identifier_values(method: dict, trial: dict | None = None) -> set[str]:
    values: set[str] = set()
    containers = [method]
    if isinstance(trial, dict):
        containers.append(trial)
    for key in ['method_contract', 'contract', 'claim_contract', 'current_find', 'execution_contract', 'plan', 'idea']:
        nested = method.get(key) if isinstance(method, dict) else None
        if isinstance(nested, dict):
            containers.append(nested)
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ['selected_plan_id', 'current_find_plan_id', 'source_plan_id', 'plan_id', 'selected_idea_id', 'current_find_idea_id', 'source_idea_id', 'idea_id']:
            value = str(container.get(key) or '').strip()
            if value:
                values.add(value)
    return values


def method_matches_current_find_contract(method: dict, contract: dict) -> bool:
    if not isinstance(contract, dict) or not contract.get('required'):
        return True
    selected_plan_id = str(contract.get('selected_plan_id') or '').strip()
    selected_idea_id = str(contract.get('selected_idea_id') or '').strip()
    if not selected_plan_id:
        return False
    values = _method_identifier_values(method)
    if selected_plan_id and selected_plan_id in values:
        return True
    if selected_idea_id and selected_idea_id in values:
        return True
    for trial in method.get('trials', []) if isinstance(method.get('trials'), list) else []:
        values = _method_identifier_values(method, trial if isinstance(trial, dict) else None)
        if selected_plan_id and selected_plan_id in values:
            return True
        if selected_idea_id and selected_idea_id in values:
            return True
    return False


def enforce_current_find_selected_plan(paths, methods: list[dict]) -> tuple[list[dict], dict]:
    contract = current_find_execution_contract(paths)
    if not contract.get('required'):
        return methods, contract
    selected_plan_id = str(contract.get('selected_plan_id') or '').strip()
    allowed: list[dict] = []
    skipped: list[dict] = []
    for method in methods:
        if not isinstance(method, dict):
            continue
        if method_matches_current_find_contract(method, contract):
            method['current_find_execution_contract'] = {
                key: contract.get(key)
                for key in ['run_id', 'selected_plan_id', 'selected_idea_id', 'selected_by', 'execution_policy', 'status']
            }
            allowed.append(method)
            continue
        method['status'] = method.get('status') or 'planned'
        method['decision'] = 'blocked_missing_selected_plan' if not selected_plan_id else 'candidate_backlog_not_selected'
        method['result_summary'] = (
            contract.get('reason')
            if not selected_plan_id else
            f"skipped: current Find selected_plan_id={selected_plan_id}; this method is not tagged to the selected plan/idea and remains backlog"
        )
        skipped.append(method)
    guard = {
        'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'status': 'pass' if allowed else contract.get('status'),
        'selected_contract': contract,
        'allowed_method_count': len(allowed),
        'skipped_method_count': len(skipped),
        'allowed_methods': [row.get('method') or row.get('method_slug') for row in allowed],
        'skipped_methods': [row.get('method') or row.get('method_slug') for row in skipped],
        'policy': 'Execute-plan may launch only methods tagged to selected_plan_id or selected_idea_id. Untagged/non-selected methods stay backlog.',
    }
    save_json(paths.state / 'current_find_execution_guard.json', guard)
    if skipped:
        save_parallel_plan(paths, methods)
    return allowed, contract


def save_parallel_plan(paths, methods: list[dict]) -> None:
    plan_path = paths.state / 'parallel_plan.json'
    if not plan_path.exists():
        return
    try:
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
    except Exception:
        plan = {'methods': methods}
    if isinstance(plan, dict):
        plan['methods'] = methods
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + chr(10), encoding='utf-8')
    else:
        plan_path.write_text(json.dumps(methods, indent=2, ensure_ascii=False) + chr(10), encoding='utf-8')


def update_parallel_status(paths, method_name: str, trial_index: int, status: str, result_summary: str = '', decision: str = '') -> None:
    plan_path = paths.state / 'parallel_plan.json'
    if not plan_path.exists():
        return
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    methods = plan.get('methods', []) if isinstance(plan, dict) else plan
    for method in methods:
        if method.get('method') != method_name and method.get('method_slug') != method_name:
            continue
        method['status'] = status if status in {'planned', 'running', 'completed', 'failed', 'incomplete_audit'} else method.get('status', 'planned')
        if decision:
            method['decision'] = decision
        for trial in method.get('trials', []):
            if int(trial.get('trial_index', -1)) == int(trial_index):
                trial['status'] = status
                if result_summary:
                    trial['result_summary'] = result_summary
                break
    if isinstance(plan, dict):
        plan['methods'] = methods
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    else:
        plan_path.write_text(json.dumps(methods, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def derive_metric_value(metrics: dict, metric_name: str) -> str:
    if not metrics or not metric_name:
        return ''
    value = metrics.get(metric_name)
    return '' if value is None else str(value)


def locate_conda_executable(cfg: dict | None = None) -> str:
    candidates: list[str] = []
    env_exe = os.environ.get('CONDA_EXE', '')
    if env_exe:
        candidates.append(env_exe)
    on_path = shutil.which('conda')
    if on_path:
        candidates.append(on_path)
    root = Path(__file__).resolve().parents[1]
    env_cfg = (cfg or {}).get('environment', {}) if isinstance(cfg, dict) else {}
    base_hint = str(env_cfg.get('conda_base_hint', '') or '').strip()
    bases: list[Path] = []
    if base_hint:
        bases.append(Path(base_hint))
    for parent in [root, root.parent, Path.home(), Path('/opt')]:
        for name in ['miniforge', 'miniforge3', 'miniconda', 'miniconda3', 'anaconda3', 'conda']:
            bases.append(parent / name)
    for base in bases:
        candidates.append(str(base / 'bin' / 'conda'))
        candidates.append(str(base / 'condabin' / 'conda'))
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if Path(candidate).exists():
            return candidate
    return ''


def conda_env_exists(env_name: str, conda_exe: str = '', cfg: dict | None = None) -> bool:
    if not env_name:
        return False
    conda = conda_exe or locate_conda_executable(cfg)
    if not conda:
        return False
    proc = subprocess.run([conda, 'env', 'list', '--json'], text=True, capture_output=True, env=runtime_env())
    if proc.returncode != 0:
        return False
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        return False
    envs = payload.get('envs', [])
    return any(Path(env).name == env_name for env in envs)


def load_machine_profile(paths) -> dict:
    profile_path = paths.reports / 'machine_profile.json'
    if not profile_path.exists():
        return {}
    try:
        return json.loads(profile_path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def check_runtime_readiness(paths, args: argparse.Namespace) -> tuple[bool, list[str]]:
    profile = load_machine_profile(paths)
    dependencies = profile.get('dependencies', {}) if isinstance(profile, dict) else {}
    issues: list[str] = []
    required_missing = dependencies.get('required_missing', []) if isinstance(dependencies, dict) else []
    llm_related = {'openai_api_key', 'llm_api_key', 'llm_provider'}
    filtered_missing = [name for name in required_missing if args.execute_plan or str(name).lower() not in llm_related]
    if filtered_missing:
        issues.append(f"missing required runtime dependencies: {', '.join(filtered_missing)}")
    if args.execute_plan:
        conda_available = dependencies.get('cli', {}).get('conda', {}).get('available', False) if isinstance(dependencies, dict) else False
        if not conda_available:
            issues.append('parallel plan execution requested but conda is unavailable')
    return (len(issues) == 0, issues)


def load_metrics_fallback(metrics_path: Path) -> dict:
    if not metrics_path.exists():
        return {}
    try:
        data = json.loads(metrics_path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


SHELL_META_TOKENS = ('|', '&', ';', '<', '>', '(', ')', '$', '`', '\n')


def command_needs_shell(command: str) -> bool:
    return any(token in (command or '') for token in SHELL_META_TOKENS)


def normalize_python_argv(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    normalized = list(argv)
    if normalized[0] == 'python' and not shutil.which('python'):
        normalized[0] = sys.executable or 'python3'
    return normalized


def extract_structured_argv(trial: dict) -> list[str]:
    argv = trial.get('command_argv', [])
    if isinstance(argv, list) and argv and all(isinstance(item, str) and item for item in argv):
        return normalize_python_argv(argv)
    if trial.get('command_kind') == 'argv' and trial.get('command') and not command_needs_shell(str(trial.get('command', ''))):
        try:
            return normalize_python_argv(shlex.split(str(trial.get('command', ''))))
        except ValueError:
            return []
    return []


def execute_trial_process(project: str, paths, script_dir: Path, repo_path: str, env_name: str, trial: dict, env: dict[str, str]):
    repo_cwd = Path(repo_path) if repo_path else paths.root
    command = str(trial.get('command', ''))
    structured_argv = extract_structured_argv(trial)
    conda_exe = locate_conda_executable()
    env_ready = conda_env_exists(env_name, conda_exe)
    if (not env_name or env_name == 'base') and os.environ.get('ALLOW_CONDA_BASE', '0') != '1':
        return subprocess.CompletedProcess(['refuse-base-env'], 2, '', 'refusing to run trial without a non-base project conda env; set env_name or ALLOW_CONDA_BASE=1 for diagnostics')

    if structured_argv:
        exec_cmd = structured_argv
        display = shlex.join(structured_argv)
        mode = 'structured-argv'
        if env_name and env_ready and conda_exe:
            exec_cmd = [conda_exe, 'run', '-n', env_name, *structured_argv]
            mode = 'conda-run-argv'
        try:
            proc = subprocess.run(exec_cmd, cwd=repo_cwd, env=runtime_env(env), text=True, capture_output=True)
        except FileNotFoundError as exc:
            proc = subprocess.CompletedProcess(exec_cmd, 127, '', f'executable not found: {exc.filename}')
        return proc, display, mode

    if env_name and env_ready:
        wrapped = f'cd {shlex.quote(repo_path)} && {command}'
        exec_cmd = [str(script_dir / 'run_in_conda.sh'), project, '--env-name', env_name, 'bash', '-lc', wrapped]
        proc = subprocess.run(exec_cmd, cwd=paths.root, env=runtime_env(env), text=True, capture_output=True)
        return proc, command, 'shell-fallback-in-conda'

    proc = subprocess.run(['bash', '-lc', command], cwd=repo_cwd, env=runtime_env(env), text=True, capture_output=True)
    return proc, command, 'shell-fallback'


def load_method_overrides(paths) -> dict:
    path = paths.state / 'method_overrides.json'
    if not path.exists():
        return {'methods': {}, 'repos': {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {'methods': {}, 'repos': {}}
    except Exception:
        return {'methods': {}, 'repos': {}}


def coding_backend_enabled(args: argparse.Namespace, cfg: dict) -> str:
    requested = (args.coding_backend or cfg.get('coding_agent', {}).get('backend', 'llm') or 'llm').strip().lower()
    return '' if requested in {'', 'off', 'none', 'disabled'} else requested

def coding_agent_timeout(cfg: dict) -> int:
    acfg = cfg.get('coding_agent', {}) if isinstance(cfg, dict) else {}
    env_value = os.environ.get('CODING_AGENT_TIMEOUT_SEC')
    value = env_value if env_value is not None else acfg.get('timeout_sec') or 14400
    try:
        parsed = max(60, int(value))
    except Exception:
        parsed = 14400
    # Older project configs used 1200s, which is too short for unattended
    # Claude Code experiment repair. Keep explicit env overrides respected.
    return parsed if env_value is not None else max(14400, parsed)


def llm_team_timeout(cfg: dict) -> int:
    acfg = cfg.get('coding_agent', {}) if isinstance(cfg, dict) else {}
    try:
        return max(60, int(os.environ.get('LLM_TEAM_TIMEOUT_SEC') or acfg.get('llm_team_timeout_sec') or 600))
    except Exception:
        return 600




def repo_env_locked(paths, cfg: dict, repo_path: str, env_name: str) -> bool:
    if not repo_path or not env_name:
        return False
    bootstrap = load_repo_env_bootstrap(paths)
    same_env = str(bootstrap.get('env_name', '')) == str(env_name)
    same_repo = str(bootstrap.get('repo_path', '')) == str(repo_path)
    if same_env and same_repo and str(bootstrap.get('status', '')).lower() == 'completed' and conda_env_exists(env_name, cfg=cfg):
        return True
    return conda_env_exists(env_name, cfg=cfg)

def load_repo_env_bootstrap(paths) -> dict:
    path = paths.state / 'repo_env_bootstrap.json'
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def mark_method_env_bootstrap_failed(paths, methods: list[dict], method: dict, reason: str) -> None:
    for trial in method.get('trials', []):
        update_parallel_status(
            paths,
            method.get('method', ''),
            int(trial.get('trial_index', 0)),
            'failed',
            result_summary=f'env_bootstrap_failed: {reason[:240]}',
            decision='env_bootstrap_failed',
        )
    method['status'] = 'failed'
    method['decision'] = 'env_bootstrap_failed'
    save_parallel_plan(paths, methods)


def build_coding_request(method: dict, trial: dict, mode: str, prior_summary: str = '') -> str:
    contract = method.get('method_contract', {}) or method.get('contract', {}) or {}
    selected_contract = method.get('current_find_execution_contract', {}) if isinstance(method.get('current_find_execution_contract'), dict) else {}
    parts = [
        f"mode={mode}",
        f"method={method.get('method', '')}",
        f"selected_plan_id={selected_contract.get('selected_plan_id', '')}",
        f"selected_idea_id={selected_contract.get('selected_idea_id', '')}",
        f"selected_plan_policy={(selected_contract.get('execution_policy') or {}).get('status', '') if isinstance(selected_contract.get('execution_policy'), dict) else ''}",
        f"focus={trial.get('focus', '')}",
        f"dataset={method.get('dataset', '')}",
        f"benchmark={method.get('benchmark', '')}",
        f"metric={method.get('metric', '')}",
        f"claim_to_test={contract.get('claim_to_test', '')}",
        f"novelty_hypothesis={contract.get('novelty_hypothesis', '')}",
        f"counterexample_test={contract.get('counterexample_test', '')}",
        f"bad_case_slices={'; '.join(contract.get('bad_case_slices', [])[:6]) if isinstance(contract.get('bad_case_slices'), list) else contract.get('bad_case_slices', '')}",
        f"validation_command={trial.get('command', '')}",
        f"metrics_path={trial.get('metrics_path', '')}",
        f"bad_case_path={trial.get('bad_case_path', '')}",
        f"audit_path={trial.get('audit_path', '')}",
        f"prior_summary={prior_summary}",
    ]
    return ' | '.join(part for part in parts if part and not part.endswith('='))


def write_trial_context(paths, method: dict, trial: dict, artifact_dir: Path, mode: str) -> Path:
    selected_contract = method.get('current_find_execution_contract', {}) if isinstance(method.get('current_find_execution_contract'), dict) else {}
    context = {
        'project_root': str(paths.root),
        'mode': mode,
        'method': method,
        'trial': trial,
        'current_find_execution_contract': selected_contract,
        'required_outputs': {
            'metrics_path': trial.get('metrics_path', ''),
            'bad_case_path': trial.get('bad_case_path', ''),
            'audit_path': trial.get('audit_path', ''),
        },
        'rules': [
            'Do not fabricate metrics or paper claims.',
            'Validation must run the trial command or explain why it cannot run.',
            'If outputs are missing, repair evidence export before tuning.',
            'Keep code edits minimal and scoped to the selected repo.',
            'If current_find_execution_contract is present, implement/repair only the selected_plan_id; non-selected ideas/plans are backlog and must not drive experiments or paper claims.',
        ],
    }
    path = artifact_dir / f"coding_context_{mode}.json"
    path.write_text(json.dumps(context, indent=2, ensure_ascii=False) + chr(10), encoding='utf-8')
    return path


def run_coding_backend(args: argparse.Namespace, cfg: dict, paths, script_dir: Path, method: dict, trial: dict, repo_path: str, env_name: str, artifact_dir: Path, mode: str, prior_summary: str = '') -> tuple[bool, dict]:
    backend = coding_backend_enabled(args, cfg)
    if not backend or not repo_path or not trial.get('command'):
        return True, {'skipped': True, 'reason': 'coding-backend-disabled-or-missing-input'}
    context_path = write_trial_context(paths, method, trial, artifact_dir, mode)
    request = build_coding_request(method, trial, mode, prior_summary)
    cmd = [
        sys.executable, str(script_dir / 'run_coding_agent.py'),
        '--project', args.project,
        '--method', method.get('method', ''),
        '--repo-path', repo_path,
        '--command', trial.get('command', ''),
        '--request', request,
        '--mode', mode,
        '--trial-json', str(context_path),
        '--max-rounds', str(cfg.get('coding_agent', {}).get('max_repair_rounds', 2) or 2),
        '--backend', backend,
    ]
    if env_name:
        cmd.extend(['--env-name', env_name])
    log_path = artifact_dir / f'coding_agent_{mode}.log'
    rc = run(cmd, paths.root, log_path, timeout=coding_agent_timeout(cfg))
    state_path = paths.state / f"coding_agent_{method.get('method', '')}.json"
    payload = {}
    if state_path.exists():
        try:
            payload = json.loads(state_path.read_text(encoding='utf-8'))
        except Exception:
            payload = {}
    payload['return_code'] = payload.get('return_code', rc)
    payload['log_path'] = str(log_path)
    payload['context_path'] = str(context_path)
    success = bool(payload.get('repair_success', False)) and int(payload.get('return_code', rc) or 0) == 0
    return success, payload


def completed_experiment_keys(paths) -> set[tuple[str, str, str]]:
    rows = load_json(paths.state / 'experiment_registry.json')
    if not isinstance(rows, list):
        return set()
    keys: set[tuple[str, str, str]] = set()
    for row in rows:
        if str(row.get('status', '')).lower() in {'completed', 'success'} and row.get('audit_ready'):
            exp_id = str(row.get('experiment_id') or row.get('name'))
            keys.add((exp_id, str(row.get('repo_path', '')), str(row.get('dataset', ''))))
    return keys


def execute_parallel_plan(args: argparse.Namespace, cfg: dict, paths, script_dir: Path) -> None:
    methods = read_parallel_methods(paths)
    if not methods:
        print('No parallel plan found; skipping execution.', file=sys.stderr)
        return

    methods, selected_contract = enforce_current_find_selected_plan(paths, methods)
    if selected_contract.get('required') and not methods:
        print(f"Current Find selected-plan guard blocked execute-plan: {selected_contract.get('reason')}", file=sys.stderr)
        return

    launch_budget = args.max_launches or sum(max(1, len(row.get('trials', []))) for row in methods)
    already_completed = completed_experiment_keys(paths)
    method_overrides = load_method_overrides(paths)
    override_methods = method_overrides.get('methods', {}) if isinstance(method_overrides, dict) else {}
    override_repos = method_overrides.get('repos', {}) if isinstance(method_overrides, dict) else {}
    launched = 0
    for method in methods:
        method_name = method.get('method', '')
        if override_methods.get(method_name, {}).get('recommendation') in {'pause_or_prune', 'compare_then_prune_or_pause'}:
            update_parallel_status(paths, method_name, 0, 'planned', result_summary='skipped: method paused/pruned by research veto')
            continue
        if not method.get('launch_ready'):
            continue
        repo_path = method.get('repo_path', '')
        if repo_path and override_repos.get(repo_path, {}).get('status') in {'paused_or_abandoned', 'abandoned'}:
            update_parallel_status(paths, method_name, 0, 'planned', result_summary='skipped: repo paused/abandoned by research veto')
            continue
        env_name = method.get('env_name', cfg.get('conda_env', ''))
        method_slug = method.get('method_slug', method.get('method', 'method'))
        if repo_path and not Path(str(repo_path)).exists():
            update_parallel_status(paths, method_name, 0, 'planned', result_summary='skipped: repo_path missing or archived; project Claude must refresh the current-route plan before launch', decision='repo_path_missing_or_archived')
            skip_log = paths.logs / f'env_bootstrap_{method_slug}.log'
            skip_log.write_text('Skipped stale parallel-plan method because repo_path is missing or archived. The workflow must refresh the project plan from current active route before launch.' + chr(10), encoding='utf-8')
            print(skip_log, flush=True)
            continue
        if repo_path:
            run([sys.executable, str(script_dir / 'setup_git_guardrails.py'), '--project', args.project, '--repo-path', repo_path], paths.root, paths.logs / f'git_guardrails_{method_slug}.log')
        if args.prepare_env and repo_path:
            if repo_env_locked(paths, cfg, repo_path, env_name):
                skip_log = paths.logs / f'env_bootstrap_{method_slug}.log'
                skip_log.write_text('Environment is already locked/existing; experiment stage will not reinstall, mutate, or create a new conda env.\n', encoding='utf-8')
                print(skip_log, flush=True)
            else:
                bootstrap_cmd = [
                    sys.executable, str(script_dir / 'bootstrap_repo_env.py'), '--project', args.project,
                    '--repo-path', repo_path, '--env-name', env_name, '--verify-only',
                ]
                if not args.real_bootstrap_env:
                    bootstrap_cmd.append('--prepare-only')
                else:
                    bootstrap_cmd.append('--update-project-config')
                bootstrap_rc = run(bootstrap_cmd, paths.root, paths.logs / f'env_bootstrap_{method_slug}.log', timeout=max(1800, coding_agent_timeout(cfg)))
                bootstrap_state = load_repo_env_bootstrap(paths)
                bootstrap_status = str(bootstrap_state.get('status', '')).lower()
                if args.real_bootstrap_env and (bootstrap_rc != 0 or bootstrap_status != 'completed'):
                    reason = bootstrap_state.get('failed_step') or bootstrap_state.get('missing_import') or bootstrap_state.get('status') or f'return_code={bootstrap_rc}'
                    mark_method_env_bootstrap_failed(paths, methods, method, str(reason))
                    continue

        for trial in method.get('trials', []):
            if launched >= launch_budget:
                return
            experiment_id = str(trial.get('experiment_id', method.get('method', 'experiment')))
            completed_key = (experiment_id, str(repo_path or ''), str(method.get('dataset', '')))
            if completed_key in already_completed:
                update_parallel_status(paths, method.get('method', ''), int(trial.get('trial_index', 0)), 'completed', result_summary='skipped: same repo/dataset experiment already completed and audit-ready')
                continue
            command = trial.get('command', '')
            if not command:
                continue
            artifact_dir = Path(trial.get('artifact_dir', paths.artifacts / method_slug))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            metrics_path = Path(trial.get('metrics_path', artifact_dir / 'metrics.json'))
            bad_case_path = Path(trial.get('bad_case_path', artifact_dir / 'bad_cases.json'))
            audit_path = Path(trial.get('audit_path', artifact_dir / 'audit.json'))
            log_path = artifact_dir / 'stdout_stderr.log'
            env = os.environ.copy()
            env['METHOD'] = method.get('method', '')
            env['METHOD_SLUG'] = method_slug
            env['DATASET'] = method.get('dataset', '')
            env['BENCHMARK'] = method.get('benchmark', '')
            env['METRIC'] = method.get('metric', '')
            env['ARTIFACT_DIR'] = str(artifact_dir)
            env['TRIAL_INDEX'] = str(trial.get('trial_index', ''))
            env['SEED'] = str(trial.get('seed', ''))
            env['METRICS_PATH'] = str(metrics_path)
            env['BAD_CASE_PATH'] = str(bad_case_path)
            env['AUDIT_PATH'] = str(audit_path)
            if not trial.get('implementation_ready'):
                update_parallel_status(paths, method.get('method', ''), int(trial.get('trial_index', 0)), 'running', result_summary='coding backend implementing/adapting method before official trial')
                impl_success, impl_payload = run_coding_backend(args, cfg, paths, script_dir, method, trial, repo_path, env_name, artifact_dir, 'implement')
                trial['implementation_ready'] = bool(impl_success)
                trial['implementation_backend'] = impl_payload.get('backend', impl_payload.get('requested_backend', coding_backend_enabled(args, cfg)))
                trial['implementation_state_path'] = str(paths.state / f"coding_agent_{method.get('method', '')}.json")
                trial['implementation_log_path'] = impl_payload.get('log_path', '')
                save_parallel_plan(paths, methods)
                if not impl_success:
                    result_summary = f"implementation_failed; backend={trial.get('implementation_backend', '')}; reason={(impl_payload.get('stderr') or impl_payload.get('backend_fallback_reason') or impl_payload.get('stdout') or impl_payload.get('return_code', 'unknown'))[:240]}"
                    update_parallel_status(paths, method.get('method', ''), int(trial.get('trial_index', 0)), 'failed', result_summary=result_summary, decision='implementation_failed')
                    launched += 1
                    continue

            started_at = dt.datetime.now(dt.timezone.utc).isoformat()
            update_parallel_status(paths, method.get('method', ''), int(trial.get('trial_index', 0)), 'running', result_summary='official trial running after coding backend preparation')
            start = time.time()
            proc, command_display, execution_mode = execute_trial_process(args.project, paths, script_dir, repo_path, env_name, trial, env)
            duration = time.time() - start
            finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
            log_path.write_text(proc.stdout + '\n--- STDERR ---\n' + proc.stderr, encoding='utf-8')

            audit_payload, audit_issues = load_audit_payload(audit_path)
            metrics = audit_payload.get('metrics', {}) if isinstance(audit_payload, dict) else {}
            if not metrics:
                metrics = load_metrics_fallback(metrics_path)
            metric_value = derive_metric_value(metrics, method.get('metric', ''))
            audit_ready = len(audit_issues) == 0
            status = 'completed' if proc.returncode == 0 and audit_ready else 'incomplete_audit' if proc.returncode == 0 else 'failed'
            result_summary = metric_value or f'return_code={proc.returncode}'
            if audit_issues:
                result_summary = f"{result_summary}; audit={'; '.join(audit_issues)}"
            notes = f"focus={trial.get('focus', '')}; command_source={trial.get('command_source', '')}; execution_mode={execution_mode}"

            log_cmd = [
                sys.executable, str(script_dir / 'log_experiment.py'), '--project', args.project,
                '--name', trial.get('experiment_id', method.get('method', 'experiment')),
                '--experiment-id', trial.get('experiment_id', method.get('method', 'experiment')),
                '--repo', method.get('repo_name', ''), '--repo-path', repo_path,
                '--dataset', method.get('dataset', ''), '--benchmark', method.get('benchmark', ''),
                '--method', method.get('method', ''), '--method-slug', method_slug,
                '--status', status, '--metric', method.get('metric', ''), '--metric-value', metric_value,
                '--result', result_summary, '--notes', notes, '--artifact_path', str(artifact_dir),
                '--env-name', env_name, '--command', command_display, '--return-code', str(proc.returncode),
                '--duration-sec', str(round(duration, 3)), '--started-at', started_at, '--finished-at', finished_at,
                '--trial-index', str(trial.get('trial_index', 0)), '--priority', str(method.get('priority', 0)),
                '--method-role', str(method.get('method_role') or trial.get('method_role') or ''),
                '--comparison-role', str(method.get('comparison_role') or trial.get('comparison_role') or method.get('method_role') or ''),
                '--human-label', str(method.get('human_label') or ''),
                '--human-goal', str(method.get('human_goal') or (method.get('claim_contract', {}).get('claim_to_test', '') if isinstance(method.get('claim_contract', {}), dict) else '')),
                '--config-summary', str(method.get('config_summary') or ''),
                '--bad-case-path', str(bad_case_path) if bad_case_path.exists() else '',
                '--claim-verdict', str(audit_payload.get('claim_verdict', '')),
                '--novelty-note', str(audit_payload.get('novelty_note', '')),
                '--counterexample-outcome', str(audit_payload.get('counterexample_outcome', '')),
                '--audit-path', str(audit_path), '--audit-ready', 'true' if audit_ready else 'false',
                '--missing-audit-fields', ','.join(audit_issues),
            ]
            if metrics_path.exists():
                log_cmd.extend(['--metrics-path', str(metrics_path)])
            run(log_cmd, paths.root, artifact_dir / 'log_experiment.log')
            decision = ''
            failure_data = {}
            if status != 'completed':
                fail_cmd = [
                    sys.executable, str(script_dir / 'analyze_experiment_failures.py'), '--project', args.project,
                    '--method', method.get('method', ''), '--experiment-id', trial.get('experiment_id', method.get('method', 'experiment')),
                    '--result-summary', result_summary,
                ]
                if bad_case_path.exists():
                    fail_cmd.extend(['--bad-case-file', str(bad_case_path)])
                run(fail_cmd, paths.root, artifact_dir / 'failure_analysis.log')
                failure_json = paths.state / f"failure_analysis_{method.get('method', '')}.json"
                if failure_json.exists():
                    failure_data = json.loads(failure_json.read_text(encoding='utf-8'))
                    decision = failure_data.get('recommendation', '')

                requested_backend = coding_backend_enabled(args, cfg)
                if requested_backend and repo_path and command:
                    repair_summary = ' | '.join(part for part in [
                        f"result_summary={result_summary}",
                        f"recommendation={failure_data.get('recommendation', '')}",
                        f"causes={'; '.join(failure_data.get('causes', [])[:4])}",
                        f"actions={'; '.join(failure_data.get('recommended_actions', [])[:4])}",
                        f"bad_case_slices={', '.join(failure_data.get('bad_case_slices', [])[:8])}",
                    ] if part and not part.endswith('='))
                    repair_success, coding_payload = run_coding_backend(args, cfg, paths, script_dir, method, trial, repo_path, env_name, artifact_dir, 'repair', prior_summary=repair_summary)
                    backend_used = coding_payload.get('backend', requested_backend)
                    if repair_success:
                        update_parallel_status(
                            paths,
                            method.get('method', ''),
                            int(trial.get('trial_index', 0)),
                            'running',
                            result_summary=f'repair validated by {backend_used}; rerunning trial for evidence',
                            decision='repair_validated_rerunning',
                        )
                        retry_started_at = dt.datetime.now(dt.timezone.utc).isoformat()
                        retry_start = time.time()
                        retry_proc, retry_command_display, retry_execution_mode = execute_trial_process(args.project, paths, script_dir, repo_path, env_name, trial, env)
                        retry_duration = time.time() - retry_start
                        retry_finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
                        (artifact_dir / 'stdout_stderr_after_repair.log').write_text(retry_proc.stdout + '\n--- STDERR ---\n' + retry_proc.stderr, encoding='utf-8')

                        audit_payload, audit_issues = load_audit_payload(audit_path)
                        metrics = audit_payload.get('metrics', {}) if isinstance(audit_payload, dict) else {}
                        if not metrics:
                            metrics = load_metrics_fallback(metrics_path)
                        metric_value = derive_metric_value(metrics, method.get('metric', ''))
                        audit_ready = len(audit_issues) == 0
                        status = 'completed' if retry_proc.returncode == 0 and audit_ready else 'incomplete_audit' if retry_proc.returncode == 0 else 'failed'
                        result_summary = metric_value or f'return_code={retry_proc.returncode}'
                        if audit_issues:
                            result_summary = f"{result_summary}; audit={'; '.join(audit_issues)}"
                        result_summary = f"{result_summary}; repair=validated; backend={backend_used}; rerun_after_repair={status}"
                        notes = f"focus={trial.get('focus', '')}; command_source={trial.get('command_source', '')}; execution_mode={retry_execution_mode}; rerun_after_repair=true"
                        retry_log_cmd = [
                            sys.executable, str(script_dir / 'log_experiment.py'), '--project', args.project,
                            '--name', trial.get('experiment_id', method.get('method', 'experiment')),
                            '--experiment-id', trial.get('experiment_id', method.get('method', 'experiment')),
                            '--repo', method.get('repo_name', ''), '--repo-path', repo_path,
                            '--dataset', method.get('dataset', ''), '--benchmark', method.get('benchmark', ''),
                            '--method', method.get('method', ''), '--method-slug', method_slug,
                            '--status', status, '--metric', method.get('metric', ''), '--metric-value', metric_value,
                            '--result', result_summary, '--notes', notes, '--artifact_path', str(artifact_dir),
                            '--env-name', env_name, '--command', retry_command_display, '--return-code', str(retry_proc.returncode),
                            '--duration-sec', str(round(retry_duration, 3)), '--started-at', retry_started_at, '--finished-at', retry_finished_at,
                            '--trial-index', str(trial.get('trial_index', 0)), '--priority', str(method.get('priority', 0)),
                '--method-role', str(method.get('method_role') or trial.get('method_role') or ''),
                '--comparison-role', str(method.get('comparison_role') or trial.get('comparison_role') or method.get('method_role') or ''),
                '--human-label', str(method.get('human_label') or ''),
                '--human-goal', str(method.get('human_goal') or (method.get('claim_contract', {}).get('claim_to_test', '') if isinstance(method.get('claim_contract', {}), dict) else '')),
                '--config-summary', str(method.get('config_summary') or ''),
                            '--bad-case-path', str(bad_case_path) if bad_case_path.exists() else '',
                            '--claim-verdict', str(audit_payload.get('claim_verdict', '')),
                            '--novelty-note', str(audit_payload.get('novelty_note', '')),
                            '--counterexample-outcome', str(audit_payload.get('counterexample_outcome', '')),
                            '--audit-path', str(audit_path), '--audit-ready', 'true' if audit_ready else 'false',
                            '--missing-audit-fields', ','.join(audit_issues),
                        ]
                        if metrics_path.exists():
                            retry_log_cmd.extend(['--metrics-path', str(metrics_path)])
                        run(retry_log_cmd, paths.root, artifact_dir / 'log_experiment_after_repair.log')
                        decision = 'repair_relaunch_completed' if status == 'completed' else 'repair_relaunch_failed_or_incomplete'
                    else:
                        repair_reason = coding_payload.get('stderr', '') or coding_payload.get('backend_fallback_reason', '') or f"return_code={coding_payload.get('return_code', 'unknown')}"
                        result_summary = f"{result_summary}; repair=failed; backend={backend_used}; reason={repair_reason[:200]}"
            update_parallel_status(paths, method.get('method', ''), int(trial.get('trial_index', 0)), status, result_summary=result_summary, decision=decision)
            launched += 1


def main() -> int:
    args = parse_args()
    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name)
    if guard_rc is not None:
        return guard_rc
    cfg = load_project_config(args.project)
    paths = build_paths(args.project)

    topic = effective_project_topic(args.project, args.topic, cfg, paths)
    max_results = args.max_results or cfg.get('discovery', {}).get('arxiv', {}).get('max_results', 5)
    discover_retries = args.discover_retries if args.discover_retries is not None else cfg.get('loop', {}).get('discover_retries', 1)
    script_dir = Path(__file__).resolve().parent

    if args.skip_discovery:
        os.environ.setdefault('USE_EXISTING_LITERATURE_PACKET', '1')

    run([sys.executable, str(script_dir / 'init_project.py'), '--project', args.project], paths.root, paths.logs / '01_init.log')
    run([sys.executable, str(script_dir / 'detect_machine_profile.py'), '--project', args.project], paths.root, paths.logs / '01b_machine_profile.log')
    ready, readiness_issues = check_runtime_readiness(paths, args)
    if not ready:
        run([sys.executable, str(script_dir / 'research_healthcheck.py'), '--project', args.project], paths.root, paths.logs / '01c_healthcheck.log')
        print('runtime readiness check failed:', file=sys.stderr)
        for issue in readiness_issues:
            print(f'- {issue}', file=sys.stderr)
        return 2

    discovery_mode = []
    if not args.skip_discovery:
        run([sys.executable, str(script_dir / 'plan_literature_review.py'), '--project', args.project], paths.root, paths.logs / '01d_plan_literature_review.log')
        cfg = load_project_config(args.project)
        source_selection = canonical_source_selection()
        queries = planned_discovery_queries(cfg, paths, topic, max_queries=int(cfg.get('discovery', {}).get('max_planned_queries', 6) or 6), project=args.project)
        arxiv_enabled = bool(source_selection.get('include_arxiv')) and not args.skip_arxiv
        if arxiv_enabled:
            for index, query in enumerate(queries, start=1):
                code = run([sys.executable, str(script_dir / 'discover_arxiv.py'), '--project', args.project, '--query', query, '--max-results', str(max_results), '--retries', str(discover_retries)], paths.root, paths.logs / f'02_discover_arxiv_{index}.log', timeout=int(os.environ.get('DISCOVER_ARXIV_TIMEOUT_SEC', '45')))
                if code == 0 and 'arxiv' not in discovery_mode:
                    discovery_mode.append('arxiv')
                elif code != 0:
                    print(f'arxiv discovery failed for query={query}; continuing', file=sys.stderr)

        ss_enabled = 'semantic_scholar' in cfg.get('discovery', {}).get('enabled_sources', []) and not args.skip_semantic_scholar
        if ss_enabled:
            ss_budget = int(cfg.get('discovery', {}).get('semantic_scholar', {}).get('query_budget', 3) or 3)
            for index, query in enumerate(queries[:ss_budget], start=1):
                ss_code = run([sys.executable, str(script_dir / 'discover_semantic_scholar.py'), '--project', args.project, '--query', query], paths.root, paths.logs / f'03_discover_semantic_schol{index}.log')
                if ss_code == 0 and 'semantic_scholar' not in discovery_mode:
                    discovery_mode.append('semantic_scholar')

        github_enabled = bool(source_selection.get('include_github')) and not args.skip_github
        if github_enabled:
            github_queries = [query for query in queries if any(token in query.lower() for token in ['code', 'github', 'repository', 'repo', 'implementation', 'open-source', 'opensource'])][:3] or queries[:2]
            for index, query in enumerate(github_queries, start=1):
                gh_code = run([sys.executable, str(script_dir / 'discover_github_repos.py'), '--project', args.project, '--query', query], paths.root, paths.logs / f'03b_discover_github_{index}.log', timeout=int(os.environ.get('DISCOVER_GITHUB_TIMEOUT_SEC', '45')))
                if gh_code == 0 and 'github' not in discovery_mode:
                    discovery_mode.append('github')

        run([sys.executable, str(script_dir / 'ingest_discovery.py'), '--project', args.project, '--limit', str(cfg.get('loop', {}).get('ingest_limit', 5))], paths.root, paths.logs / '04_ingest.log')
    else:
        discovery_mode.append('skipped')

    if not args.skip_initialization:
        if not args.skip_llm:
            if args.skip_discovery:
                os.environ.setdefault('USE_EXISTING_LITERATURE_PACKET', '1')
                skip_msg = 'finding frontend skipped because --skip-discovery is active; reusing existing canonical Find/literature packet. Controlled targeted literature repair remains available through scripts/run_literature_tool.py.'
                print(skip_msg, flush=True)
                (paths.logs / '05a_finding_frontend.log').write_text(skip_msg + '\n', encoding='utf-8')
            else:
                taste_cmd = [sys.executable, str(script_dir / 'run_frontend.py'), '--project', args.project, '--timeout-sec', str(int(os.environ.get('TIMEOUT_SEC', '3600')))]
                if args.deep_literature_survey:
                    taste_cmd.append('--deep-survey')
                else:
                    taste_cmd.append('--fast-mode')
                run(taste_cmd, paths.root, paths.logs / '05a_finding_frontend.log', timeout=int(os.environ.get('TIMEOUT_SEC', '3600')) + 120)
            run([sys.executable, str(script_dir / 'sync_outputs.py'), '--project', args.project, '--allow-empty'], paths.root, paths.logs / '05a_taste_sync.log', timeout=120)
            run([sys.executable, str(script_dir / 'build_literature_tool_packet.py'), '--project', args.project] + (['--venue', args.venue] if args.venue else []), paths.root, paths.logs / '05ab_literature_tool_packet.log', timeout=180)
            run([sys.executable, str(script_dir / 'run_llm_research_team.py'), '--project', args.project, '--topic', topic, '--roles', 'planner,researcher,critic', '--context-limit', '18000'] + (['--venue', args.venue] if args.venue else []), paths.root, paths.logs / '05aa_llm_research_team_preplan.log', timeout=llm_team_timeout(cfg))
        run([sys.executable, str(script_dir / 'prepare_initialization.py'), '--project', args.project], paths.root, paths.logs / '05_prepare_initialization.log')
        run([sys.executable, str(script_dir / 'build_hypothesis_arena.py'), '--project', args.project], paths.root, paths.logs / '05c_hypothesis_arena.log')
        run([sys.executable, str(script_dir / 'assess_idea_candidates.py'), '--project', args.project], paths.root, paths.logs / '05ca_idea_candidates.log')
        run([sys.executable, str(script_dir / 'build_workflow_blueprint.py'), '--project', args.project], paths.root, paths.logs / '05d_workflow_blueprint.log')

    if args.parallel_method and args.dataset and args.benchmark and args.metric:
        plan_cmd = [
            sys.executable, str(script_dir / 'plan_experiments.py'), '--project', args.project,
            '--dataset', args.dataset, '--benchmark', args.benchmark, '--metric', args.metric,
            '--methods', *args.parallel_method,
        ]
        if args.repo_name:
            plan_cmd.extend(['--repo-name', args.repo_name])
        if args.repo_path:
            plan_cmd.extend(['--repo-path', args.repo_path])
        if args.command_template:
            plan_cmd.extend(['--command-template', args.command_template])
        run(plan_cmd, paths.root, paths.logs / '05b_parallel_plan.log')

    run([sys.executable, str(script_dir / 'bootstrap_wiki.py'), '--project', args.project], paths.root, paths.logs / '06_bootstrap_wiki.log')
    run([sys.executable, str(script_dir / 'assess_paper_quality.py'), '--project', args.project], paths.root, paths.logs / '07_assess_paper_quality.log')
    run([sys.executable, str(script_dir / 'build_literature_tool_packet.py'), '--project', args.project] + (['--venue', args.venue] if args.venue else []), paths.root, paths.logs / '07aa_literature_tool_packet_refresh.log', timeout=180)
    run([sys.executable, str(script_dir / 'assess_repo_candidates.py'), '--project', args.project], paths.root, paths.logs / '07a_assess_repo_candidates.log')
    run([sys.executable, str(script_dir / 'assess_idea_candidates.py'), '--project', args.project], paths.root, paths.logs / '07b_assess_idea_candidates.log')
    run([sys.executable, str(script_dir / 'audit_reference_reproduction.py'), '--project', args.project] + (['--venue', args.venue] if args.venue else []), paths.root, paths.logs / '07c_reference_reproduction_gate.log')
    run([sys.executable, str(script_dir / 'refresh_index_and_log.py'), '--project', args.project, '--log-entry', f'iteration topic={topic} discovery={discovery_mode or ["manual_only"]}'], paths.root, paths.logs / '08_refresh_index.log')
    run([sys.executable, str(script_dir / 'compile_prompt.py'), '--project', args.project], paths.root, paths.logs / '09_compile_prompt.log')

    llm_enabled = cfg.get('loop', {}).get('llm_enabled', True) and not args.skip_llm
    llm_status = 'skipped'
    if llm_enabled:
        if llm_available(cfg):
            code = run([sys.executable, str(script_dir / 'run_llm_compile.py'), '--project', args.project], paths.root, paths.logs / '10_run_llm_compile.log')
            llm_status = f'exit_{code}'
        else:
            llm_status = f'llm-unavailable-bootstrap-only:{llm_disabled_reason(cfg)}'

    if args.execute_plan:
        execute_parallel_plan(args, cfg, paths, script_dir)
        run([sys.executable, str(script_dir / 'propose_next_actions.py'), '--project', args.project], paths.root, paths.logs / '12b_next_actions.log')
        run([sys.executable, str(script_dir / 'build_method_frontier.py'), '--project', args.project], paths.root, paths.logs / '12c_method_frontier.log')
        run([sys.executable, str(script_dir / 'build_aris_review_board.py'), '--project', args.project], paths.root, paths.logs / '12e_aris_review_board.log')
        run([sys.executable, str(script_dir / 'build_hypothesis_arena.py'), '--project', args.project], paths.root, paths.logs / '12d_refresh_hypothesis_arena.log')
        run([sys.executable, str(script_dir / 'audit_reference_reproduction.py'), '--project', args.project] + (['--venue', args.venue] if args.venue else []), paths.root, paths.logs / '12f_reference_reproduction_gate.log')

    run([sys.executable, str(script_dir / 'lint_wiki.py'), '--project', args.project], paths.root, paths.logs / '11_lint.log')
    run([sys.executable, str(script_dir / 'research_healthcheck.py'), '--project', args.project], paths.root, paths.logs / '12_healthcheck.log')
    run([sys.executable, str(script_dir / 'audit_workflow_connectivity.py'), '--project', args.project], paths.root, paths.logs / '12c_workflow_connectivity.log')
    run([sys.executable, str(script_dir / 'reflect_iteration.py'), '--project', args.project], paths.root, paths.logs / '13_reflect_iteration.log')
    run([sys.executable, str(script_dir / 'audit_experiment_iteration.py'), '--project', args.project], paths.root, paths.logs / '13a_experiment_iteration_audit.log')
    run([sys.executable, str(script_dir / 'update_evolution_memory.py'), '--project', args.project] + (['--venue', args.venue] if args.venue else []), paths.root, paths.logs / '13b_evolution_memory.log')
    run([sys.executable, str(script_dir / 'build_research_trajectory_system.py'), '--project', args.project] + (['--venue', args.venue] if args.venue else []), paths.root, paths.logs / '13d_research_trajectory_system.log')
    run([sys.executable, str(script_dir / 'audit_paper_evidence.py'), '--project', args.project] + (['--venue', args.venue] if args.venue else []), paths.root, paths.logs / '13e_paper_evidence_gate.log')
    run([sys.executable, str(script_dir / 'build_blocker_action_plan.py'), '--project', args.project] + (['--venue', args.venue] if args.venue else []), paths.root, paths.logs / '13f_blocker_action_plan.log')
    if not args.skip_llm:
        run([sys.executable, str(script_dir / 'run_llm_research_team.py'), '--project', args.project, '--topic', topic, '--roles', 'planner,researcher,critic', '--context-limit', '18000'] + (['--venue', args.venue] if args.venue else []), paths.root, paths.logs / '13c_llm_research_team_postreflect.log', timeout=llm_team_timeout(cfg))
    run([sys.executable, str(script_dir / 'compile_prompt.py'), '--project', args.project], paths.root, paths.logs / '14_recompile_prompt.log')
    export_code = run([sys.executable, str(script_dir / 'export_obsidian.py'), '--project', args.project], paths.root, paths.logs / '15_export_obsidian.log')
    status_code = run([sys.executable, str(script_dir / 'report_status.py'), '--project', args.project], paths.root, paths.logs / '16_status.log')

    history = load_json(paths.state / 'loop_history.json')
    history.append({
        'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(),
        'topic': topic,
        'max_results': max_results,
        'discover_retries': discover_retries,
        'discovery_mode': discovery_mode or ['manual_only'],
        'conda_env': cfg.get('conda_env', ''),
        'parallel_methods': args.parallel_method,
        'executed_plan': args.execute_plan,
        'llm_status': llm_status,
        'coding_backend': args.coding_backend or cfg.get('coding_agent', {}).get('backend', 'llm'),
        'export_exit': export_code,
        'status_exit': status_code,
    })
    save_json(paths.state / 'loop_history.json', history)
    run([sys.executable, str(script_dir / 'generate_handoff.py'), '--project', args.project], paths.root, paths.logs / '17_generate_handoff.log')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
