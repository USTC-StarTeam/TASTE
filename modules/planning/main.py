#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


STAGE_NAME = "planning"
DISPLAY_NAME = "Planning"
RESPONSIBILITY = "Turn selected approved current-Find Ideas into auditable plan candidates and one selected execution contract."
REQUIRED_EXTERNAL_INPUTS = ("claude_code", "framework_planning_input", "project_constraints")
ARTIFACTS_IN = ("ideas.json", "plan.md for follow-up actions", "Framework approval and selection state")
ARTIFACTS_OUT = ("plan.md", "plans.json", "experiment_plan.json", "taste_plan_bridge.json")
PRIVATE_BACKEND_ROOTS = (
    "modules/planning/scripts/core/plan_pipeline.py",
    "modules/planning/scripts/tools/planning_tools.py",
    "modules/planning/scripts/blockers/build_blocker_action_plan.py",
    "modules/planning/scripts/actions/propose_next_actions.py",
)
COMPATIBILITY_SCRIPT_ROOTS = (
    "modules/planning/scripts/plan_pipeline.py",
    "modules/planning/scripts/planning_tools.py",
    "modules/planning/scripts/build_blocker_action_plan.py",
    "modules/planning/scripts/propose_next_actions.py",
)
PLANNING_INPUT_SCHEMA = "taste.planning_input.v1"

ROOT = Path(__file__).resolve().parents[2]
PLANNING_ROOT = Path(__file__).resolve().parent
SCRIPTS = PLANNING_ROOT / "scripts"
RUNTIME_ROOT = PLANNING_ROOT / ".runtime"
RUNTIME_RUNS_ROOT = RUNTIME_ROOT / "runs"
LATEST_RUN_REVIEW_DIR = RUNTIME_ROOT / "latest_run"


def contract() -> dict[str, Any]:
    return {
        "stage": STAGE_NAME,
        "display_name": DISPLAY_NAME,
        "responsibility": RESPONSIBILITY,
        "required_external_inputs": list(REQUIRED_EXTERNAL_INPUTS),
        "artifacts_in": list(ARTIFACTS_IN),
        "artifacts_out": list(ARTIFACTS_OUT),
        "private_backend_roots": list(PRIVATE_BACKEND_ROOTS),
        "entrypoint": "modules/planning/main.py",
        "scripts_are_private_backend": True,
        "compatibility_script_roots": list(COMPATIBILITY_SCRIPT_ROOTS),
    }


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    entries = [str(PLANNING_ROOT), str(SCRIPTS)]
    existing = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([*entries, *existing]))
    env["WORKSPACE_ROOT"] = str(ROOT)
    env["PLANNING_PUBLIC_ENTRYPOINT_ACTIVE"] = "1"
    return env


def _ensure_runtime_imports() -> None:
    for entry in reversed(_python_env()["PYTHONPATH"].split(os.pathsep)):
        if entry and entry not in sys.path:
            sys.path.insert(0, entry)


def _running_in_taste_conda() -> bool:
    if os.environ.get("CONDA_DEFAULT_ENV") == "taste":
        return True
    executable = Path(sys.executable).expanduser().resolve()
    return executable.parent.name == "bin" and executable.parent.parent.name == "taste" and executable.parent.parent.parent.name == "envs"


def _require_taste_conda() -> None:
    if not _running_in_taste_conda():
        raise RuntimeError(
            "Planning must run inside the conda environment named 'taste'. "
            "Use: conda run -n taste python modules/planning/main.py ..."
        )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Failed to read JSON input {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON input must be an object: {path}")
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _safe_slug(value: object, default: str = "planning") -> str:
    text = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(value or "").strip()).strip("-").lower()
    return (text or default)[:80]


def _precise_runtime_id(action: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}_{_safe_slug(action)}_pid{os.getpid()}"


