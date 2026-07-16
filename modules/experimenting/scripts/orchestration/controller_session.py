#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

MODULE_DIR = Path(__file__).resolve().parents[2]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from scripts.common.entrypoint_guard import ensure_main_entrypoint
from scripts.common.file_utils import atomic_write_json, load_json, now_iso


QueueCallback = Callable[[dict[str, Any]], None]
WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT") or MODULE_DIR.parents[1]).expanduser().resolve()
CONTROLLERS_DIR = MODULE_DIR / ".runtime" / "controllers"
SESSION_INDEX = MODULE_DIR / ".runtime" / "controller_sessions.json"


def _safe_project(project: str) -> tuple[str, Path]:
    name = str(project or "").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("Experimenting controller requires a valid --project name.")
    projects_root = (WORKSPACE_ROOT / "projects").resolve()
    project_root = (projects_root / name).resolve()
    try:
        project_root.relative_to(projects_root)
    except ValueError as exc:
        raise ValueError(f"Experimenting project escapes projects/: {project_root}") from exc
    if project_root.name != name or not project_root.is_dir():
        raise FileNotFoundError(f"Experimenting project does not exist: {name}")
    return name, project_root


def _controller_dir(project: str) -> Path:
    path = CONTROLLERS_DIR / project
    (path / "messages").mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _locked_file(path: Path, *, blocking: bool = True) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), flags)
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _state_lock(controller_dir: Path) -> Iterator[None]:
    with _locked_file(controller_dir / "state.lock"):
        yield


def _valid_session_id(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value or "")))
    except (ValueError, TypeError, AttributeError):
        return ""


def _controller_state(controller_dir: Path, project: str, project_root: Path) -> dict[str, Any]:
    state = load_json(controller_dir / "controller.json", {})
    state = state if isinstance(state, dict) else {}
    session_id = _valid_session_id(state.get("session_id")) or str(uuid.uuid4())
    state.update({
        "schema_version": "experimenting.controller.v1",
        "owner": "modules/experimenting",
        "controller_role": "experimenting_controller",
        "project": project,
        "project_root": str(project_root),
        "session_id": session_id,
    })
    state.setdefault("session_initialized", False)
    state.setdefault("queue", [])
    state.setdefault("busy", False)
    state.setdefault("turn_count", 0)
    return state


def _sync_session_index(project: str, state: dict[str, Any]) -> None:
    with _locked_file(SESSION_INDEX.with_suffix(".lock")):
        index = load_json(SESSION_INDEX, {})
        index = index if isinstance(index, dict) else {}
        sessions = index.get("sessions") if isinstance(index.get("sessions"), dict) else {}
        sessions[project] = {
            "session_id": state.get("session_id", ""),
            "project_root": state.get("project_root", ""),
            "owner": "modules/experimenting",
            "updated_at": now_iso(),
        }
        atomic_write_json(SESSION_INDEX, {
            "schema_version": "experimenting.controller_sessions.v1",
            "policy": "Exactly one Experimenting controller Claude session per project.",
            "sessions": sessions,
        })


def _publish_project_state(project_root: Path, state: dict[str, Any]) -> None:
    pending = [
        {
            "id": str(item.get("message_id") or ""),
            "stage": "experiment",
            "source": "web" if item.get("kind") == "chat" else "framework",
            "message": str(item.get("message") or ""),
            "status": str(item.get("status") or "queued"),
            "created_at": str(item.get("created_at") or ""),
            "interrupt_current": bool(item.get("interrupt_current")),
        }
        for item in state.get("queue", [])
        if isinstance(item, dict) and item.get("status") in {"queued", "running"}
    ]
    atomic_write_json(project_root / "state" / "experimenting_controller.json", {
        "schema_version": "experimenting.controller_public.v1",
        "module": "experimenting",
        "project": project_root.name,
        "session_id": state.get("session_id", ""),
        "busy": bool(state.get("busy")),
        "active_kind": state.get("active_kind", ""),
        "active_started_at": state.get("active_started_at", ""),
        "queued_messages": pending,
        "last_result_path": state.get("last_result_path", ""),
        "updated_at": now_iso(),
    })


