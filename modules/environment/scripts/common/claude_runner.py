from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from scripts.common.io_utils import ensure_within, read_json, utc_now, write_json, write_text
from scripts.common.shell import runtime_env


QueueCallback = Callable[[dict[str, Any]], None]


def find_claude() -> str:
    env = runtime_env()
    return shutil.which("claude", path=env.get("PATH", "")) or "claude"


def _environment_root() -> Path:
    configured = str(os.environ.get("ENVIRONMENT_ROOT") or "").strip()
    return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[2]


def _workspace_root() -> Path:
    configured = str(os.environ.get("WORKSPACE_ROOT") or "").strip()
    return Path(configured).expanduser().resolve() if configured else _environment_root().parents[1]


def _safe_project(project: str, project_root: Path | None = None) -> tuple[str, Path]:
    name = str(project or "").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("Environment controller requires a valid project name.")
    projects_root = (_workspace_root() / "projects").resolve()
    root = (project_root or projects_root / name).expanduser().resolve()
    try:
        root.relative_to(projects_root)
    except ValueError as exc:
        raise ValueError(f"Environment controller project escapes projects/: {root}") from exc
    if root.name != name or not root.is_dir():
        raise FileNotFoundError(f"Environment controller project does not exist: {name}")
    return name, root


def _controller_dir(project: str) -> Path:
    root = _environment_root() / ".runtime" / "controllers" / project
    (root / "messages").mkdir(parents=True, exist_ok=True)
    return root


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    write_json(temp, payload)
    temp.replace(path)


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


def _controller_state(controller_dir: Path, project: str, project_root: Path) -> dict[str, Any]:
    path = controller_dir / "controller.json"
    state = read_json(path, {})
    if not isinstance(state, dict):
        state = {}
    state.update({
        "schema_version": "environment.controller.v1",
        "project": project,
        "project_root": str(project_root),
        "controller_dir": str(controller_dir),
    })
    existing_session_id = str(state.get("session_id") or "").strip()
    if existing_session_id:
        state["session_id"] = existing_session_id
        state.setdefault("session_initialized", True)
    else:
        state["session_id"] = str(uuid4())
        state["session_initialized"] = False
    state.setdefault("queue", [])
    state.setdefault("busy", False)
    return state


def _save_controller_state(controller_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    _atomic_write_json(controller_dir / "controller.json", state)


def _json_output(stdout: str) -> dict[str, Any]:
    text = str(stdout or "").strip()
    candidates = [text, *[line.strip() for line in reversed(text.splitlines()) if line.strip().startswith("{")]]
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _safe_add_dir(path: Path, project_root: Path, run_dir: Path | None) -> Path:
    resolved = path.expanduser().resolve()
    allowed = [project_root, _environment_root() / "skills"]
    if run_dir is not None:
        allowed.append(run_dir)
    for root in allowed:
        try:
            return ensure_within(resolved, root)
        except ValueError:
            continue
    raise ValueError(f"Environment controller add-dir escapes allowed roots: {resolved}")


def _finish_log(
    log_path: Path,
    cmd: list[str],
    prompt_path: Path,
    started: str,
    stdout: str,
    stderr: str,
    note: str = "",
) -> None:
    suffix = f"\n--- NOTE ---\n{note}\n" if note else ""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        f"$ {' '.join(cmd)} < {prompt_path}\n# started_at={started}\n# finished_at={utc_now()}\n\n"
        f"--- STDOUT ---\n{stdout}\n\n--- STDERR ---\n{stderr}\n{suffix}",
        encoding="utf-8",
    )


