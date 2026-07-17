#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterator, Sequence

STAGES = ("finding", "reading", "ideation", "planning", "environment", "experimenting", "writing")
ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = Path(__file__).resolve().parent

if str(ROOT / "framework" / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "framework" / "scripts"))

from auto_research.reading_bridge import (  # noqa: E402
    extract_last_json_object,
    prepare_current_find_read_input,
    sync_current_find_read_outputs,
)
from auto_research.ideation_bridge import (  # noqa: E402
    current_find_ideation_run_dir,
    prepare_current_find_ideation_input,
    remove_prepared_ideation_input,
    sync_current_find_ideation_outputs,
)
from auto_research.planning_bridge import (  # noqa: E402
    prepare_current_find_planning_input,
    prepare_planning_refresh_after_idea_change,
    remove_prepared_planning_input,
    sync_current_find_planning_outputs,
)
from auto_research.environment_bridge import (  # noqa: E402
    prepare_environment_invocation,
    sync_environment_outputs,
)
from auto_research.paths import FRAMEWORK_INPUTS_DIR, FRAMEWORK_LOCKS_DIR, FRAMEWORK_RUNTIME_DIR, WEB_RUNTIME_DIR  # noqa: E402
from auto_research.resource_locks import crawl_resource_lease  # noqa: E402


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    entries = [str(ROOT / "framework"), str(ROOT / "framework" / "scripts"), str(ROOT / "web" / "backend"), str(ROOT)]
    for stage in STAGES:
        entries.append(str(ROOT / "modules" / stage))
        entries.append(str(ROOT / "modules" / stage / "scripts"))
    existing = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    seen: set[str] = set()
    merged: list[str] = []
    for item in [*entries, *existing]:
        if item and item not in seen:
            seen.add(item)
            merged.append(item)
    env["WORKSPACE_ROOT"] = str(ROOT)
    env["PYTHONPATH"] = os.pathsep.join(merged)
    env["TASTE_FRAMEWORK_MODULE_CALL"] = "1"
    env["ENVIRONMENT_WORKSPACE_AUDIT_IGNORE_PATHS"] = os.pathsep.join([str(FRAMEWORK_RUNTIME_DIR), str(WEB_RUNTIME_DIR)])
    return env


def _ensure_framework_imports() -> None:
    for entry in reversed([str(ROOT), str(ROOT / "framework"), str(SCRIPTS), str(ROOT / "web" / "backend")]):
        while entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)


def _load_framework_script_module(relative_path: str, module_name: str):
    _ensure_framework_imports()
    module_path = (SCRIPTS / relative_path).resolve(strict=False)
    try:
        module_path.relative_to(SCRIPTS.resolve(strict=False))
    except ValueError as exc:
        raise ModuleNotFoundError(f"Framework script module path escapes scripts root: {relative_path}") from exc
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"Cannot load framework script module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def module_entry(stage: str) -> Path:
    stage = str(stage or "").strip().replace("-", "_")
    if stage not in STAGES:
        raise SystemExit(f"Unknown TASTE module: {stage}. Expected one of: {', '.join(STAGES)}")
    return ROOT / "modules" / stage / "main.py"


def command_for(stage: str, action: str = "", args: Sequence[str] = ()) -> list[str]:
    cmd = [sys.executable, str(module_entry(stage))]
    if action:
        cmd.extend(["--action", action])
    cmd.extend(args)
    return cmd


def _arg_value(args: Sequence[str], name: str) -> str:
    for index, value in enumerate(args):
        if value == name and index + 1 < len(args):
            return str(args[index + 1])
        prefix = name + "="
        if str(value).startswith(prefix):
            return str(value)[len(prefix):]
    return ""


def _arg_values(args: Sequence[str], name: str) -> list[str]:
    values: list[str] = []
    for index, value in enumerate(args):
        if value == name and index + 1 < len(args):
            values.append(str(args[index + 1]))
        elif str(value).startswith(name + "="):
            values.append(str(value)[len(name) + 1 :])
    return [value for value in values if value]


