from __future__ import annotations

import json
import os
import stat
import threading
import time
import uuid
from pathlib import Path

from modules.writing import main as writing_main


def _setup_writing(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "projects" / "demo"
    (project_root / "state").mkdir(parents=True)
    (project_root / "project.json").write_text(
        json.dumps({"paper": {"target_venue": "ICLR", "title": "Demo Paper"}}),
        encoding="utf-8",
    )
    runtime = tmp_path / "writing_runtime"
    monkeypatch.setattr(writing_main, "ROOT", tmp_path)
    monkeypatch.setattr(writing_main, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(writing_main, "CONTROLLERS_ROOT", runtime / "controllers")
    monkeypatch.setattr(writing_main, "SESSION_INDEX", runtime / "controller_sessions.json")

    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        """#!/usr/bin/env python3
import json
import os
import re
import sys
import time
from pathlib import Path

args = sys.argv[1:]
prompt = sys.stdin.read()
project = Path.cwd()
state = project / "state"
state.mkdir(parents=True, exist_ok=True)
session_flag = "--resume" if "--resume" in args else "--session-id" if "--session-id" in args else ""
session_id = args[args.index(session_flag) + 1] if session_flag else ""
call = {
    "cwd": str(project),
    "mode": "resume" if session_flag == "--resume" else "create" if session_flag else "independent",
    "session_id": session_id,
    "prompt": prompt[:200],
}
with (state / "fake_claude_calls.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(call) + "\\n")

if "INTERRUPT_ME" in prompt:
    slept = state / "interrupt_sleep_started"
    if not slept.exists():
        slept.write_text("1", encoding="utf-8")
        time.sleep(10)
if "QUEUE_SLOW" in prompt:
    time.sleep(0.8)

workspace = Path(os.environ["WRITING_WORKSPACE"])
if "Audit only this canonical manuscript workspace:" in prompt:
    match = re.search(r"Write audit outputs only in: (.+)", prompt)
    audit_dir = Path(match.group(1).strip())
    audit_dir.mkdir(parents=True, exist_ok=True)
    counter_path = state / "audit_count"
    count = int(counter_path.read_text(encoding="utf-8") or "0") + 1 if counter_path.exists() else 1
    counter_path.write_text(str(count), encoding="utf-8")
    blocked = (state / "block_first_audit").exists() and count == 1
    audit = {
        "status": "blocked" if blocked else "pass",
        "final_verdict": "blocked" if blocked else "pass",
        "checked_files": [str(workspace / "workspace" / "final" / "paper.tex")],
        "blockers": ["paper.tex: repair required"] if blocked else [],
        "warnings": [],
        "repair_instructions": ["Update paper.tex and verify the repair."] if blocked else [],
    }
    (audit_dir / "claude_quality_audit.json").write_text(json.dumps(audit), encoding="utf-8")
    (audit_dir / "claude_quality_audit.md").write_text("# Audit\\n", encoding="utf-8")
    print(json.dumps({"result": "audit complete"}))
    raise SystemExit(0)

if "Complete the formal Writing duty." in prompt or "Resume and complete the unfinished formal Writing duty." in prompt:
    (workspace / "workspace" / "final").mkdir(parents=True, exist_ok=True)
    (workspace / "workspace" / "audits").mkdir(parents=True, exist_ok=True)
    (workspace / "venue").mkdir(parents=True, exist_ok=True)
    (workspace / "workspace" / "final" / "paper.tex").write_text("\\\\section{Demo}", encoding="utf-8")
    (workspace / "workspace" / "final" / "paper.pdf").write_bytes(b"%PDF-1.4")
    (workspace / "workspace" / "refs.bib").write_text("@article{demo,title={Demo}}", encoding="utf-8")
    (workspace / "workspace" / "audits" / "claim_evidence_audit.json").write_text("{}", encoding="utf-8")
    (workspace / "workspace" / "audits" / "page_audit.json").write_text(json.dumps({"total_pages": 9, "body_pages": 8, "reference_pages": 1}), encoding="utf-8")
    (workspace / "workspace" / "provenance.json").write_text("{}", encoding="utf-8")
    (workspace / "venue" / "venue_requirements.json").write_text("{}", encoding="utf-8")
    (workspace / "venue" / "template_source.json").write_text("{}", encoding="utf-8")

payload = {"result": "handled: " + prompt.splitlines()[0]}
if session_id:
    payload["session_id"] = session_id
print(json.dumps(payload))
""",
        encoding="utf-8",
    )
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    return project_root, runtime


def _calls(project_root: Path) -> list[dict]:
    path = project_root / "state" / "fake_claude_calls.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_project_writing_controller_is_created_then_resumed(monkeypatch, tmp_path):
    project_root, runtime = _setup_writing(monkeypatch, tmp_path)

    first = writing_main.run_controller_message(project="demo", kind="chat", message="first", timeout_sec=30)
    second = writing_main.run_controller_message(project="demo", kind="chat", message="second", timeout_sec=30)

    index = json.loads((runtime / "controller_sessions.json").read_text(encoding="utf-8"))
    session_id = str(uuid.UUID(index["sessions"]["demo"]["session_id"]))
    state = json.loads((runtime / "controllers" / "demo" / "controller.json").read_text(encoding="utf-8"))
    assert state["owner"] == "modules/writing"
    assert state["session_id"] == session_id
    assert state["project_root"] == str(project_root)
    assert state["turn_count"] == 2
    assert first["session_id"] == second["session_id"] == session_id

    calls = _calls(project_root)
    assert [row["mode"] for row in calls] == ["create", "resume"]
    assert all(row["cwd"] == str(project_root) for row in calls)
    assert all(row["session_id"] == session_id for row in calls)
    assert not (runtime / "runs").exists()

    history = json.loads((project_root / "state" / "writing_controller_history.json").read_text(encoding="utf-8"))
    assert [turn["instruction"] for turn in history["turns"]] == ["first", "second"]


def test_removed_run_action_creates_no_runtime_run(monkeypatch, tmp_path):
    _project_root, runtime = _setup_writing(monkeypatch, tmp_path)

    assert writing_main.main(["--action", "run", "--project", "demo"]) == 2
    assert not (runtime / "runs").exists()


def test_work_uses_fresh_audits_and_same_controller_for_repair(monkeypatch, tmp_path):
    project_root, _runtime = _setup_writing(monkeypatch, tmp_path)
    (project_root / "state" / "block_first_audit").write_text("1", encoding="utf-8")

    result = writing_main.run_controller_message(
        project="demo",
        kind="work",
        message="write the paper",
        timeout_sec=30,
        max_audit_repair_rounds=2,
    )

    assert result["status"] == "generated"
    assert result["quality_audit_status"] == "pass"
    calls = _calls(project_root)
    assert [row["mode"] for row in calls] == ["create", "independent", "resume", "independent"]
    session_ids = [row["session_id"] for row in calls]
    assert session_ids[0]
    assert session_ids[1] == ""
    assert session_ids[2] == session_ids[0]
    assert session_ids[3] == ""

    paper_root = project_root / "paper" / "writing"
    loop = json.loads((paper_root / "audit_repair_loop.json").read_text(encoding="utf-8"))
    assert [row["status"] for row in loop["audit_history"]] == ["blocked", "pass"]
    assert len(loop["repair_history"]) == 1
    pipeline = json.loads((project_root / "paper" / "metadata" / "paper_pipeline.json").read_text(encoding="utf-8"))
    assert pipeline["writing_status"] == "generated"
    assert pipeline["writing_workspace"] == str(paper_root)


def test_busy_message_is_queued_and_visible(monkeypatch, tmp_path):
    project_root, _runtime = _setup_writing(monkeypatch, tmp_path)
    first_result: dict = {}

    def run_first() -> None:
        first_result.update(
            writing_main.run_controller_message(
                project="demo",
                kind="chat",
                message="QUEUE_SLOW",
                timeout_sec=30,
            )
        )

    thread = threading.Thread(target=run_first)
    thread.start()
    state_path = project_root / "state" / "writing_controller.json"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if state.get("busy"):
            break
        time.sleep(0.02)
    events: list[dict] = []
    second = writing_main.run_controller_message(
        project="demo",
        kind="chat",
        message="queued message",
        timeout_sec=30,
        on_queued=events.append,
    )
    thread.join(timeout=10)

    assert first_result["status"] == "completed"
    assert second["status"] == "completed"
    assert events[0]["event"] == "writing_controller_queued"
    assert events[0]["message"] == "queued message"
    assert second["queued"] is True


def test_interrupt_runs_web_message_first_then_resumes_old_work(monkeypatch, tmp_path):
    project_root, _runtime = _setup_writing(monkeypatch, tmp_path)
    old_result: dict = {}

    def run_old() -> None:
        old_result.update(
            writing_main.run_controller_message(
                project="demo",
                kind="chat",
                message="INTERRUPT_ME",
                timeout_sec=30,
            )
        )

    thread = threading.Thread(target=run_old)
    thread.start()
    state_path = project_root / "state" / "writing_controller.json"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if state.get("busy"):
            break
        time.sleep(0.02)
    events: list[dict] = []
    priority = writing_main.run_controller_message(
        project="demo",
        kind="chat",
        message="WEB_PRIORITY",
        timeout_sec=30,
        interrupt_current=True,
        on_queued=events.append,
    )
    thread.join(timeout=10)

    assert priority["status"] == "completed"
    assert priority["interrupted_current"] is True
    assert old_result["status"] == "completed"
    prompts = [row["prompt"] for row in _calls(project_root)]
    assert "WEB_PRIORITY" in prompts[-2]
    assert "INTERRUPT_ME" in prompts[-1]
    assert events[0]["interrupted_current"] is True


def test_web_public_receipt_uses_writing_controller_state(tmp_path):
    from auto_research import project_bridge

    root = tmp_path / "demo"
    state_dir = root / "state"
    state_dir.mkdir(parents=True)
    session_id = str(uuid.uuid4())
    (root / "project.json").write_text(json.dumps({"paper": {"target_venue": "ICLR"}}), encoding="utf-8")
    receipt = {
        "status": "completed",
        "stage": "paper",
        "message_id": "message-1",
        "session_id": session_id,
        "instruction": "check citations",
        "response_markdown": "Citation audit complete.",
        "response_source": "state/writing_controller_last_result.json",
        "web_visible_response": True,
        "kind": "chat",
        "controller_turn": 1,
        "finished_at": "2026-07-10T00:00:00Z",
        "target_venue": "ICLR",
    }
    (state_dir / "writing_controller_last_result.json").write_text(json.dumps(receipt), encoding="utf-8")
    (state_dir / "writing_controller_history.json").write_text(json.dumps({"turns": [receipt]}), encoding="utf-8")

    public = project_bridge._public_claude_receipts_by_stage(root)["paper"]
    assert public["response_markdown"] == "Citation audit complete."
    assert public["instruction"] == "check citations"
    assert public["session_id"] == session_id
    assert public["message_id"] == "message-1"
    assert public["conversation"][0]["message_id"] == "message-1"
