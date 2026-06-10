#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Any

from experiment_contracts import experiment_rows, metric_higher_is_better, parse_float, row_metric
from project_paths import build_paths, load_project_config
from reference_reproduction_state import (
    bounded_reference_audit_recorded,
    full_reference_audit_passed,
    import_full_audit_if_verified,
    is_full_pass_audit,
    latest_reference_audit,
    reference_full_job_state as indexed_reference_full_job_state,
)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_text(path: Path, limit: int = 200000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit] if path.exists() else ""
    except Exception:
        return ""


def one_line(value: Any, limit: int = 320) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    return text[:limit] + ("..." if len(text) > limit else "")



def fresh_base_data_required(plan: Any) -> bool:
    if not isinstance(plan, dict):
        return False
    if str(plan.get("status") or "") == "blocked_fresh_base_data_required":
        return True
    for key in ["fresh_base_data_acquisition", "data_acquisition"]:
        data = plan.get(key)
        if isinstance(data, dict) and str(data.get("decision") or "") == "blocked_external_data_required":
            return True
    blocked = plan.get("blocked_datasets", [])
    blockers = plan.get("blocker_reasons", []) if isinstance(plan.get("blocker_reasons"), list) else []
    joined = "\n".join(str(item).lower() for item in blockers)
    return bool(blocked) and any(term in joined for term in ["dataset", "loader", "google drive", "required file", "required_files", "dataset_contract", "missing_required_files"])


def fresh_base_loader_contract_passed(paths) -> bool:
    """Current selected-base loader gate, with legacy state-file compatibility."""
    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    real_probe = load_json(paths.state / "real_dataset_probe.json", {})
    if isinstance(impl, dict) and impl.get("status") == "implementation_ready_for_reference_probe":
        ready = impl.get("ready_datasets", []) if isinstance(impl.get("ready_datasets"), list) else []
        blockers = impl.get("blocker_reasons", []) if isinstance(impl.get("blocker_reasons"), list) else []
        repo = impl.get("repo", {}) if isinstance(impl.get("repo"), dict) else {}
        repo_path = str(repo.get("repo_path") or "").strip()
        probe_repo_path = str(real_probe.get("repo_path") or "").strip() if isinstance(real_probe, dict) else ""
        probe_rows = real_probe.get("probes", []) if isinstance(real_probe, dict) and isinstance(real_probe.get("probes"), list) else []
        has_loader_success = any(isinstance(row, dict) and row.get("claim_ready") and row.get("loader_probe_success") for row in probe_rows)
        if ready and not blockers and (not repo_path or not probe_repo_path or repo_path == probe_repo_path) and has_loader_success:
            return True
    data = load_json(paths.state / "fresh_base_data_acquisition.json", {})
    loader = current_payload(paths, ["real_dataset_probe.json"] + fresh_base_state_names(paths, "loader_contract_probe"))
    if not isinstance(data, dict) or not isinstance(loader, dict):
        return False
    ready = loader.get("ready_datasets", []) if isinstance(loader.get("ready_datasets"), list) else []
    return bool(
        ready
        and data.get("status") == "ready"
        and data.get("decision") == "ready_for_loader_probe"
        and loader.get("decision") == "loader_contract_passed"
    )



def fresh_base_state_names(paths, suffix: str) -> list[str]:
    names = [f"fresh_base_{suffix}.json"]
    try:
        for path in sorted(paths.state.glob(f"*_{suffix}.json")):
            name = path.name
            if name not in names:
                names.append(name)
    except Exception:
        pass
    return names


def current_payload(paths, names: list[str]) -> dict[str, Any]:
    fallback: dict[str, Any] = {}
    for name in names:
        payload = load_json(paths.state / name, {})
        if not isinstance(payload, dict) or not payload:
            continue
        if _artifact_matches_current_repo(paths, payload):
            return payload
        if not fallback:
            fallback = payload
    return fallback