def _has_arg(args: Sequence[str], name: str) -> bool:
    return any(value == name or str(value).startswith(name + "=") for value in args)


def _without_arg(args: Sequence[str], name: str) -> list[str]:
    out: list[str] = []
    skip = False
    for value in args:
        if skip:
            skip = False
            continue
        if value == name:
            skip = True
            continue
        if str(value).startswith(name + "="):
            continue
        out.append(value)
    return out


def _current_find_read_action(stage: str, action: str) -> bool:
    normalized = str(action or "").strip().replace("-", "_")
    return stage == "reading" and normalized in {"current_find_research_plan", "ensure_current_find_research_plan", "current_find_read"}


def _current_find_ideation_action(stage: str, action: str) -> bool:
    normalized = str(action or "").strip().replace("-", "_")
    return stage == "ideation" and normalized in {
        "", "idea", "patch", "update_markdown",
    }


def _current_find_planning_action(stage: str, action: str) -> bool:
    normalized = str(action or "plan").strip().replace("-", "_") or "plan"
    return stage == "planning" and normalized in {
        "plan", "planning", "pipeline", "plan_pipeline",
        "polish", "plan_polish",
        "finish", "finish_plan", "select_plan", "complete_plan",
        "select", "claude_select", "select_best",
        "update_markdown", "save_markdown",
    }


def _environment_bridge_action(stage: str, action: str) -> bool:
    normalized = str(action or "").strip().replace("-", "_")
    return stage == "environment" and normalized in {"", "deploy_from_plan", "chat"}


def _writing_bridge_action(stage: str, action: str) -> bool:
    normalized = str(action or "").strip().replace("-", "_")
    return stage == "writing" and normalized in {"", "work", "chat", "controller_status"}


def _run_streaming(cmd: list[str], *, env: dict[str, str], input_text: str = "") -> tuple[int, str]:
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=env,
        text=True,
        stdin=subprocess.PIPE if input_text else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    if input_text and proc.stdin is not None:
        proc.stdin.write(input_text)
        proc.stdin.close()
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        print(line, end="", flush=True)
    return int(proc.wait()), "".join(lines)


