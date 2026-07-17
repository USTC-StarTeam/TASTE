#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from project.project_paths import ROOT, build_paths
from policies.current_find_route import current_find_run_id, repo_path_from_mapping as _repo_path_from_mapping
from project.project_config import project_target_venue
from reporting.work_status import append_guard_status as append_work_guard_status
from runtime.framework_io import read_json as load_json

BLOCKED_STATUS = "blocked_fresh_base_data_required"
BLOCKED_DECISION = "blocked_external_data_required"
FRESH_BASE_BLOCKED_STATUSES = {
    "blocked_fresh_base_data_required",
    "blocked_fresh_base_reference_probe_required",
    "blocked_fresh_base_reference_smoke_required",
    "blocked_fresh_base_reference_reproduction_required",
    "blocked_fresh_base_implementation_required",
}
FRESH_BASE_BLOCKED_DECISIONS = {
    "fresh_base_data_required",
    "fresh_base_reference_probe_required",
    "fresh_base_reference_smoke_required",
    "fresh_base_reference_reproduction_required",
    "fresh_base_implementation_required",
    "blocked_external_data_required",
}


def _status_text(payload: Any, *keys: str) -> list[str]:
    if not isinstance(payload, dict):
        return []
    return [str(payload.get(key) or "") for key in keys]



def current_selected_execution_ids(paths) -> tuple[str, str]:
    for candidate in [paths.state / "current_find_research_plan.json", paths.planning / "finding" / "plans.json"]:
        payload = load_json(candidate, {})
        if isinstance(payload, dict):
            plan_id = str(payload.get("selected_plan_id") or "").strip()
            idea_id = str(payload.get("selected_idea_id") or "").strip()
            if plan_id or idea_id:
                return plan_id, idea_id
    return "", ""


def _selected_base_viability_current_selection(paths, current_run: str = "") -> dict[str, Any]:
    gate = load_json(paths.state / "selected_base_viability_gate.json", {})
    if not isinstance(gate, dict):
        return {}
    status = str(gate.get("status") or "").lower()
    decision = str(gate.get("decision") or "").lower()
    if status != "blocked" or decision not in {"base_switch_gate_required", "continue_experiment_evidence_repair"}:
        return {}
    repo_path = _repo_path_from_mapping(gate)
    repo_name = str(gate.get("current_selected_repo") or "").strip()
    title = str(gate.get("selected_base_title") or gate.get("literature_base_title") or repo_name or "").strip()
    if not (repo_path or repo_name or title):
        return {}

    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    impl_repo = impl.get("repo") if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    guard = load_json(paths.state / "selected_base_route_guard.json", {})
    trusted = guard.get("trusted_audit") if isinstance(guard, dict) and isinstance(guard.get("trusted_audit"), dict) else {}
    aligned_paths: set[str] = set()
    for audit_name in ["fresh_base_reference_full_reproduction_audit.json", "fresh_base_reference_reproduction_audit.json"]:
        audit = load_json(paths.state / audit_name, {})
        selected = audit.get("selected_base") if isinstance(audit, dict) and isinstance(audit.get("selected_base"), dict) else {}
        audit_path = _repo_path_from_mapping(audit) or _repo_path_from_mapping(selected)
        if audit_path:
            aligned_paths.add(audit_path)
    for value in [_repo_path_from_mapping(impl_repo), _repo_path_from_mapping(trusted)]:
        if value:
            aligned_paths.add(value)
    if repo_path and aligned_paths and repo_path not in aligned_paths:
        return {}

    selected_run = str(gate.get("fresh_find_run_id") or (guard.get("selected_base_find_run_id") if isinstance(guard, dict) else "") or "").strip()
    selected_plan_id = str(gate.get("selected_plan_id") or (guard.get("selected_base_selected_plan_id") if isinstance(guard, dict) else "") or "").strip()
    selected_idea_id = str(gate.get("selected_idea_id") or (guard.get("selected_base_selected_idea_id") if isinstance(guard, dict) else "") or "").strip()
    ready_datasets = impl.get("ready_datasets", []) if isinstance(impl, dict) and isinstance(impl.get("ready_datasets"), list) else []
    selected = {
        "name": repo_name,
        "repo": repo_name,
        "repo_name": repo_name,
        "repo_path": repo_path,
        "local_path": repo_path,
        "title": title,
        "literature_base_title": title,
        "selected_base_title": title,
        "fresh_find_run_id": selected_run,
        "selection_stage": "environment_claude_code",
        "selected_by_stage": "environment_claude_code",
        "selection_gate": "selected_base_viability_gate_current_route",
        "decision": "continue_current_selected_base_evidence_repair",
        "claim_ready_datasets": ready_datasets,
        "ready_datasets": ready_datasets,
    }
    if ready_datasets:
        selected["claim_ready_dataset"] = str(ready_datasets[0])
    if selected_plan_id:
        selected["selected_plan_id"] = selected_plan_id
    if selected_idea_id:
        selected["selected_idea_id"] = selected_idea_id
    return {
        "valid": True,
        "current_find_run_id": current_run,
        "fresh_find_run_id": selected_run,
        "selection_stage": "environment_claude_code",
        "accepted_by_claude": True,
        "selected": selected,
        "selection_gate": "selected_base_viability_gate_current_route",
        "raw_selection_gate": str(gate.get("selection_gate") or "selected_base_viability_gate_current_route"),
        "reason": "selected_base_viability_current_route",
        "candidate_switch_conflict": True,
        "selected_plan_id": selected_plan_id,
        "selected_idea_id": selected_idea_id,
    }


