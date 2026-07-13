from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import ROOT


ENVIRONMENT_POLICY_VERSION = "environment.deployment_decision.v79"


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _arg_value(args: list[str], name: str) -> str:
    for index, value in enumerate(args):
        if value == name and index + 1 < len(args):
            return str(args[index + 1])
        if str(value).startswith(name + "="):
            return str(value)[len(name) + 1:]
    return ""


def _has_arg(args: list[str], name: str) -> bool:
    return any(value == name or str(value).startswith(name + "=") for value in args)


def _payload_run_id(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    return str(data.get("run_id") or data.get("source_run_id") or data.get("find_run_id") or data.get("current_find_run_id") or "").strip()


def _project_current_find_run_id(project_root: Path) -> str:
    for rel in (
        Path("state/current_find_research_plan.json"),
        Path("planning/finding/find_results.json"),
        Path("planning/finding/find_progress.json"),
        Path("planning/finding/read_results.json"),
    ):
        run_id = _payload_run_id(_read_json(project_root / rel, {}))
        if run_id:
            return run_id
    return ""


def _project_selected_execution_ids(project_root: Path) -> tuple[str, str]:
    current = _read_json(project_root / "state" / "current_find_research_plan.json", {})
    if not isinstance(current, dict):
        return "", ""
    plan_id = str(
        current.get("selected_plan_id")
        or current.get("current_find_plan_id")
        or current.get("plan_id")
        or ""
    ).strip()
    idea_id = str(
        current.get("selected_idea_id")
        or current.get("current_find_idea_id")
        or current.get("idea_id")
        or ""
    ).strip()
    return plan_id, idea_id


def project_experiment_plan_path(project: str, *, projects_root: Path | None = None) -> Path:
    project_root = (projects_root or ROOT / "projects") / project
    for rel in (
        Path("state/experiment_plan.json"),
        Path("state/taste_plan_bridge.json"),
        Path("planning/finding/experiment_plan.json"),
        Path("planning/finding/taste_plan_bridge.json"),
    ):
        path = project_root / rel
        payload = _read_json(path, {})
        if isinstance(payload, dict) and payload:
            return path
    raise ValueError(
        "当前项目缺少可执行实验计划：需要先完成当前 Find 的 Read/Idea/Plan 并选择唯一计划，"
        "生成 state/experiment_plan.json 或 state/taste_plan_bridge.json 后再运行环境阶段。"
    )


def project_environment_conda_name(project: str, *, projects_root: Path | None = None) -> str:
    project_root = (projects_root or ROOT / "projects") / project
    config = _read_json(project_root / "project.json", {})
    if not isinstance(config, dict):
        config = {}
    runtime = config.get("runtime") if isinstance(config.get("runtime"), dict) else {}
    environment = config.get("environment") if isinstance(config.get("environment"), dict) else {}
    for container in (config, runtime, environment):
        if "conda_env" in container:
            return str(container.get("conda_env") or "").strip()
    handoff = _read_json(project_root / "state" / "environment_handoff.json", {})
    payload = handoff.get("environment_handoff") if isinstance(handoff, dict) and isinstance(handoff.get("environment_handoff"), dict) else {}
    conda = payload.get("conda") if isinstance(payload.get("conda"), dict) else {}
    return str(conda.get("env_name") or "").strip()


def prepare_environment_invocation(project: str, *, action: str, args: list[str]) -> list[str]:
    normalized = str(action or "").strip().replace("-", "_") or "deploy_from_plan"
    prepared = list(args)
    argument_project = _arg_value(prepared, "--project")
    selected_project = str(project or argument_project).strip()
    if normalized in {"deploy_from_plan", "chat"} and not selected_project:
        raise ValueError(f"Environment {normalized} requires a project.")
    if argument_project and selected_project and argument_project != selected_project:
        raise ValueError(f"Environment project mismatch: {argument_project} != {selected_project}")
    if selected_project and not _has_arg(prepared, "--project"):
        prepared.extend(["--project", selected_project])
    if normalized == "deploy_from_plan":
        if not _has_arg(prepared, "--plan"):
            prepared.extend(["--plan", str(project_experiment_plan_path(selected_project))])
        if not _has_arg(prepared, "--conda-env"):
            configured_conda = project_environment_conda_name(selected_project)
            if configured_conda:
                prepared.extend(["--conda-env", configured_conda])
    return prepared


def _stdout_run_hint(stdout_text: str) -> dict[str, str]:
    text = str(stdout_text or "")
    out: dict[str, str] = {}
    for key in ("run_dir", "environment_run_id", "run_id"):
        match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', text)
        if match:
            out[key] = match.group(1)
    return out


def _environment_run_dir(result_payload: dict[str, Any], *, environment_root: Path | None = None, stdout_text: str = "") -> Path:
    environment_root = environment_root or ROOT / "modules" / "environment"
    result = result_payload if isinstance(result_payload, dict) else {}
    if not result and stdout_text:
        result = _stdout_run_hint(stdout_text)
    nested = result.get("result") if isinstance(result.get("result"), dict) else {}
    run_dir_text = str(result.get("run_dir") or nested.get("run_dir") or "").strip()
    if not run_dir_text:
        run_id = str(result.get("environment_run_id") or result.get("run_id") or nested.get("run_id") or "").strip()
        if run_id:
            run_dir_text = str(environment_root / ".runtime" / "runs" / run_id)
    if not run_dir_text:
        raise ValueError("Environment result did not include run_dir or environment_run_id.")
    requested_run_dir = Path(run_dir_text).expanduser()
    if requested_run_dir.name == "latest_run":
        raise ValueError("latest_run is a human review link and cannot be used as a program run.")
    run_dir = requested_run_dir.resolve()
    runs_root = (environment_root / ".runtime" / "runs").resolve()
    try:
        run_dir.relative_to(runs_root)
    except ValueError as exc:
        raise ValueError(f"Environment run_dir is outside .runtime/runs: {run_dir}") from exc
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Environment run_dir does not exist: {run_dir}")
    return run_dir


def _handoff_ready(decision: dict[str, Any]) -> bool:
    handoff = decision.get("environment_handoff") if isinstance(decision.get("environment_handoff"), dict) else {}
    gate = handoff.get("handoff_gate") if isinstance(handoff.get("handoff_gate"), dict) else {}
    checks = gate.get("checks") if isinstance(gate.get("checks"), list) else []
    return bool(
        handoff.get("ready_for_experimenting") is True
        and str(handoff.get("policy_version") or "") == ENVIRONMENT_POLICY_VERSION
        and str(gate.get("policy_version") or "") == ENVIRONMENT_POLICY_VERSION
        and gate.get("passed") is True
        and gate.get("missing") in (None, [], "")
        and checks
        and all(isinstance(row, dict) and row.get("passed") is True for row in checks)
    )


def _sync_conda_to_project_config(
    project_root: Path,
    *,
    run_dir: Path,
    env_name: str,
    conda_prefix: str,
    experiment_python: str,
    ready: bool,
) -> dict[str, Any]:
    if not env_name:
        return {"status": "not_available"}
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", env_name):
        raise ValueError(f"Environment returned an invalid Conda environment name: {env_name}")
    config_path = project_root / "project.json"
    config = _read_json(config_path, {})
    if not isinstance(config, dict):
        config = {}
    runtime = config.get("runtime") if isinstance(config.get("runtime"), dict) else {}
    environment = config.get("environment") if isinstance(config.get("environment"), dict) else {}
    configured = ""
    for container in (config, runtime, environment):
        if "conda_env" in container:
            configured = str(container.get("conda_env") or "").strip()
            break
    if configured and configured != env_name:
        raise ValueError(f"Environment Conda name does not match the saved project value: {env_name} != {configured}")
    config["conda_env"] = env_name
    runtime["conda_env"] = env_name
    runtime["environment_run_id"] = run_dir.name
    if ready:
        runtime["conda_env_prefix"] = conda_prefix
        runtime["experiment_python"] = experiment_python
    else:
        previous_run_id = str(runtime.get("environment_runtime_source_run_id") or "")
        if previous_run_id:
            runtime.pop("conda_env_prefix", None)
            runtime.pop("experiment_python", None)
    runtime["environment_runtime_source_run_id"] = run_dir.name
    config["runtime"] = runtime
    _write_json(config_path, config)
    return {
        "status": "ready" if ready else "name_fixed",
        "conda_env": env_name,
        "conda_env_prefix": conda_prefix if ready else "",
        "experiment_python": experiment_python if ready else "",
    }


def _sync_decision_to_project(project_root: Path, run_dir: Path, decision: dict[str, Any]) -> dict[str, Any]:
    state_dir = project_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    handoff = decision.get("environment_handoff") if isinstance(decision.get("environment_handoff"), dict) else {}
    ready = _handoff_ready(decision)
    repo = handoff.get("repo") if isinstance(handoff.get("repo"), dict) else {}
    conda = handoff.get("conda") if isinstance(handoff.get("conda"), dict) else {}
    if not conda:
        fixed_conda = _read_json(run_dir / "conda_environment.json", {})
        if isinstance(fixed_conda, dict) and fixed_conda.get("env_name"):
            env_name = str(fixed_conda.get("env_name") or "").strip()
            prefix = run_dir / "conda_envs" / env_name
            conda = {"env_name": env_name, "prefix": str(prefix), "python": str(prefix / "bin" / "python")}
    paper = handoff.get("paper") if isinstance(handoff.get("paper"), dict) else {}
    repo_path = str(repo.get("repo_path") or "").strip()
    conda_prefix = str(conda.get("prefix") or "").strip()
    experiment_python = str(conda.get("python") or (str(Path(conda_prefix) / "bin" / "python") if conda_prefix else "")).strip()
    conda_env_name = str(conda.get("env_name") or "").strip()
    conda_config_sync = _sync_conda_to_project_config(
        project_root,
        run_dir=run_dir,
        env_name=conda_env_name,
        conda_prefix=conda_prefix,
        experiment_python=experiment_python,
        ready=ready,
    )
    current_run = _project_current_find_run_id(project_root)
    current_plan_id, current_idea_id = _project_selected_execution_ids(project_root)
    selected_plan_id = str(paper.get("selected_plan_id") or current_plan_id).strip()
    selected_idea_id = str(paper.get("selected_idea_id") or current_idea_id).strip()
    selected = {
        "name": str(repo.get("repo_url") or repo_path),
        "repo": str(repo.get("repo_url") or ""),
        "repo_url": str(repo.get("repo_url") or ""),
        "repo_path": repo_path,
        "local_path": repo_path,
        "head_commit": str(repo.get("head_commit") or ""),
        "fresh_find_run_id": current_run,
        "current_find_run_id": current_run,
        "selected_plan_id": selected_plan_id,
        "selected_idea_id": selected_idea_id,
        "selection_stage": "environment_claude_code",
        "selected_by_stage": "environment_claude_code",
        "selection_gate": "environment_handoff_ready_for_experimenting" if ready else "environment_handoff_not_ready",
        "environment_run_id": str(handoff.get("run_id") or decision.get("run_id") or run_dir.name),
    }
    env_record = {
        "schema_version": "project.environment_handoff_projection.v1",
        "updated_at": _utc_now(),
        "status": "ready_for_experimenting" if ready else "not_ready_for_experimenting",
        "valid": ready,
        "source": str(run_dir / "environment_deployment_decision.json"),
        "module_run_dir": str(run_dir),
        "environment_run_id": run_dir.name,
        "requested_run_id": str((_read_json(run_dir / "run_meta.json", {}) or {}).get("requested_run_id") or ""),
        "decision": str(decision.get("decision") or ""),
        "exit_code": decision.get("exit_code"),
        "environment_handoff": handoff,
        "repo_path": repo_path,
        "local_path": repo_path,
        "repo_url": str(repo.get("repo_url") or ""),
        "conda_env": conda_env_name,
        "conda_env_prefix": conda_prefix,
        "experiment_python": experiment_python,
        "pending_downstream_metrics": handoff.get("pending_downstream_metrics") if isinstance(handoff.get("pending_downstream_metrics"), list) else [],
        "selected": selected,
        "conda_config_sync": conda_config_sync,
    }
    _write_json(state_dir / "environment_handoff.json", env_record)
    _write_json(state_dir / "environment_latest_run.json", {
        "schema_version": "project.environment_latest_run.v1",
        "updated_at": env_record["updated_at"],
        "environment_run_id": run_dir.name,
        "module_run_dir": str(run_dir),
        "decision_path": str(run_dir / "environment_deployment_decision.json"),
        "status": env_record["status"],
    })
    if ready and repo_path:
        selection_record = {
            "schema_version": "project.evidence_ready_repo_selection.v2",
            "status": "ready_for_experimenting",
            "decision": "environment_handoff_ready_for_experimenting",
            "valid": True,
            "accepted_by_claude": True,
            "selection_stage": "environment_claude_code",
            "selected_by_stage": "environment_claude_code",
            "selection_gate": "environment_handoff_ready_for_experimenting",
            "fresh_find_run_id": current_run,
            "current_find_run_id": current_run,
            "selected_plan_id": selected_plan_id,
            "selected_idea_id": selected_idea_id,
            "selected": selected,
            "environment_handoff_path": str(state_dir / "environment_handoff.json"),
        }
        active_repo = {
            "name": selected["name"],
            "repo": selected["repo"],
            "repo_url": selected["repo_url"],
            "repo_path": repo_path,
            "local_path": repo_path,
            "head_commit": selected["head_commit"],
            "conda_env": env_record["conda_env"],
            "conda_env_prefix": env_record["conda_env_prefix"],
            "experiment_python": env_record["experiment_python"],
            "environment_run_id": selected["environment_run_id"],
            "role": "main_fresh_base",
            "selection_stage": "environment_claude_code",
            "selection_gate": "environment_handoff_ready_for_experimenting",
            "selected_plan_id": selected_plan_id,
            "selected_idea_id": selected_idea_id,
        }
        _write_json(state_dir / "evidence_ready_repo_selection.json", selection_record)
        _write_json(state_dir / "active_repo.json", active_repo)
    return env_record


def _sync_chat_to_project(project_root: Path, run_dir: Path) -> dict[str, Any]:
    state_dir = project_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    chat = _read_json(run_dir / "environment_chat_result.json", {})
    if not isinstance(chat, dict):
        chat = {}
    controller = chat.get("controller_result") if isinstance(chat.get("controller_result"), dict) else {}
    if controller:
        conversation = controller.get("conversation") if isinstance(controller.get("conversation"), list) else []
        last_result = {key: value for key, value in controller.items() if key != "conversation"}
        _write_json(state_dir / "environment_controller_last_result.json", last_result)
        _write_json(state_dir / "environment_controller_history.json", {
            "schema_version": "project.environment_controller_history.v1",
            "updated_at": _utc_now(),
            "project": project_root.name,
            "session_id": str(controller.get("session_id") or ""),
            "turns": [item for item in conversation if isinstance(item, dict)],
        })
    else:
        conversation = []
    record = {
        "schema_version": "project.environment_chat_latest.v1",
        "updated_at": _utc_now(),
        "environment_run_id": run_dir.name,
        "module_run_dir": str(run_dir),
        "chat_result_path": str(run_dir / "environment_chat_result.json"),
        "status": str(chat.get("status") or ""),
        "return_code": chat.get("return_code"),
        "message_id": str(chat.get("message_id") or controller.get("message_id") or ""),
        "queued": bool(chat.get("queued") or controller.get("queued")),
        "interrupted_current": bool(chat.get("interrupted_current") or controller.get("interrupted_current")),
        "instruction": str(controller.get("instruction") or ""),
        "response_markdown": str(controller.get("response_markdown") or ""),
        "session_id": str(controller.get("session_id") or ""),
        "conversation_count": len(conversation),
    }
    _write_json(state_dir / "environment_chat_latest.json", record)
    return record


def sync_environment_outputs(
    project: str,
    *,
    action: str,
    result_payload: dict[str, Any],
    stdout_text: str = "",
    projects_root: Path | None = None,
    environment_root: Path | None = None,
) -> dict[str, Any]:
    projects_root = projects_root or ROOT / "projects"
    environment_root = environment_root or ROOT / "modules" / "environment"
    project_root = projects_root / project
    if not project_root.exists():
        raise FileNotFoundError(f"Project does not exist: {project}")
    run_dir = _environment_run_dir(result_payload, environment_root=environment_root, stdout_text=stdout_text)
    normalized = str(action or "").strip().replace("-", "_")
    if normalized == "chat":
        state_record = _sync_chat_to_project(project_root, run_dir)
        return {
            "status": "ok",
            "project": project,
            "action": "chat",
            "environment_run_id": run_dir.name,
            "module_run_dir": str(run_dir),
            "state": state_record,
        }
    decision = _read_json(run_dir / "environment_deployment_decision.json", {})
    if not isinstance(decision, dict) or not decision:
        raise FileNotFoundError(f"Environment decision is missing: {run_dir / 'environment_deployment_decision.json'}")
    state_record = _sync_decision_to_project(project_root, run_dir, decision)
    return {
        "status": "ready_for_experimenting" if state_record.get("valid") else "not_ready_for_experimenting",
        "project": project,
        "action": "deploy_from_plan",
        "environment_run_id": run_dir.name,
        "module_run_dir": str(run_dir),
        "decision": decision.get("decision"),
        "exit_code": decision.get("exit_code"),
        "environment_handoff_path": str(project_root / "state" / "environment_handoff.json"),
        "ready_for_experimenting": bool(state_record.get("valid")),
    }
