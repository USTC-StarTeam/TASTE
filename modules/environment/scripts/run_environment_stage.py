#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from project_paths import ROOT, build_paths, load_project_config
from run_project import current_find_execution_contract

from taste_pythonpath import ensure_taste_pythonpath
ensure_taste_pythonpath(ROOT)
from auto_research.source_selection import canonical_source_selection


def module_cmd(stage: str, action: str, *extra: str) -> list[str]:
    return [sys.executable, 'framework/scripts/run_module.py', stage, '--action', action, *extra]


def replace_arg_value(cmd: list[str], key: str, value: str) -> list[str]:
    out = list(cmd)
    if key in out:
        idx = out.index(key)
        if idx + 1 < len(out):
            out[idx + 1] = value
        else:
            out.append(value)
    else:
        out.extend([key, value])
    return out


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default




def adapter_contract_diagnostics(paths) -> dict:
    """Summarize structural adapter/probe problems for project Claude repair.

    This does not promote a repo or mark data ready. It only explains why a
    project-local adapter's own output is internally inconsistent, so the next
    pending-loader repair pass can fix the adapter instead of guessing from a
    vague blocker such as "Unknown import blockers".
    """
    diagnostics: list[dict] = []

    def inspect_payload(label: str, payload: dict, path: Path) -> None:
        if not isinstance(payload, dict) or not payload:
            return
        status = str(payload.get('status') or '').strip()
        decision = str(payload.get('decision') or '').strip()
        blocker_reasons = payload.get('blocker_reasons') if isinstance(payload.get('blocker_reasons'), list) else []
        summary = payload.get('import_probe_summary') if isinstance(payload.get('import_probe_summary'), dict) else {}
        if not summary:
            summary = payload.get('repo_import_probe') if isinstance(payload.get('repo_import_probe'), dict) else {}
        data_probe = summary.get('data_probe') if isinstance(summary.get('data_probe'), dict) else {}
        imports = summary.get('imports') if isinstance(summary.get('imports'), dict) else {}
        repo_keys = [
            'repo_dataset_imports',
            'repo_model_import',
            'repo_utils_import',
            'repo_lerp_fm_import',
            'repo_slerp_fm_import',
            'repo_iso3_import',
            'repo_allatom_import',
        ]
        observed_repo_keys = [key for key in repo_keys if key in summary]
        repo_imports_ok = bool(observed_repo_keys) and all(str(summary.get(key) or '') == 'ok' for key in observed_repo_keys)
        data_loaded_ok = bool(data_probe.get('perturb_data_pt_loaded') or data_probe.get('md_data_pt_loaded') or data_probe.get('loader_probe_success'))
        success = payload.get('loader_probe_success') is True or summary.get('success') is True or any(
            isinstance(row, dict) and (row.get('claim_ready') or row.get('loader_probe_success') or (isinstance(row.get('loader_probe'), dict) and row.get('loader_probe', {}).get('success')))
            for row in (payload.get('probes') if isinstance(payload.get('probes'), list) else [])
        )
        issues: list[str] = []
        if repo_imports_ok and data_loaded_ok and not success:
            issues.append('repo_imports_and_data_load_pass_but_adapter_reports_failure')
        if (repo_imports_ok or data_loaded_ok) and not imports:
            issues.append('adapter_import_probe_summary_missing_imports_map')
        if any('Unknown import blockers' in str(item) for item in blocker_reasons):
            issues.append('adapter_reports_unknown_import_blockers')
        if issues:
            diagnostics.append({
                'label': label,
                'path': str(path),
                'status': status,
                'decision': decision,
                'issues': issues,
                'repo_imports_ok': repo_imports_ok,
                'data_loaded_ok': data_loaded_ok,
                'success': bool(success),
                'observed_repo_import_keys': observed_repo_keys,
                'import_keys_present': sorted(imports.keys())[:40],
                'blocker_reasons': blocker_reasons[:8],
                'required_adapter_repair': [
                    'Make the adapter output self-explaining: include an imports/package_checks mapping used by its success logic.',
                    'Do not leave success=false with Unknown import blockers when repo imports and real data loading pass; either set success/claim_ready true or report the exact failing module/field.',
                    'After editing, rerun the TASTE module command that dispatches the adapter and inspect the resulting real_dataset_probe/candidate_loader_probe JSON.',
                ],
            })

    real_path = paths.state / 'real_dataset_probe.json'
    inspect_payload('real_dataset_probe', load_json(real_path, {}), real_path)
    for candidate_path in sorted(paths.state.glob('candidate_loader_probe_*.json')):
        inspect_payload(candidate_path.stem, load_json(candidate_path, {}), candidate_path)
    return {
        'has_blocking_adapter_contract_issues': bool(diagnostics),
        'diagnostics': diagnostics[:8],
    }

def infer_repo(paths, explicit: str = '') -> str:
    if explicit:
        return explicit
    if current_env_selection_valid(paths):
        selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
        selected = selection.get('selected', {}) if isinstance(selection, dict) and isinstance(selection.get('selected'), dict) else {}
        for key in ('repo_path', 'local_path', 'path'):
            repo = str(selected.get(key) or '').strip()
            if repo and Path(repo).exists():
                return str(Path(repo).resolve())
        plan = load_json(paths.state / 'fresh_base_implementation_plan.json', {})
        repo_obj = plan.get('repo', {}) if isinstance(plan, dict) and isinstance(plan.get('repo'), dict) else {}
        repo = str(repo_obj.get('repo_path') or repo_obj.get('local_path') or repo_obj.get('path') or '').strip()
        if repo and Path(repo).exists():
            return str(Path(repo).resolve())
    active = load_json(paths.state / 'active_repo.json', {})
    if isinstance(active, dict) and active.get('repo_path') and Path(active['repo_path']).exists():
        return str(active['repo_path'])
    plan = load_json(paths.state / 'parallel_plan.json', {})
    methods = plan.get('methods', []) if isinstance(plan, dict) else plan if isinstance(plan, list) else []
    for method in methods:
        repo = method.get('repo_path') if isinstance(method, dict) else ''
        if repo and Path(repo).exists():
            return str(repo)
    rows = load_json(paths.state / 'repo_candidates.json', [])
    for row in rows if isinstance(rows, list) else []:
        repo = row.get('local_path') if isinstance(row, dict) else ''
        if repo and Path(repo).exists():
            return str(repo)
    raise SystemExit('No local repo is available yet. Run TASTE initialization and repo candidate assessment first.')




def current_find_run_id(paths) -> str:
    progress = load_json(paths.planning / 'finding' / 'find_progress.json', {})
    if isinstance(progress, dict) and str(progress.get('run_id') or '').strip():
        return str(progress.get('run_id') or '').strip()
    fresh = load_json(paths.state / 'fresh_research_base.json', {})
    return str(fresh.get('fresh_find_run_id') or '').strip() if isinstance(fresh, dict) else ''


def current_selected_execution_ids(paths) -> tuple[str, str]:
    try:
        contract = current_find_execution_contract(paths)
    except Exception:
        return '', ''
    if not isinstance(contract, dict):
        return '', ''
    return str(contract.get('selected_plan_id') or '').strip(), str(contract.get('selected_idea_id') or '').strip()


def current_selected_plan_id(paths) -> str:
    selected_plan_id, _ = current_selected_execution_ids(paths)
    return selected_plan_id


def _claim_ready_dataset_names(row: dict) -> list[str]:
    if not isinstance(row, dict):
        return []
    names: list[str] = []
    for key in ['claim_ready_dataset', 'dataset']:
        value = str(row.get(key) or '').strip()
        if value and value not in names:
            names.append(value)
    for key in ['claim_ready_datasets', 'ready_datasets']:
        values = row.get(key)
        if isinstance(values, str):
            values = [values]
        for item in values if isinstance(values, list) else []:
            value = str(item or '').strip()
            if value and value not in names:
                names.append(value)
    probe_summary = row.get('probe_summary') if isinstance(row.get('probe_summary'), dict) else {}
    values = probe_summary.get('claim_ready_datasets')
    if isinstance(values, str):
        values = [values]
    for item in values if isinstance(values, list) else []:
        value = str(item or '').strip()
        if value and value not in names:
            names.append(value)
    return names


def _pending_loader_selection(selection: dict, selected: dict) -> bool:
    gate = str(selection.get('selection_gate') or selected.get('selection_gate') or '').strip() if isinstance(selection, dict) else ''
    return bool(
        gate in {'accepted_by_claude_transformable_pending_loader_bootstrap', 'blocked_pending_data_loader_for_claude_best_candidate', 'blocked_candidate_base_switch_gate_required'}
        or selected.get('pending_loader_bootstrap')
        or (isinstance(selection.get('pending_environment_candidate'), dict) and not _claim_ready_dataset_names(selected))
    )


def _route_path(row: dict) -> str:
    if not isinstance(row, dict):
        return ''
    for key in ['repo_path', 'local_path', 'path', 'proposed_path_hint']:
        value = str(row.get(key) or '').strip()
        if value:
            return value
    return ''


def _route_names(row: dict) -> set[str]:
    if not isinstance(row, dict):
        return set()
    return {str(row.get(key) or '').strip() for key in ['name', 'repo', 'repo_name', 'full_name'] if str(row.get(key) or '').strip()}


def _route_matches(row: dict, other: dict) -> bool:
    row_path = _route_path(row)
    other_path = _route_path(other)
    if row_path and other_path and row_path == other_path:
        return True
    names = _route_names(row)
    other_names = _route_names(other)
    return bool(names and other_names and names.intersection(other_names))


def _base_switch_authorized_for(paths, selected: dict) -> bool:
    gate = load_json(paths.state / 'base_switch_gate.json', {})
    execution = load_json(paths.state / 'base_switch_execution.json', {})
    candidate = gate.get('candidate_route') if isinstance(gate, dict) and isinstance(gate.get('candidate_route'), dict) else {}
    return bool(
        isinstance(gate, dict)
        and gate.get('status') == 'pass'
        and gate.get('decision') in {'authorize_base_switch', 'base_switch_authorized'}
        and gate.get('switch_authorized') is True
        and _route_matches(selected, candidate)
        and isinstance(execution, dict)
        and str(execution.get('status') or '').startswith('authorized_by_deterministic_base_switch_gate')
    )


def _unresolved_base_switch_candidate(paths, selected: dict) -> bool:
    if not isinstance(selected, dict) or not selected:
        return False
    gate = load_json(paths.state / 'base_switch_gate.json', {})
    candidate = gate.get('candidate_route') if isinstance(gate, dict) and isinstance(gate.get('candidate_route'), dict) else {}
    if not _route_matches(selected, candidate):
        return False
    if _base_switch_authorized_for(paths, selected):
        return False
    current = gate.get('current_selected_route') if isinstance(gate, dict) and isinstance(gate.get('current_selected_route'), dict) else {}
    current_path = _route_path(current)
    selected_path = _route_path(selected)
    return bool(current_path and selected_path and current_path != selected_path)