def current_environment_selection(paths) -> dict[str, Any]:
    current_run = current_find_run_id(paths)
    current_plan_id, current_idea_id = current_selected_execution_ids(paths)
    viability_current = _selected_base_viability_current_selection(paths, current_run)
    if viability_current:
        viability_selected = viability_current.get("selected") if isinstance(viability_current.get("selected"), dict) else {}
        viability_plan_id = str(viability_current.get("selected_plan_id") or "").strip()
        viability_route_plan_id = str(viability_selected.get("selected_plan_id") or "").strip()
        viability_run_id = str(viability_current.get("fresh_find_run_id") or "").strip()
        viability_route_run_id = str(viability_selected.get("fresh_find_run_id") or "").strip()
        run_current = bool(not current_run or (viability_run_id == current_run and viability_route_run_id == current_run))
        plan_current = bool(not current_plan_id or (viability_plan_id == current_plan_id and viability_route_plan_id == current_plan_id))
        if not current_plan_id and run_current and plan_current:
            return {**viability_current, "selected_plan_id": viability_plan_id, "selected_idea_id": str(viability_current.get("selected_idea_id") or viability_selected.get("selected_idea_id") or "").strip(), "current_selected_plan_id": current_plan_id, "current_selected_idea_id": current_idea_id}
    selection = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    if not isinstance(selection, dict):
        return {"valid": False, "current_find_run_id": current_run, "selected": {}, "reason": "missing_evidence_ready_repo_selection", "current_selected_plan_id": current_plan_id, "current_selected_idea_id": current_idea_id}
    selected = selection.get("selected", {}) if isinstance(selection.get("selected"), dict) else {}
    selected_run = str(selection.get("fresh_find_run_id") or "").strip()
    selected_route_run = str(selected.get("fresh_find_run_id") or "").strip()
    selection_plan_id = str(selection.get("selected_plan_id") or "").strip()
    selected_route_plan_id = str(selected.get("selected_plan_id") or "").strip()
    stage = str(selection.get("selection_stage") or selection.get("selected_by_stage") or selected.get("selection_stage") or "").strip()
    decision = selection.get("claude_topic_decision") if isinstance(selection.get("claude_topic_decision"), dict) else {}
    raw_selection_gate = str(selection.get("selection_gate") or selected.get("selection_gate") or "").strip()
    pending_loader_selection = bool(
        raw_selection_gate in {"accepted_by_claude_transformable_pending_loader_bootstrap", "blocked_pending_data_loader_for_claude_best_candidate", "blocked_candidate_base_switch_gate_required"}
        or selected.get("pending_loader_bootstrap")
    )
    accepted = bool(
        not pending_loader_selection
        and (selection.get("accepted_by_claude") or raw_selection_gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")) or decision.get("accept_as_current_best"))
    )
    public_selection_gate = raw_selection_gate
    if accepted and not raw_selection_gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")):
        public_selection_gate = "accepted_by_claude_topic_fit"
    run_current = bool(current_run and selected_run == current_run and (not selected or selected_route_run == current_run))
    plan_current = bool(not current_plan_id or (selection_plan_id == current_plan_id and (not selected or selected_route_plan_id == current_plan_id)))
    valid = bool(current_run and selected and run_current and plan_current and stage == "environment_claude_code" and accepted)
    if valid:
        reason = "current_environment_base_selected"
    elif raw_selection_gate == "blocked_pending_data_loader_for_claude_best_candidate":
        reason = "environment_repo_selection_blocked_pending_loader_candidate"
    elif raw_selection_gate == "blocked_candidate_base_switch_gate_required":
        reason = "environment_repo_selection_blocked_candidate_base_switch_gate"
    elif current_run and not run_current:
        reason = "environment_selection_find_run_missing_or_stale"
    elif current_plan_id and not plan_current:
        reason = "environment_selection_selected_plan_missing_or_stale"
    else:
        reason = "environment_base_selection_pending_or_stale"
    selected_out = selected if valid else {}
    result = {"valid": valid, "current_find_run_id": current_run, "fresh_find_run_id": selected_run, "selected_plan_id": selection_plan_id, "selected_idea_id": str(selection.get("selected_idea_id") or selected.get("selected_idea_id") or current_idea_id or "").strip(), "current_selected_plan_id": current_plan_id, "current_selected_idea_id": current_idea_id, "selection_stage": stage, "accepted_by_claude": accepted if valid else False, "selected": selected_out, "selection_gate": public_selection_gate, "raw_selection_gate": raw_selection_gate, "reason": reason}
    if not valid and selected:
        result["blocked_selection"] = selected
    return result


def selected_base_label(paths) -> str:
    env = current_environment_selection(paths)
    selected = env.get("selected", {}) if isinstance(env.get("selected"), dict) else {}
    return str(selected.get("title") or selected.get("literature_base_title") or selected.get("selected_base_title") or selected.get("name") or selected.get("repo") or selected.get("repo_path") or "环境阶段选出的基底")


def _current_impl_repo_path(paths) -> str:
    env = current_environment_selection(paths)
    selected = env.get("selected", {}) if isinstance(env.get("selected"), dict) else {}
    if env.get("valid"):
        for key in ["repo_path", "local_path", "path"]:
            value = str(selected.get(key) or "").strip()
            if value:
                return value
    current_run = current_find_run_id(paths)
    active = load_json(paths.state / "active_repo.json", {})
    if isinstance(active, dict):
        gate = str(active.get("selection_gate") or "")
        active_run = str(active.get("fresh_find_run_id") or active.get("selected_by") or "").strip()
        stage = str(active.get("selection_stage") or active.get("selected_by_stage") or "").strip()
        if current_run and active_run == current_run and stage == "environment_claude_code" and gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")):
            for key in ["repo_path", "local_path", "path"]:
                value = str(active.get(key) or "").strip()
                if value:
                    return value
    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    impl_run = str(impl.get("fresh_find_run_id") or impl.get("current_find_run_id") or "").strip() if isinstance(impl, dict) else ""
    if current_run and impl_run != current_run:
        return ""
    repo = impl.get("repo", {}) if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    return str(repo.get("repo_path") or repo.get("local_path") or repo.get("path") or "").strip()


def _fresh_impl_for_current_route(paths, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    env = current_environment_selection(paths)
    if not env.get("valid"):
        return payload
    current_repo = _current_impl_repo_path(paths)
    repo = payload.get("repo", {}) if isinstance(payload.get("repo"), dict) else {}
    payload_repo = _repo_path_from_mapping(payload) or _repo_path_from_mapping(repo)
    if payload_repo and current_repo and payload_repo != current_repo:
        return {}
    if not payload_repo and str(payload.get("status") or "").startswith("blocked_"):
        return {}
    return payload


def _artifact_matches_current_repo(paths, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    current_repo = _current_impl_repo_path(paths)
    if not current_repo:
        return False
    for key in ["repo_path", "active_repo_path", "local_path", "path"]:
        value = str(payload.get(key) or "").strip()
        if value and value != current_repo:
            return False
    return True


def fresh_base_state_names(paths, suffix: str) -> list[str]:
    names = [f"fresh_base_{suffix}.json"]
    index = load_json(paths.state / "fresh_base_reference_reproduction_index.json", {})
    if isinstance(index, dict):
        for row in index.get("entries", []) if isinstance(index.get("entries", []), list) else []:
            if not isinstance(row, dict):
                continue
            value = str(row.get("state_audit_path") or "").strip()
            if value and value.endswith(f"{suffix}.json"):
                names.append(Path(value).name)
    return list(dict.fromkeys(names))


def _current_payload(paths, names: list[str]) -> dict[str, Any]:
    for name in names:
        payload = load_json(paths.state / name, {})
        if _artifact_matches_current_repo(paths, payload):
            return payload if isinstance(payload, dict) else {}
    return {}


def fresh_base_data_blocked(project: str) -> bool:
    return fresh_base_hard_gate_blocked(project)


def fresh_base_hard_gate_blocked(project: str) -> bool:
    paths = build_paths(project)
    full = load_json(paths.state / "full_research_cycle.json", {})
    reference = load_json(paths.state / "reference_reproduction_gate.json", {})
    blocker = load_json(paths.state / "blocker_action_plan.json", {})
    fresh = _fresh_impl_for_current_route(paths, load_json(paths.state / "fresh_base_implementation_plan.json", {}))
    data = load_json(paths.state / "fresh_base_data_acquisition.json", {})
    loader = load_json(paths.state / "real_dataset_probe.json", {})
    if not isinstance(loader, dict) or not loader:
        loader = _current_payload(paths, ["real_dataset_probe.json"] + fresh_base_state_names(paths, "loader_contract_probe"))
    protocol = _current_payload(paths, fresh_base_state_names(paths, "reference_protocol_probe"))
    smoke = _current_payload(paths, fresh_base_state_names(paths, "reference_smoke"))
    statuses = []
    statuses += _status_text(full, "status", "full_status")
    statuses += _status_text(fresh, "status")
    statuses += _status_text(loader, "status")
    statuses += _status_text(protocol, "status")
    statuses += _status_text(smoke, "status")
    decisions = []
    decisions += _status_text(reference, "decision")
    decisions += _status_text(data, "decision")
    decisions += _status_text(loader, "decision")
    decisions += _status_text(protocol, "decision")
    decisions += _status_text(smoke, "decision")
    categories: list[str] = []
    if isinstance(full, dict):
        current = full.get("current_blocker", {}) if isinstance(full.get("current_blocker"), dict) else {}
        categories.append(str(current.get("category") or ""))
        for row in full.get("latest_blockers", []) or []:
            if isinstance(row, dict):
                categories.append(str(row.get("category") or ""))
    if isinstance(blocker, dict):
        summary = blocker.get("summary", {}) if isinstance(blocker.get("summary"), dict) else {}
        categories.append(str(summary.get("top_route") or ""))
        for row in blocker.get("actions", []) or []:
            if isinstance(row, dict):
                categories.append(str(row.get("category") or row.get("route") or ""))
    reference_text = "\n".join(str(item) for item in (reference.get("blockers", []) if isinstance(reference, dict) else []))
    category_text = "\n".join(categories)
    return bool(
        any(status in FRESH_BASE_BLOCKED_STATUSES for status in statuses)
        or any(decision in FRESH_BASE_BLOCKED_DECISIONS for decision in decisions)
        or "fresh_base_data_contract" in categories
        or "fresh_base_reference_probe" in categories
        or "fresh_base_reference_smoke" in categories
        or "fresh_base_reference_reproduction" in categories
        or "fresh_base_implementation" in categories
        or any(token in reference_text for token in ["train_data.df", "data_statis.df", "emb.pickle", "bounded reference smoke", "reference protocol", "reference reproduction audit"])
        or any(token in category_text for token in ["fresh_base_data", "fresh_base_reference", "fresh_base_implementation"])
    )


def append_guard_status(project: str, entrypoint: str, venue: str, action: str, rc: int | str = "") -> None:
    try:
        append_work_guard_status(project, entrypoint, venue, action, rc)
    except Exception:
        pass


def run_safe_unblock(project: str, venue: str, *, download_timeout_sec: int = 120) -> int:
    message = (
        f"Must only inspect and repair the current Environment repository, data, Conda, and loader blockers for venue {venue or 'unspecified'}. "
        "Must write project-local evidence for every repair and finish with the exact remaining Environment gate."
    )
    cmd = [
        sys.executable,
        str(ROOT / "framework" / "scripts" / "main.py"),
        "module",
        "environment",
        "--action",
        "chat",
        "--project",
        project,
        "--message",
        message,
    ]
    proc = subprocess.run(cmd, cwd=ROOT, text=True)
    return int(proc.returncode or 0)


def guard_fresh_base_blocker_entry(
    project: str,
    venue: str = "",
    entrypoint: str = "",
    *,
    safe_unblock: bool = True,
) -> int | None:
    project = str(project or "").strip()
    if not project:
        return None
    venue = (venue or project_target_venue(project, "ICLR") or "ICLR").upper()
    entrypoint = entrypoint or "unknown_entrypoint"
    if not fresh_base_data_blocked(project):
        return None
    full = load_json(build_paths(project).state / "full_research_cycle.json", {})
    status = str(full.get("status") or full.get("full_status") or "blocked_fresh_base_gate_required") if isinstance(full, dict) else "blocked_fresh_base_gate_required"
    paths = build_paths(project)
    base_label = selected_base_label(paths)
    message = (
        f"{entrypoint}: {status}. 当前环境阶段选出的基底 {base_label} 仍有 fresh-base 硬门控未过；"
        "不会启动完整训练、论文写作、claim promotion、第二条 Find 或历史路线主线回退。"
    )
    print(message, flush=True)
    if safe_unblock:
        append_guard_status(project, entrypoint, venue, "safe_unblock_start")
        rc = run_safe_unblock(project, venue)
        append_guard_status(project, entrypoint, venue, "safe_unblock_finished", rc)
        return rc if rc != 0 else 2
    append_guard_status(project, entrypoint, venue, "blocked_no_unsafe_action", 2)
    return 2