def _run_current_find_read_bridge(action: str, rest: Sequence[str]) -> int:
    args = list(rest)
    project = _arg_value(args, "--project")
    if not project:
        print("reading current_find_research_plan requires --project so framework can prepare the local Reading input.", file=sys.stderr)
        return 2
    if not _has_arg(args, "--input-json"):
        try:
            prepared = prepare_current_find_read_input(project, read_limit=int(_arg_value(args, "--read-limit") or 0))
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 2
        args.extend(["--input-json", str(prepared["input_json"])])
    for framework_arg in [
        "--project",
        "--read-limit",
        "--idea-count",
        "--find-run-id",
        "--venue",
    ]:
        args = _without_arg(args, framework_arg)
    args = [value for value in args if value not in {"--force-selection", "--allow-runtime-cache"}]
    if not _has_arg(args, "--claude-mode"):
        args.extend(["--claude-mode", "auto"])
    if not _has_arg(args, "--read-workers"):
        configured_workers = str(os.environ.get("READING_CURRENT_FIND_READ_WORKERS") or "16").strip()
        args.extend(["--read-workers", configured_workers if configured_workers.isdigit() else "16"])
    cmd = [sys.executable, str(module_entry("reading")), "--action", "read", *args]
    child_env = _python_env()
    child_env["PYTHONUNBUFFERED"] = "1"
    current_cap = str(os.environ.get("READING_CURRENT_FIND_READ_WORKER_CAP") or "").strip()
    if current_cap and "READING_READ_WORKER_CAP" not in child_env:
        child_env["READING_READ_WORKER_CAP"] = current_cap
    rc, stdout_text = _run_streaming(cmd, env=child_env)
    result_payload = extract_last_json_object(stdout_text)
    try:
        sync_result = sync_current_find_read_outputs(project, result_payload=result_payload, stdout_text=stdout_text)
        print(json.dumps({"status": "framework_synced_reading_outputs", "reading_sync": sync_result}, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"framework failed to sync Reading outputs: {exc}", file=sys.stderr)
        return 1 if rc == 0 else rc
    if sync_result.get("public_final_artifact_present") is True:
        return 0
    return rc


@contextmanager
def _project_module_lock(stage: str, project: str) -> Iterator[None]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(project or "")):
        raise ValueError(f"Invalid project name for {stage} lock: {project}")
    lock_root = FRAMEWORK_LOCKS_DIR
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"{stage}_{project}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _run_current_find_ideation_bridge(action: str, rest: Sequence[str]) -> int:
    normalized = str(action or "idea").strip().replace("-", "_") or "idea"
    project = _arg_value(rest, "--project")
    if not project:
        return subprocess.run([sys.executable, str(module_entry("ideation")), "--action", normalized, *rest], cwd=ROOT, env=_python_env(), text=True).returncode
    args = _without_arg(rest, "--project")
    requested_run_id = _arg_value(args, "--run-id")
    prepared_input = ""
    input_text = sys.stdin.read() if normalized == "update_markdown" else ""
    if normalized == "update_markdown" and not input_text.strip():
        print("Ideation Markdown update requires Markdown on stdin.", file=sys.stderr)
        return 2
    try:
        with _project_module_lock("ideation", project):
            if normalized in {"", "idea"}:
                prepared = prepare_current_find_ideation_input(project, requested_run_id=requested_run_id)
                prepared_input = str(prepared["input_json"])
                args = _without_arg(_without_arg(args, "--run-id"), "--input-json")
                args.extend(["--run-id", str(prepared["run_id"]), "--input-json", prepared_input])
                normalized = "idea"
            else:
                run_dir = current_find_ideation_run_dir(project, requested_run_id=requested_run_id)
                args = _without_arg(_without_arg(args, "--run-dir"), "--base-ideas")
                args.extend(["--run-dir", str(run_dir)])
                if normalized == "update_markdown" and not _has_arg(args, "--stdin-markdown"):
                    args.append("--stdin-markdown")
            cmd = [sys.executable, str(module_entry("ideation")), "--action", normalized, *args]
            env = _python_env()
            env["PROJECT_ID"] = project
            env["DEFAULT_PROJECT_ID"] = project
            rc, stdout_text = _run_streaming(cmd, env=env, input_text=input_text)
            if rc != 0:
                return rc
            result_payload = extract_last_json_object(stdout_text)
            sync_result = sync_current_find_ideation_outputs(project, result_payload=result_payload)
            refresh_code, refresh_result = _refresh_current_find_planning_after_idea_change(
                project,
                str(sync_result.get("run_id") or ""),
                changed_idea_id=_arg_value(args, "--idea-id") if normalized == "patch" else "",
            )
            if refresh_code != 0:
                print("framework invalidated the outdated Plan but failed to regenerate it after the Idea change.", file=sys.stderr)
                return refresh_code
            print(json.dumps({
                "status": "framework_synced_ideation_outputs",
                "action": normalized,
                "project": project,
                "run_id": sync_result.get("run_id"),
                "ideation_sync": sync_result,
                "planning_refresh": refresh_result,
            }, ensure_ascii=False, indent=2))
            return 0
    except Exception as exc:
        print(f"framework failed to run Ideation: {exc}", file=sys.stderr)
        return 2
    finally:
        if prepared_input:
            remove_prepared_ideation_input(prepared_input)


def _normalize_planning_action(action: str) -> str:
    normalized = str(action or "plan").strip().replace("-", "_") or "plan"
    if normalized in {"planning", "pipeline", "plan_pipeline"}:
        return "plan"
    if normalized in {"plan_polish"}:
        return "polish"
    if normalized in {"finish_plan", "select_plan", "complete_plan"}:
        return "finish"
    if normalized in {"claude_select", "select_best"}:
        return "select"
    if normalized == "save_markdown":
        return "update_markdown"
    return normalized


