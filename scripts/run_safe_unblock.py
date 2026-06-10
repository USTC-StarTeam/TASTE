#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths, project_experiment_python_from_config
from work_status import append_safe_unblock_status
from project_config import project_target_venue
from reference_reproduction_state import (
    bounded_reference_audit_recorded,
    full_reference_audit_passed,
    latest_reference_audit,
    reference_full_job_state as indexed_reference_full_job_state,
)


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


def trim(value: Any, limit: int = 900) -> str:
    text = ' '.join(str(value or '').replace('\n', ' ').split())
    return text[:limit] + ('...' if len(text) > limit else '')




def current_find_run_id(paths) -> str:
    for candidate in [
        paths.planning / "finding" / "find_results.json",
        paths.state / "current_find_research_plan.json",
    ]:
        payload = load_json(candidate, {})
        if isinstance(payload, dict):
            run_id = str(payload.get("run_id") or payload.get("find_run_id") or "").strip()
            if run_id:
                return run_id
    return ""


def current_environment_selection(paths) -> dict[str, Any]:
    current_run = current_find_run_id(paths)
    selection = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    if not isinstance(selection, dict):
        return {"valid": False, "current_find_run_id": current_run, "selected": {}, "reason": "missing_evidence_ready_repo_selection"}
    selected = selection.get("selected", {}) if isinstance(selection.get("selected"), dict) else {}
    selected_run = str(selection.get("fresh_find_run_id") or selected.get("fresh_find_run_id") or "").strip()
    stage = str(selection.get("selection_stage") or selection.get("selected_by_stage") or selected.get("selection_stage") or "").strip()
    decision = selection.get("claude_topic_decision") if isinstance(selection.get("claude_topic_decision"), dict) else {}
    accepted = bool(
        selection.get("accepted_by_claude")
        or str(selection.get("selection_gate") or "").startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate"))
        or decision.get("accept_as_current_best")
    )
    valid = bool(current_run and selected and selected_run == current_run and stage == "environment_claude_code" and accepted)
    return {
        "valid": valid,
        "current_find_run_id": current_run,
        "fresh_find_run_id": selected_run,
        "selection_stage": stage,
        "accepted_by_claude": accepted,
        "selected": selected,
        "selection_gate": selection.get("selection_gate", ""),
        "reason": "current_environment_base_selected" if valid else "environment_base_selection_pending_or_stale",
    }


def selected_base_label(env: dict[str, Any]) -> str:
    selected = env.get("selected", {}) if isinstance(env.get("selected"), dict) else {}
    return str(
        selected.get("title")
        or selected.get("literature_base_title")
        or selected.get("selected_base_title")
        or selected.get("name")
        or selected.get("repo")
        or selected.get("repo_path")
        or "environment-stage Claude Code selection pending"
    )


def selected_base_has_current_adapter(paths) -> bool:
    repo_path = current_impl_repo_path(paths)
    repo = Path(repo_path) if repo_path else Path("")
    return bool(repo_path and repo.exists() and any((repo / name).exists() for name in ["main.py", "finetune.py", "run.sh", "train.py", "single_train.py"]))