def _save_state(controller_dir: Path, project_root: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    atomic_write_json(controller_dir / "controller.json", state)
    _sync_session_index(project_root.name, state)
    _publish_project_state(project_root, state)


def _json_output(stdout: str) -> dict[str, Any]:
    text = str(stdout or "").strip()
    for candidate in [text, *[line.strip() for line in reversed(text.splitlines()) if line.strip().startswith("{")]]:
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _repo_dirs(project_root: Path) -> list[Path]:
    keys = {"repo_path", "selected_repo_path", "local_path", "repository_path"}
    candidates: list[Path] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in keys and isinstance(child, str) and child.strip():
                    path = Path(child).expanduser()
                    path = (project_root / path).resolve() if not path.is_absolute() else path.resolve()
                    if path.is_dir():
                        candidates.append(path)
                elif isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for relative in [
        "state/environment_handoff.json",
        "state/environment_deployment_decision.json",
        "state/evidence_ready_repo_selection.json",
        "state/active_repo.json",
    ]:
        visit(load_json(project_root / relative, {}))
    seen: set[str] = set()
    result: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _iso_time(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _registry_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("experiments"), list):
        return [row for row in payload["experiments"] if isinstance(row, dict)]
    return []


def _validation_record_gate(project_root: Path, item: dict[str, Any]) -> dict[str, Any]:
    registry_path = project_root / "state" / "experiment_registry.json"
    rows = _registry_rows(load_json(registry_path, []))
    previous_mtime = int(item.get("registry_mtime_ns") or 0)
    current_mtime = registry_path.stat().st_mtime_ns if registry_path.exists() else 0
    transaction_started_at = _iso_time(item.get("validation_window_started_at") or item.get("created_at"))
    registry_written_at = dt.datetime.fromtimestamp(current_mtime / 1_000_000_000, tz=dt.timezone.utc) if current_mtime else None
    candidates = []
    for row in rows:
        row_time = _iso_time(row.get("recorded_at")) or _iso_time(row.get("timestamp"))
        if transaction_started_at is not None and row_time is not None and row_time >= transaction_started_at:
            candidates.append(row)
    if not candidates and current_mtime > previous_mtime and rows:
        candidates = [rows[-1]]

    blockers: list[str] = []
    checked: list[str] = []
    for index, row in enumerate(candidates):
        row_id = str(row.get("experiment_id") or row.get("run_id") or f"row-{index + 1}")
        checked.append(row_id)
        validation_finished = _iso_time(row.get("validation_finished_at"))
        recorded_at = _iso_time(row.get("recorded_at"))
        if validation_finished is None:
            blockers.append(f"{row_id}: missing validation_finished_at")
        if recorded_at is None:
            blockers.append(f"{row_id}: missing recorded_at")
        if validation_finished and recorded_at and recorded_at < validation_finished:
            blockers.append(f"{row_id}: recorded_at precedes validation_finished_at")
        if registry_written_at and validation_finished and validation_finished > registry_written_at:
            blockers.append(f"{row_id}: validation_finished_at is later than the registry write")
        if registry_written_at and recorded_at and recorded_at > registry_written_at:
            blockers.append(f"{row_id}: recorded_at is later than the registry write")
        try:
            validation_code = int(row.get("validation_return_code"))
        except (TypeError, ValueError):
            validation_code = None
            blockers.append(f"{row_id}: missing validation_return_code")
        if str(row.get("status") or "").strip().lower() in {"completed", "success", "pass", "passed"}:
            if validation_code != 0:
                blockers.append(f"{row_id}: completed evidence requires validation_return_code=0")
    if not candidates:
        blockers.append("no experiment registry row was recorded by this work request")
    gate = {
        "schema_version": "experimenting.validation_record_order.v1",
        "status": "pass" if not blockers else "blocked",
        "project": project_root.name,
        "message_id": item.get("message_id", ""),
        "registry_path": str(registry_path),
        "checked_rows": checked,
        "blockers": blockers,
        "checked_at": now_iso(),
    }
    atomic_write_json(project_root / "state" / "experiment_validation_record_order_gate.json", gate)
    return gate


def _pid_alive(value: Any) -> bool:
    try:
        pid = int(value or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").split()[2].upper() == "Z":
            return False
    except (FileNotFoundError, IndexError, OSError):
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _recover_orphaned_active(controller_dir: Path, project_root: Path) -> bool:
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project_root.name, project_root)
        active_pid = state.get("active_pid")
        if _pid_alive(active_pid):
            return False
        active_id = str(state.get("active_id") or "")
        if state.get("busy") or active_pid or active_id:
            for row in state["queue"]:
                if isinstance(row, dict) and row.get("message_id") == active_id and row.get("status") == "running":
                    row.update({
                        "status": "queued",
                        "interrupt_current": False,
                        "resume_after_web": True,
                        "created_at": now_iso(),
                    })
            state.update({
                "busy": False,
                "active_pid": 0,
                "active_kind": "",
                "active_id": "",
                "active_started_at": "",
                "session_initialized": bool(state.get("session_initialized") or active_pid),
            })
            _save_state(controller_dir, project_root, state)
        return True


def _interrupt_active(controller_dir: Path, project_root: Path, message_id: str) -> bool:
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project_root.name, project_root)
        try:
            pid = int(state.get("active_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0:
            state["interrupt_requested_by"] = message_id
            _save_state(controller_dir, project_root, state)
    if pid <= 0:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except Exception:
            return False


def _system_prompt(project: str) -> str:
    iteration_skill = MODULE_DIR / "skills" / "experiment-iteration" / "SKILL.md"
    runtime_skill = MODULE_DIR / "skills" / "experiment-runtime-tools" / "SKILL.md"
    recording_prompt = MODULE_DIR / "prompts" / "experiment-recording.md"
    return (
        f"You are the only Experimenting controller Claude Code session for project {project}. "
        "Your working directory must remain this project directory. Complete only Experimenting duties for this project. "
        f"You must apply {iteration_skill}, {runtime_skill}, and {recording_prompt} to Experimenting work. "
        "Use the current selected Idea/Plan and Environment handoff. Read current Find and Read evidence when it improves the experiment design. "
        "Immediately before each plan, implementation, launch, validation, and record decision, reread the current selected IDs, "
        "human_supervision_updated_at, and human-edited idea/plan files. Evidence refreshes must use Framework public stage entrypoints. "
        "Treat every Web instruction as highest priority, then resume unfinished Experimenting work in this same session."
    )


def _message_prompt(item: dict[str, Any], project_root: Path) -> str:
    module_main = MODULE_DIR / "main.py"
    framework_entrypoint = WORKSPACE_ROOT / "framework" / "scripts" / "run_module.py"
    common = f"""
Project directory: {project_root}
Experimenting public entrypoint: {module_main}
Framework stage entrypoint: {framework_entrypoint}

Mandatory experiment transaction order:
1. Read the current selected research contract and Environment handoff from this project.
2. Form or update the execution-level experiment plan inside the selected route.
3. Modify the selected repository and run the real experiment with the locked project environment.
4. Wait for the experiment and final validation commands to finish.
5. Parse metrics, failures, bad cases, and counterexamples only from those completed outputs.
6. Write the artifact record, project experiment registry, CSV, and Markdown experiment table from the validated outputs. Every new registry row must include validation_finished_at, validation_return_code, and recorded_at.
7. Run `conda run -n taste python {module_main} --action audit_iteration --project {project_root.name}` and repair every failed gate before promoting a claim. Run `runtime_integrity` or `reference_reproduction` with the same project when that evidence changed.

Only completed validation output may be recorded as completed experiment evidence.
When a specific literature gap blocks the selected experiment, use `conda run -n taste python {framework_entrypoint} finding --action literature_tool --project {project_root.name} --query "<exact evidence gap>" --publish-current-find`.
When current-Find reading is stale or incomplete, use `conda run -n taste python {framework_entrypoint} reading --action current_find_research_plan --project {project_root.name}`.
When the current idea must be regenerated, use `conda run -n taste python {framework_entrypoint} ideation --action idea --project {project_root.name}`, then wait for one selected Plan and matching Environment handoff before launching experiments.
After any refresh, re-read the resulting project files before continuing.
Task subagents must omit worktree isolation unless their cwd is an independent Git repository whose top-level remains inside {project_root}.
""".strip()
    message = str(item.get("message") or "").strip()
    gate_feedback = [str(value) for value in item.get("gate_feedback", []) if str(value).strip()]
    if gate_feedback:
        task = (
            "Repair only the failed validation-and-record transaction below, then complete the original Experimenting duty. "
            "Run any missing validation before rewriting records. Hard-gate blockers:\n- " + "\n- ".join(gate_feedback)
        )
    elif item.get("resume_after_web"):
        task = "Resume and complete the interrupted Experimenting duty below."
    elif item.get("kind") == "work":
        task = "Complete the Experimenting module duty below."
    else:
        task = (
            "Execute this Web instruction first. After it is complete, inspect this session and project records for unfinished "
            "Experimenting work and resume that work."
        )
    return f"{task}\n\n{common}\n\nInstruction:\n{message}\n"


def _invoke_controller(
    *,
    controller_dir: Path,
    project_root: Path,
    item: dict[str, Any],
    timeout_sec: int,
    permission_mode: str,
    dry_run: bool,
) -> dict[str, Any]:
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project_root.name, project_root)
        session_id = str(state["session_id"])
        initialized = bool(state.get("session_initialized"))

    prompt = _message_prompt(item, project_root)
    message_id = str(item.get("message_id") or "")
    repair_attempt = int(item.get("gate_repair_attempt") or 0)
    suffix = f".repair{repair_attempt}" if repair_attempt else ""
    prompt_path = controller_dir / "messages" / f"{message_id}{suffix}.prompt.md"
    log_path = controller_dir / "messages" / f"{message_id}{suffix}.log"
    prompt_path.write_text(prompt, encoding="utf-8")
    if dry_run:
        return {
            "return_code": 0,
            "status": "dry_run",
            "session_id": session_id,
            "response": "dry-run: Experimenting controller was not called.",
            "started_at": now_iso(),
            "finished_at": now_iso(),
            "interrupted_by": "",
            "claude_json": {},
        }

    claude = shutil.which("claude", path=os.environ.get("PATH", ""))
    if not claude:
        return {
            "return_code": 127,
            "status": "claude_unavailable",
            "session_id": session_id,
            "response": "",
            "started_at": now_iso(),
            "finished_at": now_iso(),
            "interrupted_by": "",
            "claude_json": {},
        }
    cmd = [
        claude,
        "-p",
        "--permission-mode",
        permission_mode,
        "--output-format",
        "json",
        "--system-prompt",
        _system_prompt(project_root.name),
        "--add-dir",
        str(MODULE_DIR),
    ]
    for repo_dir in _repo_dirs(project_root):
        cmd.extend(["--add-dir", str(repo_dir)])
    cmd.extend(["--resume" if initialized else "--session-id", session_id])

    started = now_iso()
    proc: subprocess.Popen[str] | None = None
    stdout = ""
    stderr = ""
    timed_out = False
    controller_env = os.environ.copy()
    # Hide the enclosing TASTE repository while preserving project-local repositories.
    controller_env["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(
        filter(None, (str(WORKSPACE_ROOT), controller_env.get("GIT_CEILING_DIRECTORIES", "")))
    )
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_root),
            env=controller_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        with _state_lock(controller_dir):
            state = _controller_state(controller_dir, project_root.name, project_root)
            state.update({
                "busy": True,
                "session_initialized": True,
                "active_pid": proc.pid,
                "active_kind": item.get("kind", ""),
                "active_id": message_id,
                "active_started_at": started,
            })
            _save_state(controller_dir, project_root, state)
        stdout, stderr = proc.communicate(prompt, timeout=timeout_sec if timeout_sec > 0 else None)
        return_code = int(proc.returncode or 0)
    except subprocess.TimeoutExpired:
        timed_out = True
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
            stdout, stderr = proc.communicate(timeout=10)
        return_code = 124
    except Exception as exc:
        stderr = f"{type(exc).__name__}: {exc}"
        return_code = 125

    parsed = _json_output(stdout)
    reported_session_id = _valid_session_id(parsed.get("session_id"))
    if reported_session_id and reported_session_id != session_id:
        return_code = 125
        stderr = (stderr + "\n" if stderr else "") + "Claude returned another project's Experimenting session ID."
    response = str(parsed.get("result") or parsed.get("response") or stdout or "").strip()
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project_root.name, project_root)
        interrupted_by = str(state.get("interrupt_requested_by") or "")
        state.update({
            "busy": False,
            "active_pid": 0,
            "active_kind": "",
            "active_id": "",
            "active_started_at": "",
            "session_initialized": bool(initialized or return_code == 0 or reported_session_id or interrupted_by),
            "last_finished_at": now_iso(),
        })
        if interrupted_by:
            state["interrupt_requested_by"] = ""
        _save_state(controller_dir, project_root, state)
    log_path.write_text(
        f"# cwd={project_root}\n# started_at={started}\n# finished_at={now_iso()}\n\n"
        f"--- STDOUT ---\n{stdout}\n\n--- STDERR ---\n{stderr}\n",
        encoding="utf-8",
    )
    return {
        "return_code": return_code,
        "status": "interrupted" if interrupted_by else "timeout" if timed_out else "completed" if return_code == 0 else "failed",
        "session_id": session_id,
        "response": response,
        "stdout_tail": stdout[-8000:],
        "stderr_tail": stderr[-4000:],
        "started_at": started,
        "finished_at": now_iso(),
        "interrupted_by": interrupted_by,
        "claude_json": parsed,
    }