def _planning_config_path(env: dict[str, str]) -> str:
    config_payload = env.get("TASTE_APP_CONFIG_JSON", "").strip()
    if not config_payload:
        return ""
    try:
        source = json.loads(config_payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid TASTE_APP_CONFIG_JSON: {exc}") from exc
    if not isinstance(source, dict):
        raise ValueError("TASTE_APP_CONFIG_JSON must be an object.")
    planning_config = {
        "research_interest": str(source.get("research_interest") or ""),
        "researcher_profile": str(source.get("researcher_profile") or ""),
    }
    config_root = FRAMEWORK_INPUTS_DIR / "planning"
    config_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=config_root, prefix="config_", suffix=".json", delete=False) as handle:
        json.dump(planning_config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        return handle.name


def _run_current_find_planning_bridge(action: str, rest: Sequence[str], *, _lock: bool = True) -> int:
    normalized = _normalize_planning_action(action)
    project = _arg_value(rest, "--project")
    if not project:
        print("Project Planning requires --project so Framework can validate inputs and synchronize the explicit module run.", file=sys.stderr)
        return 2
    requested_run_id = _arg_value(rest, "--run-id")
    plan_id = _arg_value(rest, "--plan-id")
    version_id = _arg_value(rest, "--version-id")
    idea_ids = _arg_values(rest, "--idea-id")
    input_text = ""
    if normalized == "update_markdown":
        input_text = sys.stdin.read()
        if not input_text.strip():
            print("Planning Markdown update requires plan.md content on stdin.", file=sys.stderr)
            return 2

    prepared_input = ""
    config_path = ""
    env = _python_env()
    try:
        with (_project_module_lock("planning", project) if _lock else nullcontext()):
            prepared = prepare_current_find_planning_input(
                project,
                action=normalized,
                requested_run_id=requested_run_id,
                requested_idea_ids=idea_ids,
                plan_id=plan_id,
                version_id=version_id,
            )
            prepared_input = str(prepared["input_json"])
            args = list(rest)
            for name in ("--project", "--run-id", "--input-json", "--idea-id", "--backend", "--config-json"):
                args = _without_arg(args, name)
            args.extend([
                "--project", project,
                "--run-id", str(prepared["run_id"]),
                "--input-json", prepared_input,
                "--backend", "claude_code",
            ])
            if normalized == "update_markdown" and not _has_arg(args, "--stdin-markdown"):
                args.append("--stdin-markdown")
            config_path = _planning_config_path(env)
            if config_path:
                args.extend(["--config-json", config_path])
            cmd = [sys.executable, str(module_entry("planning")), "--action", normalized, *args]
            rc, stdout_text = _run_streaming(cmd, env=env, input_text=input_text)
            if rc != 0:
                return rc
            result_payload = extract_last_json_object(stdout_text)
            sync_result = sync_current_find_planning_outputs(project, result_payload=result_payload)
            print(json.dumps({
                "status": "framework_synced_planning_outputs",
                "action": normalized,
                "project": project,
                "run_id": sync_result.get("run_id"),
                "planning_sync": sync_result,
            }, ensure_ascii=False, indent=2))
            return 0
    except Exception as exc:
        print(f"framework failed to run Planning: {exc}", file=sys.stderr)
        return 2
    finally:
        if prepared_input:
            remove_prepared_planning_input(prepared_input)
        if config_path:
            Path(config_path).unlink(missing_ok=True)


def _refresh_current_find_planning_after_idea_change(
    project: str,
    run_id: str,
    *,
    changed_idea_id: str = "",
) -> tuple[int, dict[str, Any]]:
    with _project_module_lock("planning", project):
        result = prepare_planning_refresh_after_idea_change(project, changed_idea_id=changed_idea_id)
        refresh_ids = [str(value) for value in result.get("idea_ids", []) if str(value)]
        if result.get("required") is not True or not refresh_ids:
            return 0, result
        args = ["--project", project, "--run-id", str(result.get("run_id") or run_id), "--repair-rounds", "3"]
        for idea_id in refresh_ids:
            args.extend(["--idea-id", idea_id])
        code = _run_current_find_planning_bridge("plan", args, _lock=False)
        result["return_code"] = code
        return code, result


def _run_environment_bridge(action: str, rest: Sequence[str]) -> int:
    normalized = str(action or "").strip().replace("-", "_") or "deploy_from_plan"
    project = _arg_value(rest, "--project")
    try:
        args = prepare_environment_invocation(project, action=normalized, args=list(rest))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2
    cmd = [sys.executable, str(module_entry("environment")), "--action", normalized, *args]
    rc, stdout_text = _run_streaming(cmd, env=_python_env())
    if not project or normalized not in {"deploy_from_plan", "chat"}:
        return rc
    result_payload = extract_last_json_object(stdout_text)
    try:
        sync_result = sync_environment_outputs(project, action=normalized, result_payload=result_payload, stdout_text=stdout_text)
        print(json.dumps({"status": "framework_synced_environment_outputs", "environment_sync": sync_result}, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"framework failed to sync Environment outputs: {exc}", file=sys.stderr)
        return 1 if rc == 0 else rc
    return rc


def _run_writing_bridge(action: str, rest: Sequence[str]) -> int:
    normalized = str(action or "").strip().replace("-", "_") or "work"
    project = _arg_value(rest, "--project")
    if not project:
        print("Writing requires --project so Framework can route the request to the project's Writing controller.", file=sys.stderr)
        return 2
    cmd = [sys.executable, str(module_entry("writing")), "--action", normalized, *rest]
    rc, _stdout_text = _run_streaming(cmd, env=_python_env())
    return rc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Framework bridge for running exactly one TASTE backend module entrypoint.")
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("--action", default="")
    parser.add_argument("--contract", action="store_true")
    ns, rest = parser.parse_known_args(argv)
    cmd = [sys.executable, str(module_entry(ns.stage))]
    if ns.contract:
        cmd.append("--contract")
    elif ns.action:
        cmd.extend(["--action", ns.action])
    cmd.extend(rest)
    if not ns.contract and ns.stage in {"finding", "reading"}:
        project = _arg_value(rest, "--project")
        with crawl_resource_lease(
            operation=ns.stage,
            project=project,
            on_wait=lambda: print("Waiting for the shared crawl resource.", flush=True),
            on_acquired=lambda: print("Shared crawl resource acquired.", flush=True),
        ):
            return _run_module_dispatch(ns, rest, cmd)
    return _run_module_dispatch(ns, rest, cmd)


def _run_module_dispatch(ns: argparse.Namespace, rest: Sequence[str], cmd: list[str]) -> int:
    if not ns.contract and _current_find_read_action(ns.stage, ns.action):
        return _run_current_find_read_bridge(ns.action, rest)
    if not ns.contract and _current_find_ideation_action(ns.stage, ns.action):
        return _run_current_find_ideation_bridge(ns.action or "idea", rest)
    if not ns.contract and _current_find_planning_action(ns.stage, ns.action):
        return _run_current_find_planning_bridge(ns.action or "plan", rest)
    if not ns.contract and _environment_bridge_action(ns.stage, ns.action):
        return _run_environment_bridge(ns.action, rest)
    if not ns.contract and _writing_bridge_action(ns.stage, ns.action):
        return _run_writing_bridge(ns.action, rest)
    env = _python_env()
    temp_config = ""
    try:
        if ns.stage == "planning" and not _has_arg(rest, "--config-json"):
            temp_config = _planning_config_path(env)
        if temp_config:
            cmd.extend(["--config-json", temp_config])
        proc = subprocess.run(cmd, cwd=ROOT, env=env, text=True)
        return int(proc.returncode)
    finally:
        if temp_config:
            Path(temp_config).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
