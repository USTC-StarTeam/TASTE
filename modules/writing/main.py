#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
from typing import Any, Callable, Iterator, Sequence


ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = MODULE_ROOT / ".runtime"
CONTROLLERS_ROOT = RUNTIME_ROOT / "controllers"
SESSION_INDEX = RUNTIME_ROOT / "controller_sessions.json"
PAPER_REL = Path("paper") / "writing"
PUBLIC_STATE_REL = Path("state") / "writing_controller.json"
PUBLIC_RESULT_REL = Path("state") / "writing_controller_last_result.json"
PUBLIC_HISTORY_REL = Path("state") / "writing_controller_history.json"
PIPELINE_REL = Path("paper") / "metadata" / "paper_pipeline.json"
REPORT_REL = Path("reports") / "writing_controller.md"
QueueCallback = Callable[[dict[str, Any]], None]


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(str(text), encoding="utf-8")
    os.replace(tmp, path)


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


def _require_taste() -> None:
    if os.environ.get("CONDA_DEFAULT_ENV") == "taste" or Path(sys.prefix).name == "taste":
        return
    raise SystemExit(
        "Writing must run in conda environment taste. "
        "Use: conda run -n taste python framework/scripts/run_module.py writing --action work --project <project>"
    )


def _valid_session_id(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value or "")))
    except (ValueError, TypeError, AttributeError):
        return ""


def _safe_project(project: str) -> tuple[str, Path]:
    name = str(project or "").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("Writing requires a valid --project name.")
    projects_root = (ROOT / "projects").resolve()
    project_root = (projects_root / name).resolve()
    try:
        project_root.relative_to(projects_root)
    except ValueError as exc:
        raise ValueError(f"Writing project escapes projects/: {project_root}") from exc
    if project_root.name != name or not project_root.is_dir():
        raise FileNotFoundError(f"Writing project does not exist: {name}")
    return name, project_root


def _controller_dir(project: str) -> Path:
    path = CONTROLLERS_ROOT / project
    (path / "messages").mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _state_lock(controller_dir: Path) -> Iterator[None]:
    with _locked_file(controller_dir / "state.lock"):
        yield


def _controller_state(controller_dir: Path, project: str, project_root: Path) -> dict[str, Any]:
    state = _load_json(controller_dir / "controller.json", {})
    state = state if isinstance(state, dict) else {}
    state.update(
        {
            "schema_version": "writing.controller.v1",
            "owner": "modules/writing",
            "controller_role": "writing_controller",
            "project": project,
            "project_root": str(project_root),
            "session_id": _valid_session_id(state.get("session_id")) or str(uuid.uuid4()),
        }
    )
    state.setdefault("session_initialized", False)
    state.setdefault("queue", [])
    state.setdefault("busy", False)
    state.setdefault("turn_count", 0)
    return state


def _sync_session_index(project: str, state: dict[str, Any]) -> None:
    with _locked_file(SESSION_INDEX.with_suffix(".lock")):
        index = _load_json(SESSION_INDEX, {})
        index = index if isinstance(index, dict) else {}
        sessions = index.get("sessions") if isinstance(index.get("sessions"), dict) else {}
        sessions[project] = {
            "session_id": state.get("session_id", ""),
            "project_root": state.get("project_root", ""),
            "owner": "modules/writing",
            "updated_at": _now_iso(),
        }
        _write_json(
            SESSION_INDEX,
            {
                "schema_version": "writing.controller_sessions.v1",
                "policy": "Exactly one Writing controller Claude session per project.",
                "sessions": sessions,
            },
        )


def _publish_project_state(project_root: Path, state: dict[str, Any]) -> None:
    pending = [
        {
            "id": str(item.get("message_id") or ""),
            "stage": "paper",
            "source": "web" if item.get("kind") == "chat" else "framework",
            "message": str(item.get("message") or ""),
            "status": str(item.get("status") or "queued"),
            "created_at": str(item.get("created_at") or ""),
            "interrupt_current": bool(item.get("interrupt_current")),
        }
        for item in state.get("queue", [])
        if isinstance(item, dict) and item.get("status") in {"queued", "running"}
    ]
    _write_json(
        project_root / PUBLIC_STATE_REL,
        {
            "schema_version": "writing.controller_public.v1",
            "module": "writing",
            "project": project_root.name,
            "session_id": state.get("session_id", ""),
            "busy": bool(state.get("busy")),
            "active_kind": state.get("active_kind", ""),
            "active_started_at": state.get("active_started_at", ""),
            "queued_messages": pending,
            "last_result_path": state.get("last_result_path", ""),
            "updated_at": _now_iso(),
        },
    )