def current_env_selection_valid(paths) -> bool:
    run_id = current_find_run_id(paths)
    selected_plan_id = current_selected_plan_id(paths)
    if not selected_plan_id:
        return False
    selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    if not isinstance(selection, dict):
        return False
    selected = selection.get('selected') if isinstance(selection.get('selected'), dict) else {}
    selected_run = str(selection.get('fresh_find_run_id') or selected.get('fresh_find_run_id') or '').strip()
    selected_route_run = str(selected.get('fresh_find_run_id') or '').strip()
    selection_plan_id = str(selection.get('selected_plan_id') or selected.get('selected_plan_id') or '').strip()
    selected_route_plan_id = str(selected.get('selected_plan_id') or '').strip()
    stage = str(selection.get('selection_stage') or selection.get('selected_by_stage') or selected.get('selection_stage') or '').strip()
    pending_loader = _pending_loader_selection(selection, selected)
    unresolved_base_switch = _unresolved_base_switch_candidate(paths, selected)
    claim_ready = bool(_claim_ready_dataset_names(selected))
    accepted = bool(
        not pending_loader
        and not unresolved_base_switch
        and claim_ready
        and (
            str(selection.get('selection_gate') or '').startswith(('accepted_by_claude', 'accepted_by_deterministic_base_switch_gate'))
            or (isinstance(selection.get('claude_topic_decision'), dict) and selection['claude_topic_decision'].get('accept_as_current_best'))
        )
    )
    return bool(
        run_id
        and selected_plan_id
        and selection_plan_id == selected_plan_id
        and selected_route_plan_id == selected_plan_id
        and selected
        and selected_run == run_id
        and selected_route_run == run_id
        and stage == 'environment_claude_code'
        and accepted
    )


BASE_SWITCH_SELECTION_GATE = 'blocked_candidate_base_switch_gate_required'


def _candidate_from_base_switch_selection(paths, selection: dict) -> dict:
    pending = selection.get('pending_environment_candidate') if isinstance(selection.get('pending_environment_candidate'), dict) else {}
    if pending:
        return pending
    gate = load_json(paths.state / 'base_switch_gate.json', {})
    candidate = gate.get('candidate_route') if isinstance(gate, dict) and isinstance(gate.get('candidate_route'), dict) else {}
    return candidate if isinstance(candidate, dict) else {}


def _candidate_arg_values(candidate: dict) -> tuple[str, str, str, str]:
    repo = str(candidate.get('repo_path') or candidate.get('local_path') or candidate.get('path') or candidate.get('proposed_path_hint') or '').strip()
    name = str(candidate.get('name') or candidate.get('repo') or candidate.get('repo_name') or '').strip()
    title = str(candidate.get('literature_base_title') or candidate.get('selected_base_title') or candidate.get('title') or '').strip()
    dataset = str(candidate.get('claim_ready_dataset') or candidate.get('dataset') or '').strip()
    if not dataset:
        summary = candidate.get('probe_summary') if isinstance(candidate.get('probe_summary'), dict) else {}
        ready = summary.get('claim_ready_datasets') if isinstance(summary.get('claim_ready_datasets'), list) else []
        dataset = str(ready[0]) if ready else ''
    return repo, name, title, dataset



def pending_environment_candidate(selection: dict) -> dict:
    if not isinstance(selection, dict):
        return {}
    pending = selection.get('pending_environment_candidate') if isinstance(selection.get('pending_environment_candidate'), dict) else {}
    if pending and str(pending.get('repo_path') or pending.get('local_path') or '').strip():
        return pending
    selected = selection.get('selected') if isinstance(selection.get('selected'), dict) else {}
    if _pending_loader_selection(selection, selected) and str(selected.get('repo_path') or selected.get('local_path') or '').strip():
        return selected
    return {}


def run_pending_loader_bootstrap(project: str, paths, env_name: str, selection: dict, venue: str = '', allow_env_bootstrap: bool = False) -> list[dict[str, Any]]:
    pending = pending_environment_candidate(selection)
    if not pending:
        return []
    repo = str(pending.get('repo_path') or pending.get('local_path') or '').strip()
    if not repo or not Path(repo).exists():
        return []
    results: list[dict[str, Any]] = []
    print(f'TASTE pending-loader bootstrap: preparing data/env adapter work for {pending.get("name") or repo}', flush=True)
    fresh_rc = run_optional(module_cmd('environment', 'fresh_base_plan', '--project', project), ROOT, timeout=180)
    results.append({'step': 'fresh_base_plan_for_pending_loader', 'return_code': fresh_rc})
    prompt_path = paths.state / 'environment_pending_loader_bootstrap_prompt.md'
    adapter_diagnostics = adapter_contract_diagnostics(paths)
    prompt = f"""
TASTE Environment pending-loader bootstrap for project `{project}`.

You are Claude Code working inside the project workspace. The environment-stage repo selector has chosen a current-Find candidate as the best transformable pending-loader base, but TASTE cannot promote it until real data contracts and loader/import probes pass.

Hard rules:
- Work only on the current environment-stage pending candidate repo below. Do not switch to another paper/repo and do not run another Find.
- Add project-local adapter/probe files only under `projects/{project}/scripts/adapters/` or project state/report artifacts. Do not edit TASTE framework/module source.
- Project-local adapters are active-route adapters by default. If an adapter is intended for this pending candidate before promotion, also write `projects/{project}/scripts/adapters/adapter_scope.json` with explicit `candidate_adapters` entries binding each adapter filename to this candidate's repo_path, candidate_repo/name, and candidate_title. Without that sidecar, TASTE candidate-scope probes will ignore the adapter and use generic evidence to prevent cross-candidate data contamination.
- Do not fabricate datasets or mark synthetic/toy data as paper evidence. If you create a tiny smoke fixture, label it as smoke-only and keep claim gates blocked.
- Do not enumerate, decompress, or scan large archives/datasets. Use TASTE data contracts/probe JSON and lightweight local metadata only, such as `ls -lh`, `du -sh`, or bounded `find -maxdepth`; if deeper validation is needed, request or call the TASTE data probe wrapper instead of manually walking `.tar.gz` contents.
- Do not run raw curl/wget/gdown or any mutating conda/pip command yourself. Data acquisition must go through `framework/scripts/run_module.py environment --action fresh_base_data_probe --project {project} --attempt-download --timeout-sec <N>`. Environment creation is wrapper-owned.
- You may use quick local read-only environment probes such as `conda info`, `conda env list`, and `conda list -n <env>` when needed to understand this machine. Do not run `conda search`, `conda create`, `conda install`, `conda update`, `conda env update`, `conda remove`, `conda run`, `pip install`, environment Python probes, or downloads. Do not combine an allowed probe with a forbidden command in the same shell line via `&&`, `;`, `|`, subshells, or command substitution.
- Use the configured management Python command shown by TASTE for TASTE wrapper commands; do not use bare python or a candidate experiment-env Python directly.
- Never call the parent environment stage from inside this worker: do not run `framework/scripts/run_module.py environment --action run_environment_stage`, `modules/environment/main.py --action run_environment_stage`, or `modules/environment/scripts/run_environment_stage.py`. Only run specific bounded sub-actions such as `fresh_base_plan`, `build_repo_data_requirements`, `probe_repo_dataset`, or `fresh_base_data_probe`.

Pending candidate:
```json
{json.dumps(pending, ensure_ascii=False, indent=2)[:18000]}
```

Current repo/env strategy:
```json
{json.dumps(load_repo_env_strategy(paths), ensure_ascii=False, indent=2)[:12000]}
```

Current adapter/probe contract diagnostics from the latest TASTE probes:
```json
{json.dumps(adapter_diagnostics, ensure_ascii=False, indent=2)[:12000]}
```

Adapter contract requirements:
- Every project-local loader adapter must emit self-explaining structured evidence: package/import checks used by the success logic, repo loader/import checks, real data loading checks, exact blocker reasons, and ready/blocked dataset lists.
- If repo imports and real data loading pass, the adapter must not leave `success=false`, empty `ready_datasets`, or `Unknown import blockers` without naming the exact remaining failing check.
- If the adapter uses an inline subprocess probe, it must return the import/package check map in the JSON it parses; do not keep checks only in a local variable.
- After each adapter edit, rerun the TASTE module command that dispatches that adapter and inspect `real_dataset_probe.json` and candidate loader probe JSON.

Required work:
1. Inspect the selected repo README, examples, environment file, dataset loaders, and entrypoints.
2. Write or repair project-local adapters for `build_fresh_base_implementation_plan.py`, `build_repo_data_requirements.py`, `probe_repo_dataset.py`, and reference probes only when needed.
3. The adapters must expose exact dataset contract, expected roots/files, public download sources, import/package requirements, minimal smoke commands, honest blockers, and explicit candidate scope metadata when they are candidate-specific.
4. Repair adapter contract issues reported above before doing new repo search or new environment planning. A vague blocker is not enough when the latest TASTE probe already shows which subchecks passed.
5. Run TASTE module commands only to regenerate `fresh_base_implementation_plan.json` and bounded repo data/loader probes. Do not run large data downloads or conda creation yourself; the Environment wrapper runs data acquisition and env bootstrap after you return.
6. Leave gates blocked unless a real dataset contract and repo loader/import probe pass.

Return concise Markdown with Files Inspected, Adapters/Artifacts Written, TASTE Commands Run, Data/Loader Status, Still Blocked or Cleared.
""".strip() + "\n"
    prompt_path.write_text(prompt, encoding='utf-8')
    claude_timeout = int(os.environ.get('ENV_PENDING_LOADER_CLAUDE_TIMEOUT_SEC', '3600') or '3600')
    claude_rc = run_optional([
        sys.executable,
        'framework/scripts/claude_project_session.py',
        '--project', project,
        '--stage', 'environment-pending-loader-bootstrap',
        '--message-file', str(prompt_path),
        '--timeout-sec', str(claude_timeout),
        '--agent-id', 'main',
        '--no-resume',
    ], ROOT, timeout=claude_timeout + 300)
    results.append({'step': 'project_claude_pending_loader_bootstrap', 'return_code': claude_rc})
    fresh_rc = run_optional(module_cmd('environment', 'fresh_base_plan', '--project', project), ROOT, timeout=180)
    results.append({'step': 'fresh_base_plan_after_pending_loader_claude', 'return_code': fresh_rc})
    data_timeout = int(os.environ.get('ENV_PENDING_LOADER_DATA_TIMEOUT_SEC', '7200') or '7200')
    data_rc = run_optional(module_cmd('environment', 'fresh_base_data_probe', '--project', project, '--attempt-download', '--timeout-sec', str(data_timeout)), ROOT, timeout=data_timeout + 120)
    results.append({'step': 'fresh_base_data_probe_pending_loader', 'return_code': data_rc})
    bootstrap_timeout = int(os.environ.get('ENV_PENDING_LOADER_BOOTSTRAP_TIMEOUT_SEC', '7200') or '7200')
    bootstrap_env_name = env_name
    if allow_env_bootstrap:
        bootstrap_env_name, plan_path, bootstrap_rc = run_machine_aware_env_bootstrap(
            project,
            paths,
            repo,
            env_name,
            load_project_config(project),
            reason='Pending-loader candidate has real data; decide a local-machine-aware environment bootstrap plan before any conda mutation.',
        )
        results.append({'step': 'machine_aware_env_bootstrap_pending_loader', 'return_code': bootstrap_rc, 'env_name': bootstrap_env_name, 'plan_path': str(plan_path), 'repo_path': repo})
        if bootstrap_rc != 0:
            reason = 'Machine-aware environment bootstrap for the pending-loader candidate failed; this candidate remains non-executable on the current machine and TASTE must continue repo search/audit instead of promoting it.'
            run_optional(module_cmd('environment', 'fresh_base_plan', '--project', project), ROOT, timeout=180)
            mark_pending_candidate_non_executable(paths, pending, repo, reason, bootstrap_rc or 2)
            write_repo_selection_blocker(paths, reason, selection=selection)
            write_fresh_base_implementation_blocker(paths, current_find_run_id(paths), reason)
            results.append({'step': 'pending_loader_candidate_marked_non_executable', 'return_code': bootstrap_rc or 2, 'reason': reason, 'repo_path': repo})
            return results
    else:
        bootstrap_cmd = module_cmd('environment', 'bootstrap', '--project', project, '--repo-path', repo, '--env-name', bootstrap_env_name, '--verify-only', '--prepare-only')
        bootstrap_rc = run_optional(bootstrap_cmd, ROOT, timeout=bootstrap_timeout)
        results.append({'step': 'repo_env_bootstrap_pending_loader', 'return_code': bootstrap_rc, 'env_name': bootstrap_env_name, 'repo_path': repo})
    return results