def _interrupt_active(controller_dir: Path, message_id: str) -> bool:
    pid = 0
    with _state_lock(controller_dir):
        state = read_json(controller_dir / "controller.json", {})
        if not isinstance(state, dict):
            return False
        try:
            pid = int(state.get("active_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0:
            state["interrupt_requested_by"] = message_id
            _save_controller_state(controller_dir, state)
    interrupted = _terminate_controller_pid(pid)
    if not interrupted:
        with _state_lock(controller_dir):
            state = read_json(controller_dir / "controller.json", {})
            if isinstance(state, dict) and str(state.get("interrupt_requested_by") or "") == message_id:
                state["interrupt_requested_by"] = ""
                _save_controller_state(controller_dir, state)
    return interrupted


def _terminate_controller_pid(pid: int) -> bool:
    if pid <= 0:
        return False
    used_group = True
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except Exception:
        used_group = False
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        except Exception:
            return False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            if used_group:
                os.killpg(pid, 0)
            else:
                os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except Exception:
            return True
        time.sleep(0.05)
    try:
        if used_group:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        return False
    return True


def interrupt_project_controller(project: str, project_root: Path) -> bool:
    project, project_root = _safe_project(project, project_root)
    controller_dir = _controller_dir(project)
    pid = 0
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project, project_root)
        try:
            pid = int(state.get("active_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        state.update({
            "busy": False,
            "active_pid": 0,
            "active_kind": "",
            "active_id": "",
            "active_started_at": "",
            "cancelled_at": utc_now(),
        })
        _save_controller_state(controller_dir, state)
    return _terminate_controller_pid(pid)


def _cancel_controller_message(controller_dir: Path, project: str, project_root: Path, message_id: str) -> None:
    pid = 0
    payload: dict[str, Any] = {}
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project, project_root)
        queue = [row for row in state.get("queue", []) if isinstance(row, dict)]
        item = next((row for row in queue if str(row.get("message_id") or "") == message_id), {})
        if item:
            item["status"] = "cancelled"
            item["finished_at"] = utc_now()
        if str(state.get("active_id") or "") == message_id:
            try:
                pid = int(state.get("active_pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            state.update({
                "busy": False,
                "active_pid": 0,
                "active_kind": "",
                "active_id": "",
                "active_started_at": "",
            })
        state["queue"] = queue[-100:]
        payload = {
            "schema_version": "environment.controller_result.v1",
            "project": project,
            "stage": "environment",
            "message_id": message_id,
            "instruction": str(item.get("message") or ""),
            "status": "cancelled",
            "return_code": 130,
            "session_id": str(state.get("session_id") or ""),
            "response_markdown": "",
            "web_visible_response": True,
            "started_at": str(item.get("started_at") or ""),
            "finished_at": str(item.get("finished_at") or utc_now()),
            "queued": bool(item.get("was_queued")),
            "interrupt_current": bool(item.get("interrupt_current")),
            "interrupted_current": bool(item.get("interrupted_current")),
        }
        _save_controller_state(controller_dir, state)
    _atomic_write_json(controller_dir / "messages" / f"{message_id}.json", payload)
    _terminate_controller_pid(pid)


def _invoke_controller(
    *,
    project: str,
    project_root: Path,
    controller_dir: Path,
    prompt: str,
    prompt_path: Path,
    log_path: Path,
    timeout_sec: int,
    add_dirs: list[Path],
    system_prompt: str,
    env: dict[str, str] | None,
    work_kind: str,
    work_id: str = "",
) -> dict[str, Any]:
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project, project_root)
        session_id = str(state.get("session_id") or "")
        session_initialized = bool(state.get("session_initialized"))
        _save_controller_state(controller_dir, state)

    cmd = [find_claude(), "-p", "--permission-mode", "bypassPermissions", "--output-format", "json"]
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])
    cmd.extend(["--add-dir", str(project_root)])
    seen = {str(project_root)}
    for path in add_dirs:
        resolved = str(path)
        if resolved not in seen:
            seen.add(resolved)
            cmd.extend(["--add-dir", resolved])
    if session_initialized:
        cmd.extend(["--resume", session_id])
    else:
        cmd.extend(["--session-id", session_id])

    started = utc_now()
    proc: subprocess.Popen[str] | None = None
    stdout = ""
    stderr = ""
    timed_out = False
    controller_env = dict(env or runtime_env())
    # Hide the enclosing TASTE repository while preserving project-local repositories.
    controller_env["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(
        filter(None, (str(_workspace_root()), controller_env.get("GIT_CEILING_DIRECTORIES", "")))
    )
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=controller_env,
            start_new_session=True,
        )
        with _state_lock(controller_dir):
            state = _controller_state(controller_dir, project, project_root)
            state.update({
                "busy": True,
                "session_initialized": True,
                "active_pid": proc.pid,
                "active_kind": work_kind,
                "active_id": work_id,
                "active_started_at": started,
            })
            _save_controller_state(controller_dir, state)
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
    reported_session_id = str(parsed.get("session_id") or "").strip()
    session_mismatch = bool(reported_session_id and reported_session_id != session_id)
    returned_session_id = session_id
    if session_mismatch:
        return_code = 125
        stderr = (stderr + "\n" if stderr else "") + "Claude returned a session_id that does not match this project's Environment controller."
    response = str(parsed.get("result") or parsed.get("response") or stdout or "").strip()
    interrupted_by = ""
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project, project_root)
        interrupted_by = str(state.get("interrupt_requested_by") or "")
        state.update({
            "busy": False,
            "active_pid": 0,
            "active_kind": "",
            "active_id": "",
            "active_started_at": "",
            "last_finished_at": utc_now(),
        })
        if interrupted_by:
            state["interrupt_requested_by"] = ""
        _save_controller_state(controller_dir, state)

    _finish_log(
        log_path,
        cmd,
        prompt_path,
        started,
        stdout or "",
        stderr or "",
        "interrupted by queued Web instruction" if interrupted_by else "timeout" if timed_out else "",
    )
    return {
        "return_code": return_code,
        "status": "interrupted" if interrupted_by else "timeout" if timed_out else "passed" if return_code == 0 else "failed",
        "session_id": returned_session_id,
        "response": response,
        "stdout_tail": (stdout or "")[-8000:],
        "stderr_tail": (stderr or "")[-4000:],
        "started_at": started,
        "finished_at": utc_now(),
        "interrupted_by": interrupted_by,
        "claude_json": parsed,
    }