def _save_state(controller_dir: Path, project_root: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now_iso()
    _write_json(controller_dir / "controller.json", state)
    _sync_session_index(project_root.name, state)
    _publish_project_state(project_root, state)


def _project_config(project_root: Path) -> dict[str, Any]:
    payload = _load_json(project_root / "project.json", {})
    return payload if isinstance(payload, dict) else {}


def _project_venue(project_root: Path, requested: str = "") -> str:
    if str(requested or "").strip():
        return str(requested).strip()
    cfg = _project_config(project_root)
    paper = cfg.get("paper") if isinstance(cfg.get("paper"), dict) else {}
    return str(
        cfg.get("target_venue")
        or cfg.get("venue")
        or paper.get("target_venue")
        or paper.get("venue")
        or ""
    ).strip()


def _project_title(project_root: Path, requested: str = "") -> str:
    if str(requested or "").strip():
        return str(requested).strip()
    cfg = _project_config(project_root)
    paper = cfg.get("paper") if isinstance(cfg.get("paper"), dict) else {}
    return str(paper.get("title") or "").strip()


def _paper_root(project_root: Path) -> Path:
    root = project_root / PAPER_REL
    for rel in [
        "workspace/inputs",
        "workspace/final",
        "workspace/audits",
        "workspace/repair_rounds",
        "venue",
    ]:
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


def _system_prompt(project: str) -> str:
    module_skill = MODULE_ROOT / "SKILL.md"
    router = MODULE_ROOT / "skills" / "skill-router" / "SKILL.md"
    return (
        f"You are the only Writing controller Claude Code session for project {project}. "
        "Your working directory must remain the project directory. Complete only Writing duties for this project. "
        f"At the start of every turn, read {module_skill} and {router}, then read and apply every skill selected by the router. "
        "Formal writing must use the full paper-orchestra route. Audit repair must use the exact blockers and repair instructions "
        "from the newest independent audit. Treat each Web instruction as highest priority. After completing a Web instruction, "
        "inspect the canonical paper workspace and this session for unfinished Writing work and resume it. "
        "Write scientific paper artifacts only under paper/writing in this project."
    )


def _work_prompt(project_root: Path, venue: str, title: str, *, resume: bool = False) -> str:
    paper_root = _paper_root(project_root)
    task = "Resume and complete the unfinished formal Writing duty." if resume else "Complete the formal Writing duty."
    return f"""
{task}

Project: {project_root.name}
Project directory: {project_root}
Canonical paper workspace: {paper_root}
Target venue: {venue}
Title hint: {title or "(derive from current evidence)"}

Must read current project evidence before writing:
1. AGENTS.md and visible project status files.
2. state/current_find_research_plan.json and the selected Idea/Plan artifacts.
3. state/experiment_registry.json, experiments/, reports/, and raw experiment logs.
4. paper/metadata/paper_pipeline.json and the existing canonical paper workspace when present.

Must produce in the canonical paper workspace:
1. venue/venue_requirements.json from current official venue sources.
2. venue/template_source.json and the official template files.
3. workspace/inputs/template.tex.
4. workspace/final/paper.tex containing submission-facing manuscript content only.
5. workspace/final/paper.pdf when local compilation succeeds.
6. workspace/refs.bib with real, verified references whose keys match paper.tex.
7. workspace/audits/claim_evidence_audit.json and workspace/audits/page_audit.json.
8. workspace/provenance.json with evidence, venue, template, reference, and compile provenance.

The manuscript must meet the current venue body-page rule and the recorded real-reference target, use evidence-calibrated claims,
and reach oral-level conference writing quality. Finish with a concise response containing status, changed paths, and blockers.
""".strip()


def _chat_prompt(project_root: Path, venue: str, title: str, message: str) -> str:
    return f"""
Execute this Web instruction first:

{message}

Project: {project_root.name}
Project directory: {project_root}
Canonical paper workspace: {_paper_root(project_root)}
Target venue: {venue or "(read from project config)"}
Title hint: {title or "(read from project evidence)"}

Use current project evidence and the canonical paper workspace. Apply requested paper changes directly to canonical files.
Reply with the concrete conclusion, inspected or changed paths, blockers, and completed actions. Then inspect this session and the
canonical workspace for unfinished Writing work and resume that work.
""".strip()


def _repair_prompt(
    project_root: Path,
    venue: str,
    title: str,
    round_index: int,
    audit_path: Path,
    audit: dict[str, Any],
) -> str:
    blockers = [str(value) for value in audit.get("blockers", []) if str(value).strip()]
    instructions = [str(value) for value in audit.get("repair_instructions", []) if str(value).strip()]
    return f"""
Repair the canonical manuscript from independent audit round {round_index - 1}.

Project: {project_root.name}
Canonical paper workspace: {_paper_root(project_root)}
Target venue: {venue}
Title hint: {title or "(derive from current evidence)"}
Audit JSON: {audit_path}

Must resolve these blockers:
{json.dumps(blockers, ensure_ascii=False, indent=2)}

Must follow these repair instructions:
{json.dumps(instructions, ensure_ascii=False, indent=2)}

Update only the canonical paper, references, venue, provenance, and audit-support files required by these instructions.
Write workspace/repair_rounds/round_{round_index:02d}/repair_report.json with addressed_blockers, unresolved_blockers,
changed_files, verification_commands, and next_audit_ready. Finish with changed paths and unresolved blockers.
""".strip()


def _audit_prompt(project_root: Path, round_index: int, audit_dir: Path) -> str:
    paper_root = _paper_root(project_root)
    skills = MODULE_ROOT / "skills"
    return f"""
You are a fresh independent Writing audit Claude Code process.

Audit only this canonical manuscript workspace: {paper_root}
Write audit outputs only in: {audit_dir}

Before judging, read:
1. {MODULE_ROOT / "SKILL.md"}
2. {skills / "skill-router" / "SKILL.md"}
3. {skills / "writing-audit" / "SKILL.md"}
4. {skills / "writing-quality" / "SKILL.md"}
5. {skills / "citation-integrity" / "SKILL.md"}
6. {skills / "venue-intelligence" / "SKILL.md"}

Write claude_quality_audit.json and claude_quality_audit.md. The JSON must contain status, checked_files, blockers, warnings,
claim_evidence_verdict, citation_verdict, venue_verdict, page_verdict, paper_normality_verdict, repair_instructions, and final_verdict.
Use status pass only when paper.tex, refs.bib, current official venue requirements, official template provenance, claim evidence,
page evidence, citation-key consistency, real-reference target, evidence-bound claims, venue shape, and oral-level writing quality all pass.
Use status blocked for every other result. For blocked, each blocker must identify the path, failed condition, evidence or count,
and each repair instruction must identify the file and required verifiable result for the Writing controller.
""".strip()


def _json_output(stdout: str) -> dict[str, Any]:
    text = str(stdout or "").strip()
    candidates = [text, *[line.strip() for line in reversed(text.splitlines()) if line.strip().startswith("{")]]
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _pid_alive(value: Any) -> bool:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
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
                    row.update(
                        {
                            "status": "queued",
                            "interrupt_current": False,
                            "resume_after_web": True,
                            "created_at": _now_iso(),
                        }
                    )
            state.update(
                {
                    "busy": False,
                    "active_pid": 0,
                    "active_kind": "",
                    "active_id": "",
                    "active_started_at": "",
                    "session_initialized": bool(state.get("session_initialized") or active_pid),
                }
            )
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


def _invoke_claude(
    *,
    controller_dir: Path,
    project_root: Path,
    item: dict[str, Any],
    prompt: str,
    prompt_label: str,
    active_kind: str,
    timeout_sec: int,
    permission_mode: str,
    dry_run: bool,
    controller_session: bool,
) -> dict[str, Any]:
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project_root.name, project_root)
        session_id = str(state["session_id"])
        initialized = bool(state.get("session_initialized"))
    message_id = str(item.get("message_id") or "")
    prompt_path = controller_dir / "messages" / f"{message_id}.{prompt_label}.prompt.md"
    log_path = controller_dir / "messages" / f"{message_id}.{prompt_label}.log"
    _write_text(prompt_path, prompt + "\n")
    if dry_run:
        return {
            "return_code": 0,
            "status": "dry_run",
            "session_id": session_id if controller_session else "",
            "response": "dry-run: Claude Code was not called.",
            "started_at": _now_iso(),
            "finished_at": _now_iso(),
            "interrupted_by": "",
            "command": [],
            "working_directory": str(project_root),
        }

    claude = shutil.which("claude", path=os.environ.get("PATH", ""))
    if not claude:
        return {
            "return_code": 127,
            "status": "claude_unavailable",
            "session_id": session_id if controller_session else "",
            "response": "",
            "started_at": _now_iso(),
            "finished_at": _now_iso(),
            "interrupted_by": "",
            "command": [],
            "working_directory": str(project_root),
        }
    system_prompt = _system_prompt(project_root.name) if controller_session else (
        "You are a fresh independent Writing audit process. Complete only the supplied audit and return an evidence-backed verdict."
    )
    cmd = [
        claude,
        "-p",
        "--permission-mode",
        permission_mode,
        "--output-format",
        "json",
        "--system-prompt",
        system_prompt,
        "--add-dir",
        str(MODULE_ROOT),
    ]
    if controller_session:
        cmd.extend(["--resume" if initialized else "--session-id", session_id])

    env = os.environ.copy()
    env["WRITING_PROJECT_ROOT"] = str(project_root)
    env["WRITING_WORKSPACE"] = str(_paper_root(project_root))
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"] = "1"
    started = _now_iso()
    proc: subprocess.Popen[str] | None = None
    stdout = ""
    stderr = ""
    timed_out = False
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        with _state_lock(controller_dir):
            state = _controller_state(controller_dir, project_root.name, project_root)
            state.update(
                {
                    "busy": True,
                    "active_pid": proc.pid,
                    "active_kind": active_kind,
                    "active_id": message_id,
                    "active_started_at": started,
                }
            )
            if controller_session:
                state["session_initialized"] = True
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
    reported_session = _valid_session_id(parsed.get("session_id"))
    if controller_session and reported_session and reported_session != session_id:
        return_code = 125
        stderr = (stderr + "\n" if stderr else "") + "Claude returned another project's Writing session ID."
    response = str(parsed.get("result") or parsed.get("response") or stdout or "").strip()
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project_root.name, project_root)
        interrupted_by = str(state.get("interrupt_requested_by") or "")
        state.update(
            {
                "busy": False,
                "active_pid": 0,
                "active_kind": "",
                "active_id": "",
                "active_started_at": "",
                "last_finished_at": _now_iso(),
            }
        )
        if controller_session:
            state["turn_count"] = int(state.get("turn_count") or 0) + 1
        if interrupted_by:
            state["interrupt_requested_by"] = ""
        _save_state(controller_dir, project_root, state)
        controller_turn = int(state.get("turn_count") or 0)
    _write_text(
        log_path,
        f"# cwd={project_root}\n# started_at={started}\n# finished_at={_now_iso()}\n"
        f"# command={json.dumps(cmd, ensure_ascii=False)}\n\n--- STDOUT ---\n{stdout}\n\n--- STDERR ---\n{stderr}\n",
    )
    return {
        "return_code": return_code,
        "status": "interrupted" if interrupted_by else "timeout" if timed_out else "completed" if return_code == 0 else "failed",
        "session_id": session_id if controller_session else "",
        "controller_turn": controller_turn,
        "response": response,
        "stdout_tail": stdout[-12000:],
        "stderr_tail": stderr[-8000:],
        "started_at": started,
        "finished_at": _now_iso(),
        "interrupted_by": interrupted_by,
        "claude_json": parsed,
        "command": cmd,
        "working_directory": str(project_root),
    }