def _new_runtime_dir(action: str, requested: str = "") -> Path:
    RUNTIME_RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    if requested:
        path = Path(requested).expanduser()
        path = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
        try:
            path.relative_to(RUNTIME_RUNS_ROOT.resolve())
        except ValueError as exc:
            raise ValueError(f"--output-dir must stay under {RUNTIME_RUNS_ROOT}") from exc
        path.mkdir(parents=True, exist_ok=False)
        return path
    for _ in range(20):
        path = RUNTIME_RUNS_ROOT / _precise_runtime_id(action)
        try:
            path.mkdir(parents=True, exist_ok=False)
            return path
        except FileExistsError:
            continue
    raise RuntimeError("Failed to create a unique Planning runtime directory.")


def _update_run_meta(run_dir: Path, **updates: Any) -> None:
    path = run_dir / "run_meta.json"
    payload: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            payload = loaded if isinstance(loaded, dict) else {}
        except Exception:
            payload = {}
    payload.update(updates)
    _write_json(path, payload)


def _start_runtime_run(action: str, source_run_id: str, requested: str = "") -> Path:
    run_dir = _new_runtime_dir(action, requested)
    _update_run_meta(
        run_dir,
        planning_run_id=run_dir.name,
        source_run_id=source_run_id,
        action=action,
        status="running",
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        pid=os.getpid(),
        python=sys.executable,
        conda_env=os.environ.get("CONDA_DEFAULT_ENV", ""),
    )
    return run_dir