def _next_queued_message(controller_dir: Path, project: str, project_root: Path) -> dict[str, Any]:
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project, project_root)
        queue = [item for item in state.get("queue", []) if isinstance(item, dict)]
        queued = [item for item in queue if item.get("status") == "queued"]
        if not queued:
            return {}
        queued.sort(key=lambda item: (0 if item.get("interrupt_current") else 1, str(item.get("created_at") or "")))
        selected = queued[0]
        selected["status"] = "running"
        selected["started_at"] = utc_now()
        state["queue"] = queue
        _save_controller_state(controller_dir, state)
        return dict(selected)


def _save_message_result(
    controller_dir: Path,
    project_root: Path,
    item: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    message_id = str(item.get("message_id") or "")
    payload = {
        "schema_version": "environment.controller_result.v1",
        "project": str(item.get("project") or project_root.name),
        "stage": "environment",
        "message_id": message_id,
        "instruction": str(item.get("message") or ""),
        "status": "completed" if result.get("return_code") == 0 else str(result.get("status") or "failed"),
        "return_code": int(result.get("return_code") or 0),
        "session_id": str(result.get("session_id") or ""),
        "response_markdown": str(result.get("response") or ""),
        "web_visible_response": True,
        "started_at": str(result.get("started_at") or item.get("started_at") or ""),
        "finished_at": str(result.get("finished_at") or utc_now()),
        "queued": bool(item.get("was_queued")),
        "interrupt_current": bool(item.get("interrupt_current")),
        "interrupted_current": bool(item.get("interrupted_current")),
    }
    _atomic_write_json(controller_dir / "messages" / f"{message_id}.json", payload)
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project_root.name, project_root)
        queue = [row for row in state.get("queue", []) if isinstance(row, dict)]
        for row in queue:
            if str(row.get("message_id") or "") == message_id:
                row["status"] = payload["status"]
                row["finished_at"] = payload["finished_at"]
        state["queue"] = queue[-100:]
        state["last_result_path"] = str(controller_dir / "messages" / f"{message_id}.json")
        _save_controller_state(controller_dir, state)
    return payload


def _controller_conversation(controller_dir: Path, project: str, project_root: Path) -> list[dict[str, Any]]:
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project, project_root)
        queue = [dict(row) for row in state.get("queue", []) if isinstance(row, dict)]
    turns: list[dict[str, Any]] = []
    for item in queue:
        message_id = str(item.get("message_id") or "")
        result = read_json(controller_dir / "messages" / f"{message_id}.json", {}) if message_id else {}
        if isinstance(result, dict) and result:
            turns.append(result)
            continue
        turns.append({
            "schema_version": "environment.controller_result.v1",
            "project": project,
            "stage": "environment",
            "message_id": message_id,
            "instruction": str(item.get("message") or ""),
            "status": str(item.get("status") or "queued"),
            "return_code": None,
            "session_id": str(state.get("session_id") or ""),
            "response_markdown": "",
            "web_visible_response": True,
            "started_at": str(item.get("started_at") or ""),
            "finished_at": str(item.get("finished_at") or ""),
            "queued": bool(item.get("was_queued")),
            "interrupt_current": bool(item.get("interrupt_current")),
            "interrupted_current": bool(item.get("interrupted_current")),
        })
    return turns


