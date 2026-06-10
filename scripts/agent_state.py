#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, is_dataclass
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from project_paths import build_paths


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return json_safe(asdict(value))
    if isinstance(value, dict):
        skip_key = chr(95) + chr(112) + chr(97) + chr(116) + chr(104) + chr(115)
        return {str(key): json_safe(item) for key, item in value.items() if str(key) != skip_key}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(json_safe(payload), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def agents_path(project: str) -> Path:
    return build_paths(project).state / "agents.json"


def queue_path(project: str) -> Path:
    return build_paths(project).state / "guidance_queue.json"


def _normalize_state(data: Any, project: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    agents = data.get("agents", [])
    if not isinstance(agents, list):
        agents = []
    return {
        "project": data.get("project") or project,
        "updated_at": data.get("updated_at") or now_iso(),
        "agents": [row for row in agents if isinstance(row, dict)],
    }


def _write_state(project: str, state: dict[str, Any]) -> dict[str, Any]:
    state["project"] = project
    state["updated_at"] = now_iso()
    save_json(agents_path(project), state)
    return state


def load_agents(project: str) -> dict[str, Any]:
    return _normalize_state(load_json(agents_path(project), {}), project)


def list_agents(project: str) -> list[dict[str, Any]]:
    return load_agents(project).get("agents", [])


def upsert_agent(
    project: str,
    agent_id: str,
    *,
    name: str = "",
    role: str = "",
    stage: str = "",
    status: str = "",
    goal: str = "",
    parent_id: str = "",
    pid: int | None = None,
    job_id: str = "",
    command: list[str] | None = None,
    current_step: str = "",
    children: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = load_agents(project)
    agents = state["agents"]
    now = now_iso()
    found = None
    for row in agents:
        if row.get("id") == agent_id:
            found = row
            break
    if found is None:
        found = {
            "id": agent_id,
            "created_at": now,
            "log_tail": [],
            "queued_guidance": [],
            "children": [],
        }
        agents.append(found)
    updates = {
        "name": name,
        "role": role,
        "stage": stage,
        "status": status,
        "goal": goal,
        "parent_id": parent_id,
        "pid": pid,
        "job_id": job_id,
        "current_step": current_step,
    }
    for key, value in updates.items():
        if value not in ("", None):
            found[key] = value
    if command is not None:
        found["command"] = command
    if children is not None:
        found["children"] = children
    if extra:
        clelog_tail = bool(extra.pop("clelog_tail", False))
        if clelog_tail:
            found["log_tail"] = []
        found.update(extra)
    found["updated_at"] = now
    _write_state(project, state)
    return found


def mark_agent(project: str, agent_id: str, status: str, *, current_step: str = "", result: dict[str, Any] | None = None) -> None:
    extra = {"finished_at": now_iso()} if status in {"done", "error", "cancelled", "failed", "completed"} else {}
    if result is not None:
        extra["result"] = result
    upsert_agent(project, agent_id, status=status, current_step=current_step, extra=extra)


def append_agent_log(project: str, agent_id: str, line: str, *, max_lines: int = 120) -> None:
    text = str(line or "").rstrip()
    if not text:
        return
    state = load_agents(project)
    for row in state["agents"]:
        if row.get("id") == agent_id:
            tail = row.get("log_tail", [])
            if not isinstance(tail, list):
                tail = []
            if tail and str(tail[-1]) == text:
                row["updated_at"] = now_iso()
                _write_state(project, state)
                return
            tail.append(text)
            row["log_tail"] = tail[-max_lines:]
            row["updated_at"] = now_iso()
            _write_state(project, state)
            return
    upsert_agent(project, agent_id, status="running", current_step=text, extra={"log_tail": [text]})


def queue_guidance(project: str, target_agent_id: str, message: str, *, stage: str = "", source: str = "web") -> dict[str, Any]:
    text = str(message or "").strip()
    if not text:
        raise ValueError("Guidance message is empty.")
    item = {
        "id": f"guidance_{uuid4().hex[:10]}",
        "project": project,
        "target_agent_id": target_agent_id,
        "stage": stage,
        "source": source,
        "message": text,
        "status": "queued",
        "delivery_mode": "queued_until_safe_checkpoint",
        "delivery_note": "If a Claude Code process is already running, this guidance is consumed by the next project agent project-session checkpoint; it is not hot-injected into an already started claude -p call.",
        "created_at": now_iso(),
    }
    queue = load_json(queue_path(project), [])
    if not isinstance(queue, list):
        queue = []
    queue.append(item)
    save_json(queue_path(project), queue[-500:])

    state = load_agents(project)
    target = None
    for row in state["agents"]:
        if row.get("id") == target_agent_id:
            target = row
            break
    if target is None:
        target = {
            "id": target_agent_id,
            "name": "主控 Agent" if target_agent_id == "main" else target_agent_id,
            "role": "main" if target_agent_id == "main" else "worker",
            "stage": stage or "queued-guidance",
            "status": "queued",
            "goal": "等待网页排队引导",
            "created_at": now_iso(),
            "log_tail": [],
            "queued_guidance": [],
            "children": [],
        }
        state["agents"].append(target)
    queued = target.get("queued_guidance", [])
    if not isinstance(queued, list):
        queued = []
    queued.append(item)
    target["queued_guidance"] = queued[-30:]
    target["updated_at"] = now_iso()
    _write_state(project, state)
    return item


def _stage_matches(requested: str, actual: str) -> bool:
    requested = str(requested or "").strip()
    actual = str(actual or "").strip()
    requested_key = requested.replace("_", "-")
    actual_key = actual.replace("_", "-")
    if not requested or requested in {"project", "any", "all"}:
        return True
    if requested == actual:
        return True
    # Web supervision panels are coarse-grained; project agent uses finer stages
    # such as experiment-evidence-repair or reference-reproduction-repair.
    # Keep queued human guidance project-scoped for the active research owner.
    if actual and requested in {"environment", "experiment", "paper"}:
        return True
    # Full-cycle stages are also one project-level owner. A user instruction
    # queued while the UI shows ideation/experiment must be consumed by the
    # next full-cycle guidance checkpoint instead of lingering as unread.
    if requested_key.startswith("full-cycle") and actual_key.startswith("full-cycle"):
        return True
    return False


def consume_guidance(project: str, *, target_agent_id: str = "", stage: str = "", limit: int = 20) -> list[dict[str, Any]]:
    queue = load_json(queue_path(project), [])
    if not isinstance(queue, list):
        return []
    consumed: list[dict[str, Any]] = []
    now = now_iso()
    for item in queue:
        if not isinstance(item, dict) or item.get("status") != "queued":
            continue
        if target_agent_id and item.get("target_agent_id") not in {target_agent_id, "main", "project"}:
            continue
        if not _stage_matches(str(item.get("stage") or ""), stage):
            continue
        item["status"] = "consumed"
        item["consumed_at"] = now
        item["consumed_by_stage"] = stage
        consumed.append(item)
        if len(consumed) >= limit:
            break
    save_json(queue_path(project), queue[-500:])
    if consumed:
        state = load_agents(project)
        consumed_ids = {item.get("id") for item in consumed}
        for row in state["agents"]:
            queued = row.get("queued_guidance", [])
            if isinstance(queued, list):
                row["queued_guidance"] = [item for item in queued if item.get("id") not in consumed_ids]
        _write_state(project, state)
    return consumed


def active_main_agent(project: str, stage: str = "") -> dict[str, Any]:
    agents = list_agents(project)
    running = [
        row for row in agents
        if row.get("role") in {"main", "claude-main"}
        and row.get("status") in {"queued", "running", "cancelling"}
        and (not stage or row.get("stage") == stage)
    ]
    if running:
        return sorted(running, key=lambda row: str(row.get("updated_at") or ""), reverse=True)[0]
    main = [row for row in agents if row.get("role") in {"main", "claude-main"}]
    if main:
        return sorted(main, key=lambda row: str(row.get("updated_at") or ""), reverse=True)[0]
    return {}


def process_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except Exception:
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
        return True
    except OSError:
        return False


def refresh_process_flags(project: str) -> dict[str, Any]:
    state = load_agents(project)
    changed = False
    for row in state["agents"]:
        pid = row.get("pid")
        if pid:
            alive = process_alive(pid)
            if row.get("process_alive") != alive:
                row["process_alive"] = alive
                changed = True
            if row.get("status") in {"running", "cancelling"} and not alive:
                row["process_alive"] = False
                row["status"] = "stopped"
                row.setdefault("finished_at", now_iso())
                if not str(row.get("current_step") or "").strip():
                    row["current_step"] = "process exited; waiting for parent TASTE status refresh"
                changed = True
    if changed:
        return _write_state(project, state)
    return state