def _refresh_latest_run_review_copy(run_dir: Path) -> str:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = RUNTIME_ROOT / ".latest_run.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        temp = RUNTIME_ROOT / f".latest_run.tmp.{os.getpid()}.{run_dir.name}"
        try:
            if temp.exists():
                shutil.rmtree(temp)
            shutil.copytree(run_dir, temp)
            if LATEST_RUN_REVIEW_DIR.exists():
                shutil.rmtree(LATEST_RUN_REVIEW_DIR)
            temp.rename(LATEST_RUN_REVIEW_DIR)
            return str(LATEST_RUN_REVIEW_DIR)
        finally:
            if temp.exists():
                shutil.rmtree(temp)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _complete_runtime_run(run_dir: Path, status: str, error: str = "") -> str:
    updates: dict[str, Any] = {
        "status": status,
        "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if error:
        updates["error"] = error
    _update_run_meta(run_dir, **updates)
    return _refresh_latest_run_review_copy(run_dir)


def _set_backend(backend: str) -> None:
    normalized = str(backend or "claude_code").strip().lower()
    if normalized == "off":
        os.environ["PLAN_USE_LLM"] = "0"
        os.environ["PLAN_BACKEND"] = "off"
        return
    os.environ["PLAN_USE_LLM"] = "1"
    os.environ["PLAN_BACKEND"] = normalized


def _load_config(path: str):
    _ensure_runtime_imports()
    from scripts.core.plan_pipeline import PlanningConfig

    payload = _read_json(Path(path).expanduser()) if path else {}
    return PlanningConfig(
        research_interest=str(payload.get("research_interest") or ""),
        researcher_profile=str(payload.get("researcher_profile") or ""),
    )


def _normalize_action(action: str) -> str:
    normalized = str(action or "plan").strip().replace("-", "_") or "plan"
    aliases = {
        "planning": "plan",
        "pipeline": "plan",
        "plan_pipeline": "plan",
        "plan_polish": "polish",
        "finish_plan": "finish",
        "complete_plan": "finish",
        "claude_select": "select",
        "select_best": "select",
        "save_markdown": "update_markdown",
    }
    return aliases.get(normalized, normalized)


def _load_framework_bundle(path: str, action: str, project: str) -> dict[str, Any]:
    if not path:
        raise ValueError("Project Planning requires the explicit --input-json prepared by Framework.")
    bundle = _read_json(Path(path).expanduser())
    if bundle.get("schema_version") != PLANNING_INPUT_SCHEMA:
        raise ValueError("Unsupported or missing Planning input schema.")
    if _normalize_action(str(bundle.get("action") or "")) != action:
        raise ValueError("Planning input action does not match the requested action.")
    bundle_project = str(bundle.get("project") or "").strip()
    if project and bundle_project != project:
        raise ValueError("Planning input project does not match --project.")
    if bundle_project and os.environ.get("TASTE_FRAMEWORK_MODULE_CALL") != "1":
        raise PermissionError("Project Planning may only be invoked by Framework.")
    run_id = str(bundle.get("run_id") or "").strip()
    ideas = bundle.get("ideas")
    if not run_id or not isinstance(ideas, dict) or str(ideas.get("run_id") or "").strip() != run_id:
        raise ValueError("Planning input is missing normalized current-Find Ideas.")
    if action != "plan":
        plans = bundle.get("plans")
        if not isinstance(plans, dict) or str(plans.get("run_id") or "").strip() != run_id:
            raise ValueError("Planning input is missing current plan candidates.")
        if not str(bundle.get("plan_markdown") or "").lstrip().startswith("# Research Plans"):
            raise ValueError("Planning input is missing the canonical current plan.md.")
    return bundle


def _standalone_bundle(ns: argparse.Namespace) -> dict[str, Any]:
    run_id = str(ns.run_id or "").strip()
    raw: Any = json.loads(Path(ns.idea_json).expanduser().read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("ideas"), list):
        ideas = [dict(row) for row in raw["ideas"] if isinstance(row, dict)]
    elif isinstance(raw, dict):
        ideas = [dict(raw)]
    elif isinstance(raw, list):
        ideas = [dict(row) for row in raw if isinstance(row, dict)]
    else:
        raise ValueError("--idea-json must contain an Idea object, list, or {'ideas': [...]} object.")
    for index, idea in enumerate(ideas, 1):
        idea.setdefault("id", _safe_slug(idea.get("idea_id") or idea.get("title") or f"idea-{index}"))
        idea.setdefault("title", idea["id"])
        idea["approved_for_planning"] = True
        idea["status"] = "approved"
    if not run_id:
        run_id = f"standalone-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    ns.run_id = run_id
    return {
        "schema_version": PLANNING_INPUT_SCHEMA,
        "action": "plan",
        "run_id": run_id,
        "project": "",
        "ideas": {"run_id": run_id, "source": "planning_standalone", "ideas": ideas},
        "plans": {},
    }


def _seed_runtime_inputs(run_dir: Path, bundle: dict[str, Any]) -> None:
    _write_json(run_dir / "ideas.json", bundle["ideas"])
    plans = bundle.get("plans")
    if isinstance(plans, dict) and plans:
        _write_json(run_dir / "plans.json", plans)
    plan_markdown = str(bundle.get("plan_markdown") or "")
    if plan_markdown:
        (run_dir / "plan.md").write_text(plan_markdown.rstrip() + "\n", encoding="utf-8")
    _update_run_meta(
        run_dir,
        input_schema=bundle.get("schema_version"),
        input_action=bundle.get("action"),
        project=bundle.get("project", ""),
        approved_idea_count=len(bundle["ideas"].get("ideas", [])),
    )


def _runtime_result(result: dict[str, Any], run_dir: Path, latest_review_dir: str) -> dict[str, Any]:
    plans = [row for row in result.get("plans", []) if isinstance(row, dict)]
    markdown_meta = result.get("plan_markdown_generation") if isinstance(result.get("plan_markdown_generation"), dict) else {}
    markdown_audit = markdown_meta.get("audit") if isinstance(markdown_meta.get("audit"), dict) else {}
    return {
        "run_id": str(result.get("run_id") or ""),
        "plan_count": len(plans),
        "selected_idea_id": str(result.get("selected_idea_id") or ""),
        "selected_plan_id": str(result.get("selected_plan_id") or ""),
        "public_final_artifact": str(result.get("public_final_artifact") or "plan.md"),
        "plan_markdown_source": str(markdown_meta.get("source") or ""),
        "plan_markdown_audit": markdown_audit,
        "planning_run_id": run_dir.name,
        "planning_run_dir": str(run_dir),
        "latest_run_review_dir": latest_review_dir,
    }


def _execute_runtime(
    action: str,
    bundle: dict[str, Any],
    callback: Callable[[Path], dict[str, Any]],
    *,
    output_dir: str = "",
) -> dict[str, Any]:
    run_id = str(bundle.get("run_id") or "").strip()
    run_dir = _start_runtime_run(action, run_id, output_dir)
    previous_entrypoint = os.environ.get("PLANNING_PUBLIC_ENTRYPOINT_ACTIVE")
    os.environ["PLANNING_PUBLIC_ENTRYPOINT_ACTIVE"] = "1"
    try:
        _seed_runtime_inputs(run_dir, bundle)
        result = callback(run_dir)
        latest = _complete_runtime_run(run_dir, "complete")
        return _runtime_result(result, run_dir, latest)
    except Exception as exc:
        _complete_runtime_run(run_dir, "failed", str(exc))
        raise
    finally:
        if previous_entrypoint is None:
            os.environ.pop("PLANNING_PUBLIC_ENTRYPOINT_ACTIVE", None)
        else:
            os.environ["PLANNING_PUBLIC_ENTRYPOINT_ACTIVE"] = previous_entrypoint


def _common_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--input-json", default="")
    parser.add_argument("--config-json", default="")
    parser.add_argument("--backend", choices=["claude_code", "off"], default="claude_code")
    return parser


def _run_plan(args: Sequence[str]) -> int:
    parser = _common_parser("Generate Planning candidates from selected approved Ideas.")
    parser.add_argument("--repair-rounds", type=int, default=3)
    parser.add_argument("--idea-json", default="", help="Standalone mode: JSON containing one or more Ideas.")
    parser.add_argument("--output-dir", default="")
    ns = parser.parse_args(list(args))
    _set_backend(ns.backend)
    standalone = bool(ns.idea_json)
    bundle = _standalone_bundle(ns) if standalone else _load_framework_bundle(ns.input_json, "plan", ns.project)
    run_id = str(bundle["run_id"])
    if ns.run_id and ns.run_id != run_id:
        raise ValueError("--run-id does not match the explicit Planning input.")
    approved_ids = [str(row.get("id") or row.get("idea_id") or row.get("title") or "").strip() for row in bundle["ideas"]["ideas"]]
    config = _load_config(ns.config_json)
    _ensure_runtime_imports()
    from scripts.core.plan_pipeline import PlanRequest, run_plan_at_directory

    request = PlanRequest(run_id=run_id, idea_ids=approved_ids, repair_rounds=ns.repair_rounds)
    result = _execute_runtime(
        "plan",
        bundle,
        lambda directory: run_plan_at_directory(directory, request, config),
        output_dir=ns.output_dir,
    )
    print(json.dumps({"stage": STAGE_NAME, "action": "plan", "run_id": run_id, "result": result}, ensure_ascii=False, indent=2))
    return 0


def _run_polish(args: Sequence[str]) -> int:
    parser = _common_parser("Repair one current Planning candidate.")
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--version-id", default="")
    parser.add_argument("--rounds", type=int, default=1)
    ns = parser.parse_args(list(args))
    _set_backend(ns.backend)
    bundle = _load_framework_bundle(ns.input_json, "polish", ns.project)
    run_id = str(bundle["run_id"])
    config = _load_config(ns.config_json)
    _ensure_runtime_imports()
    from scripts.core.plan_pipeline import PlanPolishRequest, polish_plan_at_directory

    request = PlanPolishRequest(run_id=run_id, plan_id=ns.plan_id, version_id=ns.version_id, rounds=ns.rounds)
    result = _execute_runtime("polish", bundle, lambda directory: polish_plan_at_directory(directory, request, config))
    print(json.dumps({"stage": STAGE_NAME, "action": "polish", "run_id": run_id, "result": result}, ensure_ascii=False, indent=2))
    return 0


def _run_select(args: Sequence[str]) -> int:
    parser = _common_parser("Ask Claude Code to select one current Planning candidate.")
    ns = parser.parse_args(list(args))
    _set_backend(ns.backend)
    bundle = _load_framework_bundle(ns.input_json, "select", ns.project)
    run_id = str(bundle["run_id"])
    config = _load_config(ns.config_json)
    _ensure_runtime_imports()
    from scripts.core.plan_pipeline import select_plan_at_directory

    result = _execute_runtime("select", bundle, lambda directory: select_plan_at_directory(directory, run_id, config))
    print(json.dumps({"stage": STAGE_NAME, "action": "select", "run_id": run_id, "result": result}, ensure_ascii=False, indent=2))
    return 0


def _run_finish(args: Sequence[str]) -> int:
    parser = _common_parser("Select one current Planning candidate by human decision.")
    parser.add_argument("--plan-id", required=True)
    ns = parser.parse_args(list(args))
    _set_backend(ns.backend)
    bundle = _load_framework_bundle(ns.input_json, "finish", ns.project)
    run_id = str(bundle["run_id"])
    _load_config(ns.config_json)
    _ensure_runtime_imports()
    from scripts.core.plan_pipeline import finish_plan_at_directory

    result = _execute_runtime("finish", bundle, lambda directory: finish_plan_at_directory(directory, run_id, ns.plan_id))
    print(json.dumps({"stage": STAGE_NAME, "action": "finish", "run_id": run_id, "result": result}, ensure_ascii=False, indent=2))
    return 0


def _run_update_markdown(args: Sequence[str]) -> int:
    parser = _common_parser("Validate and save a human-edited plan.md.")
    parser.add_argument("--stdin-markdown", action="store_true")
    ns = parser.parse_args(list(args))
    bundle = _load_framework_bundle(ns.input_json, "update_markdown", ns.project)
    markdown = sys.stdin.read() if ns.stdin_markdown else ""
    if not markdown.strip():
        raise ValueError("update_markdown requires plan.md content on stdin.")
    run_id = str(bundle["run_id"])
    _ensure_runtime_imports()
    from scripts.core.plan_pipeline import update_plan_markdown_at_directory

    result = _execute_runtime(
        "update_markdown",
        bundle,
        lambda directory: update_plan_markdown_at_directory(directory, run_id, markdown),
    )
    print(json.dumps({"stage": STAGE_NAME, "action": "update_markdown", "run_id": run_id, "result": result}, ensure_ascii=False, indent=2))
    return 0


def _run_private_script(script_stem: str, args: Sequence[str]) -> int:
    script = SCRIPTS / f"{_normalize_action(script_stem)}.py"
    if not script.is_file():
        raise ValueError(f"Unknown Planning action: {script_stem}")
    return int(subprocess.run([sys.executable, str(script), *args], cwd=ROOT, env=_python_env(), text=True).returncode)


PLANNING_TOOL_ACTIONS = {
    "experiments": "experiments",
    "workflow": "workflow",
    "blocker_resolution": "blocker_resolution",
    "review_board": "review_board",
    "method_frontier": "method_frontier",
    "reflect": "reflect",
}
ACTION_ALIASES = {
    "blocker_action": "build_blocker_action_plan",
    "next_actions": "propose_next_actions",
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Planning module public entrypoint.", add_help=False)
    parser.add_argument("--action", default="plan")
    parser.add_argument("--contract", action="store_true")
    ns, rest = parser.parse_known_args(argv)
    if ns.contract:
        print(json.dumps(contract(), ensure_ascii=False, indent=2))
        return 0
    _require_taste_conda()
    action = _normalize_action(ns.action)
    if action == "plan":
        return _run_plan(rest)
    if action == "polish":
        return _run_polish(rest)
    if action == "select":
        return _run_select(rest)
    if action in {"finish", "select_plan"}:
        return _run_finish(rest)
    if action == "update_markdown":
        return _run_update_markdown(rest)
    if action in PLANNING_TOOL_ACTIONS:
        return _run_private_script("planning_tools", ["--tool-action", PLANNING_TOOL_ACTIONS[action], *rest])
    return _run_private_script(ACTION_ALIASES.get(action, action), rest)


if __name__ == "__main__":
    raise SystemExit(main())