def _run_independent_audit(
    *,
    controller_dir: Path,
    project_root: Path,
    item: dict[str, Any],
    round_index: int,
    timeout_sec: int,
    permission_mode: str,
    dry_run: bool,
) -> dict[str, Any]:
    audit_dir = _paper_root(project_root) / "workspace" / "audits" / f"round_{round_index:02d}"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_json = audit_dir / "claude_quality_audit.json"
    audit_md = audit_dir / "claude_quality_audit.md"
    audit_json.unlink(missing_ok=True)
    audit_md.unlink(missing_ok=True)
    result = _invoke_claude(
        controller_dir=controller_dir,
        project_root=project_root,
        item=item,
        prompt=_audit_prompt(project_root, round_index, audit_dir),
        prompt_label=f"audit_{round_index:02d}",
        active_kind="audit",
        timeout_sec=timeout_sec,
        permission_mode=permission_mode,
        dry_run=dry_run,
        controller_session=False,
    )
    if result.get("interrupted_by"):
        return result
    if dry_run:
        return {**result, "audit": {"status": "dry_run", "blockers": [], "repair_instructions": []}}
    audit = _load_json(audit_json, {})
    if not isinstance(audit, dict) or not audit:
        audit = {
            "status": "blocked",
            "final_verdict": "blocked",
            "checked_files": [],
            "blockers": [f"Independent audit did not write {audit_json}"],
            "warnings": [],
            "repair_instructions": [f"Write a complete audit result at {audit_json} after checking the canonical manuscript."],
        }
    paper_root = _paper_root(project_root)
    required_paths = [
        paper_root / "workspace" / "final" / "paper.tex",
        paper_root / "workspace" / "final" / "paper.pdf",
        paper_root / "workspace" / "refs.bib",
        paper_root / "venue" / "venue_requirements.json",
        paper_root / "venue" / "template_source.json",
        paper_root / "workspace" / "audits" / "claim_evidence_audit.json",
        paper_root / "workspace" / "audits" / "page_audit.json",
        paper_root / "workspace" / "provenance.json",
    ]
    missing = [str(path) for path in required_paths if not path.is_file()]
    status = str(audit.get("status") or audit.get("final_verdict") or "").strip().lower()
    audit["status"] = "pass" if status == "pass" and int(result.get("return_code") or 0) == 0 and not missing else "blocked"
    audit["final_verdict"] = audit["status"]
    audit["audit_round"] = round_index
    audit["claude_return_code"] = int(result.get("return_code") or 0)
    audit["audit_json"] = str(audit_json)
    audit["audit_markdown"] = str(audit_md)
    if int(result.get("return_code") or 0) != 0:
        blockers = audit.get("blockers") if isinstance(audit.get("blockers"), list) else []
        blockers.append(f"Independent audit Claude returned {result.get('return_code')}")
        audit["blockers"] = blockers
    if missing:
        blockers = audit.get("blockers") if isinstance(audit.get("blockers"), list) else []
        instructions = audit.get("repair_instructions") if isinstance(audit.get("repair_instructions"), list) else []
        blockers.extend(f"Required canonical Writing artifact is missing: {path}" for path in missing)
        instructions.extend(f"Create and validate the required canonical Writing artifact: {path}" for path in missing)
        audit["blockers"] = blockers
        audit["repair_instructions"] = instructions
    _write_json(audit_json, audit)
    if not audit_md.is_file():
        lines = ["# Writing Quality Audit", "", f"- status: {audit['status']}"]
        lines.extend(f"- blocker: {value}" for value in audit.get("blockers", []))
        _write_text(audit_md, "\n".join(lines) + "\n")
    current_dir = _paper_root(project_root) / "workspace" / "audits"
    _write_json(current_dir / "claude_quality_audit.json", audit)
    _write_text(current_dir / "claude_quality_audit.md", audit_md.read_text(encoding="utf-8", errors="replace"))
    return {**result, "audit": audit}


