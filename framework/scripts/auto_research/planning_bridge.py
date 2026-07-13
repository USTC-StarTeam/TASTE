from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import FRAMEWORK_INPUTS_DIR, ROOT


PLANNING_INPUT_SCHEMA = "taste.planning_input.v1"


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        handle.write(text)
        temp = Path(handle.name)
    os.replace(temp, path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        handle.write(str(text))
        temp = Path(handle.name)
    os.replace(temp, path)


def _payload_run_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("run_id") or payload.get("source_run_id") or payload.get("find_run_id") or payload.get("current_find_run_id") or "").strip()


def _safe_project_root(project: str, projects_root: Path) -> Path:
    value = str(project or "").strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise ValueError("Invalid project name. Use only letters, numbers, dash, underscore, and dot.")
    root = (projects_root / value).resolve(strict=False)
    try:
        root.relative_to(projects_root.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"Project path escapes projects root: {value}") from exc
    if not root.is_dir():
        raise FileNotFoundError(f"Project not found: {value}")
    return root


def _project_current_find_run_id(project_root: Path) -> str:
    for rel in (
        Path("planning/finding/find_progress.json"),
        Path("state/current_find_research_plan.json"),
        Path("planning/finding/read_results.json"),
        Path("planning/finding/ideas.json"),
    ):
        run_id = _payload_run_id(_read_json(project_root / rel, {}))
        if run_id:
            return run_id
    return ""


def _idea_key(idea: dict[str, Any]) -> str:
    return str(idea.get("id") or idea.get("idea_id") or idea.get("title") or "").strip()


def _approved_for_planning(idea: Any) -> bool:
    if not isinstance(idea, dict):
        return False
    status = str(idea.get("status") or idea.get("recommendation") or "").strip().lower()
    if status in {"deleted", "rejected", "reject", "archived", "pending"}:
        return False
    return bool(
        idea.get("approved") is True
        or idea.get("approved_for_planning") is True
        or idea.get("pursue") is True
        or status == "approved"
        or "approved" in status
        or "pursue" in status
    )


def _plan_id(plan: Any) -> str:
    return str((plan if isinstance(plan, dict) else {}).get("plan_id") or (plan if isinstance(plan, dict) else {}).get("id") or "").strip()