def collect_candidate_base_switch_evidence(project: str, paths, env_name: str, venue: str = '') -> list[dict]:
    selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    if not isinstance(selection, dict) or str(selection.get('selection_gate') or '') != BASE_SWITCH_SELECTION_GATE:
        return []
    candidate = _candidate_from_base_switch_selection(paths, selection)
    repo, name, title, dataset = _candidate_arg_values(candidate)
    if not repo:
        print('TASTE candidate base-switch evidence collection skipped: candidate repo_path is missing.', flush=True)
        return []
    base_cmd = module_cmd(
        'environment', 'probe_candidate_base_reference',
        '--project', project,
        '--repo-path', repo,
        '--env-name', env_name,
        '--timeout-sec', '300',
    )
    if name:
        base_cmd.extend(['--candidate-name', name])
    if title:
        base_cmd.extend(['--candidate-title', title])
    if dataset:
        base_cmd.extend(['--dataset', dataset])
    print(f'TASTE candidate base-switch evidence collection: repo={name or repo} dataset={dataset or "unknown"}', flush=True)
    results: list[dict] = []
    run_optional(module_cmd('environment', 'fresh_base_plan', '--project', project), ROOT, timeout=180)
    protocol_rc = run_optional([*base_cmd, '--mode', 'protocol'], ROOT, timeout=360)
    results.append({'step': 'candidate_reference_protocol', 'return_code': protocol_rc})
    smoke_rc = 2
    if protocol_rc == 0:
        smoke_rc = run_optional([*base_cmd, '--mode', 'smoke'], ROOT, timeout=360)
        results.append({'step': 'candidate_reference_smoke', 'return_code': smoke_rc})
    else:
        results.append({'step': 'candidate_reference_smoke', 'return_code': 2, 'skipped': 'protocol_blocked'})
    if smoke_rc == 0:
        full_rc = run_optional([*base_cmd, '--mode', 'full', '--execute'], ROOT, timeout=360)
        results.append({'step': 'candidate_reference_full_reproduction_audit', 'return_code': full_rc})
    else:
        results.append({'step': 'candidate_reference_full_reproduction_audit', 'return_code': 2, 'skipped': 'smoke_blocked'})
    gate_cmd = module_cmd('environment', 'base_switch_gate', '--project', project)
    if venue:
        gate_cmd.extend(['--venue', venue])
    gate_rc = run_optional(gate_cmd, ROOT, timeout=180)
    results.append({'step': 'deterministic_base_switch_gate', 'return_code': gate_rc})
    gate = load_json(paths.state / 'base_switch_gate.json', {})
    if isinstance(gate, dict) and gate.get('status') == 'pass' and gate.get('decision') == 'authorize_base_switch' and gate.get('switch_authorized') is True:
        exec_cmd = module_cmd('environment', 'execute_base_switch', '--project', project)
        if venue:
            exec_cmd.extend(['--venue', venue])
        exec_rc = run_optional(exec_cmd, ROOT, timeout=180)
        results.append({'step': 'execute_authorized_base_switch', 'return_code': exec_rc})
    audit_cmd = module_cmd('experimenting', 'reference_reproduction', '--project', project)
    if venue:
        audit_cmd.extend(['--venue', venue])
    results.append({'step': 'reference_reproduction_gate', 'return_code': run_optional(audit_cmd, ROOT, timeout=180)})
    blocker_cmd = module_cmd('planning', 'blocker_action', '--project', project)
    if venue:
        blocker_cmd.extend(['--venue', venue])
    results.append({'step': 'blocker_action_plan', 'return_code': run_optional(blocker_cmd, ROOT, timeout=180)})
    return results