def _write_pipeline_state(
    project_root: Path,
    *,
    venue: str,
    title: str,
    status: str,
    audit: dict[str, Any],
    item: dict[str, Any],
    controller_turn: int,
) -> dict[str, Any]:
    paper_root = _paper_root(project_root)
    tex = paper_root / "workspace" / "final" / "paper.tex"
    pdf = paper_root / "workspace" / "final" / "paper.pdf"
    refs = paper_root / "workspace" / "refs.bib"
    blockers = audit.get("blockers") if isinstance(audit.get("blockers"), list) else []
    payload = {
        "schema_version": "writing.paper_pipeline.v1",
        "project": project_root.name,
        "venue": venue,
        "target_venue": venue,
        "title": title,
        "writing_workspace": str(paper_root),
        "writing_status": status,
        "status": "conference_preview_ready" if status == "generated" else "blocked",
        "summary": "Writing controller completed the canonical manuscript and independent audit passed." if status == "generated" else "; ".join(str(value) for value in blockers[:3]),
        "conference_preview_ready": bool(status == "generated" and tex.is_file() and pdf.is_file()),
        "paper_tex": str(tex) if tex.is_file() else "",
        "paper_pdf": str(pdf) if pdf.is_file() else "",
        "refs_bib": str(refs) if refs.is_file() else "",
        "quality_audit_status": str(audit.get("status") or ""),
        "quality_audit": str(paper_root / "workspace" / "audits" / "claude_quality_audit.json"),
        "blockers": blockers,
        "message_id": str(item.get("message_id") or ""),
        "controller_turn": controller_turn,
        "updated_at": _now_iso(),
    }
    _write_json(project_root / PIPELINE_REL, payload)
    return payload