def _ideas_revision(ideas: list[dict[str, Any]]) -> str:
    normalized = json.dumps(ideas, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _planned_idea_ids(plans_payload: dict[str, Any]) -> list[str]:
    explicit = plans_payload.get("planned_idea_ids") if isinstance(plans_payload.get("planned_idea_ids"), list) else []
    values = [str(value or "").strip() for value in explicit if str(value or "").strip()]
    if not values:
        values = [str(row.get("idea_id") or "").strip() for row in plans_payload.get("plans", []) if isinstance(row, dict) and str(row.get("idea_id") or "").strip()]
    return list(dict.fromkeys(values))


def _read_plan_inputs(project_root: Path, requested_run_id: str) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    taste_dir = project_root / "planning" / "finding"
    read_results = _read_json(taste_dir / "read_results.json", {})
    validation = _read_json(project_root / "state" / "current_find_claude_reading_validation.json", {})
    ideas_payload = _read_json(taste_dir / "ideas.json", {})
    current_run_id = _project_current_find_run_id(project_root)
    requested = str(requested_run_id or "").strip()
    if not current_run_id:
        raise ValueError("The project has no current Find run.")
    if requested and requested != current_run_id:
        raise ValueError(f"Requested Planning run_id {requested} does not match current Find {current_run_id}.")
    if not isinstance(read_results, dict) or _payload_run_id(read_results) != current_run_id:
        raise ValueError("Current Find Read results are missing or stale; complete Read before Planning.")
    if not isinstance(validation, dict) or _payload_run_id(validation) != current_run_id or validation.get("valid") is not True:
        raise ValueError("Current Find Read validation has not passed; complete or repair Read before Planning.")
    if not isinstance(ideas_payload, dict) or _payload_run_id(ideas_payload) != current_run_id:
        raise ValueError("Current Find Ideas are missing or stale; generate Ideas before Planning.")
    idea_markdown_path = taste_dir / "idea.md"
    idea_markdown = idea_markdown_path.read_text(encoding="utf-8", errors="replace") if idea_markdown_path.is_file() else ""
    if not idea_markdown.lstrip().startswith("#"):
        raise ValueError("Current idea.md is missing or invalid; repair Ideation before Planning.")
    approved = [dict(row) for row in ideas_payload.get("ideas", []) if _approved_for_planning(row)]
    approved_ids = [_idea_key(row) for row in approved]
    if not approved:
        raise ValueError("Planning requires at least one explicitly approved Idea from the current Find run.")
    if not all(approved_ids) or len(set(approved_ids)) != len(approved_ids):
        raise ValueError("Approved Ideas must have unique non-empty IDs before Planning.")
    return current_run_id, ideas_payload, approved


def prepare_current_find_planning_input(
    project: str,
    *,
    action: str,
    requested_run_id: str = "",
    requested_idea_ids: list[str] | None = None,
    plan_id: str = "",
    version_id: str = "",
    projects_root: Path | None = None,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    projects_root = projects_root or ROOT / "projects"
    project_root = _safe_project_root(project, projects_root)
    normalized_action = str(action or "plan").strip().replace("-", "_") or "plan"
    if normalized_action not in {"plan", "polish", "finish", "select", "update_markdown"}:
        raise ValueError(f"Unsupported Planning bridge action: {action}")
    run_id, ideas_payload, approved = _read_plan_inputs(project_root, requested_run_id)
    approved_ids = [_idea_key(row) for row in approved]
    approved_id_set = set(approved_ids)
    requested_ids = [str(value).strip() for value in (requested_idea_ids or []) if str(value).strip()]
    if len(requested_ids) != len(set(requested_ids)):
        raise ValueError("Planning Idea selection contains duplicate IDs.")
    unknown_ids = [idea_id for idea_id in requested_ids if idea_id not in approved_id_set]
    if unknown_ids:
        raise ValueError(f"Planning may consume only explicitly approved current-Find Ideas; invalid IDs: {', '.join(unknown_ids)}")
    selected_ids = requested_ids or approved_ids
    selected_id_set = set(selected_ids)
    selected_ideas = [row for row in approved if _idea_key(row) in selected_id_set]
    if normalized_action == "plan" and not selected_ideas:
        raise ValueError("Planning requires at least one selected, explicitly approved Idea.")

    taste_dir = project_root / "planning" / "finding"
    plans_payload: dict[str, Any] = {}
    plan_markdown = ""
    if normalized_action != "plan":
        loaded = _read_json(taste_dir / "plans.json", {})
        if not isinstance(loaded, dict) or _payload_run_id(loaded) != run_id:
            raise ValueError("Current Planning candidates are missing or stale.")
        plans_payload = loaded
        plans = [row for row in loaded.get("plans", []) if isinstance(row, dict)]
        if not plans:
            raise ValueError("Planning selection and editing require at least one current plan candidate.")
        if normalized_action in {"polish", "finish"} and not any(_plan_id(row) == str(plan_id or "").strip() for row in plans):
            raise ValueError(f"Plan not found in the current Planning artifact: {plan_id}")
        plan_path = taste_dir / "plan.md"
        plan_markdown = plan_path.read_text(encoding="utf-8", errors="replace") if plan_path.is_file() else ""
        if not plan_markdown.lstrip().startswith("# Research Plans"):
            raise ValueError("Current canonical plan.md is missing or invalid.")

    bundle = {
        "schema_version": PLANNING_INPUT_SCHEMA,
        "action": normalized_action,
        "run_id": run_id,
        "source_run_id": run_id,
        "project": project,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ideas": {
            "run_id": run_id,
            "source_run_id": run_id,
            "source": ideas_payload.get("source") or "taste_ideation",
            "ideas": selected_ideas if normalized_action == "plan" else approved,
        },
        "plans": plans_payload,
        "plan_markdown": plan_markdown,
        "selection": {"plan_id": str(plan_id or "").strip(), "version_id": str(version_id or "").strip()},
        "source_manifest": {
            "idea_markdown": str(taste_dir / "idea.md"),
            "ideas": str(taste_dir / "ideas.json"),
            "read_results": str(taste_dir / "read_results.json"),
            "reading_validation": str(project_root / "state" / "current_find_claude_reading_validation.json"),
            "plan_markdown": str(taste_dir / "plan.md") if normalized_action != "plan" else "",
        },
    }
    runtime_root = runtime_root or FRAMEWORK_INPUTS_DIR / "planning"
    runtime_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=runtime_root, prefix=f"{normalized_action}_", suffix=".json", delete=False) as handle:
        json.dump(bundle, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        input_path = Path(handle.name)
    return {
        "status": "prepared_current_find_planning_input",
        "project": project,
        "run_id": run_id,
        "action": normalized_action,
        "input_json": str(input_path),
        "approved_idea_count": len(approved),
        "approved_idea_ids": approved_ids,
        "selected_idea_count": len(selected_ideas),
        "selected_idea_ids": [_idea_key(row) for row in selected_ideas],
    }


def remove_prepared_planning_input(path: str | Path) -> None:
    candidate = Path(path).expanduser()
    expected_root = (FRAMEWORK_INPUTS_DIR / "planning").resolve(strict=False)
    try:
        candidate.resolve(strict=False).relative_to(expected_root)
    except ValueError:
        return
    candidate.unlink(missing_ok=True)


def prepare_planning_refresh_after_idea_change(
    project: str,
    *,
    changed_idea_id: str = "",
    projects_root: Path | None = None,
) -> dict[str, Any]:
    projects_root = projects_root or ROOT / "projects"
    project_root = _safe_project_root(project, projects_root)
    taste_dir = project_root / "planning" / "finding"
    plans_payload = _read_json(taste_dir / "plans.json", {})
    current_run_id = _project_current_find_run_id(project_root)
    if not isinstance(plans_payload, dict) or _payload_run_id(plans_payload) != current_run_id:
        return {"required": False, "reason": "no_current_plan"}
    planned_ids = _planned_idea_ids(plans_payload)
    changed = str(changed_idea_id or "").strip()
    if changed and changed not in planned_ids:
        return {"required": False, "reason": "changed_idea_has_no_plan"}
    ideas_payload = _read_json(taste_dir / "ideas.json", {})
    approved_by_id = {
        _idea_key(row): dict(row)
        for row in ideas_payload.get("ideas", [])
        if isinstance(row, dict) and _approved_for_planning(row) and _idea_key(row)
    }
    refresh_ids = [idea_id for idea_id in planned_ids if idea_id in approved_by_id]
    for path in (
        taste_dir / "plan.md",
        taste_dir / "plans.json",
        project_root / "state" / "experiment_plan.json",
        project_root / "state" / "taste_plan_bridge.json",
    ):
        path.unlink(missing_ok=True)
    state_path = project_root / "state" / "current_find_research_plan.json"
    state = _read_json(state_path, {})
    if isinstance(state, dict) and _payload_run_id(state) in {"", current_run_id}:
        for key in (
            "selected_idea_id", "selected_plan_id", "selected_idea", "selected_plan",
            "selected_by", "execution_policy", "planning_run_id",
        ):
            state.pop(key, None)
        state["current_find_plan_count"] = 0
        state["read_idea_plan_ready"] = False
        state["claude_current_find_ready"] = False
        state["status"] = "planning_refresh_pending" if refresh_ids else "awaiting_approved_idea_for_planning"
        state["human_supervision_updated_at"] = datetime.now(timezone.utc).isoformat()
        state["human_supervision_source"] = "framework_idea_change_planning_invalidation"
        _atomic_write_json(state_path, state)
    return {
        "required": True,
        "project": project,
        "run_id": current_run_id,
        "idea_ids": refresh_ids,
        "invalidated_plan_count": len(plans_payload.get("plans", [])) if isinstance(plans_payload.get("plans"), list) else 0,
    }


def _module_run_dir(result_payload: dict[str, Any], planning_root: Path) -> Path:
    result = result_payload.get("result") if isinstance(result_payload.get("result"), dict) else result_payload
    result = result if isinstance(result, dict) else {}
    run_dir_text = str(result.get("planning_run_dir") or result.get("run_dir") or result.get("runtime_dir") or "").strip()
    if not run_dir_text:
        raise ValueError("Planning result did not include result.planning_run_dir.")
    run_dir = Path(run_dir_text).expanduser().resolve()
    runs_root = (planning_root / ".runtime" / "runs").resolve()
    try:
        run_dir.relative_to(runs_root)
    except ValueError as exc:
        raise ValueError(f"Planning run_dir is outside .runtime/runs: {run_dir}") from exc
    if run_dir.name == "latest_run":
        raise ValueError("latest_run is a human review copy and cannot be synchronized.")
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Planning run_dir does not exist: {run_dir}")
    return run_dir


def _copy_run(run_dir: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f".{target.name}.tmp.{os.getpid()}"
    if temp.exists():
        shutil.rmtree(temp)
    shutil.copytree(run_dir, temp)
    if target.exists():
        shutil.rmtree(target)
    temp.rename(target)


def sync_current_find_planning_outputs(
    project: str,
    *,
    result_payload: dict[str, Any],
    projects_root: Path | None = None,
    planning_root: Path | None = None,
) -> dict[str, Any]:
    projects_root = projects_root or ROOT / "projects"
    planning_root = planning_root or ROOT / "modules" / "planning"
    project_root = _safe_project_root(project, projects_root)
    run_dir = _module_run_dir(result_payload, planning_root)
    plans_payload = _read_json(run_dir / "plans.json", {})
    experiment_plan = _read_json(run_dir / "experiment_plan.json", {})
    bridge = _read_json(run_dir / "taste_plan_bridge.json", {})
    plan_markdown = (run_dir / "plan.md").read_text(encoding="utf-8", errors="replace") if (run_dir / "plan.md").is_file() else ""
    if not isinstance(plans_payload, dict) or not isinstance(experiment_plan, dict) or not isinstance(bridge, dict):
        raise ValueError("Planning run is missing a required JSON artifact.")
    run_id = _payload_run_id(plans_payload)
    current_run_id = _project_current_find_run_id(project_root)
    if not run_id or run_id != current_run_id:
        raise ValueError(f"Planning run_id {run_id or '<missing>'} does not match project current Find {current_run_id or '<missing>'}.")
    if not plan_markdown.lstrip().startswith("# Research Plans"):
        raise ValueError("Planning run did not produce a valid plan.md top-level heading.")
    markdown_meta = plans_payload.get("plan_markdown_generation") if isinstance(plans_payload.get("plan_markdown_generation"), dict) else {}
    markdown_audit = markdown_meta.get("audit") if isinstance(markdown_meta.get("audit"), dict) else {}
    if markdown_audit.get("status") != "pass":
        raise ValueError(f"Planning plan.md audit did not pass: {markdown_audit.get('issues') or 'missing audit'}")
    plans = [row for row in plans_payload.get("plans", []) if isinstance(row, dict)]
    if not plans:
        raise ValueError("Planning output must contain at least one plan candidate.")
    plan_ids = [_plan_id(row) for row in plans]
    if not all(plan_ids) or len(set(plan_ids)) != len(plan_ids):
        raise ValueError("Planning output plan IDs must be unique and non-empty.")
    if plans_payload.get("machine_projection_from") != "plan.md":
        raise ValueError("Planning plans.json must be a projection of canonical plan.md.")
    forbidden_plan_fields = {
        "title", "initial_plan", "evaluation_rounds", "final_plan", "new_method", "method_details",
        "initial_experiment", "steps", "risks", "metrics", "plan_markdown", "markdown",
    }
    for row in plans:
        duplicate_fields = forbidden_plan_fields.intersection(row)
        if duplicate_fields:
            raise ValueError(f"plans.json duplicates plan.md content: {sorted(duplicate_fields)}")
        for version in row.get("versions", []) if isinstance(row.get("versions"), list) else []:
            if isinstance(version, dict) and forbidden_plan_fields.intersection(version):
                raise ValueError("plans.json version metadata contains duplicated plan body content.")
    if any(key in bridge for key in ("plans_json", "plan_markdown_excerpt", "experiment_plan_json")):
        raise ValueError("taste_plan_bridge.json must remain a lightweight path and selection index.")
    expected_sha = hashlib.sha256(plan_markdown.encode("utf-8")).hexdigest()
    if str(markdown_meta.get("sha256") or "") != expected_sha:
        raise ValueError("Planning plans.json does not match the canonical plan.md hash.")
    ideas_payload = _read_json(project_root / "planning" / "finding" / "ideas.json", {})
    planned_ids = _planned_idea_ids(plans_payload)
    current_ideas = [
        dict(row) for row in ideas_payload.get("ideas", [])
        if isinstance(row, dict) and _idea_key(row) in set(planned_ids) and _approved_for_planning(row)
    ]
    if [_idea_key(row) for row in current_ideas] != planned_ids:
        raise ValueError("Planning output no longer matches the approved current-Find Idea set.")
    if str(plans_payload.get("idea_revision") or "") != _ideas_revision(current_ideas):
        raise ValueError("Planning output was generated from an outdated Idea revision.")

    taste_dir = project_root / "planning" / "finding"
    project_run_dir = taste_dir / "planning_runs" / run_dir.name
    _copy_run(run_dir, project_run_dir)

    plans_current = dict(plans_payload)
    plans_current["planning_run_id"] = run_dir.name
    plans_current["module_run_dir"] = str(run_dir)
    plans_current["project_run_dir"] = str(project_run_dir)
    experiment_current = dict(experiment_plan)
    experiment_current.update({
        "plans_json_path": str(taste_dir / "plans.json"),
        "plan_markdown_path": str(taste_dir / "plan.md"),
        "planning_run_id": run_dir.name,
    })
    bridge_current = dict(bridge)
    bridge_current.update({
        "plans_json_path": str(taste_dir / "plans.json"),
        "plan_markdown_path": str(taste_dir / "plan.md"),
        "experiment_plan_json_path": str(project_root / "state" / "experiment_plan.json"),
        "selected_plan_contract_path": str(project_root / "state" / "experiment_plan.json"),
        "planning_run_id": run_dir.name,
    })
    _atomic_write_json(taste_dir / "plans.json", plans_current)
    _atomic_write_text(taste_dir / "plan.md", plan_markdown)
    _atomic_write_json(project_root / "state" / "experiment_plan.json", experiment_current)
    _atomic_write_json(project_root / "state" / "taste_plan_bridge.json", bridge_current)

    state_path = project_root / "state" / "current_find_research_plan.json"
    state = _read_json(state_path, {})
    if not isinstance(state, dict) or _payload_run_id(state) not in {"", run_id}:
        raise ValueError("Project current_find_research_plan.json changed during Planning synchronization.")
    state["run_id"] = run_id
    state.pop("plans", None)
    state["current_find_plan_count"] = len(plans)
    state["current_find_approved_idea_count"] = len({str(row.get("idea_id") or "").strip() for row in plans if str(row.get("idea_id") or "").strip()})
    state["planning_run_id"] = run_dir.name
    for key in ("selected_idea_id", "selected_plan_id", "selected_idea", "selected_plan", "selected_by", "execution_policy"):
        if key in plans_current:
            state[key] = plans_current[key]
    state["human_supervision_updated_at"] = datetime.now(timezone.utc).isoformat()
    state["human_supervision_source"] = "framework_planning_sync"
    state["source"] = "framework_planning_bridge"
    state["read_idea_plan_ready"] = True
    state["claude_current_find_ready"] = bool(str(plans_current.get("selected_plan_id") or "").strip())
    state["status"] = "ready" if state["claude_current_find_ready"] else "awaiting_plan_selection"
    _atomic_write_json(state_path, state)
    return {
        "status": "framework_synced_planning_outputs",
        "project": project,
        "run_id": run_id,
        "planning_run_id": run_dir.name,
        "module_run_dir": str(run_dir),
        "runtime_project_sync_dir": str(project_run_dir),
        "plan_count": len(plans),
        "selected_plan_id": str(plans_current.get("selected_plan_id") or ""),
        "artifacts": {
            "plan_md": str(taste_dir / "plan.md"),
            "plans": str(taste_dir / "plans.json"),
            "experiment_plan": str(project_root / "state" / "experiment_plan.json"),
            "bridge": str(project_root / "state" / "taste_plan_bridge.json"),
        },
    }