def select_current_run_environment_repo(project: str, paths, env_name: str, max_rounds: int = 3, venue: str = '', allow_env_bootstrap: bool = False) -> str:
    run_id = current_find_run_id(paths)
    if not run_id:
        raise SystemExit('Current Find run_id is missing; cannot perform environment-stage base selection.')
    # Build/refresh current-run candidate pool first; audit must consume the same Find run that the UI shows.
    run_optional(module_cmd('environment', 'fresh_base_selection', '--project', project), ROOT)
    run_optional(module_cmd('finding', 'literature_base_audit', '--project', project, '--limit', '12', '--repo-search-per-candidate', '2', '--repo-limit', '5', '--probe-timeout-sec', '120', '--fresh-find-run-id', run_id), ROOT)
    selector = [
        *module_cmd('environment', 'select_evidence_ready', '--project', project),
        '--env-name', env_name, '--limit', '12', '--timeout-sec', '180',
        '--allow-veto-fallback', '--write-active', '--use-claude-review',
        '--selection-stage', 'environment_claude_code',
        '--candidate-source', 'fresh_literature_github_search',
        '--fresh-find-run-id', run_id,
    ]
    pending_bootstrap_attempted: set[str] = set()
    for round_index in range(1, max(1, max_rounds) + 1):
        print(f'TASTE current-run environment repo-selection iteration {round_index}/{max_rounds}', flush=True)
        round_selector = list(selector)
        if round_index > 1 and '--candidate-source' in round_selector:
            source_index = round_selector.index('--candidate-source')
            del round_selector[source_index:source_index + 2]
        selector_timeout = int(os.environ.get('ENV_REPO_SELECTOR_TIMEOUT_SEC', '900') or '900')
        rc = run_optional(round_selector, ROOT, timeout=selector_timeout)
        selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
        selected = selection.get('selected') if isinstance(selection, dict) and isinstance(selection.get('selected'), dict) else {}
        repo = str(selected.get('repo_path') or selected.get('local_path') or '').strip()
        pending = pending_environment_candidate(selection if isinstance(selection, dict) else {})
        pending_repo_key = ''
        if pending:
            raw_pending_repo = str(pending.get('repo_path') or pending.get('local_path') or '').strip()
            try:
                pending_repo_key = str(Path(raw_pending_repo).expanduser().resolve()) if raw_pending_repo else ''
            except Exception:
                pending_repo_key = raw_pending_repo
        if pending and pending_repo_key and pending_repo_key not in pending_bootstrap_attempted:
            pending_bootstrap_attempted.add(pending_repo_key)
            bootstrap_results = run_pending_loader_bootstrap(project, paths, env_name, selection, venue=venue, allow_env_bootstrap=allow_env_bootstrap)
            for result in reversed(bootstrap_results):
                planned_env = str(result.get('env_name') or '').strip() if isinstance(result, dict) else ''
                if planned_env:
                    env_name = planned_env
                    selector = replace_arg_value(selector, '--env-name', env_name)
                    round_selector = replace_arg_value(round_selector, '--env-name', env_name)
                    break
            append_search_memory(paths, {'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(), 'round': round_index, 'pending_loader_bootstrap_results': bootstrap_results, 'pending_candidate': {'name': pending.get('name', ''), 'repo_path': pending.get('repo_path', '')}})
            rc = run_optional(list(round_selector), ROOT, timeout=selector_timeout)
            selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
            selected = selection.get('selected') if isinstance(selection, dict) and isinstance(selection.get('selected'), dict) else {}
            repo = str(selected.get('repo_path') or selected.get('local_path') or '').strip()
        if isinstance(selection, dict) and str(selection.get('selection_gate') or '') == BASE_SWITCH_SELECTION_GATE:
            collect_candidate_base_switch_evidence(project, paths, env_name, venue=venue)
            selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
            selected = selection.get('selected') if isinstance(selection, dict) and isinstance(selection.get('selected'), dict) else {}
            repo = str(selected.get('repo_path') or selected.get('local_path') or '').strip()
            if str(selection.get('selection_gate') or '') == BASE_SWITCH_SELECTION_GATE and not current_env_selection_valid(paths):
                blocker_reason = 'Current Find environment-stage selection found a loader/data-ready candidate, but deterministic base-switch evidence remains incomplete; candidate stays proposal-only while TASTE records the exact reference/audit blockers.'
                write_repo_selection_blocker(paths, blocker_reason, selection=selection)
                run_optional(module_cmd('environment', 'fresh_base_plan', '--project', project), ROOT, timeout=180)
                raise SystemExit(2)
        if rc == 0 and repo and Path(repo).exists() and current_env_selection_valid(paths):
            return repo
        latest_pending = pending_environment_candidate(selection if isinstance(selection, dict) else {})
        latest_pending_repo = str(latest_pending.get('repo_path') or latest_pending.get('local_path') or '').strip() if latest_pending else ''
        try:
            latest_pending_key = str(Path(latest_pending_repo).expanduser().resolve()) if latest_pending_repo else ''
        except Exception:
            latest_pending_key = latest_pending_repo
        if latest_pending and latest_pending_key in pending_bootstrap_attempted and round_index >= max(1, max_rounds):
            blocker_reason = 'Current Find environment-stage selection identified a transformable pending-loader candidate and TASTE attempted data/adapter/env bootstrap, but loader/import evidence is still not claim-ready on this machine; experiments remain blocked with exact artifacts.'
            write_repo_selection_blocker(paths, blocker_reason, selection=selection if isinstance(selection, dict) else {})
            run_optional(module_cmd('environment', 'fresh_base_plan', '--project', project), ROOT, timeout=180)
            raise SystemExit(2)
        expand_repo_search(project, round_index, fresh_find_run_id=run_id)
    latest_selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    if isinstance(latest_selection, dict) and str(latest_selection.get('selection_gate') or '') == BASE_SWITCH_SELECTION_GATE:
        blocker_reason = 'Current Find environment-stage selection found a loader/data-ready candidate, but deterministic base-switch evidence remains incomplete; candidate stays proposal-only while TASTE continues evidence collection/search.'
        write_repo_selection_blocker(paths, blocker_reason, selection=latest_selection)
        run_optional(module_cmd('environment', 'fresh_base_plan', '--project', project), ROOT, timeout=180)
    else:
        blocker_reason = 'Current Find environment-stage selection did not find an evidence-ready repo; old active_repo remains legacy/control only.'
        write_repo_selection_blocker(paths, blocker_reason, selection=latest_selection if isinstance(latest_selection, dict) else {})
        write_fresh_base_implementation_blocker(paths, run_id, blocker_reason)
    raise SystemExit(2)

def run(cmd: list[str], cwd: Path, timeout: int | None = None) -> int:
    print('$ ' + ' '.join(cmd), flush=True)
    try:
        proc = subprocess.run(cmd, cwd=cwd, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f'command timed out after {timeout}s: {" ".join(cmd)}', flush=True)
        raise SystemExit(124)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return proc.returncode


def run_optional(cmd: list[str], cwd: Path, timeout: int | None = None) -> int:
    print('$ ' + ' '.join(cmd), flush=True)
    try:
        proc = subprocess.run(cmd, cwd=cwd, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f'optional command timed out after {timeout}s: {" ".join(cmd)}', flush=True)
        return 124
    if proc.returncode != 0:
        print(f'optional command failed rc={proc.returncode}: {" ".join(cmd)}', flush=True)
    return proc.returncode


def reference_gate_passed(paths) -> bool:
    gate = load_json(paths.state / 'reference_reproduction_gate.json', {})
    return bool(isinstance(gate, dict) and gate.get('status') == 'pass' and gate.get('decision') == 'continue_base')


def repair_reference_reproduction_if_needed(project: str, paths, venue: str = '') -> None:
    gate = load_json(paths.state / 'reference_reproduction_gate.json', {})
    if reference_gate_passed(paths):
        print('Reference reproduction gate already passed; environment stage will not start a repair run.', flush=True)
        return
    blockers = gate.get('blockers', []) if isinstance(gate, dict) and isinstance(gate.get('blockers', []), list) else []
    print('Reference reproduction is still blocked after environment setup; launching TASTE reference-reproduction repair before novel experiments.', flush=True)
    if blockers:
        print('Top reference blocker: ' + str(blockers[0])[:500], flush=True)
    cmd = [
        sys.executable,
        'framework/scripts/run_autonomous_research.py',
        '--project',
        project,
        '--iterations',
        '1',
        '--execute-plan',
        '--prepare-env',
        '--real-bootstrap-env',
        '--skip-discovery',
        '--skip-paper',
        '--max-launches',
        '1',
    ]
    if venue:
        cmd.extend(['--venue', venue])
    run_optional(cmd, ROOT)


def conda_env_exists(cfg: dict, env_name: str) -> bool:
    if not env_name:
        return False
    candidates = []
    env_cfg = cfg.get('environment', {}) if isinstance(cfg, dict) else {}
    hint = str(env_cfg.get('conda_base_hint', '') or '') if isinstance(env_cfg, dict) else ''
    if hint:
        candidates.append(Path(hint) / 'envs' / env_name)
    candidates.extend([
        ROOT.parent / 'miniforge' / 'envs' / env_name,
        ROOT.parent / 'miniforge3' / 'envs' / env_name,
        Path.home() / 'miniforge3' / 'envs' / env_name,
        Path.home() / 'miniconda3' / 'envs' / env_name,
    ])
    return any(path.exists() for path in candidates)


def environment_is_locked(paths, cfg: dict, env_name: str, repo: str) -> bool:
    bootstrap = load_json(paths.state / 'repo_env_bootstrap.json', {})
    same_env = isinstance(bootstrap, dict) and str(bootstrap.get('env_name', '')) == env_name
    same_repo = isinstance(bootstrap, dict) and str(bootstrap.get('repo_path', '')) == str(repo)
    completed = same_env and same_repo and bootstrap.get('status') == 'completed'
    if stale_runtime_failure_for_env(paths, env_name):
        return False
    if completed:
        return bootstrap_runtime_validation_passed(bootstrap)
    # Existing env directories alone are not enough: compiled runtimes can import
    # successfully while failing on the local GPU architecture. The bootstrap
    # receipt must include the wrapper runtime validation before TASTE locks it.
    return False


def has_claim_ready_probe(paths, repo: str) -> bool:
    probe = load_json(paths.state / 'real_dataset_probe.json', {})
    if not isinstance(probe, dict) or str(probe.get('repo_path', '')) != str(repo):
        return False
    for row in probe.get('probes', []) or []:
        if isinstance(row, dict) and row.get('claim_ready') and row.get('loader_probe', {}).get('success'):
            return True
    return False


def refresh_repo_data(project: str, repo: str, env_name: str) -> None:
    paths = build_paths(project)
    run(module_cmd('environment', 'data_requirements', '--project', project, '--repo-path', repo), ROOT)
    if has_claim_ready_probe(paths, repo):
        print(f'TASTE repo/data refresh: reusing existing claim-ready loader probe for {repo}', flush=True)
        return
    run(module_cmd('environment', 'probe_repo', '--project', project, '--repo-path', repo, '--env-name', env_name, '--timeout-sec', '180'), ROOT)


def claude_accepts_current_route(paths) -> bool:
    selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    active = load_json(paths.state / 'active_repo.json', {})
    selected = selection.get('selected', {}) if isinstance(selection, dict) and isinstance(selection.get('selected'), dict) else {}
    gate = str(selection.get('selection_gate', '')) if isinstance(selection, dict) else ''
    if isinstance(selection, dict) and _pending_loader_selection(selection, selected):
        return False
    if _unresolved_base_switch_candidate(paths, selected):
        return False
    if gate.startswith('accepted_by_deterministic_base_switch_gate'):
        return _base_switch_authorized_for(paths, selected)
    if gate.startswith('accepted_by_claude'):
        return True
    decision = {}
    if isinstance(selection, dict) and isinstance(selection.get('claude_topic_decision'), dict):
        decision = selection.get('claude_topic_decision', {})
    elif isinstance(active, dict) and isinstance(active.get('claude_topic_fit_decision'), dict):
        decision = active.get('claude_topic_fit_decision', {})
    return bool(decision.get('accept_as_current_best'))


def load_repo_env_strategy(paths) -> dict:
    strategy = load_json(paths.state / 'repo_env_strategy.json', {})
    if isinstance(strategy, dict) and strategy:
        return strategy
    active = load_json(paths.state / 'active_repo.json', {})
    if isinstance(active, dict) and isinstance(active.get('claude_repo_env_strategy'), dict):
        return active.get('claude_repo_env_strategy', {})
    return {}



def _read_text_limited(path: Path, limit: int = 12000) -> str:
    try:
        if path.exists() and path.is_file():
            return path.read_text(encoding='utf-8', errors='replace')[:limit]
    except Exception:
        return ''
    return ''


def _ensure_machine_profile(project: str, paths) -> dict:
    profile_path = paths.reports / 'machine_profile.json'
    if not profile_path.exists():
        run_optional([sys.executable, 'framework/scripts/detect_machine_profile.py', '--project', project], ROOT, timeout=180)
    return load_json(profile_path, {})


def _conda_package_cache_inventory(conda_exe: str) -> dict:
    out: dict = {'package_cache_dirs': [], 'relevant_cached_packages': []}
    if not conda_exe:
        return out
    candidates: list[Path] = []
    try:
        conda_base = Path(conda_exe).resolve().parents[1]
        candidates.append(conda_base / 'pkgs')
    except Exception:
        pass
    for base in [ROOT.parent / 'miniforge', ROOT.parent / 'miniforge3', Path.home() / 'miniforge3', Path.home() / 'miniconda3', Path.home() / 'anaconda3']:
        candidates.append(base / 'pkgs')
    seen: set[str] = set()
    package_prefixes = (
        'pytorch-', 'pytorch-cuda-', 'pytorch-mutex-', 'cuda-version-', 'cuda-toolkit-', 'torchvision-', 'torchaudio-',
        'pyg-', 'torch-geometric-', 'pytorch-geometric-', 'pytorch-scatter-', 'pytorch-sparse-', 'pytorch-cluster-', 'torch-scatter-', 'torch-sparse-', 'torch-cluster-',
        'numpy-', 'scipy-', 'scikit-learn-', 'pandas-', 'h5py-', 'pyyaml-', 'tqdm-',
    )
    for cache_dir in candidates:
        text = str(cache_dir)
        if text in seen or not cache_dir.exists():
            continue
        seen.add(text)
        out['package_cache_dirs'].append(text)
        try:
            for item in sorted(cache_dir.iterdir()):
                name = item.name
                if name.endswith('.conda'):
                    stem = name[:-6]
                elif name.endswith('.tar.bz2'):
                    stem = name[:-8]
                else:
                    stem = name
                if stem.startswith(package_prefixes):
                    row = {'cache_dir': text, 'name': name}
                    info_path = item / 'info' / 'index.json' if item.is_dir() else None
                    if info_path and info_path.exists():
                        try:
                            info = json.loads(info_path.read_text(encoding='utf-8', errors='replace'))
                            row['depends'] = info.get('depends', [])[:40] if isinstance(info.get('depends'), list) else []
                            row['version'] = info.get('version', '')
                            row['build'] = info.get('build', '')
                        except Exception as exc:
                            row['index_error'] = str(exc)
                    out['relevant_cached_packages'].append(row)
        except Exception as exc:
            out.setdefault('cache_scan_errors', []).append({'cache_dir': text, 'error': str(exc)})
    out['relevant_cached_packages'] = out['relevant_cached_packages'][:240]
    return out


def _conda_env_import_probe(conda_exe: str, env_name: str) -> dict:
    if not conda_exe or not env_name:
        return {}
    probe_code = "import importlib.util, json\nmods = [\n    \"torch\", \"torch_geometric\", \"torch_scatter\", \"torch_sparse\", \"torch_cluster\",\n    \"atom3d\", \"Bio\", \"mdtraj\", \"ml_collections\", \"tree\", \"einops\",\n    \"numpy\", \"scipy\", \"sklearn\", \"pandas\", \"h5py\", \"yaml\", \"tqdm\",\n]\nout = {\"imports\": {name: bool(importlib.util.find_spec(name)) for name in mods}}\nif out[\"imports\"].get(\"torch\"):\n    try:\n        import torch\n        version_obj = getattr(torch, \"version\", None)\n        torch_version = str(getattr(torch, \"__version__\", \"\") or \"\")\n        torch_info = {\n            \"version\": torch_version,\n            \"file\": str(getattr(torch, \"__file__\", \"\") or \"\"),\n            \"cuda_version\": str(getattr(version_obj, \"cuda\", \"\") or \"\"),\n            \"cuda_available\": bool(hasattr(torch, \"cuda\") and torch.cuda.is_available()),\n            \"device_count\": int(torch.cuda.device_count()) if hasattr(torch, \"cuda\") else 0,\n            \"usable\": bool(torch_version and hasattr(torch, \"cuda\")),\n        }\n        if torch_info[\"cuda_available\"]:\n            torch_info[\"device_name\"] = torch.cuda.get_device_name(0)\n            torch_info[\"device_capability\"] = list(torch.cuda.get_device_capability(0))\n            try:\n                x = torch.randn(32, device=\"cuda\")\n                y = (x * x).sum()\n                torch.cuda.synchronize()\n                torch_info[\"cuda_kernel_test\"] = \"passed\"\n                torch_info[\"cuda_kernel_value\"] = float(y.detach().cpu())\n            except Exception as exc:\n                torch_info[\"cuda_kernel_test\"] = \"failed\"\n                torch_info[\"cuda_kernel_error\"] = f\"{type(exc).__name__}: {exc}\"\n                torch_info[\"usable\"] = False\n        out[\"torch\"] = torch_info\n    except Exception as exc:\n        out[\"torch_error\"] = str(exc)\nprint(json.dumps(out, ensure_ascii=False))"
    try:
        proc = subprocess.run(
            [conda_exe, 'run', '-n', env_name, 'python', '-c', probe_code],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {'status': 'timeout', 'timeout_sec': 60}
    except Exception as exc:
        return {'status': 'error', 'error': str(exc)}
    payload: dict = {'status': 'passed' if proc.returncode == 0 else 'failed', 'return_code': proc.returncode}
    if proc.returncode == 0:
        try:
            parsed = json.loads((proc.stdout or '').strip().splitlines()[-1])
            if isinstance(parsed, dict):
                payload.update(parsed)
        except Exception as exc:
            payload['parse_error'] = str(exc)
            payload['stdout_tail'] = (proc.stdout or '')[-1200:]
    else:
        payload['stdout_tail'] = (proc.stdout or '')[-1200:]
        payload['stderr_tail'] = (proc.stderr or '')[-1200:]
    return payload


def _conda_env_inventory(machine: dict, env_name: str) -> dict:
    cli = machine.get('dependencies', {}).get('cli', {}) if isinstance(machine, dict) else {}
    conda_exe = str((cli.get('conda') or {}).get('path') or '').strip() if isinstance(cli.get('conda'), dict) else ''
    out: dict = {'conda_executable': conda_exe, 'envs': [], 'selected_env_packages': [], 'package_cache': {}}
    if not conda_exe or not Path(conda_exe).exists():
        return out
    out['package_cache'] = _conda_package_cache_inventory(conda_exe)
    try:
        proc = subprocess.run([conda_exe, 'env', 'list', '--json'], cwd=ROOT, text=True, capture_output=True, timeout=30)
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            for item in data.get('envs', []) if isinstance(data, dict) else []:
                p = Path(str(item))
                out['envs'].append({'name': p.name, 'path': str(p), 'matches_requested': p.name == env_name})
    except Exception as exc:
        out['env_list_error'] = str(exc)
    if env_name:
        try:
            proc = subprocess.run([conda_exe, 'list', '-n', env_name, '--json'], cwd=ROOT, text=True, capture_output=True, timeout=45)
            if proc.returncode == 0:
                packages = json.loads(proc.stdout)
                if isinstance(packages, list):
                    out['selected_env_packages'] = [{k: row.get(k) for k in ['name', 'version', 'channel']} for row in packages[:160] if isinstance(row, dict)]
                    out['selected_env_import_probe'] = _conda_env_import_probe(conda_exe, env_name)
            else:
                out['selected_env_list_error'] = (proc.stderr or proc.stdout)[-1200:]
        except Exception as exc:
            out['selected_env_list_error'] = str(exc)
    tokens = {token for token in re.split(r'[^a-zA-Z0-9]+', env_name.lower()) if len(token) >= 4}
    nearby: list[dict[str, Any]] = []
    for row in out.get('envs', []):
        name = str(row.get('name') or '')
        if not name or name == env_name:
            continue
        lowered = name.lower()
        if not tokens or not any(token in lowered for token in tokens):
            continue
        try:
            proc = subprocess.run([conda_exe, 'list', '-n', name, '--json'], cwd=ROOT, text=True, capture_output=True, timeout=30)
            if proc.returncode != 0:
                continue
            packages = json.loads(proc.stdout)
        except Exception:
            continue
        if not isinstance(packages, list):
            continue
        important = []
        for pkg in packages:
            if not isinstance(pkg, dict):
                continue
            pkg_name = str(pkg.get('name') or '')
            if pkg_name.startswith(('torch', 'pytorch', 'cuda', 'pyg')) or pkg_name in {'numpy', 'scipy', 'pandas', 'scikit-learn', 'biopython', 'mdtraj', 'h5py', 'einops', 'atom3d'}:
                important.append({k: pkg.get(k) for k in ['name', 'version', 'channel']})
        if important:
            nearby.append({'name': name, 'path': row.get('path', ''), 'important_packages': important[:80], 'import_probe': _conda_env_import_probe(conda_exe, name)})
        if len(nearby) >= 6:
            break
    out['nearby_project_env_packages'] = nearby
    return out


CUDA_RUNTIME_FAILURE_MARKERS = (
    'no kernel image is available for execution on the device',
    'not compatible with the current pytorch installation',
    'device kernel image is invalid',
    'cuda error: no kernel image',
    'sm_120',
)


def _json_from_stdout_tail(text: str) -> dict:
    for line in reversed(str(text or '').splitlines()):
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def bootstrap_runtime_validation_passed(bootstrap: dict) -> bool:
    if not isinstance(bootstrap, dict):
        return False
    executed = bootstrap.get('executed') if isinstance(bootstrap.get('executed'), list) else []
    for row in reversed(executed):
        if not isinstance(row, dict):
            continue
        stdout = str(row.get('stdout') or '')
        if 'taste_runtime_validation' not in stdout and 'runtime_validation_passed' not in stdout:
            continue
        payload = _json_from_stdout_tail(stdout)
        return bool(row.get('return_code') == 0 and payload.get('runtime_validation_passed') is True)
    return False


def stale_runtime_failure_for_env(paths, env_name: str) -> bool:
    if not env_name:
        return False
    candidates = [
        paths.state / 'fresh_base_reference_full_reproduction_job.json',
        paths.state / 'fresh_base_reference_full_reproduction_audit.json',
        paths.state / 'fresh_base_reference_reproduction_audit.json',
    ]
    for state_path in candidates:
        payload = load_json(state_path, {})
        if not isinstance(payload, dict) or not payload:
            continue
        if str(payload.get('status') or '').strip().lower() not in {'blocked', 'failed', 'error', 'blocked_reference_reproduction_audit'}:
            continue
        if int(payload.get('return_code') or 0) == 0:
            continue
        refs = json.dumps({k: payload.get(k) for k in ['python_executable', 'command', 'env_name']}, ensure_ascii=False)
        stdout_path = Path(str(payload.get('stdout_path') or ''))
        text = refs
        if stdout_path.exists():
            try:
                text += '\n' + stdout_path.read_text(encoding='utf-8', errors='ignore')[-12000:]
            except Exception:
                pass
        lowered = text.lower()
        if env_name.lower() in lowered and any(marker in lowered for marker in CUDA_RUNTIME_FAILURE_MARKERS):
            return True
    return False


def _bootstrap_receipt_summary(paths) -> dict:
    receipt = load_json(paths.state / 'repo_env_bootstrap.json', {})
    if not isinstance(receipt, dict) or not receipt:
        return {}
    keys = [
        'timestamp', 'status', 'repo_path', 'env_name', 'python_version', 'detected_backend',
        'detected_cuda', 'env_exists_before', 'env_exists_after', 'failed_step', 'missing_import',
        'machine_aware_plan_path',
    ]
    summary = {key: receipt.get(key) for key in keys if key in receipt}
    executed = receipt.get('executed') if isinstance(receipt.get('executed'), list) else []
    summary['executed_tail'] = [
        {k: row.get(k) for k in ['command', 'return_code', 'stdout', 'stderr', 'reason'] if k in row}
        for row in executed[-4:]
        if isinstance(row, dict)
    ]
    return summary


def _load_state_glob(paths, pattern: str, limit: int = 8) -> dict:
    rows = {}
    try:
        files = sorted(paths.state.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]
    except Exception:
        files = []
    for file in files:
        rows[file.name] = load_json(file, {})
    return rows


def _repo_environment_inputs(repo: Path) -> dict:
    return {
        'environment_yml': _read_text_limited(repo / 'environment.yml', 16000),
        'requirements_txt': _read_text_limited(repo / 'requirements.txt', 16000),
        'pyproject_toml': _read_text_limited(repo / 'pyproject.toml', 12000),
        'setup_py': _read_text_limited(repo / 'setup.py', 12000),
        'readme_excerpt': '\n\n'.join(_read_text_limited(path, 6000) for path in sorted(repo.glob('README*'))[:2]),
    }


def current_machine_aware_env_name(paths, repo: str = '') -> str:
    bootstrap = load_json(paths.state / 'repo_env_bootstrap.json', {})
    if isinstance(bootstrap, dict) and str(bootstrap.get('status') or '').strip().lower() == 'completed':
        boot_env = str(bootstrap.get('env_name') or '').strip()
        boot_repo = str(bootstrap.get('repo_path') or '').strip()
        same_repo = True
        if repo:
            try:
                same_repo = bool(boot_repo and Path(boot_repo).resolve() == Path(repo).resolve())
            except Exception:
                same_repo = bool(boot_repo and boot_repo == repo)
        if boot_env and same_repo and bootstrap_runtime_validation_passed(bootstrap) and not stale_runtime_failure_for_env(paths, boot_env):
            return boot_env

    plan_path = paths.state / 'machine_aware_env_plan.json'
    plan = load_json(plan_path, {})
    if not isinstance(plan, dict):
        return ''
    if str(plan.get('status') or '').strip().lower() not in {'ready', 'approved', 'execute', 'pass'}:
        return ''
    planned_env = str(plan.get('env_name') or plan.get('selected_env_name') or '').strip()
    if repo:
        planned_repo = str(plan.get('repo_path') or '').strip()
        try:
            if planned_repo and Path(planned_repo).resolve() != Path(repo).resolve():
                return ''
        except Exception:
            if planned_repo and planned_repo != repo:
                return ''
    bootstrap = load_json(paths.state / 'repo_env_bootstrap.json', {})
    if stale_runtime_failure_for_env(paths, planned_env):
        return ''
    if isinstance(bootstrap, dict) and str(bootstrap.get('status') or '').strip().lower() in {'failed', 'blocked'}:
        same_plan = str(bootstrap.get('machine_aware_plan_path') or '').strip() == str(plan_path.resolve())
        same_repo = not repo or str(bootstrap.get('repo_path') or '').strip() == str(Path(repo).resolve())
        same_env = not planned_env or str(bootstrap.get('env_name') or '').strip() == planned_env
        if same_plan and same_repo and same_env:
            return ''
    return planned_env


def prepare_machine_aware_env_plan(project: str, paths, repo: str, env_name: str, cfg: dict, reason: str = '', previous_bootstrap: dict | None = None, attempt: int = 1) -> tuple[str, Path, int]:
    repo_path = Path(repo).resolve()
    plan_path = paths.state / 'machine_aware_env_plan.json'
    prompt_path = paths.state / 'machine_aware_env_plan_prompt.md'
    machine = _ensure_machine_profile(project, paths)
    conda_inventory = _conda_env_inventory(machine, env_name)
    initial_plan = {
        'status': 'planning',
        'project': project,
        'repo_path': str(repo_path),
        'requested_env_name': env_name,
        'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'attempt': attempt,
        'blocker': 'Claude Code machine-aware environment plan has not completed yet.',
    }
    plan_path.write_text(json.dumps(initial_plan, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    prompt = f"""
TASTE machine-aware environment planning for project `{project}`.

You are Claude Code inside the project workspace. Your task is to decide the environment bootstrap plan for the selected repo on THIS machine. Prefer the machine profile, conda inventory, local conda package cache, repo files, loader evidence, and previous bootstrap receipt supplied below; you may run only quick local read-only probes such as `conda info`, `conda env list`, `conda list -n <env>`, `nvidia-smi`, and file inspections when needed. Do not run remote package-index searches such as `conda search`, and do not execute any command that creates, installs, updates, removes, downloads, solves a full environment, runs inside a conda env, executes repo training, or otherwise mutates environments/data; no `conda create`, `conda install`, `conda update`, `conda env update`, `conda remove`, `conda run`, `pip install`, environment Python probes, or downloads during planning. Do not hide a forbidden operation inside a mixed shell command: a line that combines an allowed probe with `&&`, `;`, `|`, command substitution, or redirection to a forbidden `conda run`/env-python/pip/download action is still forbidden. You may write wrapper-executable argv arrays such as `["install", ...]` or `["run", "-n", env, ...]` into the JSON plan; the TASTE wrapper, not Claude Code, executes them. Use the supplied cache/channel/receipt evidence to avoid speculative pins; if a previous wrapper step failed, revise the plan instead of repeating the same failing command. The TASTE wrapper will execute the approved plan after you write it.

Hard rules:
- Consider the local machine profile, GPU/driver/CUDA evidence, existing conda environments, repo environment files, repo loader/import requirements, and current project runtime config.
- Treat TASTE-supplied env import probes as authoritative execution evidence. A package name in `conda list` or a bare `import` is not enough: for core runtime packages such as PyTorch, use explicit fields such as `torch.usable`, `torch.version`, CUDA availability, `torch.cuda_kernel_test`, and required import booleans before deciding an env is reusable.
- Repo `environment.yml` / `requirements.txt` are evidence, not commands to copy blindly. If old pins are incompatible with the local machine, adapt or block with a clear reason.
- If a partial env already exists, do not include a `conda create -n <same env>` step. Choose `repair_existing_env` with install/update/run verification steps, or choose a clearly different new env name.
- If the local package cache shows a CUDA-enabled PyTorch build directly, you may target that direct package/build evidence instead of inventing a separate `pytorch-cuda=<version>` pin. Never repeat a package spec that the previous receipt shows as unavailable.
- If the cache/inventory does not show required compiled extension packages for the selected Python/CUDA/PyTorch combination, do not invent a conda solution. Prefer a wrapper-executed pip/prebuilt-wheel route from a non-rate-limited source, an already installed compatible env that passes import probes, or set `status="blocked"` so TASTE can continue route selection.
- A previous `repo_env_bootstrap.status=completed` proves only the wrapper plan verification steps listed in that receipt. If the Data and loader evidence below still reports repo import, package compatibility, or real-data load failures, treat the environment as needing another scoped repair plan; do not declare success merely because the env exists.
- If TASTE-supplied conda import probes and real data/loader evidence below already pass for the requested env, choose `decision="reuse_existing_env"`, leave `conda_steps` empty, and provide verification_steps only. Do not repeat pip/conda installs just to have install steps.
- If the previous receipt shows Python package build isolation or import-order failure, encode a different wrapper-executable approach such as compatible conda packages, no-build-isolation, prebuilt wheel index, or a clear blocked decision. Do not repeat the same failing pip/conda step.
- If the previous receipt shows package-channel/network failures such as HTTP 429, timeout, or unavailable repodata, treat this as a local-machine/network fact, not an env-name-specific failure. The receipt `blocker_reason` is authoritative even if an older plan text claims no 429. After any conda.anaconda.org HTTP 429, do not include any mutating remote conda channel step at all (`conda create/install/update/env update` with `-c pytorch`, `-c nvidia`, `-c pyg`, `-c conda-forge`, defaults, or another remote channel), even as a later fallback in the same plan. The wrapper will reject such plans before execution. A conda mutation is acceptable only when it is explicitly offline/local-cache based (`--offline` and/or `--use-local` where appropriate) and justified by supplied cache evidence. Otherwise prefer already-installed compatible packages, pip/prebuilt wheels from a non-rate-limited source, an alternate configured mirror, or set `status="blocked"` with a clear local-machine blocker.
- If the previous receipt shows compiler/CUDA header/toolkit errors such as missing runtime headers or an unusable local compiler, adapt the wrapper plan from machine evidence: add the compatible toolkit/headers, set verification steps that expose CUDA_HOME/include paths, choose prebuilt wheels, or block clearly. Do not guess a toolkit version unsupported by the local driver/cache evidence.
- Never delete or remove an environment. If a partial env exists, decide whether to repair it or create a new project-specific env. Package-level removal is allowed only when it is explicitly scoped to the selected project env with `-n <env>`, does not use `--all`/`--force`, and does not remove protected runtime packages such as python, pip, conda, setuptools, or wheel.
- Write exactly one JSON object to `{plan_path}`. Do not leave Markdown in that file.
- Conda steps must be argv arrays WITHOUT the conda executable prefix, for example `["create", "-y", "-n", "env", "python=3.10", "pip", "-c", "conda-forge"]`.
- If no safe machine-aware plan exists, set `status="blocked"` and explain the blocker.

Required JSON schema:
```json
{{
  "status": "ready | blocked",
  "project": "{project}",
  "repo_path": "{repo_path}",
  "env_name": "chosen env name",
  "python_version": "chosen Python version",
  "decision": "create_new_project_env | repair_existing_env | reuse_existing_env | blocked",
  "repo_env_file_policy": "use_as_is | adapt | avoid | blocked",
  "conda_steps": [["create", "-y", "-n", "...", "python=...", "pip"]],
  "verification_steps": [["run", "-n", "...", "python", "-c", "import torch; x=torch.randn(32, device='cuda') if torch.cuda.is_available() else None; print(torch.__version__)"]],
  "machine_reasoning": "why this plan fits the local machine and repo",
  "compatibility_risks": [],
  "blocker": "only when blocked",
  "guardrails": ["no env deletion", "wrapper executes the plan", "package removals must be project-env scoped"]
}}
```

Current request/reason:
{reason or 'Create or repair the project experiment environment for the environment stage.'}

Machine-aware planning attempt: {attempt}

Current project config (secrets omitted by TASTE):
```json
{json.dumps({k: v for k, v in cfg.items() if k not in {'llm', 'api_key', 'smtp'}}, ensure_ascii=False, indent=2)[:16000]}
```

Machine profile:
```json
{json.dumps(machine, ensure_ascii=False, indent=2)[:24000]}
```

Conda inventory:
```json
{json.dumps(conda_inventory, ensure_ascii=False, indent=2)[:22000]}
```

Previous wrapper bootstrap receipt, if any:
```json
{json.dumps(previous_bootstrap or {}, ensure_ascii=False, indent=2)[:18000]}
```

Repo environment inputs:
```json
{json.dumps(_repo_environment_inputs(repo_path), ensure_ascii=False, indent=2)[:30000]}
```

Current repo/data strategy:
```json
{json.dumps(load_repo_env_strategy(paths), ensure_ascii=False, indent=2)[:16000]}
```

Data and loader evidence:
```json
{json.dumps({
    'fresh_base_data_acquisition': load_json(paths.state / 'fresh_base_data_acquisition.json', {}),
    'repo_data_requirements': load_json(paths.state / 'repo_data_requirements.json', {}),
    'real_dataset_probe': load_json(paths.state / 'real_dataset_probe.json', {}),
    'candidate_loader_probes': _load_state_glob(paths, 'candidate_loader_probe_*.json'),
}, ensure_ascii=False, indent=2)[:26000]}
```
""".strip() + "\n"
    prompt_path.write_text(prompt, encoding='utf-8')
    timeout = int(os.environ.get('ENV_MACHINE_AWARE_PLAN_TIMEOUT_SEC', '1800') or '1800')
    rc = run_optional([
        sys.executable,
        'framework/scripts/claude_project_session.py',
        '--project', project,
        '--stage', 'environment-machine-aware-bootstrap-plan',
        '--message-file', str(prompt_path),
        '--timeout-sec', str(timeout),
        '--agent-id', 'main',
        '--no-resume',
    ], ROOT, timeout=timeout + 180)
    plan = load_json(plan_path, {})
    chosen_env = str(plan.get('env_name') or plan.get('selected_env_name') or env_name).strip() if isinstance(plan, dict) else env_name
    return chosen_env or env_name, plan_path, rc


def run_machine_aware_env_bootstrap(project: str, paths, repo: str, env_name: str, cfg: dict, reason: str = '', max_attempts: int | None = None) -> tuple[str, Path, int]:
    attempts = max(1, max_attempts or int(os.environ.get('ENV_MACHINE_AWARE_BOOTSTRAP_ATTEMPTS', '3') or '3'))
    current_env = env_name
    plan_path = paths.state / 'machine_aware_env_plan.json'
    previous = _bootstrap_receipt_summary(paths)
    last_rc = 2
    for attempt in range(1, attempts + 1):
        attempt_reason = reason or 'Create or repair the project experiment environment for the environment stage.'
        if previous:
            attempt_reason += '\n\nPrevious wrapper execution did not complete successfully. Revise the machine-aware plan using the receipt below; do not repeat the same failed command/spec.'
        current_env, plan_path, plan_rc = prepare_machine_aware_env_plan(
            project,
            paths,
            repo,
            current_env,
            cfg,
            reason=attempt_reason,
            previous_bootstrap=previous,
            attempt=attempt,
        )
        last_rc = plan_rc
        if plan_rc != 0:
            previous = {'status': 'plan_failed', 'return_code': plan_rc, 'attempt': attempt, 'plan_path': str(plan_path)}
            plan = load_json(plan_path, {})
            if not isinstance(plan, dict) or str(plan.get('status') or '').strip().lower() == 'planning':
                blocked = {
                    'status': 'blocked',
                    'project': project,
                    'repo_path': str(Path(repo).resolve()),
                    'env_name': current_env,
                    'decision': 'blocked',
                    'attempt': attempt,
                    'blocker': 'Claude Code did not produce a machine-aware environment plan within policy; wrapper execution was not started.',
                    'last_plan_return_code': plan_rc,
                    'guardrails': ['Claude plans from local machine evidence', 'wrapper executes environment mutation'],
                    'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                }
                plan_path.write_text(json.dumps(blocked, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            continue
        bootstrap_timeout = int(os.environ.get('ENV_MACHINE_AWARE_BOOTSTRAP_TIMEOUT_SEC', '7200') or '7200')
        bootstrap_cmd = module_cmd('environment', 'bootstrap', '--project', project, '--repo-path', repo, '--env-name', current_env, '--machine-aware-plan', str(plan_path), '--require-machine-aware-plan', '--update-project-config')
        last_rc = run_optional(bootstrap_cmd, ROOT, timeout=bootstrap_timeout)
        receipt = _bootstrap_receipt_summary(paths)
        if last_rc == 0 and receipt.get('status') == 'completed':
            return current_env, plan_path, 0
        previous = receipt or {'status': 'bootstrap_failed', 'return_code': last_rc, 'attempt': attempt, 'plan_path': str(plan_path)}
    return current_env, plan_path, last_rc or 2

def strategy_env_name(strategy: dict, fallback: str, *, explicit_env_name: str = '') -> str:
    if explicit_env_name:
        return explicit_env_name
    if not isinstance(strategy, dict):
        return fallback
    if str(strategy.get('env_action') or '') == 'create_new_project_env':
        proposed = str(strategy.get('recommended_env_name') or '').strip()
        if proposed:
            return proposed
    return fallback


def env_bootstrap_should_run(paths, cfg: dict, env_name: str, repo: str, strategy: dict) -> tuple[bool, str]:
    action = str(strategy.get('env_action') or '').strip()
    locked = environment_is_locked(paths, cfg, env_name, repo)
    if action == 'reuse_existing_env' and locked:
        return False, 'Claude strategy says to reuse the existing conda env; it is already present/locked.'
    if action == 'defer_until_repo_selected':
        return False, 'Claude strategy defers conda changes until a repo is accepted.'
    if action == 'repair_existing_env':
        return True, 'Claude strategy says to repair the current project env from local missing-dependency evidence.'
    if action == 'create_new_project_env':
        return True, 'Claude strategy says to create/use a new project-specific env; The workflow will not delete any old env.'
    if locked:
        return False, 'Environment is already present/locked and Claude did not request a repair/new project env.'
    return True, 'No locked env exists yet; first-time bootstrap is allowed.'




def _query_placeholder_key(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').lower())


def _query_looks_like_project_id(value: str, project: str) -> bool:
    text = str(value or '').strip()
    if not text:
        return True
    project_key = _query_placeholder_key(project)
    text_key = _query_placeholder_key(text)
    if not project_key:
        return False
    if text_key == project_key:
        return True
    # Generated smoke/default prompts can repeat the project id; those are still
    # placeholders and should never become repo-search queries.
    return bool(text_key and not text_key.replace(project_key, ''))


def _append_query(queries: list[str], value: object, project: str) -> None:
    text = ' '.join(str(value or '').split()).strip()
    lowered = text.lower()
    if not text or _query_looks_like_project_id(text, project):
        return
    if any(marker in lowered for marker in ['no repo selected', 'no repo has been selected', 'no audited repos exist', 'zero audited candidates', 'zero evidence-ready', 'initial search phase', 'future memory', 'none satisfied', 'none data-ready', 'no data-ready', 'current search found', 'needs-more-search', 'if no repo is good enough', 'after auditing', 'after reviewing', 'selected_active_repo=none']):
        return
    if lowered.startswith('topic ') and _query_looks_like_project_id(lowered.removeprefix('topic '), project):
        return
    generic_find_markers = [
        'ideas that directly help the current research loop',
        'the workflow should prioritize papers and ideas that directly help the current research loop',
    ]
    project_key = _query_placeholder_key(project)
    text_key = _query_placeholder_key(text)
    if any(marker in lowered for marker in generic_find_markers) and (
        lowered in generic_find_markers
        or 'research goal:' in lowered
        or bool(project_key and project_key in text_key)
    ):
        return
    # Search backends work better with concise, evidence-bearing phrases than
    # long generated paragraphs. Keep titles intact, trim only oversized text.
    if len(text) > 180:
        text = text[:180].rsplit(' ', 1)[0].strip() or text[:180]
    if text:
        queries.append(text)


def _quoted_search_terms(text: object) -> list[str]:
    raw = str(text or '')
    terms = re.findall(r"['\"]([^'\"]{4,120})['\"]", raw)
    out: list[str] = []
    for term in terms:
        cleaned = ' '.join(term.split()).strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _stewardship_short_phrases(text: object) -> list[str]:
    raw = ' '.join(str(text or '').replace('；', ',').replace('，', ',').split())
    if not raw:
        return []
    first_sentence = re.split(r'[。.!?]\s+', raw, maxsplit=1)[0]
    first_sentence = re.sub(r'^(?:Search|Look)\s+for\s+(?:repositories|repos)\s+(?:explicitly\s+)?(?:related\s+to\s+)?', '', first_sentence, flags=re.I)
    first_sentence = re.sub(r'^Prioritize\s+(?:repositories|repos)\s+(?:with\s+)?', '', first_sentence, flags=re.I)
    pieces = re.split(r',|\bor\b|\band\b|/|、|\s+for\s+', first_sentence)
    phrases: list[str] = []
    for piece in pieces:
        cleaned = ' '.join(piece.split()).strip(' .;:')
        cleaned = re.sub(r'^(?:with|for|the|a|an|or)\s+', '', cleaned, flags=re.I)
        cleaned = re.sub(r'^(?:evaluating|evaluate|evaluation\s+of)\s+', '', cleaned, flags=re.I)
        if 4 <= len(cleaned) <= 90 and not re.search(r'\b(no|none|without|lack|lacking|missing)\b', cleaned, flags=re.I):
            phrases.append(cleaned)
    out: list[str] = []
    for phrase in phrases:
        if phrase and phrase.lower() not in {item.lower() for item in out}:
            out.append(phrase)
    return out



def _stewardship_memory_is_search_guidance(text: object) -> bool:
    lowered = str(text or '').lower().strip()
    if not lowered:
        return False
    if lowered.startswith(('after auditing', 'after reviewing', 'after the audit')) or any(marker in lowered for marker in ['none satisfied', 'none data-ready', 'current search found', 'needs-more-search', 'no repo selected', 'future memory']):
        return False
    if any(marker in lowered for marker in ['no repo has been selected', 'no audited repos exist', 'zero audited candidates', 'zero evidence-ready', 'initial search phase']):
        return False
    if any(marker in lowered for marker in ['search for', 'priority search', 'target repos', 'target repositories', 'next search', 'continue searching for']):
        return True
    return False


def _current_find_query_context(paths, project: str) -> list[str]:
    queries: list[str] = []

    def selected_row(rows: object, selected_id: str, id_keys: tuple[str, ...]) -> dict:
        if not isinstance(rows, list):
            return {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get('selected_for_execution') or row.get('execute_next'):
                return row
            selection = row.get('execution_selection')
            if isinstance(selection, dict) and selection.get('selected'):
                return row
            if selected_id and any(str(row.get(key) or '').strip() == selected_id for key in id_keys):
                return row
        return {}

    # Repository search is usually English-keyword based. Prefer paper titles
    # and code/dataset anchors before generated plan prose, which may be Chinese.
    find_results = load_json(paths.planning / 'finding' / 'find_results.json', {})
    if isinstance(find_results, dict):
        paper_rows: list[dict] = []
        for key in ('articles', 'read_candidates', 'strong_recommendations', 'recommended_papers', 'papers'):
            rows = find_results.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    paper_rows.append(row)
        seen_titles: set[str] = set()
        for row in paper_rows[:12]:
            title = str(row.get('title') or '').strip()
            title_key = title.lower()
            if title and title_key not in seen_titles:
                seen_titles.add(title_key)
                _append_query(queries, f'{title} code dataset', project)

    plans_payload = load_json(paths.planning / 'finding' / 'plans.json', {})
    if isinstance(plans_payload, dict):
        selected_plan = selected_row(plans_payload.get('plans'), str(plans_payload.get('selected_plan_id') or '').strip(), ('plan_id', 'id'))
        if selected_plan:
            for key in ('title', 'objective', 'summary', 'research_question', 'description'):
                _append_query(queries, selected_plan.get(key), project)

    ideas_payload = load_json(paths.planning / 'finding' / 'ideas.json', {})
    if isinstance(ideas_payload, dict):
        selected_idea = selected_row(ideas_payload.get('ideas'), str(ideas_payload.get('selected_idea_id') or '').strip(), ('id', 'idea_id'))
        if selected_idea:
            for key in ('title', 'objective', 'summary', 'hypothesis', 'method'):
                _append_query(queries, selected_idea.get(key), project)

    if isinstance(find_results, dict):
        stage0 = find_results.get('stage0_profile') if isinstance(find_results.get('stage0_profile'), dict) else {}
        profile = stage0.get('profile') if isinstance(stage0.get('profile'), dict) else {}
        explicit = profile.get('explicit_profile') if isinstance(profile.get('explicit_profile'), dict) else {}
        summary = explicit.get('research_interest_summary')
        for phrase in _stewardship_short_phrases(summary):
            _append_query(queries, phrase, project)
        _append_query(queries, summary, project)
        retrieval_text = stage0.get('retrieval_text')
        for phrase in _stewardship_short_phrases(retrieval_text):
            _append_query(queries, phrase, project)
        _append_query(queries, retrieval_text, project)
    return queries


def _stewardship_query_context(paths, project: str) -> list[str]:
    queries: list[str] = []
    sources = [
        load_json(paths.reports / 'repo_topic_fit_decision.json', {}),
        (load_json(paths.state / 'evidence_ready_repo_selection.json', {}) or {}).get('claude_topic_decision', {}),
    ]
    for payload in sources:
        if not isinstance(payload, dict):
            continue
        memory = payload.get('stewardship_memory')
        if _stewardship_memory_is_search_guidance(memory):
            for term in _quoted_search_terms(memory):
                _append_query(queries, term, project)
            for phrase in _stewardship_short_phrases(memory):
                _append_query(queries, phrase, project)
            first_sentence = re.split(r'[。.!?]\s+', str(memory or '').strip(), maxsplit=1)[0]
            _append_query(queries, first_sentence, project)
        for key in ['data_action_reason', 'repo_action_reason', 'rationale']:
            for term in _quoted_search_terms(payload.get(key)):
                _append_query(queries, term, project)
    return queries


def project_search_queries(project: str) -> list[str]:
    cfg = load_project_config(project)
    paths = build_paths(project)
    explicit_queries: list[str] = []
    config_context: list[str] = []
    if isinstance(cfg, dict):
        for key in ('queries', 'github_queries', 'repo_search_queries', 'literature_queries'):
            values = cfg.get(key)
            if isinstance(values, list):
                for value in values:
                    _append_query(explicit_queries, value, project)
        for key in ('topic', 'research_interest', 'user_prompt'):
            _append_query(config_context, cfg.get(key), project)
    queries: list[str] = []
    queries.extend(explicit_queries)
    queries.extend(_stewardship_query_context(paths, project))
    # If the saved config topic is only a project id, the current Find/Plan
    # outputs are the authoritative research context for environment search.
    queries.extend(_current_find_query_context(paths, project))
    queries.extend(config_context)
    seen: set[str] = set()
    out: list[str] = []
    for query in queries:
        key = query.lower()
        if key and key not in seen and not _query_looks_like_project_id(query, project):
            seen.add(key)
            out.append(query)
    return out or ['reproducible scientific code with real dataset']

def expand_repo_search(project: str, round_index: int, limit: int = 6, fresh_find_run_id: str = '') -> None:
    queries = project_search_queries(project)
    query = queries[(round_index - 1) % len(queries)]
    print(f"TASTE autonomous repo-search round {round_index}: {query}", flush=True)
    print("Environment repo search ignores Find-only source toggles; repo/data audit needs code evidence.", flush=True)
    github_cmd = module_cmd('finding', 'discover_github', '--project', project, '--query', query, '--limit', str(limit), '--sort', 'stars', '--order', 'desc', '--ignore-source-selection', '--candidate-source', 'environment_expanded_github_search')
    if fresh_find_run_id:
        github_cmd.extend(["--fresh-find-run-id", fresh_find_run_id])
    run_optional(github_cmd, ROOT)
    run_optional(module_cmd('finding', 'discover_arxiv', '--project', project, '--query', query, '--max-results', '5', '--ignore-source-selection'), ROOT)
    run_optional(module_cmd('finding', 'ingest_discovery', '--project', project, '--limit', '12'), ROOT)
    run_optional(module_cmd('environment', 'assess_repo', '--project', project), ROOT)
    run_optional(module_cmd('environment', 'candidate_pool', '--project', project, '--limit', str(limit), '--include-watch', '--use-cursor'), ROOT)

def append_search_memory(paths, payload: dict) -> None:
    history_path = paths.state / 'repo_search_iteration_memory.json'
    history = load_json(history_path, [])
    if not isinstance(history, list):
        history = []
    history.append(payload)
    history_path.write_text(json.dumps(history[-30:], indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def mark_pending_candidate_non_executable(paths, pending: dict, repo: str, reason: str, return_code: int = 2) -> None:
    rows = load_json(paths.state / 'repo_candidates.json', [])
    if not isinstance(rows, list):
        return
    pending_name = str(pending.get('name') or pending.get('repo') or '').strip()
    pending_url = str(pending.get('url') or '').strip()
    try:
        repo_resolved = str(Path(repo).expanduser().resolve()) if repo else ''
    except Exception:
        repo_resolved = str(repo or '')
    updated = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_path = str(row.get('local_path') or row.get('repo_path') or '').strip()
        try:
            row_path_resolved = str(Path(row_path).expanduser().resolve()) if row_path else ''
        except Exception:
            row_path_resolved = row_path
        same = bool(
            (repo_resolved and row_path_resolved and repo_resolved == row_path_resolved)
            or (pending_name and str(row.get('name') or '').strip() == pending_name)
            or (pending_url and str(row.get('url') or '').strip() == pending_url)
        )
        if not same:
            continue
        row['repo_execution_ready'] = False
        row['repo_selection_bucket'] = 'environment_non_executable'
        row['environment_bootstrap_blocked'] = True
        row['environment_bootstrap_blocker'] = reason
        row['environment_bootstrap_return_code'] = return_code
        row['environment_bootstrap_blocked_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        updated = True
    if updated:
        (paths.state / 'repo_candidates.json').write_text(json.dumps(rows, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def write_repo_selection_blocker(paths, reason: str, *, selection: dict | None = None) -> None:
    selection = selection or {}
    selected = selection.get('selected', {}) if isinstance(selection.get('selected'), dict) else {}
    current_plan_id, current_idea_id = current_selected_execution_ids(paths)
    run_id = str(selection.get('fresh_find_run_id') or current_find_run_id(paths) or '').strip()
    selected_plan_id = str(selection.get('selected_plan_id') or selected.get('selected_plan_id') or current_plan_id or '').strip()
    selected_idea_id = str(selection.get('selected_idea_id') or selected.get('selected_idea_id') or current_idea_id or '').strip()
    payload = {
        'status': 'blocked',
        'blocker_type': 'environment_repo_selection_blocked',
        'fresh_find_run_id': run_id,
        'selected_plan_id': selected_plan_id,
        'selected_idea_id': selected_idea_id,
        'reason': reason,
        'selection_gate': selection.get('selection_gate', ''),
        'selection_stage': selection.get('selection_stage', ''),
        'selected': selected,
        'rejected_selected': selection.get('rejected_selected', {}),
        'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    (paths.state / 'repo_selection_blocker.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def write_fresh_base_implementation_blocker(paths, run_id: str, reason: str) -> None:
    selected_plan_id, selected_idea_id = current_selected_execution_ids(paths)
    payload = {
        'status': 'blocked_environment_repo_selection_required',
        'fresh_find_run_id': run_id,
        'selected_plan_id': selected_plan_id,
        'selected_idea_id': selected_idea_id,
        'reason': reason,
        'repo': {},
        'ready_datasets': [],
        'blocked_datasets': [],
        'blocker_reasons': [reason],
        'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'policy': 'Implementation plans are valid only for the current Find run after environment_claude_code selects an evidence-ready repo.',
    }
    (paths.state / 'fresh_base_implementation_plan.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def clerepo_selection_blocker(paths) -> None:
    blocker = paths.state / 'repo_selection_blocker.json'
    if blocker.exists():
        blocker.unlink()


def write_iteration_strategy(paths) -> None:
    report = paths.reports / 'repo_search_iteration_strategy.md'
    lines = [
        '# Repo Search Iteration Strategy\n\n',
        'TASTE now treats repo selection as an autonomous research loop rather than a one-shot data fallback.\n\n',
        '## Borrowed Patterns\n',
        '- ARIS pattern: executor evidence is reviewed adversarially; unsupported or off-topic success claims are rejected instead of carried forward.\n',
        '- EvoScientist pattern: failed validations become persistent memory, so the next search round uses prior rejections as search pressure rather than repeating them.\n',
        '- writing pattern: discovery and verification are decoupled; high-throughput candidate discovery feeds slower sequential verification and refinement.\n\n',
        '## Repo Gate\n',
        '- A repo must satisfy code entrypoint and real dataset loader success before it can be considered for experiments.\n',
        '- Topic fit is dynamic: Claude Code judges whether a repo is directly aligned or the best transformable base for the current project topic.\n',
        '- Claude Code can accept a runnable/data-ready repo with explicit required modifications, or reject it and trigger another search/audit round.\n',
    ]
    report.write_text(''.join(lines), encoding='utf-8')


def maybe_switch_to_evidence_ready_repo(project: str, paths, env_name: str, current_repo: str, max_rounds: int = 3) -> str:
    write_iteration_strategy(paths)
    if has_claim_ready_probe(paths, current_repo) and claude_accepts_current_route(paths):
        print('Active repo has claim-ready real data and Claude has accepted it as the current best aligned/transformable route; no repo switch needed.', flush=True)
        return current_repo
    print('Active repo lacks either claim-ready data evidence or Claude acceptance as the best transformable route; The workflow will iterate repo discovery/audit until a paired route is found or explicitly rejected.', flush=True)
    selector = [
        *module_cmd('environment', 'select_evidence_ready', '--project', project),
        '--env-name', env_name, '--limit', '12', '--timeout-sec', '180',
        '--allow-veto-fallback', '--write-active', '--use-claude-review',
        '--selection-stage', 'environment_claude_code',
    ]
    for round_index in range(1, max(1, max_rounds) + 1):
        print(f'TASTE repo-selection iteration {round_index}/{max_rounds}', flush=True)
        selector_timeout = int(os.environ.get('ENV_REPO_SELECTOR_TIMEOUT_SEC', '900') or '900')
        rc = run_optional(selector, ROOT, timeout=selector_timeout)
        selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
        active = load_json(paths.state / 'active_repo.json', {})
        next_repo = str(active.get('repo_path', '') if isinstance(active, dict) else '')
        append_search_memory(paths, {
            'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(),
            'round': round_index,
            'selector_return_code': rc,
            'selection_gate': selection.get('selection_gate', ''),
            'selected': selection.get('selected', {}),
            'rejected_selected': selection.get('rejected_selected', {}),
            'audited_count': selection.get('audited_count', 0),
            'evidence_ready_count': selection.get('evidence_ready_count', 0),
            'repo_env_strategy': load_repo_env_strategy(paths),
        })
        if isinstance(selection, dict) and str(selection.get('selection_gate') or '') == BASE_SWITCH_SELECTION_GATE:
            collect_candidate_base_switch_evidence(project, paths, env_name)
            selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
            if str(selection.get('selection_gate') or '') == BASE_SWITCH_SELECTION_GATE and not current_env_selection_valid(paths):
                write_repo_selection_blocker(paths, 'Loader/data-ready base-switch candidate remains proposal-only because deterministic reference/audit gates are incomplete.', selection=selection)
                return current_repo
        if rc == 0 and next_repo and Path(next_repo).exists() and claude_accepts_current_route(paths):
            if next_repo != current_repo:
                print(f'TASTE switched active repo to Claude-accepted evidence-ready/transformable route: {next_repo}', flush=True)
            else:
                print('TASTE kept active repo after Claude accepted it as the current best transformable route.', flush=True)
            clerepo_selection_blocker(paths)
            return next_repo
        expand_repo_search(project, round_index)
    latest_selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    reason = 'Claude Code did not accept any audited repo as the current best evidence-ready and transformable route after autonomous search rounds; current repo remains blocked and must not be treated as final.'
    print(reason, flush=True)
    write_repo_selection_blocker(paths, reason, selection=latest_selection if isinstance(latest_selection, dict) else {})
    return current_repo


def main() -> int:
    parser = argparse.ArgumentParser(description='TASTE web stage 1: repo/data/env preparation with honest gates.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--repo-path', default='')
    parser.add_argument('--env-name', default='')
    parser.add_argument('--real-bootstrap-env', action='store_true')
    parser.add_argument('--repo-search-rounds', type=int, default=3)
    parser.add_argument('--venue', default='')
    parser.add_argument('--skip-reference-repair', action='store_true')
    args = parser.parse_args()
    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    cli_env_name = str(args.env_name or '').strip()
    configured_env_name = str(cfg.get('conda_env', '') or '').strip()
    env_name = cli_env_name or configured_env_name or f"{args.project}_env".replace('-', '_')
    if args.repo_path:
        repo = infer_repo(paths, args.repo_path)
    elif current_env_selection_valid(paths):
        repo = infer_repo(paths, '')
    else:
        repo = select_current_run_environment_repo(args.project, paths, env_name, max_rounds=args.repo_search_rounds, venue=args.venue, allow_env_bootstrap=args.real_bootstrap_env)
    env_name = current_machine_aware_env_name(paths, repo) or cli_env_name or configured_env_name or f"{args.project}_{Path(repo).name}".replace('-', '_')
    print(f'TASTE environment stage: project={args.project}', flush=True)
    print(f'selected repo={repo}', flush=True)
    print(f'conda env={env_name}', flush=True)
    run([sys.executable, 'framework/scripts/setup_git_guardrails.py', '--project', args.project, '--repo-path', repo], ROOT)
    refresh_repo_data(args.project, repo, env_name)
    if current_env_selection_valid(paths):
        repo = maybe_switch_to_evidence_ready_repo(args.project, paths, env_name, repo, max_rounds=args.repo_search_rounds)
    strategy = load_repo_env_strategy(paths)
    recommended_env_name = str(strategy.get('recommended_env_name') or '').strip() if isinstance(strategy, dict) else ''
    env_from_plan = current_machine_aware_env_name(paths, repo)
    if env_from_plan:
        env_name = env_from_plan
    elif configured_env_name and environment_is_locked(paths, cfg, configured_env_name, repo):
        env_name = configured_env_name
    else:
        env_name = strategy_env_name(strategy, env_name, explicit_env_name=cli_env_name)
    if strategy:
        if cli_env_name and recommended_env_name and recommended_env_name != cli_env_name:
            print(
                f"Explicit environment name {cli_env_name} overrides Claude recommended_env_name={recommended_env_name}; "
                "environment stage will repair/reuse the configured environment instead of switching names.",
                flush=True,
            )
        print(
            f"Claude stewardship strategy: repo_action={strategy.get('repo_action', '')} "
            f"env_action={strategy.get('env_action', '')} data_action={strategy.get('data_action', '')} "
            f"env_name={env_name}",
            flush=True,
        )
    run([sys.executable, 'framework/scripts/setup_git_guardrails.py', '--project', args.project, '--repo-path', repo], ROOT)
    refresh_repo_data(args.project, repo, env_name)
    run(module_cmd('environment', 'fresh_base_plan', '--project', args.project), ROOT)
    should_bootstrap, bootstrap_reason = env_bootstrap_should_run(paths, cfg, env_name, repo, strategy)
    if not should_bootstrap:
        print(bootstrap_reason, flush=True)
    else:
        print(bootstrap_reason, flush=True)
        if args.real_bootstrap_env:
            env_name, plan_path, bootstrap_rc = run_machine_aware_env_bootstrap(
                args.project,
                paths,
                repo,
                env_name,
                cfg,
                reason='Environment stage is about to create or repair the selected repo environment; decide a local-machine-aware plan before conda mutation.',
            )
            if bootstrap_rc != 0:
                reason = 'Machine-aware environment bootstrap failed after Claude Code planning/replanning; experiments remain blocked until project Claude produces an executable local-machine-aware plan.'
                write_repo_selection_blocker(paths, reason, selection=load_json(paths.state / 'evidence_ready_repo_selection.json', {}))
                write_fresh_base_implementation_blocker(paths, current_find_run_id(paths), reason)
                raise SystemExit(2)
        else:
            bootstrap = module_cmd('environment', 'bootstrap', '--project', args.project, '--repo-path', repo, '--env-name', env_name, '--verify-only', '--prepare-only')
            run(bootstrap, ROOT)
    run(module_cmd('environment', 'data_policy', '--project', args.project), ROOT)
    audit_reference_cmd = module_cmd('experimenting', 'reference_reproduction', '--project', args.project)
    if args.venue:
        audit_reference_cmd.extend(['--venue', args.venue])
    run_optional(audit_reference_cmd, ROOT)
    if not args.skip_reference_repair:
        repair_reference_reproduction_if_needed(args.project, paths, venue=args.venue)
        run_optional(audit_reference_cmd, ROOT)
    run_optional(module_cmd('experimenting', 'audit_iteration', '--project', args.project), ROOT)
    run_optional(module_cmd('writing', 'audit_evidence', '--project', args.project), ROOT)
    run_optional([sys.executable, 'framework/scripts/build_research_trajectory_system.py', '--project', args.project], ROOT)
    run_optional(module_cmd('planning', 'blocker_action', '--project', args.project), ROOT)
    run([sys.executable, 'framework/scripts/report_status.py', '--project', args.project], ROOT)
    print('TASTE environment stage complete. Environment creation is one-time; reruns only refresh read-only repo/data/status checks once locked. Formal experiment claims still require reference_reproduction_gate=pass and scientific_progress_gate=pass.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