def _run_workflow(
    *,
    controller_dir: Path,
    project_root: Path,
    item: dict[str, Any],
    timeout_sec: int,
    permission_mode: str,
    dry_run: bool,
) -> dict[str, Any]:
    venue = _project_venue(project_root, str(item.get("venue") or ""))
    title = _project_title(project_root, str(item.get("title") or ""))
    if not venue:
        return {
            "return_code": 2,
            "status": "blocked",
            "session_id": "",
            "response": "Writing work requires target venue in the request or project configuration.",
            "started_at": _now_iso(),
            "finished_at": _now_iso(),
            "interrupted_by": "",
        }
    writer = _invoke_claude(
        controller_dir=controller_dir,
        project_root=project_root,
        item=item,
        prompt=_work_prompt(project_root, venue, title, resume=bool(item.get("resume_after_web"))),
        prompt_label="work",
        active_kind="work",
        timeout_sec=timeout_sec,
        permission_mode=permission_mode,
        dry_run=dry_run,
        controller_session=True,
    )
    if writer.get("interrupted_by") or dry_run or int(writer.get("return_code") or 0) != 0:
        return writer

    audit_history: list[dict[str, Any]] = []
    repair_history: list[dict[str, Any]] = []
    audit: dict[str, Any] = {}
    max_repairs = max(0, min(10, int(item.get("max_audit_repair_rounds") or 2)))
    last_controller_turn = int(writer.get("controller_turn") or 0)
    for round_index in range(max_repairs + 1):
        audit_result = _run_independent_audit(
            controller_dir=controller_dir,
            project_root=project_root,
            item=item,
            round_index=round_index,
            timeout_sec=timeout_sec,
            permission_mode=permission_mode,
            dry_run=dry_run,
        )
        if audit_result.get("interrupted_by"):
            return audit_result
        audit = audit_result.get("audit") if isinstance(audit_result.get("audit"), dict) else {}
        audit_history.append(
            {
                "round": round_index,
                "status": audit.get("status", "blocked"),
                "audit_json": audit.get("audit_json", ""),
                "audit_markdown": audit.get("audit_markdown", ""),
                "blockers": audit.get("blockers", []),
                "repair_instructions": audit.get("repair_instructions", []),
            }
        )
        if audit.get("status") == "pass":
            break
        if round_index >= max_repairs:
            break
        repair = _invoke_claude(
            controller_dir=controller_dir,
            project_root=project_root,
            item=item,
            prompt=_repair_prompt(
                project_root,
                venue,
                title,
                round_index + 1,
                Path(str(audit.get("audit_json") or "")),
                audit,
            ),
            prompt_label=f"repair_{round_index + 1:02d}",
            active_kind="repair",
            timeout_sec=timeout_sec,
            permission_mode=permission_mode,
            dry_run=dry_run,
            controller_session=True,
        )
        if repair.get("interrupted_by"):
            return repair
        last_controller_turn = int(repair.get("controller_turn") or last_controller_turn)
        repair_history.append(
            {
                "round": round_index + 1,
                "return_code": repair.get("return_code"),
                "status": repair.get("status"),
                "response": repair.get("response", ""),
            }
        )
        if int(repair.get("return_code") or 0) != 0:
            break

    final_status = "generated" if audit.get("status") == "pass" else "blocked"
    loop = {
        "max_repair_rounds": max_repairs,
        "audit_history": audit_history,
        "repair_history": repair_history,
        "final_audit_status": audit.get("status", "blocked"),
        "updated_at": _now_iso(),
    }
    _write_json(_paper_root(project_root) / "audit_repair_loop.json", loop)
    pipeline = _write_pipeline_state(
        project_root,
        venue=venue,
        title=title,
        status=final_status,
        audit=audit,
        item=item,
        controller_turn=last_controller_turn,
    )
    response = str(writer.get("response") or "").strip()
    audit_note = (
        "Independent Writing audit passed."
        if final_status == "generated"
        else "Independent Writing audit blocked: " + "; ".join(str(value) for value in audit.get("blockers", [])[:5])
    )
    return {
        **writer,
        "return_code": 0 if final_status == "generated" else 2,
        "status": final_status,
        "response": "\n\n".join(value for value in [response, audit_note] if value),
        "controller_turn": last_controller_turn,
        "quality_audit_status": audit.get("status", "blocked"),
        "paper_tex": pipeline.get("paper_tex", ""),
        "paper_pdf": pipeline.get("paper_pdf", ""),
        "paper_workspace": str(_paper_root(project_root)),
        "audit_repair_loop": loop,
        "finished_at": _now_iso(),
    }