def _requeue_interrupted_message(
    controller_dir: Path,
    project: str,
    project_root: Path,
    item: dict[str, Any],
) -> None:
    message_id = str(item.get("message_id") or "")
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project, project_root)
        queue = [row for row in state.get("queue", []) if isinstance(row, dict)]
        for row in queue:
            if str(row.get("message_id") or "") == message_id:
                row["status"] = "queued"
                row["interrupt_current"] = False
                row["resume_after_interrupt"] = True
                row["created_at"] = utc_now()
        state["queue"] = queue
        _save_controller_state(controller_dir, state)


def _drain_message_queue(
    *,
    project: str,
    project_root: Path,
    controller_dir: Path,
    env: dict[str, str] | None,
) -> dict[str, Any]:
    processed: list[dict[str, Any]] = []
    interrupted_module_task = False
    while True:
        item = _next_queued_message(controller_dir, project, project_root)
        if not item:
            break
        message_id = str(item.get("message_id") or "")
        prompt = (
            "You are the single Environment controller Claude for this project. "
            "Execute the following Web instruction as the highest-priority Environment task. "
            "Work only from the project directory and the evidence available to this Environment controller. "
            "After completing it, explicitly retain the duty to resume any interrupted Environment work.\n\n"
            f"Web instruction:\n{item.get('message', '')}\n"
        )
        prompt_path = controller_dir / "messages" / f"{message_id}.prompt.md"
        log_path = controller_dir / "messages" / f"{message_id}.log"
        write_text(prompt_path, prompt)
        result = _invoke_controller(
            project=project,
            project_root=project_root,
            controller_dir=controller_dir,
            prompt=prompt,
            prompt_path=prompt_path,
            log_path=log_path,
            timeout_sec=0,
            add_dirs=[],
            system_prompt="You are the project's single TASTE Environment controller Claude Code session.",
            env=env,
            work_kind="web_instruction",
            work_id=message_id,
        )
        cancelled = read_json(controller_dir / "messages" / f"{message_id}.json", {})
        if isinstance(cancelled, dict) and cancelled.get("status") == "cancelled":
            processed.append(cancelled)
            continue
        if result.get("interrupted_by") and result.get("return_code") != 0:
            _requeue_interrupted_message(controller_dir, project, project_root, item)
            interrupted_module_task = True
            continue
        processed.append(_save_message_result(controller_dir, project_root, item, result))
    return {"processed": processed, "interrupted": interrupted_module_task}


def run_claude_json(
    prompt: str,
    cwd: Path,
    expected_json_path: Path,
    log_path: Path,
    timeout_sec: int = 1800,
    add_dirs: list[Path] | None = None,
    dry_run: bool = False,
    system_prompt: str = "",
    env: dict[str, str] | None = None,
    *,
    project: str,
    project_root: Path,
) -> dict[str, Any]:
    project, project_root = _safe_project(project, project_root)
    run_dir = Path(cwd).expanduser().resolve()
    controller_dir = _controller_dir(project)
    try:
        expected_json_path = ensure_within(expected_json_path, run_dir)
        log_path = ensure_within(log_path, run_dir)
        prompt_path = ensure_within(expected_json_path.with_suffix(".prompt.md"), run_dir)
        safe_add_dirs = [_safe_add_dir(path, project_root, run_dir) for path in add_dirs or [] if path.exists()]
    except Exception as exc:
        safe_log = run_dir / "claude_runner_path_guard.log"
        write_text(safe_log, f"Environment controller path guard blocked: {type(exc).__name__}: {exc}\n")
        return {"return_code": 126, "status": "blocked_by_path_guard", "json": {}, "stderr_tail": str(exc), "prompt_path": "", "log_path": str(safe_log)}
    expected_json_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    write_text(prompt_path, prompt)
    if dry_run:
        payload = {"status": "dry_run", "expected_json_path": str(expected_json_path), "prompt_path": str(prompt_path), "created_at": utc_now()}
        write_json(expected_json_path, payload)
        write_text(log_path, "dry-run: Environment controller was not called.\n")
        return {"return_code": 0, "status": "dry_run", "json": payload, "prompt_path": str(prompt_path), "log_path": str(log_path)}

    with _locked_file(controller_dir / "execution.lock"):
        _drain_message_queue(project=project, project_root=project_root, controller_dir=controller_dir, env=env)
        result = _invoke_controller(
            project=project,
            project_root=project_root,
            controller_dir=controller_dir,
            prompt=prompt,
            prompt_path=prompt_path,
            log_path=log_path,
            timeout_sec=timeout_sec,
            add_dirs=safe_add_dirs,
            system_prompt=system_prompt,
            env=env,
            work_kind="module_task",
            work_id=str(expected_json_path),
        )
        queued_web_messages_processed = 0
        payload = read_json(expected_json_path, {})
        while result.get("interrupted_by") and not (isinstance(payload, dict) and payload):
            queue_result = _drain_message_queue(project=project, project_root=project_root, controller_dir=controller_dir, env=env)
            queued_web_messages_processed += len(queue_result.get("processed") or [])
            resume_prompt = (
                "A priority Web instruction interrupted this Environment module task. "
                "That instruction has now been handled. Resume and complete the original module duty, "
                "writing the required JSON output exactly as requested.\n\n"
                + prompt
            )
            result = _invoke_controller(
                project=project,
                project_root=project_root,
                controller_dir=controller_dir,
                prompt=resume_prompt,
                prompt_path=prompt_path,
                log_path=log_path,
                timeout_sec=timeout_sec,
                add_dirs=safe_add_dirs,
                system_prompt=system_prompt,
                env=env,
                work_kind="resumed_module_task",
                work_id=str(expected_json_path),
            )
            payload = read_json(expected_json_path, {})
        queue_result = _drain_message_queue(project=project, project_root=project_root, controller_dir=controller_dir, env=env)
        queued_web_messages_processed += len(queue_result.get("processed") or [])

    valid_payload = payload if isinstance(payload, dict) and payload else {}
    return {
        **result,
        "return_code": 0 if valid_payload else int(result.get("return_code") or 1),
        "status": "passed" if valid_payload else "failed_missing_json" if result.get("return_code") == 0 else str(result.get("status") or "failed"),
        "json": valid_payload,
        "prompt_path": str(prompt_path),
        "log_path": str(log_path),
        "artifact_completed": bool(valid_payload),
        "queued_web_messages_processed": queued_web_messages_processed,
    }


