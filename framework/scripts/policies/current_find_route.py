from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _project_root(paths_or_root: Any) -> Path:
    if isinstance(paths_or_root, (str, Path)):
        return Path(paths_or_root)
    return Path(paths_or_root.root)


def _planning_root(paths_or_root: Any) -> Path:
    return Path(paths_or_root.planning) if hasattr(paths_or_root, "planning") else _project_root(paths_or_root) / "planning"


def payload_run_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(
        payload.get("run_id")
        or payload.get("source_run_id")
        or payload.get("find_run_id")
        or payload.get("current_find_run_id")
        or ""
    ).strip()


def current_find_run_id(paths_or_root: Any) -> str:
    root = _project_root(paths_or_root)
    planning = _planning_root(paths_or_root)
    for path in (
        planning / "finding" / "find_progress.json",
        root / "state" / "current_find_recommendation_projection.json",
        root / "state" / "current_find_research_plan.json",
        root / "state" / "literature_tool_packet.json",
        root / "state" / "finding_frontend.json",
        planning / "finding" / "find_results.json",
    ):
        run_id = payload_run_id(_read_json(path))
        if run_id:
            return run_id
    return ""


def route_run_id(row: Any) -> str:
    return str(
        row.get("fresh_find_run_id")
        or row.get("current_find_run_id")
        or row.get("find_run_id")
        or row.get("run_id")
        or ""
    ).strip() if isinstance(row, dict) else ""


def route_plan_id(row: Any) -> str:
    return str(
        row.get("selected_plan_id")
        or row.get("current_find_plan_id")
        or row.get("source_plan_id")
        or ""
    ).strip() if isinstance(row, dict) else ""


def route_idea_id(row: Any) -> str:
    return str(
        row.get("selected_idea_id")
        or row.get("current_find_idea_id")
        or row.get("source_idea_id")
        or ""
    ).strip() if isinstance(row, dict) else ""


def repo_path_from_mapping(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("repo_path", "active_repo_path", "local_path", "path", "current_selected_repo_path"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def title_key_for_current_find(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def current_find_recommended_title_keys(paths_or_root: Any) -> set[str]:
    current_run = current_find_run_id(paths_or_root)
    payload = _read_json(_planning_root(paths_or_root) / "finding" / "find_results.json")
    payload_run = payload_run_id(payload)
    if current_run and payload_run and payload_run != current_run:
        return set()
    keys: set[str] = set()
    for pool in ("articles", "strong_recommendations"):
        rows = payload.get(pool)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = title_key_for_current_find(row.get("title") or row.get("paper_title"))
            if key:
                keys.add(key)
    return keys


def selected_title_in_current_find(
    paths_or_root: Any,
    selected: dict[str, Any],
    decision: dict[str, Any] | None = None,
    *,
    audit_is_current: Callable[[dict[str, Any]], bool] | None = None,
) -> bool:
    decision = decision if isinstance(decision, dict) else {}
    title = (
        selected.get("title")
        or selected.get("literature_base_title")
        or selected.get("selected_base_title")
        or decision.get("selected_base_title")
        or selected.get("name")
        or ""
    )
    key = title_key_for_current_find(title)
    current_run = current_find_run_id(paths_or_root)
    selected_run = route_run_id(selected)
    run_matches = not current_run or not selected_run or selected_run == current_run
    if run_matches and key and key in current_find_recommended_title_keys(paths_or_root):
        return True

    root = _project_root(paths_or_root)
    audit: dict[str, Any] = {}
    for audit_name in (
        "fresh_base_reference_full_reproduction_audit.json",
        "fresh_base_reference_reproduction_audit.json",
    ):
        candidate = _read_json(root / "state" / audit_name)
        if candidate and (audit_is_current is None or audit_is_current(candidate)):
            audit = candidate
            break
    audit_selected = audit.get("selected_base") if isinstance(audit.get("selected_base"), dict) else {}
    selected_repo = str(selected.get("repo_path") or selected.get("local_path") or "").strip()
    audit_repo = str(
        audit.get("repo_path")
        or audit.get("active_repo_path")
        or audit_selected.get("repo_path")
        or audit_selected.get("local_path")
        or ""
    ).strip()
    audit_title = (
        audit_selected.get("literature_base_title")
        or audit_selected.get("title")
        or audit.get("paper_title")
        or audit.get("base_title")
        or ""
    )
    audit_run = route_run_id(audit_selected)
    if selected_repo and audit_repo and selected_repo == audit_repo and (
        (audit_run and selected_run == audit_run and (not current_run or selected_run == current_run))
        or (run_matches and key and title_key_for_current_find(audit_title) == key)
    ):
        return True

    gate = _read_json(root / "state" / "base_switch_gate.json")
    execution = _read_json(root / "state" / "base_switch_execution.json")
    candidate = gate.get("candidate_route") if isinstance(gate.get("candidate_route"), dict) else {}
    candidate_repo = str(candidate.get("repo_path") or "").strip()
    return bool(
        selected_repo
        and candidate_repo
        and selected_repo == candidate_repo
        and gate.get("status") == "pass"
        and gate.get("decision") == "authorize_base_switch"
        and gate.get("switch_authorized") is True
        and str(execution.get("status") or "").startswith("authorized_by_deterministic_base_switch_gate")
    )