def _next_message(controller_dir: Path, project_root: Path) -> dict[str, Any]:
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project_root.name, project_root)
        queued = [item for item in state["queue"] if isinstance(item, dict) and item.get("status") == "queued"]
        if not queued:
            return {}
        queued.sort(
            key=lambda item: (
                0 if item.get("interrupt_current") else 1,
                0 if item.get("kind") == "chat" else 1,
                str(item.get("created_at") or ""),
            )
        )
        selected = queued[0]
        selected["status"] = "running"
        selected["started_at"] = _now_iso()
        _save_state(controller_dir, project_root, state)
        return dict(selected)


def _requeue_interrupted(controller_dir: Path, project_root: Path, item: dict[str, Any]) -> None:
    message_id = str(item.get("message_id") or "")
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project_root.name, project_root)
        for row in state["queue"]:
            if isinstance(row, dict) and row.get("message_id") == message_id:
                row.update(
                    {
                        "status": "queued",
                        "interrupt_current": False,
                        "resume_after_web": True,
                        "created_at": _now_iso(),
                    }
                )
        _save_state(controller_dir, project_root, state)


def _append_history(project_root: Path, receipt: dict[str, Any]) -> None:
    path = project_root / PUBLIC_HISTORY_REL
    history = _load_json(path, {})
    history = history if isinstance(history, dict) else {}
    turns = history.get("turns") if isinstance(history.get("turns"), list) else []
    turns = [row for row in turns if isinstance(row, dict) and row.get("message_id") != receipt.get("message_id")]
    turns.append(receipt)
    _write_json(
        path,
        {
            "schema_version": "writing.controller_history.v1",
            "project": project_root.name,
            "session_id": receipt.get("session_id", ""),
            "turns": turns[-100:],
            "updated_at": _now_iso(),
        },
    )