def _next_message(controller_dir: Path, project_root: Path) -> dict[str, Any]:
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project_root.name, project_root)
        queued = [item for item in state["queue"] if isinstance(item, dict) and item.get("status") == "queued"]
        if not queued:
            return {}
        queued.sort(key=lambda item: (
            0 if item.get("interrupt_current") else 1,
            0 if item.get("kind") == "chat" else 1,
            str(item.get("created_at") or ""),
        ))
        selected = queued[0]
        selected.setdefault("validation_window_started_at", now_iso())
        registry_path = project_root / "state" / "experiment_registry.json"
        selected.setdefault("registry_mtime_ns", registry_path.stat().st_mtime_ns if registry_path.exists() else 0)
        selected["status"] = "running"
        selected["started_at"] = now_iso()
        _save_state(controller_dir, project_root, state)
        return dict(selected)


def _requeue_interrupted(controller_dir: Path, project_root: Path, item: dict[str, Any]) -> None:
    message_id = str(item.get("message_id") or "")
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project_root.name, project_root)
        for row in state["queue"]:
            if isinstance(row, dict) and row.get("message_id") == message_id:
                row.update({
                    "status": "queued",
                    "interrupt_current": False,
                    "resume_after_web": True,
                    "created_at": now_iso(),
                })
        _save_state(controller_dir, project_root, state)


