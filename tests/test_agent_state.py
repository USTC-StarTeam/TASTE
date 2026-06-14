from pathlib import Path

from path_helpers import ensure_script_paths


ROOT = Path(__file__).resolve().parents[1]
ensure_script_paths()

import agent_state


def test_agent_running_update_can_clear_stale_terminal_state(monkeypatch, tmp_path):
    class Paths:
        state = tmp_path / "state"

    monkeypatch.setattr(agent_state, "build_paths", lambda _project: Paths)

    agent_state.mark_agent("demo", "writing_revision", "completed", result={"return_code": 0})
    stale = agent_state.list_agents("demo")[0]
    assert stale["result"] == {"return_code": 0}
    assert "finished_at" in stale

    agent_state.upsert_agent("demo", "writing_revision", status="running", current_step="heartbeat without reset")
    preserved = agent_state.list_agents("demo")[0]
    assert preserved["result"] == {"return_code": 0}
    assert "finished_at" in preserved

    agent_state.upsert_agent(
        "demo",
        "writing_revision",
        status="running",
        pid=123,
        current_step="new Claude Code process started",
        extra={"clear_terminal_state": True},
    )
    running = agent_state.list_agents("demo")[0]
    assert running["status"] == "running"
    assert running["pid"] == 123
    assert running["current_step"] == "new Claude Code process started"
    assert "result" not in running
    assert "finished_at" not in running