def _save_result(
    controller_dir: Path,
    project_root: Path,
    item: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    message_id = str(item.get("message_id") or "")
    response_path = project_root / PUBLIC_RESULT_REL
    payload = {
        "schema_version": "writing.controller_result.v1",
        "project": project_root.name,
        "stage": "paper",
        "message_id": message_id,
        "kind": item.get("kind", "chat"),
        "instruction": str(item.get("message") or ""),
        "status": str(result.get("status") or ("completed" if int(result.get("return_code") or 0) == 0 else "failed")),
        "return_code": int(result.get("return_code") or 0),
        "session_id": str(result.get("session_id") or ""),
        "controller_turn": int(result.get("controller_turn") or 0),
        "response_markdown": str(result.get("response") or ""),
        "response_source": str(response_path.relative_to(project_root)),
        "web_visible_response": True,
        "queued": bool(item.get("was_queued")),
        "interrupt_current": bool(item.get("interrupt_current")),
        "interrupted_current": bool(item.get("interrupted_current")),
        "target_venue": _project_venue(project_root, str(item.get("venue") or "")),
        "quality_audit_status": str(result.get("quality_audit_status") or ""),
        "paper_workspace": str(result.get("paper_workspace") or ""),
        "paper_tex": str(result.get("paper_tex") or ""),
        "paper_pdf": str(result.get("paper_pdf") or ""),
        "started_at": str(result.get("started_at") or item.get("started_at") or ""),
        "finished_at": str(result.get("finished_at") or _now_iso()),
    }
    result_path = controller_dir / "messages" / f"{message_id}.json"
    _write_json(result_path, payload)
    _write_json(response_path, payload)
    if item.get("kind") == "chat":
        _append_history(project_root, payload)
    report = project_root / REPORT_REL
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
        state["last_result_path"] = str(response_path)
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
        if item.get("kind") == "work":
            result = _run_workflow(
                controller_dir=controller_dir,
                project_root=project_root,
                item=item,
                timeout_sec=timeout_sec,
                permission_mode=permission_mode,
                dry_run=dry_run,
            )
        else:
            result = _invoke_claude(
                controller_dir=controller_dir,
                project_root=project_root,
                item=item,
                prompt=_chat_prompt(
                    project_root,
                    _project_venue(project_root, str(item.get("venue") or "")),
                    _project_title(project_root, str(item.get("title") or "")),
                    str(item.get("message") or ""),
                ),
                prompt_label="chat",
                active_kind="chat",
                timeout_sec=timeout_sec,
                permission_mode=permission_mode,
                dry_run=dry_run,
                controller_session=True,
            )
        if result.get("interrupted_by"):
            _requeue_interrupted(controller_dir, project_root, item)
            continue
        _save_result(controller_dir, project_root, item, result)


def run_controller_message(
    *,
    project: str,
    kind: str,
    message: str,
    venue: str = "",
    title: str = "",
    timeout_sec: int = 14400,
    permission_mode: str = "bypassPermissions",
    interrupt_current: bool = False,
    dry_run: bool = False,
    max_audit_repair_rounds: int = 2,
    on_queued: QueueCallback | None = None,
) -> dict[str, Any]:
    project, project_root = _safe_project(project)
    controller_dir = _controller_dir(project)
    message_id = uuid.uuid4().hex
    with _state_lock(controller_dir):
        state = _controller_state(controller_dir, project, project_root)
        pending = [
            item
            for item in state["queue"]
            if isinstance(item, dict) and item.get("status") in {"queued", "running"}
        ]
        busy = bool(state.get("busy") or state.get("active_pid") or pending)
        item = {
            "message_id": message_id,
            "project": project,
            "kind": kind,
            "message": message.strip(),
            "venue": venue.strip(),
            "title": title.strip(),
            "status": "queued",
            "created_at": _now_iso(),
            "was_queued": busy,
            "interrupt_current": bool(interrupt_current),
            "interrupted_current": False,
            "max_audit_repair_rounds": max(0, min(10, int(max_audit_repair_rounds))),
        }
        state["queue"].append(item)
        _save_state(controller_dir, project_root, state)
        position = len(
            [row for row in state["queue"] if isinstance(row, dict) and row.get("status") == "queued"]
        )
    interrupted = _interrupt_active(controller_dir, project_root, message_id) if interrupt_current else False
    if interrupted:
        with _state_lock(controller_dir):
            state = _controller_state(controller_dir, project, project_root)
            for row in state["queue"]:
                if isinstance(row, dict) and row.get("message_id") == message_id:
                    row["interrupted_current"] = True
            _save_state(controller_dir, project_root, state)
    if busy and on_queued:
        on_queued(
            {
                "event": "writing_controller_queued",
                "status": "queued",
                "project": project,
                "message_id": message_id,
                "message": message.strip(),
                "queue_position": position,
                "interrupt_requested": bool(interrupt_current),
                "interrupted_current": interrupted,
            }
        )

    result_path = controller_dir / "messages" / f"{message_id}.json"
    deadline = time.monotonic() + timeout_sec if timeout_sec > 0 else None
    while True:
        result = _load_json(result_path, {})
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
        result = _load_json(result_path, {})
        if isinstance(result, dict) and result:
            return result
        if deadline is not None and time.monotonic() >= deadline:
            return {
                "schema_version": "writing.controller_result.v1",
                "project": project,
                "stage": "paper",
                "message_id": message_id,
                "kind": kind,
                "instruction": message.strip(),
                "status": "queued_timeout",
                "return_code": 124,
                "response_markdown": "",
                "queued": True,
                "interrupt_current": bool(interrupt_current),
                "interrupted_current": interrupted,
                "finished_at": _now_iso(),
            }
        time.sleep(0.25)


def _asset_status() -> dict[str, Any]:
    router_path = MODULE_ROOT / "skills" / "skill-router" / "SKILL.md"
    router = router_path.read_text(encoding="utf-8", errors="replace") if router_path.is_file() else ""
    skill_docs = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (MODULE_ROOT / "skills").rglob("SKILL.md")
    )
    skills = []
    blockers = []
    for path in sorted((MODULE_ROOT / "skills").iterdir()):
        if not path.is_dir():
            continue
        skill_file = path / "SKILL.md"
        routed = path.name == "skill-router" or path.name in router
        row = {"name": path.name, "path": str(skill_file), "ready": skill_file.is_file(), "routed": routed}
        skills.append(row)
        if not row["ready"]:
            blockers.append(f"Missing skill: {path.name}")
        elif not routed:
            blockers.append(f"Skill router does not route: {path.name}")
    helpers = []
    for path in sorted((MODULE_ROOT / "skills").rglob("scripts/*")):
        if not path.is_file() or path.suffix not in {".py", ".sh"}:
            continue
        referenced = path.name in skill_docs
        helpers.append({"path": str(path.relative_to(MODULE_ROOT)), "referenced_by_skill": referenced})
        if not referenced:
            blockers.append(f"Skill helper has no SKILL.md route: {path.relative_to(MODULE_ROOT)}")
    return {
        "module": "writing",
        "status": "ok" if not blockers else "blocked",
        "skills": skills,
        "skill_helpers": helpers,
        "blockers": blockers,
        "policy": "Every Writing skill has an explicit route; Claude reads the router at the start of every turn.",
    }