def selected_base_blocked_payload(project: str, venue: str, env_selection: dict[str, Any], fresh_plan: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    base_label = selected_base_label(env_selection)
    repo = fresh_plan.get("repo", {}) if isinstance(fresh_plan.get("repo"), dict) else {}
    blockers = fresh_plan.get("blocker_reasons", []) if isinstance(fresh_plan.get("blocker_reasons"), list) else []
    status = str(fresh_plan.get("status") or "blocked_selected_base_adapter_required")
    return {
        "generated_at": now_iso(),
        "project": project,
        "target_venue": venue,
        "find_run_id": env_selection.get("current_find_run_id", ""),
        "main_base": base_label,
        "status": status,
        "full_status": status,
        "blocker": "selected_base_adapter_required" if env_selection.get("valid") else "environment_base_selection_required",
        "top_route": "selected_base_generic_gate",
        "data_status": "",
        "data_decision": "",
        "loader_status": "",
        "loader_decision": "",
        "reference_protocol_status": "",
        "reference_smoke_status": "",
        "reference_audit_status": "",
        "reference_audit_decision": "",
        "reference_audit_recorded": False,
        "reference_audit_artifact": "",
        "reference_full_job_status": "",
        "reference_full_job_pid": "",
        "reference_full_job_log": "",
        "reference_full_job_decision": "",
        "ready_datasets": fresh_plan.get("ready_datasets", []) if isinstance(fresh_plan.get("ready_datasets"), list) else [],
        "blocked_datasets": fresh_plan.get("blocked_datasets", []) if isinstance(fresh_plan.get("blocked_datasets"), list) else [],
        "latest_download_rc": "",
        "latest_download_timeout": "",
        "environment_selection": env_selection,
        "selected_base_repo": repo,
        "selected_base_blockers": blockers[:8],
        "steps": results,
        "guardrail": "No training, paper writing, claim promotion, second Find, pair_compare, or legacy main-route fallback while selected-base gates are blocked.",
    }


def project_probe_python(project: str) -> str:
    paths = build_paths(project)
    cfg = load_json(paths.config, {})
    return project_experiment_python_from_config(cfg if isinstance(cfg, dict) else {}, fallback_to_current=True)


def current_impl_repo_path(paths) -> str:
    env = current_environment_selection(paths)
    selected = env.get("selected", {}) if isinstance(env.get("selected"), dict) else {}
    if env.get("valid"):
        for key in ["repo_path", "local_path", "path"]:
            value = str(selected.get(key) or "").strip()
            if value:
                return value
    active = load_json(paths.state / "active_repo.json", {})
    if isinstance(active, dict):
        gate = str(active.get("selection_gate") or "")
        if gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")):
            for key in ["repo_path", "local_path", "path"]:
                value = str(active.get(key) or "").strip()
                if value:
                    return value
    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    repo = impl.get("repo", {}) if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    return str(repo.get("repo_path") or repo.get("local_path") or repo.get("path") or "").strip()


def artifact_matches_current_repo(paths, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    current_repo = current_impl_repo_path(paths)
    if not current_repo:
        return False
    for key in ["repo_path", "active_repo_path", "local_path", "path"]:
        value = str(payload.get(key) or "").strip()
        if value and value != current_repo:
            return False
    return True


def fresh_base_state_names(paths, suffix: str) -> list[str]:
    names = [f"fresh_base_{suffix}.json"]
    try:
        for path in sorted(paths.state.glob(f"*_{suffix}.json")):
            if path.name not in names:
                names.append(path.name)
    except Exception:
        pass
    return names


def reference_protocol_payload(paths) -> dict[str, Any]:
    for name in fresh_base_state_names(paths, "reference_protocol_probe"):
        payload = load_json(paths.state / name, {})
        if artifact_matches_current_repo(paths, payload):
            return payload
    return {}


def reference_smoke_payload(paths) -> dict[str, Any]:
    for name in fresh_base_state_names(paths, "reference_smoke"):
        payload = load_json(paths.state / name, {})
        if artifact_matches_current_repo(paths, payload):
            return payload
    return {}


def reference_protocol_passed(paths) -> bool:
    probe = reference_protocol_payload(paths)
    return bool(
        isinstance(probe, dict)
        and probe.get("status") == "reference_protocol_probe_passed"
        and probe.get("decision") == "ready_for_bounded_reference_smoke"
    )


def reference_smoke_passed(paths) -> bool:
    smoke = reference_smoke_payload(paths)
    return bool(
        isinstance(smoke, dict)
        and smoke.get("status") == "reference_smoke_passed"
        and smoke.get("decision") == "ready_for_reference_reproduction_audit"
    )


def reference_reproduction_audit_payload(paths, mode: str = "") -> dict[str, Any]:
    _, audit = latest_reference_audit(paths, mode or None)
    return audit if isinstance(audit, dict) else {}


def reference_reproduction_audit_recorded(paths) -> bool:
    # A verified paper-level full audit supersedes bounded mode and must never
    # trigger another bounded rerun just because the bounded state is absent.
    return bool(full_reference_audit_passed(paths) or bounded_reference_audit_recorded(paths))


def paper_level_reference_reproduction_passed(paths) -> bool:
    return full_reference_audit_passed(paths)


def pid_alive(pid: Any) -> bool:
    try:
        pid_i = int(pid)
    except Exception:
        return False
    if pid_i <= 0:
        return False
    try:
        os.kill(pid_i, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def reference_full_job_state(paths) -> dict[str, Any]:
    return indexed_reference_full_job_state(paths)


def selected_reference_dataset(paths) -> str:
    for name in ["fresh_base_implementation_plan.json", "fresh_base_reference_protocol_probe.json", "evidence_ready_repo_selection.json"]:
        payload = load_json(paths.state / name, {})
        if not isinstance(payload, dict):
            continue
        candidates = []
        if isinstance(payload.get("selected_dataset"), str):
            candidates.append(payload.get("selected_dataset"))
        if isinstance(payload.get("claim_ready_dataset"), str):
            candidates.append(payload.get("claim_ready_dataset"))
        selected = payload.get("selected") if isinstance(payload.get("selected"), dict) else {}
        if isinstance(selected.get("claim_ready_dataset"), str):
            candidates.append(selected.get("claim_ready_dataset"))
        if isinstance(payload.get("ready_datasets"), list):
            candidates.extend(payload.get("ready_datasets") or [])
        for value in candidates:
            dataset = str(value or "").strip()
            if dataset:
                return dataset
    return "dataset"


def ensure_reference_full_job(paths, project: str, venue: str, py: str) -> dict[str, Any]:
    """Start one wrapper-managed full reference reproduction job, or report the existing one."""
    state_path = paths.state / "fresh_base_reference_full_reproduction_job.json"
    state = reference_full_job_state(paths)
    if state.get("status") == "running" and pid_alive(state.get("pid")):
        return {**state, "already_running": True}
    audit = reference_reproduction_audit_payload(paths, "full")
    if isinstance(audit, dict) and audit.get("mode") == "full" and audit.get("return_code") == 0 and audit.get("audit_ready"):
        finished = {
            "project": project,
            "generated_at": now_iso(),
            "status": "completed",
            "decision": "ready_for_reference_gate_audit",
            "audit_path": str(audit.get("state_audit_path") or (paths.state / "fresh_base_reference_full_reproduction_audit.json")),
            "repo_path": current_impl_repo_path(paths),
            "artifact_dir": audit.get("artifact_dir", ""),
            "stdout_path": audit.get("stdout_path", ""),
            "paper_level_reproduction_passed": bool(audit.get("paper_level_reproduction_passed")),
        }
        save_json(state_path, finished)
        return finished
    log_dir = paths.logs / "safe_unblock"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"selected_base_full_reference_{stamp}.log"
    dataset = selected_reference_dataset(paths)
    expected_artifact_dir = paths.artifacts / "fresh_base_reference_reproduction" / f"selected_base_reference_full_{dataset}_30epoch_{stamp}"
    expected_stdout_path = expected_artifact_dir / "stdout_stderr.log"
    cmd = [
        py,
        str(ROOT / "scripts" / "run_selected_base_reference_reproduction_audit.py"),
        "--project", project,
        "--mode", "full",
        "--epoch", "30",
        "--timeout-sec", str(26 * 3600),
        "--execute",
    ]
    env = dict(os.environ)
    env["SELECTED_BASE_REFERENCE_STAMP"] = stamp
    with log_path.open("ab") as log_handle:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True)
    payload = {
        "project": project,
        "generated_at": now_iso(),
        "updated_at": now_iso(),
        "status": "running",
        "decision": "full_reference_reproduction_running",
        "pid": proc.pid,
        "wrapper_pid": proc.pid,
        "command": cmd,
        "log_path": str(log_path),
        "repo_path": current_impl_repo_path(paths),
        "artifact_dir": str(expected_artifact_dir),
        "stdout_path": str(expected_stdout_path),
        "timeout_sec": 26 * 3600,
        "method": "selected_base_reference",
        "dataset": dataset,
        "epoch": 30,
        "venue": venue,
        "guardrail": "Wrapper-managed selected-base reference reproduction; no paper writing, claim promotion, second Find, pair_compare, or legacy main-route fallback.",
    }
    save_json(state_path, payload)
    return payload

def run_step(name: str, cmd: list[str], *, timeout: int) -> dict[str, Any]:
    print(f'[safe-unblock] {name}: ' + ' '.join(cmd), flush=True)
    started = now_iso()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
        stdout = proc.stdout or ''
        stderr = proc.stderr or ''
        if stdout:
            print(stdout[-3000:], end='' if stdout.endswith('\n') else '\n', flush=True)
        if stderr:
            print(stderr[-3000:], end='' if stderr.endswith('\n') else '\n', file=sys.stderr, flush=True)
        return {
            'name': name,
            'command': cmd,
            'started_at': started,
            'finished_at': now_iso(),
            'return_code': proc.returncode,
            'timed_out': False,
            'stdout_tail': stdout[-3000:],
            'stderr_tail': stderr[-3000:],
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b'').decode('utf-8', 'ignore')
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b'').decode('utf-8', 'ignore')
        print(f'[safe-unblock] {name}: TIMEOUT after {timeout}s', file=sys.stderr, flush=True)
        return {
            'name': name,
            'command': cmd,
            'started_at': started,
            'finished_at': now_iso(),
            'return_code': 124,
            'timed_out': True,
            'stdout_tail': (stdout or '')[-3000:],
            'stderr_tail': ((stderr or '') + f'\nTIMEOUT after {timeout}s')[-3000:],
        }


def append_work_status(project: str, payload: dict[str, Any]) -> None:
    append_safe_unblock_status(project, payload)


def main() -> int:
    parser = argparse.ArgumentParser(description='Safe unblock loop for the current environment-stage selected fresh base.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    parser.add_argument('--download-timeout-sec', type=int, default=120)
    args = parser.parse_args()

    paths = build_paths(args.project)
    venue = (args.venue or project_target_venue(args.project, 'ICLR')).upper()
    py = sys.executable
    probe_py = project_probe_python(args.project)
    env_selection = current_environment_selection(paths)
    base_label = selected_base_label(env_selection)
    results = []
    results.append(run_step('fresh_base_implementation_plan', [py, str(ROOT / 'scripts' / 'build_fresh_base_implementation_plan.py'), '--project', args.project], timeout=180))
    fresh_plan = load_json(paths.state / 'fresh_base_implementation_plan.json', {})
    if isinstance(fresh_plan, dict) and fresh_plan.get('status') == 'implementation_ready_for_reference_probe':
        if not reference_protocol_passed(paths):
            results.append(run_step('selected_base_reference_protocol_probe', [probe_py, str(ROOT / 'scripts' / 'probe_selected_base_reference.py'), '--project', args.project, '--mode', 'protocol', '--timeout-sec', '240'], timeout=300))
        if reference_protocol_passed(paths) and not reference_smoke_passed(paths):
            results.append(run_step('selected_base_reference_smoke', [probe_py, str(ROOT / 'scripts' / 'probe_selected_base_reference.py'), '--project', args.project, '--mode', 'smoke', '--timeout-sec', '240'], timeout=300))
        if reference_smoke_passed(paths) and not reference_reproduction_audit_recorded(paths):
            results.append(run_step('selected_base_reference_reproduction_audit_bounded', [py, str(ROOT / 'scripts' / 'run_selected_base_reference_reproduction_audit.py'), '--project', args.project, '--mode', 'bounded', '--epoch', '1', '--timeout-sec', '900', '--execute'], timeout=960))
        if reference_smoke_passed(paths) and reference_reproduction_audit_recorded(paths) and not paper_level_reference_reproduction_passed(paths):
            full_job = ensure_reference_full_job(paths, args.project, venue, py)
            results.append({'name': 'selected_base_reference_reproduction_full_job', 'started_at': full_job.get('generated_at', now_iso()), 'finished_at': now_iso(), 'return_code': 0 if full_job.get('status') in {'running', 'completed'} else 2, 'timed_out': False, 'job': full_job})
    else:
        if not selected_base_has_current_adapter(paths):
            print(f'[safe-unblock] selected base {base_label} has no recognized runnable adapter yet; staying blocked.', flush=True)
    for name, cmd, timeout in [
        ('reference_reproduction_gate', [py, str(ROOT / 'scripts' / 'audit_reference_reproduction.py'), '--project', args.project, '--venue', venue], 180),
        ('blocker_action_plan', [py, str(ROOT / 'scripts' / 'build_blocker_action_plan.py'), '--project', args.project, '--venue', venue], 180),
    ]:
        results.append(run_step(name, cmd, timeout=timeout))

    full = load_json(paths.state / 'full_research_cycle.json', {})
    blocker = load_json(paths.state / 'blocker_action_plan.json', {})
    fresh = load_json(paths.state / 'fresh_base_implementation_plan.json', {})
    data = load_json(paths.state / 'fresh_base_data_acquisition.json', {})
    real_probe = load_json(paths.state / 'real_dataset_probe.json', {})
    find_plan = load_json(paths.state / 'current_find_research_plan.json', {})
    ready = fresh.get('ready_datasets', []) if isinstance(fresh, dict) and isinstance(fresh.get('ready_datasets'), list) else []
    blocked = fresh.get('blocked_datasets', []) if isinstance(fresh, dict) and isinstance(fresh.get('blocked_datasets'), list) else []
    attempts = data.get('attempts', []) if isinstance(data, dict) and isinstance(data.get('attempts'), list) else []
    latest_attempt = attempts[-1] if attempts else {}
    top_route = blocker.get('summary', {}).get('top_route', '') if isinstance(blocker.get('summary'), dict) else ''
    loader_passed = bool(isinstance(fresh, dict) and fresh.get('status') == 'implementation_ready_for_reference_probe' and ready)
    protocol_passed = reference_protocol_passed(paths)
    smoke_passed = reference_smoke_passed(paths)
    reference_audit = reference_reproduction_audit_payload(paths, 'bounded')
    audit_recorded = reference_reproduction_audit_recorded(paths)
    full_job = reference_full_job_state(paths)
    full_job_running = bool(full_job.get('status') == 'running' and pid_alive(full_job.get('pid')))
    full_status = full.get('status', '') if isinstance(full, dict) else ''
    current_blocker = full.get('current_blocker', {}) if isinstance(full, dict) and isinstance(full.get('current_blocker'), dict) else {}
    inferred_blocker = 'fresh_base_data_required'
    if loader_passed and not protocol_passed:
        inferred_blocker = 'fresh_base_reference_probe_required'
    elif protocol_passed and not smoke_passed:
        inferred_blocker = 'fresh_base_reference_smoke_required'
    elif smoke_passed:
        inferred_blocker = 'fresh_base_reference_reproduction_required'
    blocker_category = str(current_blocker.get('category') or inferred_blocker)
    still_data_blocked = not loader_passed
    status = full_status or ('blocked_fresh_base_data_required' if not loader_passed else 'blocked_fresh_base_reference_probe_required' if not protocol_passed else 'blocked_fresh_base_reference_smoke_required' if not smoke_passed else 'blocked_fresh_base_reference_reproduction_required')
    protocol_payload = reference_protocol_payload(paths)
    smoke_payload = reference_smoke_payload(paths)
    payload = {
        'generated_at': now_iso(),
        'project': args.project,
        'target_venue': venue,
        'find_run_id': find_plan.get('run_id', '') if isinstance(find_plan, dict) else '',
        'main_base': base_label,
        'status': status,
        'full_status': full_status,
        'blocker': blocker_category,
        'top_route': top_route,
        'data_status': data.get('status', '') if isinstance(data, dict) else '',
        'data_decision': data.get('decision', '') if isinstance(data, dict) else '',
        'loader_status': real_probe.get('status', '') if isinstance(real_probe, dict) else '',
        'loader_decision': 'loader_contract_passed' if loader_passed else '',
        'reference_protocol_status': protocol_payload.get('status', '') if isinstance(protocol_payload, dict) else '',
        'reference_smoke_status': smoke_payload.get('status', '') if isinstance(smoke_payload, dict) else '',
        'reference_audit_status': reference_audit.get('status', '') if isinstance(reference_audit, dict) else '',
        'reference_audit_decision': reference_audit.get('decision', '') if isinstance(reference_audit, dict) else '',
        'reference_audit_recorded': audit_recorded,
        'reference_audit_artifact': reference_audit.get('artifact_dir', '') if isinstance(reference_audit, dict) else '',
        'reference_full_job_status': full_job.get('status', ''),
        'reference_full_job_pid': full_job.get('pid', ''),
        'reference_full_job_log': full_job.get('log_path', ''),
        'reference_full_job_decision': full_job.get('decision', ''),
        'ready_datasets': ready,
        'blocked_datasets': blocked,
        'latest_download_rc': latest_attempt.get('return_code', ''),
        'latest_download_timeout': latest_attempt.get('timed_out', ''),
        'environment_selection': env_selection,
        'selected_base_repo': fresh.get('repo', {}) if isinstance(fresh, dict) else {},
        'selected_base_blockers': fresh.get('blocker_reasons', [])[:8] if isinstance(fresh, dict) and isinstance(fresh.get('blocker_reasons'), list) else [],
        'steps': results,
        'guardrail': 'No training, paper writing, claim promotion, second Find, pair_compare, or legacy main-route fallback while selected-base gates are blocked.',
    }
    save_json(paths.state / 'safe_unblock.json', payload)
    append_work_status(args.project, payload)
    print('[safe-unblock] summary: ' + json.dumps({k: payload[k] for k in ['status', 'target_venue', 'loader_decision', 'reference_protocol_status', 'reference_smoke_status', 'reference_audit_status', 'ready_datasets', 'blocked_datasets']}, ensure_ascii=False), flush=True)
    if still_data_blocked:
        print(f'[safe-unblock] still blocked: selected base {base_label} lacks loader-ready real data/protocol evidence.', flush=True)
        return 2
    if protocol_passed and not smoke_passed:
        print('[safe-unblock] data/loader and protocol ready; bounded reference smoke remains blocked or just failed.', flush=True)
        return 2
    if smoke_passed and str(status).startswith('blocked'):
        if paper_level_reference_reproduction_passed(paths):
            print('[safe-unblock] paper-level full reference reproduction audit has completed; gate refresh should advance next.', flush=True)
        elif full_job_running:
            print(f"[safe-unblock] full reference reproduction is running under wrapper pid={full_job.get('pid')} log={full_job.get('log_path')}", flush=True)
        elif audit_recorded:
            print('[safe-unblock] bounded reference reproduction audit artifact recorded; full reference reproduction job was requested or needs the next heartbeat to start.', flush=True)
        else:
            print('[safe-unblock] bounded reference smoke passed; still blocked at paper-level reference reproduction audit.', flush=True)
        return 2
    if str(status).startswith('blocked'):
        print('[safe-unblock] data/loader ready; still blocked at reference protocol/env manifest probe.', flush=True)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