def _requeue_gate_repair(
    controller_dir: Path,
    project_root: Path,
    item: dict[str, Any],
    blockers: list[Any],
) -> None:
    message_id = str(item.get("message_id") or "")
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project_root.name, project_root)
        for row in state["queue"]:
            if isinstance(row, dict) and row.get("message_id") == message_id:
                row.update({
                    "status": "queued",
                    "interrupt_current": False,
                    "gate_feedback": [str(value) for value in blockers if str(value).strip()],
                    "gate_repair_attempt": int(row.get("gate_repair_attempt") or 0) + 1,
                    "created_at": now_iso(),
                })
        _save_state(controller_dir, project_root, state)


def _save_result(controller_dir: Path, project_root: Path, item: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    message_id = str(item.get("message_id") or "")
    payload = {
        "schema_version": "experimenting.controller_result.v1",
        "project": project_root.name,
        "stage": "experiment",
        "message_id": message_id,
        "kind": item.get("kind", "chat"),
        "instruction": str(item.get("message") or ""),
        "status": "completed" if result.get("return_code") == 0 else str(result.get("status") or "failed"),
        "return_code": int(result.get("return_code") or 0),
        "session_id": str(result.get("session_id") or ""),
        "response_markdown": str(result.get("response") or ""),
        "web_visible_response": True,
        "queued": bool(item.get("was_queued")),
        "interrupt_current": bool(item.get("interrupt_current")),
        "interrupted_current": bool(item.get("interrupted_current")),
        "validation_record_gate": result.get("validation_record_gate", {}),
        "started_at": str(result.get("started_at") or item.get("started_at") or ""),
        "finished_at": str(result.get("finished_at") or now_iso()),
    }
    result_path = controller_dir / "messages" / f"{message_id}.json"
    atomic_write_json(result_path, payload)
    project_result = project_root / "state" / "experimenting_controller_last_result.json"
    atomic_write_json(project_result, payload)
    report = project_root / "reports" / "experimenting_controller.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("a", encoding="utf-8") as handle:
        handle.write(
            f"\n## {payload['finished_at']} | {payload['status']}\n\n"
            f"Instruction:\n\n{payload['instruction']}\n\nResponse:\n\n{payload['response_markdown']}\n"
        )
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project_root.name, project_root)
        for row in state["queue"]:
            if isinstance(row, dict) and row.get("message_id") == message_id:
                row.update({"status": payload["status"], "finished_at": payload["finished_at"]})
        state["queue"] = [row for row in state["queue"] if isinstance(row, dict)][-100:]
        state["turn_count"] = int(state.get("turn_count") or 0) + 1
        state["last_result_path"] = str(project_result)
        _save_state(controller_dir, project_root, state)
    return payload