def run_controller_message(
    *,
    project: str,
    project_root: Path,
    message: str,
    interrupt_current: bool = False,
    env: dict[str, str] | None = None,
    on_queued: QueueCallback | None = None,
) -> dict[str, Any]:
    project, project_root = _safe_project(project, project_root)
    controller_dir = _controller_dir(project)
    message_id = uuid4().hex
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project, project_root)
        busy = bool(state.get("busy") or state.get("active_pid"))
        queue = [item for item in state.get("queue", []) if isinstance(item, dict)]
        pending = [item for item in queue if item.get("status") in {"queued", "running"}]
        item = {
            "message_id": message_id,
            "project": project,
            "message": str(message).strip(),
            "status": "queued",
            "created_at": utc_now(),
            "was_queued": busy or bool(pending),
            "interrupt_current": bool(interrupt_current),
            "interrupted_current": False,
        }
        queue.append(item)
        state["queue"] = queue
        _save_controller_state(controller_dir, state)
        queue_position = len([row for row in queue if row.get("status") == "queued"])
    interrupted = _interrupt_active(controller_dir, message_id) if interrupt_current else False
    if interrupted:
        with _state_lock(controller_dir):
            state = _controller_state(controller_dir, project, project_root)
            for row in state.get("queue", []):
                if isinstance(row, dict) and row.get("message_id") == message_id:
                    row["interrupted_current"] = True
            _save_controller_state(controller_dir, state)
    queued_payload = {
        "event": "environment_controller_queued",
        "status": "queued",
        "project": project,
        "message_id": message_id,
        "queue_position": queue_position,
        "interrupt_requested": bool(interrupt_current),
        "interrupted_current": interrupted,
    }
    if on_queued and item["was_queued"]:
        on_queued(queued_payload)

    result_path = controller_dir / "messages" / f"{message_id}.json"
    previous_handlers: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        def cancel_message(signum, _frame) -> None:
            _cancel_controller_message(controller_dir, project, project_root, message_id)
            raise SystemExit(128 + int(signum))

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, cancel_message)
    try:
        while True:
            result = read_json(result_path, {})
            if isinstance(result, dict) and result:
                return {**result, "conversation": _controller_conversation(controller_dir, project, project_root)}
            try:
                with _locked_file(controller_dir / "execution.lock", blocking=False):
                    _drain_message_queue(project=project, project_root=project_root, controller_dir=controller_dir, env=env)
            except BlockingIOError:
                pass
            result = read_json(result_path, {})
            if isinstance(result, dict) and result:
                return {**result, "conversation": _controller_conversation(controller_dir, project, project_root)}
            time.sleep(0.25)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