def _contract_payload() -> dict[str, Any]:
    return {
        "stage": "writing",
        "display_name": "Writing",
        "responsibility": "Own one Writing controller Claude session per project and produce an independently audited canonical manuscript.",
        "entrypoint": "modules/writing/main.py",
        "runtime_root": "modules/writing/.runtime",
        "public_actions": ["work", "chat", "controller_status", "assets"],
        "required_external_inputs": ["project"],
        "artifacts_in": [
            "current selected Idea/Plan contract",
            "project Find/Read evidence",
            "project experiment registry, records, and raw logs",
        ],
        "artifacts_out": [
            "modules/writing/.runtime/controller_sessions.json",
            "projects/<project>/state/writing_controller.json",
            "projects/<project>/state/writing_controller_last_result.json",
            "projects/<project>/paper/writing/",
            "projects/<project>/paper/metadata/paper_pipeline.json",
        ],
        "controller_policy": "Exactly one module-owned Writing Claude UUID per project; work, Web chat, and audit repair resume only that UUID.",
        "working_directory": "projects/<project>/",
        "queue_policy": "Busy messages queue in Writing; interrupting Web messages run first and interrupted Writing work resumes afterward.",
        "audit_policy": "Every formal work action uses fresh audit Claude processes; blocked reasons and repair instructions return to the Writing controller before a fresh re-audit.",
        "run_policy": "Writing controller actions create no run directories. Canonical scientific artifacts stay in the project.",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TASTE Writing public entrypoint.")
    parser.add_argument("--action", default="work")
    parser.add_argument("--contract", action="store_true")
    parser.add_argument("--project", default="")
    parser.add_argument("--venue", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--message-file", default="")
    parser.add_argument("--timeout-sec", type=int, default=int(os.environ.get("WRITING_CLAUDE_TIMEOUT_SEC", "14400")))
    parser.add_argument("--permission-mode", default="bypassPermissions")
    parser.add_argument("--max-audit-repair-rounds", type=int, default=int(os.environ.get("WRITING_AUDIT_REPAIR_ROUNDS", "2")))
    parser.add_argument("--interrupt-current", action="store_true")
    parser.add_argument("--queue-if-busy", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.contract:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    _require_taste()
    action = str(args.action or "work").strip().lower().replace("-", "_")
    if action == "assets":
        payload = _asset_status()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["status"] == "ok" else 2
    try:
        project, project_root = _safe_project(args.project)
    except (ValueError, FileNotFoundError) as exc:
        print(json.dumps({"status": "blocked", "blockers": [str(exc)]}, ensure_ascii=False, indent=2))
        return 2
    if action == "controller_status":
        controller_dir = _controller_dir(project)
        with _state_lock(controller_dir):
            state = _controller_state(controller_dir, project, project_root)
            _save_state(controller_dir, project_root, state)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    if action not in {"work", "chat"}:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "action": action,
                    "blockers": [f"Unknown Writing action: {action}"],
                    "supported_actions": ["work", "chat", "controller_status", "assets"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    message = str(args.message or "").strip()
    if not message and args.message_file:
        message = Path(args.message_file).expanduser().read_text(encoding="utf-8", errors="replace").strip()
    if action == "chat" and not message:
        print(json.dumps({"status": "blocked", "blockers": ["Writing chat requires --message or --message-file."]}, ensure_ascii=False, indent=2))
        return 2
    if action == "work" and not message:
        message = "Generate or revise the canonical manuscript from the current selected project contract and completed evidence."

    def announce_queued(event: dict[str, Any]) -> None:
        print(json.dumps(event, ensure_ascii=False), flush=True)

    result = run_controller_message(
        project=project,
        kind=action,
        message=message,
        venue=args.venue,
        title=args.title,
        timeout_sec=args.timeout_sec,
        permission_mode=args.permission_mode,
        interrupt_current=bool(args.interrupt_current),
        dry_run=bool(args.dry_run),
        max_audit_repair_rounds=args.max_audit_repair_rounds,
        on_queued=announce_queued,
    )
    print(json.dumps({**result, "controller_dir": str(_controller_dir(project))}, ensure_ascii=False, indent=2))
    return int(result.get("return_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