def _drain_queue(
    *,
    controller_dir: Path,
    project_root: Path,
    timeout_sec: int,
    permission_mode: str,
    dry_run: bool,
) -> None:
    while True:
        item = _next_message(controller_dir, project_root)
        if not item:
            return
        result = _invoke_controller(
            controller_dir=controller_dir,
            project_root=project_root,
            item=item,
            timeout_sec=timeout_sec,
            permission_mode=permission_mode,
            dry_run=dry_run,
        )
        if result.get("interrupted_by"):
            _requeue_interrupted(controller_dir, project_root, item)
            continue
        registry_path = project_root / "state" / "experiment_registry.json"
        registry_changed = registry_path.exists() and registry_path.stat().st_mtime_ns > int(item.get("registry_mtime_ns") or 0)
        if not dry_run and (item.get("kind") == "work" or registry_changed):
            gate = _validation_record_gate(project_root, item)
            result["validation_record_gate"] = gate
            if gate.get("status") != "pass" and result.get("return_code") == 0:
                if int(item.get("gate_repair_attempt") or 0) < 1:
                    _requeue_gate_repair(controller_dir, project_root, item, gate.get("blockers", []))
                    continue
                result["return_code"] = 2
                result["status"] = "blocked_validation_record_order"
                result["response"] = (
                    str(result.get("response") or "").rstrip()
                    + "\n\n"
                    + "; ".join(str(value) for value in gate.get("blockers", []))
                ).strip()
        _save_result(controller_dir, project_root, item, result)