def _repo_path_from_mapping(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ["repo_path", "active_repo_path", "local_path", "path", "current_selected_repo_path"]:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _selected_base_viability_current_selection(paths, run_id: str = "") -> dict[str, Any]:
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
    legacy_audit = load_json(paths.state / "fresh_base_reference_reproduction_audit.json", {})
    legacy_selected = legacy_audit.get("selected_base") if isinstance(legacy_audit, dict) and isinstance(legacy_audit.get("selected_base"), dict) else {}
    aligned_paths = {
        value
        for value in [
            _repo_path_from_mapping(impl_repo),
            _repo_path_from_mapping(trusted),
            _repo_path_from_mapping(legacy_audit) or _repo_path_from_mapping(legacy_selected),
        ]
        if value
    }
    if repo_path and aligned_paths and repo_path not in aligned_paths:
        return {}

    selected_run = str(gate.get("fresh_find_run_id") or (guard.get("selected_base_find_run_id") if isinstance(guard, dict) else "") or run_id or "").strip()
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
    return {
        "valid": True,
        "current_find_run_id": run_id,
        "fresh_find_run_id": selected_run,
        "selected": selected,
        "selected_title": title,
        "selection_stage": "environment_claude_code",
        "accepted_by_claude": True,
        "in_current_find_recommendations": True,
        "trusted_full_reference_anchor": True,
        "reason": "selected_base_viability_current_route",
        "candidate_switch_conflict": True,
    }


def _current_impl_repo_path(paths) -> str:
    viability = _selected_base_viability_current_selection(paths, current_find_run_id(paths))
    selected = viability.get("selected") if isinstance(viability.get("selected"), dict) else {}
    repo_path = _repo_path_from_mapping(selected)
    if repo_path:
        return repo_path

    selection = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    if isinstance(selection, dict):
        selected = selection.get("selected") if isinstance(selection.get("selected"), dict) else {}
        gate = str(selection.get("selection_gate") or selected.get("selection_gate") or "")
        if gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")):
            repo_path = _repo_path_from_mapping(selected)
            if repo_path:
                return repo_path
    active = load_json(paths.state / "active_repo.json", {})
    if isinstance(active, dict):
        gate = str(active.get("selection_gate") or "")
        if gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")):
            repo_path = _repo_path_from_mapping(active)
            if repo_path:
                return repo_path
    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    repo = impl.get("repo", {}) if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    return _repo_path_from_mapping(repo)


def _artifact_matches_current_repo(paths, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    current_repo = _current_impl_repo_path(paths)
    if not current_repo:
        return False
    for key in ["repo_path", "active_repo_path", "local_path"]:
        value = str(payload.get(key) or "").strip()
        if value and value != current_repo:
            return False
    return True


def fresh_base_reference_protocol_passed(paths) -> bool:
    probe = load_json(paths.state / "fresh_base_reference_protocol_probe.json", {})
    if _artifact_matches_current_repo(paths, probe):
        return bool(probe.get("status") == "reference_protocol_probe_passed" and probe.get("decision") == "ready_for_bounded_reference_smoke")
    for name in fresh_base_state_names(paths, "reference_protocol_probe"):
        if name == "fresh_base_reference_protocol_probe.json":
            continue
        legacy = load_json(paths.state / name, {})
        if _artifact_matches_current_repo(paths, legacy) and legacy.get("status") == "reference_protocol_probe_passed" and legacy.get("decision") == "ready_for_bounded_reference_smoke":
            return True
    return False


def fresh_base_reference_smoke_passed(paths) -> bool:
    smoke = load_json(paths.state / "fresh_base_reference_smoke.json", {})
    if _artifact_matches_current_repo(paths, smoke):
        return bool(smoke.get("status") == "reference_smoke_passed" and smoke.get("decision") == "ready_for_reference_reproduction_audit")
    for name in fresh_base_state_names(paths, "reference_smoke"):
        if name == "fresh_base_reference_smoke.json":
            continue
        legacy = load_json(paths.state / name, {})
        if _artifact_matches_current_repo(paths, legacy) and legacy.get("status") == "reference_smoke_passed" and legacy.get("decision") == "ready_for_reference_reproduction_audit":
            return True
    return False


def fresh_base_reference_smoke_required(paths) -> bool:
    return fresh_base_reference_protocol_passed(paths) and not fresh_base_reference_smoke_passed(paths)


def fresh_base_full_reproduction_passed(paths) -> bool:
    return full_reference_audit_passed(paths)


def fresh_base_full_reproduction_job(paths) -> dict[str, Any]:
    return indexed_reference_full_job_state(paths)


def fresh_base_full_reproduction_record(paths) -> dict[str, Any]:
    """Return the current selected-base paper-level reproduction record.

    The full audit may live in the new mode-specific state file, the artifact
    index, or an older artifact-local audit.json produced before the split. If
    an older artifact is verified, import it into the full state file with an
    explicit imported_from_artifact marker.
    """
    audit_path, audit = latest_reference_audit(paths, "full")
    audit_path, audit = import_full_audit_if_verified(paths, audit_path, audit)
    if not is_full_pass_audit(audit):
        return {}
    metrics = audit.get("metrics") if isinstance(audit.get("metrics"), dict) else {}
    metric_name = "ndcg_at_10" if parse_float(metrics.get("ndcg_at_10")) is not None else ""
    if not metric_name:
        for key, value in metrics.items():
            if parse_float(value) is not None:
                metric_name = str(key)
                break
    metric_value = parse_float(metrics.get(metric_name)) if metric_name else None
    return {
        "experiment_id": audit.get("experiment_id") or audit.get("name"),
        "method": audit.get("method") or "selected_base_reference",
        "dataset": audit.get("dataset"),
        "metric_name": metric_name,
        "metric_value": metric_value,
        "audit_ready": True,
        "duration_sec": audit.get("duration_sec"),
        "artifact_path": audit.get("artifact_dir") or audit.get("artifact_path") or "",
        "audit_path": str(audit.get("state_audit_path") or audit_path or ""),
        "artifact_audit_path": str(audit.get("artifact_audit_path") or ""),
        "repo_path": audit.get("repo_path") or audit.get("active_repo_path") or "",
        "repo_name": audit.get("repo_name") or "",
        "mode": audit.get("mode"),
        "paper_level_reproduction_passed": True,
        "imported_from_artifact": audit.get("imported_from_artifact", ""),
        "notes": "Wrapper-managed selected-base full reference reproduction audit; authoritative for the current environment-stage anchor.",
    }

def active_repo_path(paths) -> str:
    env = current_environment_selection(paths)
    selected = env.get("selected", {}) if isinstance(env.get("selected"), dict) else {}
    if env.get("valid"):
        for key in ["repo_path", "local_path", "path"]:
            value = str(selected.get(key) or "").strip()
            if value:
                return value
    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    repo = impl.get("repo", {}) if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    if env.get("valid") or str(impl.get("fresh_find_run_id") or "").strip() == str(env.get("current_find_run_id") or "").strip():
        for key in ["repo_path", "local_path", "path"]:
            value = str(repo.get(key) or "").strip()
            if value:
                return value
    active = load_json(paths.state / "active_repo.json", {})
    if isinstance(active, dict):
        for key in ["repo_path", "local_path", "path"]:
            value = str(active.get(key) or "").strip()
            if value:
                return value
    return ""



def current_find_run_id(paths) -> str:
    for rel in [
        paths.planning / "finding" / "find_results.json",
        paths.state / "current_find_research_plan.json",
    ]:
        payload = load_json(rel, {})
        if isinstance(payload, dict):
            run_id = str(payload.get("run_id") or payload.get("find_run_id") or "").strip()
            if run_id:
                return run_id
    return ""


def normalize_title_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def current_find_recommendation_title_keys(paths) -> set[str]:
    payload = load_json(paths.planning / "finding" / "find_results.json", {})
    if not isinstance(payload, dict):
        return set()
    rows: list[Any] = []
    for key in ["articles", "strong_recommendations"]:
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(value)
    keys: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            title = normalize_title_key(row.get("title") or row.get("paper_title"))
            if title:
                keys.add(title)
    return keys


def title_in_current_find_recommendations(paths, title: Any) -> bool:
    key = normalize_title_key(title)
    return bool(key and key in current_find_recommendation_title_keys(paths))


def environment_anchor_is_current(paths, active: dict[str, Any], fresh: dict[str, Any]) -> bool:
    if not isinstance(active, dict):
        return False
    run_id = current_find_run_id(paths)
    stage = str(active.get("selection_stage") or active.get("selected_by_stage") or "")
    selected_by = str(active.get("selected_by") or active.get("fresh_find_run_id") or "")
    if stage != "environment_claude_code" or not run_id or selected_by != run_id:
        return False
    selected_title = ""
    if isinstance(fresh, dict) and isinstance(fresh.get("selected"), dict):
        selected_title = str(fresh["selected"].get("title") or fresh["selected"].get("literature_base_title") or "").strip()
    active_title = str(active.get("selected_base_title") or active.get("title") or active.get("name") or "").strip()
    if not active_title:
        return False
    if selected_title and normalize_title_key(active_title) != normalize_title_key(selected_title):
        return False
    return title_in_current_find_recommendations(paths, active_title)


def current_find_candidates_waiting_for_environment_anchor(paths) -> bool:
    fresh = load_json(paths.state / "fresh_research_base.json", {})
    if not isinstance(fresh, dict):
        return False
    run_id = current_find_run_id(paths)
    fresh_run = str(fresh.get("fresh_find_run_id") or "").strip()
    status = str(fresh.get("status") or "")
    selected = fresh.get("selected") if isinstance(fresh.get("selected"), dict) else {}
    return bool(
        run_id
        and fresh_run == run_id
        and status == "fresh_base_candidates_ready_for_environment_claude_selection"
        and not selected
        and int(fresh.get("candidate_count") or 0) > 0
    )


def selected_full_reference_anchor_valid(paths, selected: dict[str, Any] | None = None) -> bool:
    """A verified selected-base full audit remains authoritative across auxiliary Find runs."""
    selected = selected if isinstance(selected, dict) else {}
    if not selected:
        selection = load_json(paths.state / "evidence_ready_repo_selection.json", {})
        selected = selection.get("selected", {}) if isinstance(selection, dict) and isinstance(selection.get("selected"), dict) else {}
    selected_repo = str(selected.get("repo_path") or selected.get("local_path") or selected.get("path") or "").strip()
    if not selected_repo:
        active = load_json(paths.state / "active_repo.json", {})
        if isinstance(active, dict):
            selected_repo = str(active.get("repo_path") or active.get("local_path") or active.get("path") or "").strip()
    audit_path, audit = latest_reference_audit(paths, "full")
    audit_path, audit = import_full_audit_if_verified(paths, audit_path, audit)
    if not is_full_pass_audit(audit) or not _artifact_matches_current_repo(paths, audit):
        return False
    audit_repo = str(audit.get("repo_path") or audit.get("active_repo_path") or audit.get("local_path") or "").strip()
    if selected_repo and audit_repo and selected_repo != audit_repo:
        return False
    return True


def current_environment_selection(paths) -> dict[str, Any]:
    run_id = current_find_run_id(paths)
    viability = _selected_base_viability_current_selection(paths, run_id)
    if viability:
        return viability

    selection = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    if not isinstance(selection, dict):
        return {"valid": False, "current_find_run_id": run_id, "selected": {}, "reason": "missing_repo_selection"}
    selected = selection.get("selected", {}) if isinstance(selection.get("selected"), dict) else {}
    selected_run = str(selection.get("fresh_find_run_id") or selected.get("fresh_find_run_id") or "").strip()
    stage = str(selection.get("selection_stage") or selection.get("selected_by_stage") or selected.get("selection_stage") or "").strip()
    decision = selection.get("claude_topic_decision") if isinstance(selection.get("claude_topic_decision"), dict) else {}
    accepted = bool(selection.get("accepted_by_claude") or str(selection.get("selection_gate") or "").startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")) or decision.get("accept_as_current_best"))
    selected_title = str(
        selected.get("title")
        or selected.get("literature_base_title")
        or selected.get("selected_base_title")
        or decision.get("selected_base_title")
        or selected.get("name")
        or ""
    ).strip()
    in_current_find = title_in_current_find_recommendations(paths, selected_title)
    trusted_full_anchor = selected_full_reference_anchor_valid(paths, selected)
    base_switch_gate = load_json(paths.state / "base_switch_gate.json", {})
    base_switch_execution = load_json(paths.state / "base_switch_execution.json", {})
    candidate = base_switch_gate.get("candidate_route") if isinstance(base_switch_gate, dict) and isinstance(base_switch_gate.get("candidate_route"), dict) else {}
    selected_repo_path = str(selected.get("repo_path") or selected.get("local_path") or "").strip()
    deterministic_switch_valid = bool(
        selected_repo_path
        and selected_repo_path == str(candidate.get("repo_path") or "").strip()
        and isinstance(base_switch_gate, dict)
        and base_switch_gate.get("status") == "pass"
        and base_switch_gate.get("decision") == "authorize_base_switch"
        and isinstance(base_switch_execution, dict)
        and str(base_switch_execution.get("status") or "").startswith("authorized_by_deterministic_base_switch_gate")
    )
    in_current_find = in_current_find or deterministic_switch_valid or trusted_full_anchor
    accepted = accepted or deterministic_switch_valid or trusted_full_anchor
    same_run_or_trusted = bool((run_id and selected_run == run_id) or trusted_full_anchor)
    valid = bool(selected and same_run_or_trusted and stage == "environment_claude_code" and accepted and in_current_find)
    reason = ""
    if not in_current_find:
        reason = "environment-selected paper is not present in the current Find recommended-paper pool"
    elif not same_run_or_trusted:
        reason = "environment-stage repo selection is stale for the current Find run"
    elif stage != "environment_claude_code" or not accepted:
        reason = "environment-stage Claude Code selection evidence is missing"
    return {"valid": valid, "current_find_run_id": run_id, "fresh_find_run_id": selected_run, "selected": selected, "selected_title": selected_title, "in_current_find_recommendations": in_current_find, "trusted_full_reference_anchor": trusted_full_anchor, "reason": reason}


def selected_base_label(paths) -> str:
    env = current_environment_selection(paths)
    selected = env.get("selected", {}) if isinstance(env.get("selected"), dict) else {}
    return str(selected.get("title") or selected.get("literature_base_title") or selected.get("selected_base_title") or selected.get("name") or selected.get("repo") or selected.get("repo_path") or "环境阶段选出的基底")

def reference_config(cfg: dict[str, Any]) -> dict[str, Any]:
    exp_cfg = cfg.get("experiment", {}) if isinstance(cfg.get("experiment", {}), dict) else {}
    repro = exp_cfg.get("reference_reproduction", {}) if isinstance(exp_cfg.get("reference_reproduction", {}), dict) else {}
    return repro


def infer_reference_targets(paths, cfg: dict[str, Any], repo: Path | None) -> list[dict[str, Any]]:
    repro_cfg = reference_config(cfg)
    targets = repro_cfg.get("targets", []) if isinstance(repro_cfg.get("targets", []), list) else []
    normalized = []
    for row in targets:
        if not isinstance(row, dict):
            continue
        metric = str(row.get("metric") or row.get("metric_name") or "").strip()
        dataset = str(row.get("dataset") or "").strip()
        value = parse_float(row.get("target") or row.get("value"))
        if metric and dataset and value is not None:
            entry = {
                "dataset": dataset,
                "metric_name": metric,
                "target_value": value,
                "tolerance_rel": float(row.get("tolerance_rel", repro_cfg.get("tolerance_rel", 0.10)) or 0.10),
                "source": row.get("source") or "project.json experiment.reference_reproduction.targets",
                "paper_level": bool(row.get("paper_level", True)),
            }
            # Pass through optional metadata fields
            for f in ("incomparable", "mismatch_reason", "note", "required_note_token"):
                if f in row:
                    entry[f] = row[f]
            normalized.append(entry)
    if normalized:
        return normalized

    # Evidence-only fallback: use explicit local historical route full-run logs as an observed reproduction anchor,
    # but do not treat it as the paper target unless a source table is configured.
    log_targets = []
    if repo and repo.exists():
        for log in sorted((repo / "log").glob("*.txt")):
            text = read_text(log)
            if "epoches:1000" not in text or "dataset:legacy dataset" not in text:
                continue
            ndcg = re.findall(r"ndcg:\s*\[([^\]]+)\]", text, re.IGNORECASE)
            if not ndcg:
                continue
            vals = [parse_float(x) for x in re.split(r"[,\s]+", ndcg[-1]) if x.strip()]
            vals = [v for v in vals if v is not None]
            if vals:
                log_targets.append({
                    "dataset": "legacy dataset",
                    "metric_name": "ndcg_at_10",
                    "target_value": vals[0],
                    "tolerance_rel": 0.02,
                    "source": str(log.relative_to(paths.root) if str(log).startswith(str(paths.root)) else log),
                    "paper_level": False,
                    "note": "local full-run log target; paper-table target still needs explicit evidence",
                })
                break
    return log_targets


def row_is_reference_reproduction(row: dict[str, Any], active_repo: str, targets: list[dict[str, Any]]) -> bool:
    method = str(row.get("method") or row.get("method_slug") or row.get("experiment_id") or "").lower()
    notes = str(row.get("notes") or "").lower()
    if active_repo and str(row.get("repo_path") or "") != active_repo:
        return False
    if str(row.get("status", "")).lower() not in {"completed", "success"}:
        return False
    if not row.get("audit_ready"):
        return False
    if not any(str(row.get("dataset") or "") == str(t.get("dataset") or "") for t in targets):
        return False
    hay = f"{method} {notes}"
    if not any(token in hay for token in ["reproduction", "1000epoch", "1000_epoch", "full", "baseline", "reference"]):
        return False
    # If any target specifies a required_note_token, the experiment must contain it in notes/method
    req_tokens = [str(t.get("required_note_token", "")).lower().strip() for t in targets if t.get("required_note_token")]
    if req_tokens and not any(token and token in hay for token in req_tokens):
        return False
    return True


def best_reproduction(rows: list[dict[str, Any]], target: dict[str, Any]) -> dict[str, Any]:
    metric_name = str(target.get("metric_name") or "")
    dataset = str(target.get("dataset") or "")
    candidates = []
    for row in rows:
        if str(row.get("dataset") or "") != dataset:
            continue
        row_metric_name, value = row_metric(row)
        if value is None:
            continue
        if metric_name and row_metric_name and row_metric_name != metric_name:
            continue
        candidates.append((value, row))
    if not candidates:
        return {}
    reverse = metric_higher_is_better(metric_name)
    value, row = sorted(candidates, key=lambda item: item[0], reverse=reverse)[0]
    return {
        "experiment_id": row.get("experiment_id") or row.get("name"),
        "method": row.get("method") or row.get("method_slug"),
        "dataset": row.get("dataset"),
        "metric_name": metric_name,
        "metric_value": value,
        "audit_ready": bool(row.get("audit_ready")),
        "duration_sec": row.get("duration_sec"),
        "artifact_path": row.get("artifact_path", ""),
        "audit_path": row.get("audit_path", ""),
        "notes": row.get("notes", ""),
    }


def passes_target(value: float | None, target: dict[str, Any]) -> tuple[bool, float | None]:
    target_value = parse_float(target.get("target_value"))
    if value is None or target_value is None:
        return False, None
    tolerance = float(target.get("tolerance_rel", 0.10) or 0.10)
    metric = str(target.get("metric_name") or "")
    if target_value == 0:
        ok = abs(value) <= tolerance
        rel = 0.0 if ok else math.inf
    else:
        rel = (value - target_value) / abs(target_value)
        ok = rel >= -tolerance if metric_higher_is_better(metric) else rel <= tolerance
    return ok, rel


def machine_summary(paths) -> dict[str, Any]:
    profile = load_json(paths.reports / "machine_profile.json", {})
    accelerator = profile.get("accelerator", {}) if isinstance(profile, dict) else {}
    gpus = accelerator.get("gpus", []) if isinstance(accelerator, dict) and isinstance(accelerator.get("gpus", []), list) else []
    return {"gpu_count": len(gpus), "gpus": gpus[:8], "profile_exists": bool(profile)}


def base_switch_exhaustion_summary(paths) -> dict[str, Any]:
    literature_base = load_json(paths.state / "literature_base_candidate_assessment.json", {})
    if isinstance(literature_base, dict) and literature_base.get("status") == "blocked_pending_literature_base_audit":
        return {
            "status": "pending_fresh_literature_base_audit",
            "exhausted": False,
            "fresh_literature_base_audit_required": True,
            "fresh_find_run_id": literature_base.get("fresh_find_run_id", ""),
            "audit_required_count": literature_base.get("audit_required_count", 0),
            "stale_reason": literature_base.get("stale_reason", ""),
            "top_candidates": literature_base.get("audit_required_candidates", [])[:12],
            "evidence": [
                str(paths.state / "literature_base_candidate_assessment.json"),
                str(paths.reports / "literature_base_candidate_assessment.md"),
                str(paths.state / "literature_tool_packet.json"),
                str(paths.planning / "finding" / "find_results.json"),
            ],
            "policy": (
                "Fresh Find literature candidates must be audited as possible bases before TASTE may declare no viable base switch. "
                "historical route cannot remain the main route by default while this audit is pending."
            ),
        }
    literature_audit = load_json(paths.state / "literature_base_audit.json", {})
    if (
        isinstance(literature_base, dict)
        and literature_base.get("status") == "fresh_literature_base_audit_completed_no_evidence_ready_base"
        and isinstance(literature_audit, dict)
        and literature_audit.get("audit_complete")
    ):
        fresh_base = load_json(paths.state / "fresh_research_base.json", {})
        fresh_selected = fresh_base.get("selected", {}) if isinstance(fresh_base, dict) and isinstance(fresh_base.get("selected"), dict) else {}
        implementation_status = (fresh_base.get("implementation_route", {}) or {}).get("status", "") if isinstance(fresh_base, dict) and isinstance(fresh_base.get("implementation_route", {}), dict) else ""
        return {
            "status": "fresh_paper_base_requires_implementation" if fresh_selected else "fresh_literature_audit_exhausted",
            "exhausted": False if fresh_selected else True,
            "fresh_literature_base_audit_required": False,
            "fresh_literature_base_audit_complete": True,
            "fresh_paper_base_selected": bool(fresh_selected),
            "fresh_paper_base": fresh_selected,
            "implementation_status": implementation_status,
            "fresh_find_run_id": literature_audit.get("fresh_find_run_id") or literature_base.get("fresh_find_run_id", ""),
            "total_candidates_evaluated": int(literature_audit.get("candidate_count") or literature_audit.get("total_audit_required_count") or 0),
            "execution_ready_count": 0,
            "repo_candidates_discovered_count": int(literature_audit.get("repo_candidates_discovered_count") or 0),
            "selection_gate": literature_audit.get("selection_gate", ""),
            "evidence": [
                str(paths.state / "fresh_research_base.json"),
                str(paths.state / "literature_base_audit.json"),
                str(paths.reports / "fresh_research_base.md"),
                str(paths.reports / "literature_base_audit.md"),
                str(paths.state / "literature_base_candidate_assessment.json"),
                str(paths.state / "evidence_ready_repo_selection.json"),
            ],
            "policy": (
                "Environment-stage anchor selection recorded a paper/method base even though no evidence-ready fresh repo was found. "
                "The workflow must continue code/artifact search or local implementation around that fresh base, and must not silently return to historical route as the main route."
                if fresh_selected else
                "Fresh Find base candidates were fully audited and no evidence-ready fresh base was selected. "
                "This blocks autonomous continuation; The workflow must not silently return to historical route. Continue only with new search evidence or explicit user confirmation to use a known imperfect base."
            ),
        }
    blocker = load_json(paths.state / "repo_selection_blocker.json", {})
    evidence_ready = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    packet = load_json(paths.state / "literature_tool_packet.json", {})
    if not isinstance(blocker, dict):
        blocker = {}
    if not isinstance(evidence_ready, dict):
        evidence_ready = {}
    if not isinstance(packet, dict):
        packet = {}

    total_candidates = int(blocker.get("total_candidates_evaluated") or 0)
    execution_ready_count = int(blocker.get("execution_ready_count") or 0)
    blocker_type = str(blocker.get("blocker_type") or "")
    recommended_route = str(blocker.get("recommended_route") or "")
    evidence_ready_bucket = str(evidence_ready.get("bucket") or evidence_ready.get("status") or "")
    packet_summary = packet.get("summary", {}) if isinstance(packet.get("summary", {}), dict) else {}
    strong_anchor_count = int(packet_summary.get("strong_paper_anchors") or packet_summary.get("strong_papers") or 0)

    exhausted = bool(
        total_candidates > 0
        and execution_ready_count == 0
        and (
            blocker_type == "no_evidence_ready_alternative_base"
            or "no_evidence_ready" in blocker_type
            or recommended_route == "continue_with_historical route_local_baseline"
        )
    )
    return {
        "status": "exhausted" if exhausted else "open",
        "exhausted": exhausted,
        "blocker_type": blocker_type,
        "recommended_route": recommended_route,
        "total_candidates_evaluated": total_candidates,
        "execution_ready_count": execution_ready_count,
        "new_candidates_from_expanded_search": int(blocker.get("new_candidates_from_expanded_search") or 0),
        "search_timestamp": blocker.get("search_timestamp") or blocker.get("updated_at") or "",
        "evidence_ready_bucket": evidence_ready_bucket,
        "strong_anchor_count": strong_anchor_count,
        "evidence": [
            str(paths.state / "repo_selection_blocker.json"),
            str(paths.state / "evidence_ready_repo_selection.json"),
            str(paths.state / "literature_tool_packet.json"),
        ],
        "policy": (
            "Base-switch exhaustion is not reference reproduction success. It is a terminal blocker for this autonomous route; "
            "paper writing and claim promotion remain blocked until a new evidence-ready base, compatible paper protocol/data, or sufficient compute is supplied."
        ),
    }


def build_reference_reproduction_gate(project: str) -> dict[str, Any]:
    cfg = load_project_config(project)
    paths = build_paths(project)
    active_repo = active_repo_path(paths)
    repo = Path(active_repo) if active_repo else None
    experiments = experiment_rows(load_json(paths.state / "experiment_registry.json", []))
    targets = infer_reference_targets(paths, cfg, repo)
    blockers: list[str] = []
    warnings: list[str] = []
    comparisons: list[dict[str, Any]] = []

    active_repo_state = load_json(paths.state / "active_repo.json", {})
    fresh_state_for_anchor = load_json(paths.state / "fresh_research_base.json", {})
    env_selection = current_environment_selection(paths)
    current_env_anchor = bool(env_selection.get("valid")) or environment_anchor_is_current(paths, active_repo_state if isinstance(active_repo_state, dict) else {}, fresh_state_for_anchor if isinstance(fresh_state_for_anchor, dict) else {})
    waiting_for_environment_anchor = current_find_candidates_waiting_for_environment_anchor(paths)
    full_reproduction_passed = fresh_base_full_reproduction_passed(paths)
    full_reproduction_job = fresh_base_full_reproduction_job(paths)
    full_reproduction_record = fresh_base_full_reproduction_record(paths)
    trusted_full_reference_anchor = bool(full_reproduction_passed and full_reproduction_record and selected_full_reference_anchor_valid(paths, env_selection.get("selected") if isinstance(env_selection.get("selected"), dict) else None))
    if trusted_full_reference_anchor:
        current_env_anchor = True
        waiting_for_environment_anchor = False
    current_run = current_find_run_id(paths)
    active_selected_by = str(active_repo_state.get("selected_by") or "") if isinstance(active_repo_state, dict) else ""

    if not active_repo:
        blockers.append("No active repo is selected; TASTE cannot reproduce a reference work before choosing a base.")
    if waiting_for_environment_anchor and not current_env_anchor:
        blockers.append(
            "Current Find has produced a base-work candidate pool, but no environment-stage Claude Code anchor selection exists for this run. "
            f"current_find_run_id={current_run}; active_repo_selected_by={active_selected_by or 'none'}. "
            "Previous active_repo/reference-reproduction state must not satisfy the current run."
        )
    elif current_run and active_selected_by and active_selected_by != current_run and not current_env_anchor:
        blockers.append(
            "Active repo was selected by a stale Find run and lacks current environment-stage Claude Code selection evidence: "
            f"current_find_run_id={current_run}; active_repo_selected_by={active_selected_by}."
        )
    if not targets:
        blockers.append("No reference-paper target metric is configured or extractable; The workflow must record paper/table target evidence before accepting the base.")

    paper_level_targets = [row for row in targets if row.get("paper_level")]
    if targets and not paper_level_targets:
        blockers.append("Only local run-log targets were found; TASTE still needs explicit paper/table target evidence before declaring paper-level reproduction.")

    for target in targets:
        repro_rows = [row for row in experiments if row_is_reference_reproduction(row, active_repo, [target])]
        best = best_reproduction(repro_rows, target)
        metric_value = parse_float(best.get("metric_value")) if best else None
        ok, relative_delta = passes_target(metric_value, target)
        incomparable = bool(target.get("incomparable", False))
        comparison = {
            "target": target,
            "best_reproduction": best,
            "pass": bool(ok and target.get("paper_level") and not incomparable),
            "relative_delta": relative_delta,
            "incomparable": incomparable,
            "mismatch_reason": target.get("mismatch_reason", "") if incomparable else "",
        }
        comparisons.append(comparison)
        if not best:
            blockers.append(f"No audit-ready reference reproduction exists for {target.get('dataset')} {target.get('metric_name')}.")
        elif incomparable and target.get("paper_level"):
            reason = str(target.get("mismatch_reason") or "paper target and local reproduction use different protocol/data split")
            blockers.append(
                "Paper-level reference reproduction is not comparable to the configured paper target: "
                f"{reason}. The workflow must reproduce the paper protocol/data split or switch to a better base with evidence."
            )
            warnings.append(f"Documented protocol mismatch: {reason}")
        elif not ok:
            blockers.append(
                f"Best reference reproduction is below target for {target.get('dataset')} {target.get('metric_name')}: "
                f"observed={metric_value}, target={target.get('target_value')}, tolerance_rel={target.get('tolerance_rel')}."
            )

    best_any = next((row.get("best_reproduction") for row in comparisons if row.get("best_reproduction")), {})
    durations = [parse_float((row.get("best_reproduction") or {}).get("duration_sec")) for row in comparisons if row.get("best_reproduction")]
    durations = [float(v) for v in durations if v is not None and v > 0]
    repro_cfg = reference_config(cfg)
    max_full_hours = float(repro_cfg.get("max_full_reproduction_hours", 24) or 24)
    min_iteration_budget = int(repro_cfg.get("min_iteration_budget", 3) or 3)
    estimated_full_hours = max(durations) / 3600 if durations else None
    compute = machine_summary(paths)
    feasible = True
    if estimated_full_hours is not None and estimated_full_hours > max_full_hours:
        feasible = False
        blockers.append(f"Full reference reproduction cost is too high for this base: estimated_hours={estimated_full_hours:.2f}, budget_hours={max_full_hours:.2f}.")
    if estimated_full_hours is not None and estimated_full_hours * min_iteration_budget > max_full_hours * 2:
        warnings.append(f"Experiment iteration budget is tight: {min_iteration_budget} full runs would cost about {estimated_full_hours * min_iteration_budget:.2f} GPU-hours.")
    if compute.get("gpu_count", 0) <= 0:
        warnings.append("No GPU was detected in machine_profile.json; long reference reproduction may be infeasible or stale.")

    status = "pass" if not blockers and comparisons and all(row.get("pass") for row in comparisons if row.get("target", {}).get("paper_level")) else "blocked"
    incomparable_paper_target = any(
        bool(row.get("target", {}).get("paper_level")) and bool(row.get("incomparable"))
        for row in comparisons
    )
    local_protocol_reproduced = any(
        (not row.get("target", {}).get("paper_level"))
        and row.get("best_reproduction")
        and ("test_mode=2" in str(row.get("target", {}).get("note", "")).lower() or "testmode2" in str(row.get("best_reproduction", {}).get("experiment_id", "")).lower())
        for row in comparisons
    )
    switch_base_needed = (
        (incomparable_paper_target and local_protocol_reproduced)
        or (blockers and any("No reference-paper target" in b or "cost is too high" in b or "below target" in b for b in blockers))
    )
    base_switch = base_switch_exhaustion_summary(paths) if switch_base_needed else {"status": "not_required", "exhausted": False}
    if switch_base_needed and base_switch.get("fresh_literature_base_audit_required"):
        blockers.append(
            "Fresh Find produced literature base candidates that have not been repo/data/env audited: "
            f"fresh_find_run_id={base_switch.get('fresh_find_run_id')}, audit_required_count={base_switch.get('audit_required_count')}. "
            "The workflow must evaluate these candidates before keeping historical route or declaring no viable base switch."
        )
    if switch_base_needed and base_switch.get("fresh_paper_base_selected"):
        paper = base_switch.get("fresh_paper_base", {}) if isinstance(base_switch.get("fresh_paper_base"), dict) else {}
        blockers.append(
            "Environment-stage anchor selection recorded a paper/method base but no evidence-ready fresh repo is available yet: "
            f"{paper.get('title') or 'unknown fresh base'} ({paper.get('venue') or ''} {paper.get('year') or ''}). "
            "The workflow must continue code/artifact search or implement the fresh base under audit; Historical routes are legacy/control only, not the main route."
        )
    if switch_base_needed and base_switch.get("exhausted"):
        if base_switch.get("fresh_literature_base_audit_complete"):
            blockers.append(
                "Fresh Find base audit is complete but found no evidence-ready fresh base: "
                f"fresh_find_run_id={base_switch.get('fresh_find_run_id')}, "
                f"{base_switch.get('total_candidates_evaluated')} candidates audited, "
                f"{base_switch.get('repo_candidates_discovered_count')} fresh repo candidates discovered, "
                f"{base_switch.get('execution_ready_count')} evidence-ready alternatives. "
                "The workflow must not silently return to historical route; paper and claim promotion remain blocked until new evidence or explicit user confirmation."
            )
        else:
            blockers.append(
                "Base-switch search is exhausted: "
                f"{base_switch.get('total_candidates_evaluated')} candidates evaluated, "
                f"{base_switch.get('execution_ready_count')} evidence-ready alternatives. "
                "This does not pass the reference reproduction gate; paper and claim promotion remain blocked."
            )
    decision = (
        "continue_base" if status == "pass" and feasible else
        "environment_anchor_selection_required" if waiting_for_environment_anchor and not current_env_anchor else
        "literature_base_audit_required" if switch_base_needed and base_switch.get("fresh_literature_base_audit_required") else
        "fresh_base_implementation_required" if switch_base_needed and base_switch.get("fresh_paper_base_selected") else
        "no_viable_base_switch_route" if switch_base_needed and base_switch.get("exhausted") else
        "switch_base" if switch_base_needed else
        "repair_reproduction"
    )
    decision_reason = (
        "current_find_waiting_for_environment_claude_anchor" if decision == "environment_anchor_selection_required" else
        "fresh_find_base_candidates_not_audited" if decision == "literature_base_audit_required" else
        "fresh_find_paper_base_requires_code_or_implementation" if decision == "fresh_base_implementation_required" else
        "fresh_literature_audit_complete_no_evidence_ready_base" if decision == "no_viable_base_switch_route" and base_switch.get("fresh_literature_base_audit_complete") else
        "reference_blocked_and_base_switch_exhausted" if decision == "no_viable_base_switch_route" else
        "paper_target_incomparable_after_protocol_reproduction" if incomparable_paper_target and local_protocol_reproduced else
        "missing_or_infeasible_or_below_target_reference" if switch_base_needed else
        "reference_reproduction_can_still_be_repaired"
    )
    fresh_impl_for_decision = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    loader_contract_passed = fresh_base_loader_contract_passed(paths)
    selected_repo_path = active_repo
    active_is_fresh_base = bool(
        current_env_anchor
        and selected_repo_path
        and isinstance(fresh_impl_for_decision, dict)
        and isinstance(fresh_impl_for_decision.get("repo"), dict)
        and str(selected_repo_path or "").strip() == str(fresh_impl_for_decision.get("repo", {}).get("repo_path") or "").strip()
    )
    fresh_impl_ready = bool(
        isinstance(fresh_impl_for_decision, dict)
        and fresh_impl_for_decision.get("status") == "implementation_ready_for_reference_probe"
    )
    # Once environment-stage Claude Code has selected a current fresh base and real data/loader probes pass,
    # legacy historical route/legacy dataset paper targets must not pull the main route back to repair_reproduction.
    # The fresh-base route advances through deterministic, safe gates: data/loader ->
    # reference protocol/import probe -> bounded no-training reference smoke.
    protocol_passed = fresh_base_reference_protocol_passed(paths)
    smoke_passed = fresh_base_reference_smoke_passed(paths)
    smoke_required = fresh_base_reference_smoke_required(paths)
    if current_env_anchor and full_reproduction_passed and full_reproduction_record:
        # The current environment-stage selected base is authoritative.  Stale
        # implementation/base-switch artifacts must not override a verified full
        # reproduction audit for that selected repo.
        targets = [{
            "dataset": full_reproduction_record.get("dataset") or "",
            "metric_name": full_reproduction_record.get("metric_name") or "ndcg_at_10",
            "target_value": None,
            "observed_value": full_reproduction_record.get("metric_value"),
            "tolerance_rel": 0.0,
            "source": str(full_reproduction_record.get("audit_path") or full_reproduction_record.get("artifact_path") or "state/fresh_base_reference_full_reproduction_audit.json"),
            "paper_level": True,
            "incomparable": False,
            "fresh_base": True,
            "note": "Current selected-base wrapper full reproduction passed; paper/claim promotion is still gated by scientific-progress and evidence audits.",
        }]
        best_any = full_reproduction_record
        comparisons = [{
            "target": targets[0],
            "best_reproduction": full_reproduction_record,
            "pass": True,
            "relative_delta": None,
            "incomparable": False,
            "mismatch_reason": "",
            "source": "fresh_base_reference_reproduction_audit",
        }]
        blockers = []
        warnings = [w for w in warnings if not any(token in str(w) for token in ["historical route", "legacy dataset"])]
        base_switch = {"status": "not_required", "exhausted": False, "reason": "current_selected_base_full_reference_reproduction_passed"}
        status = "pass"
        decision = "continue_base"
        decision_reason = "fresh_base_full_reference_reproduction_passed"
    elif loader_contract_passed and fresh_impl_ready and active_is_fresh_base:
        ready_target_datasets = fresh_impl_for_decision.get("ready_datasets", []) if isinstance(fresh_impl_for_decision.get("ready_datasets"), list) else []
        targets = [{
            "dataset": ready_target_datasets[0] if ready_target_datasets else "",
            "metric_name": "ndcg_at_10",
            "target_value": None,
            "tolerance_rel": 0.0,
            "source": "Current environment-stage selected fresh-base repository; exact paper table target must be extracted or reproduced before claim promotion.",
            "paper_level": True,
            "incomparable": False,
            "fresh_base": True,
            "note": "Fresh-base target supersedes legacy historical route/legacy dataset targets while the current environment-stage selected repo is active."
        }]
        comparisons = []
        blockers = []
        warnings = [w for w in warnings if "historical route" not in str(w) and "legacy dataset" not in str(w)]
        status = "blocked"
        decision = "fresh_base_reference_reproduction_required" if smoke_passed else decision
        decision_reason = "fresh_base_reference_smoke_passed_full_reference_reproduction_required" if smoke_passed else decision_reason
    if status != "pass" and loader_contract_passed and fresh_impl_ready and (active_is_fresh_base or base_switch.get("fresh_paper_base_selected")):
        if smoke_required:
            decision = "fresh_base_reference_smoke_required"
            decision_reason = "fresh_base_reference_protocol_passed_bounded_smoke_required"
        elif smoke_passed:
            decision = "fresh_base_reference_reproduction_required"
            decision_reason = "fresh_base_reference_smoke_passed_reference_reproduction_required"
        else:
            decision = "fresh_base_reference_probe_required"
            decision_reason = "fresh_base_loader_contract_passed_reference_probe_required"
    fresh_data_required = fresh_base_data_required(fresh_impl_for_decision) and not loader_contract_passed
    base_label = selected_base_label(paths)
    human_summary = (
        "参考工作复现已达到可继续作为基底的门槛。" if status == "pass" else
        "Find 已产出强相关候选池，但当前 run 还没有经过环境配置阶段 Claude Code 选定锚点；旧 active_repo/旧参考复现不能放行当前流程。" if decision == "environment_anchor_selection_required" else
        "Find 已产出新的强推荐候选池，但这些候选还没有完成代码/数据/环境审计；TASTE 不能继续默认使用历史主线，也不能宣称没有更好基底。" if decision == "literature_base_audit_required" else
        f"{base_label} 参考协议/环境 manifest 只读探针已通过；当前阻塞在有界 reference smoke/audit，未通过前不能完整训练、写论文或提升 claim。" if decision == "fresh_base_reference_smoke_required" else
        f"{base_label} 数据、loader、参考协议、bounded smoke 和 bounded audit 已通过；TASTE 正在/需要通过 wrapper 跑论文级 full reference reproduction。未完成前不能写论文或提升 claim。" if decision == "fresh_base_reference_reproduction_required" else
        f"{base_label} 数据和 loader/import probe 已通过；当前阻塞在最小环境 manifest、参考协议只读探针和 reference reproduction 审计，未通过前不能训练、写论文或提升 claim。" if decision == "fresh_base_reference_probe_required" else
        (f"环境阶段 Claude Code 已选择论文/方法锚点 {base_label}，但数据/loader 合同尚未就绪；流程必须先补齐当前 repo 数据合同要求的真实文件并通过 loader/import probe，不能静默回到历史主线。" if fresh_data_required else "环境阶段 Claude Code 已选择论文/方法锚点，但尚未找到 evidence-ready 仓库；流程必须围绕该 fresh base 继续找代码、补实现和建立数据/复现协议，不能静默回到历史主线。") if decision == "fresh_base_implementation_required" else
        "Find 强推荐候选池已完成代码/数据/环境审计，但没有找到 evidence-ready 新基底；TASTE 不能静默回到历史主线，也不能继续论文写作。除非继续搜索到新证据，或用户明确确认使用某个有缺陷但可改造的基底，否则自动路线必须阻塞。" if decision == "no_viable_base_switch_route" and base_switch.get("fresh_literature_base_audit_complete") else
        "参考工作复现仍未过；换基底搜索已经穷尽，因此这是当前自动路线的终止阻塞，不是放行。论文写作和 claim promotion 继续禁止，除非补充新基底/新数据协议/更强算力证据。" if decision == "no_viable_base_switch_route" else
        "参考工作评测协议已验证，但论文级数据 split/目标仍不可比；TASTE 应进入换基底/文献回溯，而不是继续空转复现。" if decision == "switch_base" else
        "参考工作复现门控阻塞：流程必须先补齐论文目标证据、复现实验审计包和算力可行性判断；不允许直接进入新方法或论文阶段。"
    )
    legacy_reference_blockers = []
    if decision in {"fresh_base_implementation_required", "fresh_base_reference_probe_required", "fresh_base_reference_smoke_required", "fresh_base_reference_reproduction_required"}:
        legacy_reference_blockers = list(blockers)
        paper = base_switch.get("fresh_paper_base", {}) if isinstance(base_switch.get("fresh_paper_base"), dict) else {}
        title = str(paper.get("title") or "environment-stage selected anchor")
        if decision == "fresh_base_reference_smoke_required":
            protocol = current_payload(paths, fresh_base_state_names(paths, "reference_protocol_probe"))
            ready = protocol.get("ready_datasets", []) if isinstance(protocol, dict) and isinstance(protocol.get("ready_datasets"), list) else []
            blockers = [
                (
                    f"{base_label} reference protocol/env manifest probe passed, but the bounded reference smoke/audit has not passed yet: "
                    f"{title} ({paper.get('venue') or ''} {paper.get('year') or ''}). "
                    f"Ready datasets={ready}. Run the selected-base bounded reference smoke through TASTE safe-unblock before any full training, paper writing, or claim promotion; Historical routes are legacy/control only."
                )
            ]
        elif decision == "fresh_base_reference_reproduction_required":
            smoke = current_payload(paths, fresh_base_state_names(paths, "reference_smoke"))
            audit_path, audit = latest_reference_audit(paths, "bounded")
            audit_ready = bool(isinstance(audit, dict) and audit.get("mode") == "bounded" and audit.get("return_code") == 0 and audit.get("audit_ready"))
            full_job = fresh_base_full_reproduction_job(paths)
            audit_note = (
                f" Bounded audit artifact={audit.get('artifact_dir', '')}; metrics={audit.get('metrics', {})}. This is not paper-level full reproduction."
                if audit_ready
                else " Run an audited bounded reference reproduction wrapper before paper writing or claim promotion."
            )
            job_note = (
                f" Full reproduction job status={full_job.get('status')} pid={full_job.get('pid')} log={full_job.get('log_path')}."
                if full_job else
                " Next deterministic action is scripts/run_safe_unblock.py, which starts a wrapper-managed full reproduction job instead of repeating bounded smoke."
            )
            blockers = [
                (
                    f"{base_label} bounded reference smoke/audit passed, but paper-level full reference reproduction is still required: "
                    f"{title} ({paper.get('venue') or ''} {paper.get('year') or ''}). "
                    f"Smoke dataset={smoke.get('selected_dataset') if isinstance(smoke, dict) else ''.strip()}." + audit_note + job_note + " Historical routes are legacy/control only."
                )
            ]
        elif decision == "fresh_base_reference_probe_required":
            ready = fresh_impl_for_decision.get("ready_datasets", []) if isinstance(fresh_impl_for_decision, dict) else []
            blockers = [
                (
                    f"{base_label} data and loader/import probes are ready, but the fresh-base reference protocol is not audited yet: "
                    f"{title} ({paper.get('venue') or ''} {paper.get('year') or ''}). "
                    f"Ready datasets={ready}. Create/record the selected-base environment manifest and run bounded read-only reference-protocol probes before training, paper writing, or claim promotion; Historical routes are legacy/control only."
                )
            ]
        elif fresh_data_required:
            data_blockers = fresh_impl_for_decision.get("blocker_reasons", []) if isinstance(fresh_impl_for_decision, dict) and isinstance(fresh_impl_for_decision.get("blocker_reasons"), list) else []
            blockers = [
                (
                    f"Environment-stage Claude Code selected {base_label} as the current paper/method anchor, but data/loader files are not evidence-ready locally: "
                    f"{title} ({paper.get('venue') or ''} {paper.get('year') or ''}). "
                    "Resolve the repo-specific real dataset files declared in dataset_contract.required_files_per_dataset and pass loader/import probes before Claude implementation, experiments, or paper writing; historical routes are legacy/control only."
                    + (f" blockers={'; '.join(str(item) for item in data_blockers[:4])}." if data_blockers else "")
                )
            ]
        else:
            blockers = [
                (
                    "Environment-stage Claude Code selected a paper/method anchor but the implementation route is not evidence-ready yet: "
                    f"{title} ({paper.get('venue') or ''} {paper.get('year') or ''}). "
                    "The workflow must continue code/artifact search or implement the fresh base under audit; Historical routes are legacy/control only, not the main route."
                )
            ]
    elif decision == "literature_base_audit_required":
        legacy_reference_blockers = [
            item for item in blockers
            if "Best reference reproduction is below target" in item or "Paper-level reference reproduction" in item
        ]
    payload = {
        "project": project,
        "updated_at": now_iso(),
        "status": status,
        "decision": decision,
        "decision_reason": decision_reason,
        "human_summary": human_summary,
        "active_repo_path": active_repo,
        "targets": targets,
        "comparisons": comparisons,
        "best_reproduction": best_any,
        "compute_feasibility": {
            "status": "pass" if feasible else "blocked",
            "estimated_full_reproduction_hours": estimated_full_hours,
            "max_full_reproduction_hours": max_full_hours,
            "min_iteration_budget": min_iteration_budget,
            "machine": compute,
            "warnings": warnings,
        },
        "base_switch": base_switch,
        "paper_pipeline_policy": (
            "blocked_until_reference_reproduction_passes" if status != "pass" else "may_continue_after_other_gates"
        ),
        "blockers": blockers,
        "legacy_reference_blockers": legacy_reference_blockers,
        "legacy_reference_policy": (
            "historical route/legacy dataset reference reproduction evidence is retained only as legacy/control context while a fresh Find base implementation/reference-probe route is active."
            if decision in {"fresh_base_implementation_required", "fresh_base_reference_probe_required", "fresh_base_reference_smoke_required", "fresh_base_reference_reproduction_required"} else ""
        ),
        "warnings": warnings,
        "required_next_actions": (
            [
                "Do not continue historical route as the main route until fresh Find base candidates are repo/data/env audited.",
                "Resolve code repositories and dataset/protocol evidence for the top fresh literature base candidates.",
                "Update evidence_ready_repo_selection.json or repo_selection_blocker.json with fresh_find_run_id after the audit.",
            ] if decision == "literature_base_audit_required" else
            [
                "Run scripts/run_literature_base_audit.py for the current Find candidates without writing active_repo.",
                "Run scripts/run_environment_stage.py so Claude Code selects the anchor/base only after current strong recommendations, Read/Idea/Plan, repo/data/protocol evidence, and reproducibility feasibility are inspected.",
                "Do not use stale active_repo/reference_reproduction_gate state from an older Find run.",
            ] if decision == "environment_anchor_selection_required" else
            [
                "Use state/fresh_research_base.json, state/current_find_research_plan.json, and state/fresh_base_reference_protocol_probe.json as the current selected fresh-base source of truth.",
                "Run the selected-base bounded no-training reference smoke through TASTE safe-unblock.",
                "Do not run full training, paper writing, or claim promotion until the smoke and subsequent reference reproduction audit gates pass.",
                "Keep historical route only as legacy/control evidence; do not run historical route/legacy dataset as the main route.",
            ] if decision == "fresh_base_reference_smoke_required" else
            [
                "Use state/fresh_base_reference_smoke.json and state/fresh_base_reference_reproduction_audit.json as proof that bounded smoke/audit passed, not paper-level reproduction.",
                "Run the selected-base wrapper-managed full reference reproduction job through the research job entrypoint.",
                "Keep paper writing and claim promotion blocked until reference reproduction and scientific progress gates pass.",
            ] if decision == "fresh_base_reference_reproduction_required" else
            [
                "Use state/fresh_research_base.json, state/current_find_research_plan.json, and state/real_dataset_probe.json as the current selected fresh-base source of truth.",
                "Create or record a minimal selected-base environment manifest without reinstalling the locked project environment.",
                "Run a bounded read-only reference-protocol/import probe for the selected-base ready datasets before any training or scientific claim.",
                "Keep historical route only as legacy/control evidence; do not run historical route/legacy dataset as the main route.",
            ] if decision == "fresh_base_reference_probe_required" else
            ([
                "Use state/fresh_research_base.json and state/current_find_research_plan.json as the next research-base source of truth.",
                "Resolve selected-base official data files and loader contract via bounded probes before Claude implementation.",
                "Do not run training, paper writing, or claim promotion until at least one real selected-base dataset is loader-ready.",
                "Keep historical route only as legacy/control evidence unless the user explicitly confirms it as the imperfect base.",
            ] if fresh_data_required else [
                "Use state/fresh_research_base.json as the next research-base source of truth.",
                "Continue code/artifact search or implement the selected environment-stage anchor before experiments.",
                "Keep historical route only as legacy/control evidence unless the user explicitly confirms it as the imperfect base.",
            ]) if decision == "fresh_base_implementation_required" else
            [
                "Do not write, polish, or promote paper claims from this route; reference reproduction remains blocked.",
                "Do not silently return to historical route or rerun the same base-switch loop unless new external evidence-ready repos/data/protocols are added, or the user explicitly confirms a known imperfect base.",
                "Acquire a compatible paper protocol/data split, stronger compute for paper-config reproduction, or a genuinely evidence-ready alternative base before continuing autonomously.",
            ] if decision == "no_viable_base_switch_route" else
            [
                "Find or record the reference paper's official table metric for the active repo/dataset.",
                "Run the reference reproduction through TASTE's audit wrapper so metrics, logs, bad cases, runtime, config, and hashes are captured.",
                "If reproduction cannot reach the paper target within budget, route to repo/literature backtracking and choose a better base.",
                "When decision=switch_base, stop tuning this base and start evidence-ready repo/literature backtracking before any new paper work.",
            ] if status != "pass" else ["Proceed to candidate experiments only through audit-ready real-data comparisons."]
        ),
    }
    paths.state.mkdir(parents=True, exist_ok=True)
    paths.reports.mkdir(parents=True, exist_ok=True)
    save_json(paths.state / "reference_reproduction_gate.json", payload)
    report_lines = ["# Reference Reproduction Gate\n\n"]
    for key in ["status", "decision", "decision_reason", "human_summary", "paper_pipeline_policy", "active_repo_path"]:
        report_lines.append(f"- {key}: {payload.get(key)}\n")
    report_lines.append("\n## Base Switch\n")
    report_lines.append(f"- {payload.get('base_switch')}\n")
    report_lines.append("\n## Targets and Comparisons\n")
    for row in comparisons:
        report_lines.append(f"- target={row.get('target')} best={row.get('best_reproduction')} pass={row.get('pass')} relative_delta={row.get('relative_delta')}\n")
    report_lines.append("\n## Blockers\n")
    if blockers:
        for blocker in blockers:
            report_lines.append(f"- {blocker}\n")
    else:
        report_lines.append("- No blocker.\n")
    report_lines.append("\n## Warnings\n")
    if warnings:
        for warning in warnings:
            report_lines.append(f"- {warning}\n")
    else:
        report_lines.append("- No warning.\n")
    (paths.reports / "reference_reproduction_gate.md").write_text("".join(report_lines), encoding="utf-8")
    sync_full_cycle_reference_gate_state(paths, payload)
    return payload



def sync_full_cycle_reference_gate_state(paths, payload: dict[str, Any]) -> None:
    """Keep web-visible full-cycle/agent state aligned with the latest hard gate."""
    base_label = selected_base_label(paths)
    full_path = paths.state / "full_research_cycle.json"
    full = load_json(full_path, {})
    if not isinstance(full, dict):
        full = {}
    decision = str(payload.get("decision") or "")
    gate_status = str(payload.get("status") or "")
    if decision == "literature_base_audit_required":
        issue = "; ".join(str(item) for item in (payload.get("blockers") or [])[:4])
        blocker = {
            "category": "fresh_literature_base_audit",
            "severity": "block",
            "issue": issue or "Fresh Find base candidates require repo/data/env audit before legacy-route or paper continuation.",
            "evidence": [
                str(paths.state / "literature_base_candidate_assessment.json"),
                str(paths.state / "literature_base_audit.json"),
                str(paths.state / "reference_reproduction_gate.json"),
                str(paths.state / "literature_tool_packet.json"),
                str(paths.planning / "finding" / "find_results.json"),
            ],
            "next_action": "Continue scripts/run_literature_base_audit.py until all fresh Find base candidates are audited or an evidence-ready base is selected.",
            "human_summary": "Find 已融入 TASTE；当前阻塞是候选基底代码/数据/环境审计尚未完成，不能继续 历史主线或论文写作。",
        }
        full.update({
            "status": "blocked_literature_base_audit_required",
            "current_goal": "fresh Find base candidates must be audited before choosing a legacy route or a new base",
            "continuation_required": True,
            "continuation_reason": "repo/data/env audit pending for fresh literature base candidates",
            "paper_pipeline_skipped": True,
            "paper_pipeline_skipped_reason": "fresh literature base candidates are not yet repo/data/env audited",
            "reference_base_switch_required": True,
            "reference_base_switch_exhausted": False,
            "latest_blockers": [blocker],
            "latest_gate": {**(full.get("latest_gate", {}) if isinstance(full.get("latest_gate"), dict) else {}), "reference_reproduction_gate": payload},
            "updated_at": now_iso(),
        })
        save_json(full_path, full)
        agents_path = paths.state / "agents.json"
        agents = load_json(agents_path, {})
        if not isinstance(agents, dict):
            agents = {"project": paths.name, "agents": []}
        rows = agents.get("agents", []) if isinstance(agents.get("agents"), list) else []
        main = next((row for row in rows if isinstance(row, dict) and row.get("id") == "main"), None)
        if main is None:
            main = {"id": "main", "name": "主控 Agent", "role": "main", "children": [], "log_tail": []}
            rows.insert(0, main)
        main.update({
            "status": "blocked",
            "stage": "literature-base-audit",
            "current_step": "fresh Find base candidates require repo/data/env audit before legacy-route continuation",
            "goal": "finish fresh literature base audit before experiments or paper writing",
            "process_alive": False,
            "updated_at": now_iso(),
        })
        agents["project"] = paths.name
        agents["agents"] = rows
        agents["updated_at"] = now_iso()
        save_json(agents_path, agents)
    elif decision in {"fresh_base_implementation_required", "fresh_base_reference_probe_required", "fresh_base_reference_smoke_required", "fresh_base_reference_reproduction_required"}:
        base_switch = payload.get("base_switch", {}) if isinstance(payload.get("base_switch"), dict) else {}
        paper = base_switch.get("fresh_paper_base", {}) if isinstance(base_switch.get("fresh_paper_base"), dict) else {}
        title = str(paper.get("title") or "environment-stage selected anchor")
        fresh_impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
        if not isinstance(fresh_impl, dict):
            fresh_impl = {}
        impl_status = str(fresh_impl.get("status") or "")
        impl_repo = fresh_impl.get("repo", {}) if isinstance(fresh_impl.get("repo"), dict) else {}
        impl_blockers = fresh_impl.get("blocker_reasons", []) if isinstance(fresh_impl.get("blocker_reasons"), list) else []
        loader_contract_passed = fresh_base_loader_contract_passed(paths)
        data_required = fresh_base_data_required(fresh_impl) and not loader_contract_passed
        reference_probe_required = decision == "fresh_base_reference_probe_required"
        reference_smoke_required = decision == "fresh_base_reference_smoke_required"
        reference_reproduction_required = decision == "fresh_base_reference_reproduction_required"
        data_probe = load_json(paths.state / "real_dataset_probe.json", {})
        if not isinstance(data_probe, dict):
            data_probe = {}
        ready_datasets = fresh_impl.get("ready_datasets", []) if isinstance(fresh_impl.get("ready_datasets"), list) else []
        blocked_datasets = fresh_impl.get("blocked_datasets", []) if isinstance(fresh_impl.get("blocked_datasets"), list) else []
        blocker = {
            "category": "fresh_base_data_required" if data_required else "fresh_base_reference_probe_required" if reference_probe_required else "fresh_base_reference_smoke_required" if reference_smoke_required else "fresh_base_reference_reproduction_required" if reference_reproduction_required else "fresh_base_implementation_required",
            "severity": "block",
            "issue": (
                (
                    f"Environment-stage Claude Code selected {base_label} as the current paper/method anchor, but data/loader files are not evidence-ready locally: {title}. "
                    "Resolve the repo-specific real dataset files declared in dataset_contract.required_files_per_dataset and pass loader/import probes before Claude implementation, experiments, or paper writing; historical routes are legacy/control only."
                )
                if data_required
                else (
                    f"{base_label} data and loader/import probes are ready, but reference protocol/env manifest probes are not audited yet: {title}. "
                    "Run bounded read-only reference-protocol probes before training, paper writing, or claim promotion; historical routes are legacy/control only."
                )
                if reference_probe_required
                else (
                    f"{base_label} reference protocol/env manifest probe passed, but bounded reference smoke/audit is not complete yet: {title}. "
                    "Run the current-base reference smoke/audit through safe-unblock before full training, paper writing, or claim promotion; historical routes are legacy/control only."
                )
                if reference_smoke_required
                else (
                    f"{base_label} bounded reference smoke passed, but paper-level reference reproduction audit is still required: {title}. "
                    "Run an audited reference reproduction wrapper before paper writing or claim promotion; historical routes are legacy/control only."
                )
                if reference_reproduction_required
                else (
                    f"Environment-stage Claude Code selected a paper/method anchor but the implementation route is not evidence-ready yet: {title}. "
                    "Continue code/artifact search or local implementation around this fresh base; historical routes are legacy/control only."
                )
                + (f" implementation_plan_status={impl_status}; blockers={'; '.join(str(item) for item in impl_blockers[:4])}." if impl_status or impl_blockers else "")
            ),
            "evidence": [
                str(paths.state / "fresh_research_base.json"),
                str(paths.state / "fresh_base_implementation_plan.json"),
                str(paths.state / "real_dataset_probe.json"),
                str(paths.state / "current_find_research_plan.json"),
                str(paths.state / "literature_tool_packet.json"),
                str(paths.planning / "finding" / "find_results.json"),
                str(paths.planning / "finding" / "read_results.json"),
                str(paths.planning / "finding" / "ideas.json"),
                str(paths.planning / "finding" / "plans.json"),
                str(paths.state / "reference_reproduction_gate.json"),
            ],
            "next_action": (f"Resolve {base_label} data files and loader contract before Claude implementation, experiments, or paper writing." if data_required else f"Create/record the {base_label} environment manifest and run bounded read-only reference-protocol probes before any training or paper claim." if reference_probe_required else f"Run bounded no-training {base_label} reference smoke/audit before full training, paper writing, or claim promotion." if reference_smoke_required else f"Run audited {base_label} reference reproduction before paper writing or claim promotion." if reference_reproduction_required else "Resolve official code/artifacts or implement the environment-stage selected anchor with real data/protocol evidence before experiments or paper writing."),
            "human_summary": (f"环境阶段 Claude Code 已选择 {base_label} 锚点，但数据/loader 合同尚未 evidence-ready；下一步只能补真实数据文件并跑 loader/import probe，历史路线只能作为对照。" if data_required else f"{base_label} 数据和 loader/import probe 已通过；当前只阻塞在环境 manifest、参考协议只读探针和 reference reproduction 审计，历史路线只能作为对照。" if reference_probe_required else f"{base_label} 参考协议/环境 manifest 只读探针已通过；当前只阻塞在有界 reference smoke/audit，历史路线只能作为对照。" if reference_smoke_required else f"{base_label} 有界 reference smoke 已通过；当前阻塞在论文级 reference reproduction audit，历史路线只能作为对照。" if reference_reproduction_required else "环境阶段 Claude Code 已选择论文/方法锚点，但实现路线尚未 evidence-ready；下一步是找代码/补实现/建立数据协议，历史路线只能作为对照。"),
            "fresh_base_implementation_status": impl_status,
            "fresh_base_repo": impl_repo,
            "fresh_base_blockers": impl_blockers[:8],
        }
        fresh_plan_summary = {
            "status": impl_status,
            "repo": impl_repo,
            "ready_datasets": fresh_impl.get("ready_datasets", []),
            "blocked_datasets": fresh_impl.get("blocked_datasets", []),
            "blocker_reasons": impl_blockers,
        }
        full.update({
            "status": "blocked_fresh_base_data_required" if data_required else "blocked_fresh_base_reference_probe_required" if reference_probe_required else "blocked_fresh_base_reference_smoke_required" if reference_smoke_required else "blocked_fresh_base_reference_reproduction_required" if reference_reproduction_required else "blocked_fresh_base_implementation_required",
            "full_status": "blocked_fresh_base_data_required" if data_required else "blocked_fresh_base_reference_probe_required" if reference_probe_required else "blocked_fresh_base_reference_smoke_required" if reference_smoke_required else "blocked_fresh_base_reference_reproduction_required" if reference_reproduction_required else "blocked_fresh_base_implementation_required",
            "current_goal": blocker["human_summary"],
            "current_blocker": blocker,
            "data_status": "ready" if ready_datasets else data_probe.get("status", ""),
            "data_decision": "ready_for_loader_probe" if ready_datasets else data_probe.get("decision", ""),
            "loader_status": "ready_for_reference_loader_probe" if ready_datasets else "",
            "loader_decision": "loader_contract_passed" if ready_datasets else "",
            "ready_datasets": ready_datasets,
            "blocked_datasets": blocked_datasets,
            "fresh_research_base": base_switch.get("fresh_paper_base", {}),
            "fresh_base_implementation_plan": fresh_plan_summary,
            "fresh_base_implementation_plan_path": str(paths.state / "fresh_base_implementation_plan.json"),
            "continuation_required": True,
            "continuation_reason": f"resolve {base_label} data files and loader contract" if data_required else "fresh base data/loader ready; reference protocol probe required" if reference_probe_required else "fresh base reference protocol passed; bounded reference smoke required" if reference_smoke_required else "fresh base reference smoke passed; paper-level reference reproduction required" if reference_reproduction_required else "environment-stage anchor needs code/data/protocol implementation route",
            "paper_pipeline_skipped": True,
            "paper_pipeline_skipped_reason": "environment-stage anchor lacks loader-ready real data" if data_required else "fresh base reference protocol/env manifest is not audited" if reference_probe_required else "fresh base bounded reference smoke is not audited" if reference_smoke_required else "fresh base reference reproduction is not audited" if reference_reproduction_required else "environment-stage anchor needs code/data/protocol implementation route",
            "reference_base_switch_required": True,
            "reference_base_switch_exhausted": False,
            "latest_blockers": [blocker],
            "latest_gate": {**(full.get("latest_gate", {}) if isinstance(full.get("latest_gate"), dict) else {}), "reference_reproduction_gate": payload},
            "updated_at": now_iso(),
        })
        save_json(full_path, full)
        agents_path = paths.state / "agents.json"
        agents = load_json(agents_path, {})
        if not isinstance(agents, dict):
            agents = {"project": paths.name, "agents": []}
        rows = agents.get("agents", []) if isinstance(agents.get("agents"), list) else []
        main = next((row for row in rows if isinstance(row, dict) and row.get("id") == "main"), None)
        if main is None:
            main = {"id": "main", "name": "主控 Agent", "role": "main", "children": [], "log_tail": []}
            rows.insert(0, main)
        main.update({
            "status": "blocked",
            "stage": "fresh-base-data-contract" if data_required else "fresh-base-reference-probe" if reference_probe_required else "fresh-base-reference-smoke" if reference_smoke_required else "fresh-base-reference-reproduction" if reference_reproduction_required else "fresh-base-implementation",
            "current_step": blocker["human_summary"],
            "goal": f"resolve {base_label} data files and loader contract" if data_required else f"run {base_label} reference protocol probe before experiments or paper writing" if reference_probe_required else f"run bounded {base_label} reference smoke before experiments or paper writing" if reference_smoke_required else f"run audited {base_label} reference reproduction before experiments or paper writing" if reference_reproduction_required else "resolve or implement the fresh Find paper base before experiments or paper writing",
            "process_alive": False,
            "updated_at": now_iso(),
        })
        agents["project"] = paths.name
        agents["agents"] = rows
        agents["updated_at"] = now_iso()
        save_json(agents_path, agents)
    elif gate_status == "pass":
        stale_prefixes = (
            "fresh_base_",
            "fresh_literature_base_audit",
            "terminal_reference_base_block",
            "reference_reproduction_gate",
            "literature_to_base_route",
            "environment_anchor_selection_required",
            "base_selection_blocked",
        )
        current = full.get("current_blocker") if isinstance(full.get("current_blocker"), dict) else {}
        current_category = str(current.get("category") or "") if isinstance(current, dict) else ""
        if current_category.startswith(stale_prefixes):
            full.pop("current_blocker", None)
        latest = full.get("latest_blockers", []) if isinstance(full.get("latest_blockers"), list) else []
        retained = []
        stale_text_markers = (
            "environment-stage Claude Code anchor selection exists for this run",
            "active reference base is still blocked",
            "reference base is still blocked",
            "Previous active_repo/reference-reproduction state must not satisfy",
        )
        for row in latest:
            if not isinstance(row, dict):
                retained.append(row)
                continue
            category = str(row.get("category") or "")
            issue = str(row.get("issue") or row.get("human_summary") or "")
            if category.startswith(stale_prefixes) or any(marker in issue for marker in stale_text_markers):
                continue
            retained.append(row)
        evidence_blocker = {
            "category": "selected_base_experiment_evidence_required",
            "severity": "block",
            "issue": "参考复现已通过；当前缺少当前主线下可审计、可写入论文的 项目目标候选实验。",
            "human_summary": "参考复现已通过；当前主线下一步是由 project agent 继续真实候选实验迭代，而不是写论文或切回旧路线。",
            "next_action": "继续在当前选中基底下设计、运行并审计真实项目目标候选实验；产出 artifact-local audit 和 experiment_registry 后刷新 scientific_progress、paper_evidence、submission_readiness。",
            "evidence": [
                str(paths.state / "reference_reproduction_gate.json"),
                str(paths.state / "scientific_progress_gate.json"),
                str(paths.state / "blocker_action_plan.json"),
            ],
        }
        if not any(isinstance(row, dict) and row.get("category") == evidence_blocker["category"] for row in retained):
            retained.insert(0, evidence_blocker)
        full["latest_blockers"] = retained[:6]
        if str(full.get("status") or "").startswith("blocked_fresh_base_") or str(full.get("status") or "") in {"blocked_literature_base_audit_required", "blocked_no_viable_base_switch_route"}:
            full["status"] = "running"
        if str(full.get("full_status") or "").startswith("blocked_fresh_base_") or str(full.get("full_status") or "") in {"blocked_literature_base_audit_required", "blocked_no_viable_base_switch_route"}:
            full["full_status"] = "reference_reproduction_passed"
        next_goal = (
            "参考复现已通过；当前主线转为当前选中基底上的真实 项目目标候选实验。"
            "没有 audit-ready 可推广候选前，论文/claim promotion 和自动切基底都保持阻塞。"
        )
        full.update({
            "reference_reproduction_passed": True,
            "reference_gate_decision": decision,
            "reference_gate_status": gate_status,
            "reference_base_switch_required": False,
            "reference_base_switch_exhausted": False,
            "current_goal": next_goal,
            "summary": next_goal,
            "summary_zh": next_goal,
            "continuation_required": False,
            "continuation_reason": "",
            "latest_gate": {**(full.get("latest_gate", {}) if isinstance(full.get("latest_gate"), dict) else {}), "reference_reproduction_gate": payload},
            "updated_at": now_iso(),
        })
        save_json(full_path, full)
    elif decision == "no_viable_base_switch_route":
        base_switch = payload.get("base_switch", {}) if isinstance(payload.get("base_switch"), dict) else {}
        fresh_done = bool(base_switch.get("fresh_literature_base_audit_complete"))
        issue = "; ".join(str(item) for item in (payload.get("blockers") or [])[:4])
        category = "fresh_literature_base_audit_exhausted" if fresh_done else "terminal_reference_base_block"
        blocker = {
            "category": category,
            "severity": "block",
            "issue": issue or "Reference reproduction and base selection remain blocked.",
            "evidence": [
                str(paths.state / "reference_reproduction_gate.json"),
                str(paths.state / "literature_base_audit.json"),
                str(paths.state / "evidence_ready_repo_selection.json"),
                str(paths.state / "repo_selection_blocker.json"),
            ],
            "next_action": "Continue external fresh-base search or get explicit user confirmation before using an imperfect base." if fresh_done else "Add new evidence-ready base/data/protocol evidence before continuing.",
            "human_summary": (
                "Find 强推荐候选池已审完，但没有 evidence-ready 新基底；TASTE 已阻止静默回到 historical route，等待继续搜索或用户确认。"
                if fresh_done else
                "参考复现和换基底均阻塞；自动路线不能继续论文或 claim promotion。"
            ),
        }
        full.update({
            "status": "blocked_no_viable_base_switch_route",
            "current_goal": blocker["human_summary"],
            "continuation_required": True,
            "continuation_reason": "fresh literature audit complete without evidence-ready new base" if fresh_done else "reference blocked and base-switch exhausted",
            "paper_pipeline_skipped": True,
            "paper_pipeline_skipped_reason": "reference/base gate is blocked",
            "reference_base_switch_required": True,
            "reference_base_switch_exhausted": True,
            "latest_blockers": [blocker],
            "latest_gate": {**(full.get("latest_gate", {}) if isinstance(full.get("latest_gate"), dict) else {}), "reference_reproduction_gate": payload},
            "updated_at": now_iso(),
        })
        save_json(full_path, full)
        agents_path = paths.state / "agents.json"
        agents = load_json(agents_path, {})
        if not isinstance(agents, dict):
            agents = {"project": paths.name, "agents": []}
        rows = agents.get("agents", []) if isinstance(agents.get("agents"), list) else []
        main = next((row for row in rows if isinstance(row, dict) and row.get("id") == "main"), None)
        if main is None:
            main = {"id": "main", "name": "主控 Agent", "role": "main", "children": [], "log_tail": []}
            rows.insert(0, main)
        main.update({
            "status": "blocked",
            "stage": "base-selection-blocked",
            "current_step": blocker["human_summary"],
            "goal": "wait for new evidence-ready base search or explicit user confirmation",
            "process_alive": False,
            "updated_at": now_iso(),
        })
        agents["agents"] = rows
        agents["updated_at"] = now_iso()
        save_json(agents_path, agents)

def main() -> int:
    parser = argparse.ArgumentParser(description="Audit whether The workflow has reproduced the active reference work before building on it.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    args = parser.parse_args()
    payload = build_reference_reproduction_gate(args.project)
    print(build_paths(args.project).reports / "reference_reproduction_gate.md")
    return 0 if payload.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