def run_controller_message(
    *,
    project: str,
    kind: str,
    message: str,
    timeout_sec: int,
    permission_mode: str,
    interrupt_current: bool,
    dry_run: bool,
    on_queued: QueueCallback | None = None,
) -> dict[str, Any]:
    project, project_root = _safe_project(project)
    controller_dir = _controller_dir(project)
    message_id = uuid.uuid4().hex
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project, project_root)
        pending = [item for item in state["queue"] if isinstance(item, dict) and item.get("status") in {"queued", "running"}]
        busy = bool(state.get("busy") or state.get("active_pid") or pending)
        item = {
            "message_id": message_id,
            "project": project,
            "kind": kind,
            "message": message.strip(),
            "status": "queued",
            "created_at": now_iso(),
            "was_queued": busy,
            "interrupt_current": bool(interrupt_current),
            "interrupted_current": False,
        }
        state["queue"].append(item)
        _save_state(controller_dir, project_root, state)
        position = len([row for row in state["queue"] if isinstance(row, dict) and row.get("status") == "queued"])
    interrupted = _interrupt_active(controller_dir, project_root, message_id) if interrupt_current else False
    if interrupted:
        with _state_lock(controller_dir):
            state = _controller_state(controller_dir, project, project_root)
            for row in state["queue"]:
                if isinstance(row, dict) and row.get("message_id") == message_id:
                    row["interrupted_current"] = True
            _save_state(controller_dir, project_root, state)
    if busy and on_queued:
        on_queued({
            "event": "experimenting_controller_queued",
            "status": "queued",
            "project": project,
            "message_id": message_id,
            "message": message.strip(),
            "queue_position": position,
            "interrupt_requested": bool(interrupt_current),
            "interrupted_current": interrupted,
        })

    result_path = controller_dir / "messages" / f"{message_id}.json"
    deadline = time.monotonic() + timeout_sec if timeout_sec > 0 else None
    while True:
        result = load_json(result_path, {})
        if isinstance(result, dict) and result:
            return result
        try:
            with _locked_file(controller_dir / "execution.lock", blocking=False):
                if _recover_orphaned_active(controller_dir, project_root):
                    _drain_queue(
                        controller_dir=controller_dir,
                        project_root=project_root,
                        timeout_sec=timeout_sec,
                        permission_mode=permission_mode,
                        dry_run=dry_run,
                    )
        except BlockingIOError:
            pass
        result = load_json(result_path, {})
        if isinstance(result, dict) and result:
            return result
        if deadline is not None and time.monotonic() >= deadline:
            return {
                "schema_version": "experimenting.controller_result.v1",
                "project": project,
                "stage": "experiment",
                "message_id": message_id,
                "kind": kind,
                "instruction": message.strip(),
                "status": "queued_timeout",
                "return_code": 124,
                "response_markdown": "",
                "queued": True,
                "interrupt_current": bool(interrupt_current),
                "interrupted_current": interrupted,
                "finished_at": now_iso(),
            }
        time.sleep(0.25)


def _read_message(message: str, message_file: str) -> str:
    if str(message or "").strip():
        return str(message).strip()
    if message_file:
        return Path(message_file).expanduser().read_text(encoding="utf-8", errors="replace").strip()
    return ""


def main() -> int:
    ensure_main_entrypoint()
    parser = argparse.ArgumentParser(description="Manage the project-unique Experimenting controller Claude session.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--kind", choices=["work", "chat", "status"], default="work")
    parser.add_argument("--message", default="")
    parser.add_argument("--message-file", default="")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--timeout-sec", type=int, default=14400)
    parser.add_argument("--permission-mode", default="bypassPermissions")
    parser.add_argument("--interrupt-current", action="store_true")
    parser.add_argument("--queue-if-busy", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    project, project_root = _safe_project(args.project)
    output_root = _controller_dir(project)
    if args.kind == "status":
        controller_dir = _controller_dir(project)
        with _state_lock(controller_dir):
            payload = _controller_state(controller_dir, project, project_root)
            _save_state(controller_dir, project_root, payload)
        atomic_write_json(output_root / "controller_status.json", payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    message = _read_message(args.message, args.message_file)
    if args.kind == "work" and not message:
        rounds = max(1, min(50, int(args.iterations or 1)))
        message = (
            f"Continue the Experimenting module work for this project for up to {rounds} bounded experiment iterations. "
            "Use the current selected contract and Environment handoff, improve the experiment design from current Find/Read evidence when needed, "
            "run real experiments, validate final outputs, then update records and audits in the mandatory order."
        )
    if not message:
        raise SystemExit("Experimenting controller chat requires --message or --message-file.")
    atomic_write_json(output_root / "controller_request.json", {
        "project": project,
        "project_root": str(project_root),
        "kind": args.kind,
        "message": message,
        "interrupt_current": bool(args.interrupt_current),
        "created_at": now_iso(),
    })

    def announce_queued(event: dict[str, Any]) -> None:
        print(json.dumps(event, ensure_ascii=False), flush=True)

    result = run_controller_message(
        project=project,
        kind=args.kind,
        message=message,
        timeout_sec=args.timeout_sec,
        permission_mode=args.permission_mode,
        interrupt_current=bool(args.interrupt_current),
        dry_run=bool(args.dry_run),
        on_queued=announce_queued,
    )
    atomic_write_json(output_root / "controller_result.json", result)
    print(json.dumps({**result, "controller_dir": str(output_root)}, ensure_ascii=False, indent=2))
    return int(result.get("return_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
