from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from bridges import environment_bridge, project_bridge
from orchestration import run_project
from scripts.common import claude_runner
from scripts.common.shell import command_is_dangerous


ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_ROOT = ROOT / "modules" / "environment"


def _load_environment_script(relative_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, ENVIRONMENT_ROOT / "scripts" / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


autonomous_deploy = _load_environment_script("orchestration/autonomous_deploy.py", "environment_autonomous_deploy_test")


def _load_environment_main():
    spec = importlib.util.spec_from_file_location("environment_public_main_test", ENVIRONMENT_ROOT / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _controller_workspace(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    project_root = workspace / "projects" / "demo"
    project_root.mkdir(parents=True)
    module_root = workspace / "modules" / "environment"
    module_root.mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("ENVIRONMENT_ROOT", str(module_root))
    return project_root


def _fake_controller_call(**kwargs):
    state = claude_runner.read_json(kwargs["controller_dir"] / "controller.json", {})
    now = claude_runner.utc_now()
    return {
        "return_code": 0,
        "status": "passed",
        "session_id": str(state.get("session_id") or ""),
        "response": "completed: " + str(kwargs.get("work_id") or "turn"),
        "started_at": now,
        "finished_at": now,
    }


def test_environment_public_contract_and_private_script_guard():
    contract = subprocess.run(
        [sys.executable, str(ENVIRONMENT_ROOT / "main.py"), "--contract"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert contract.returncode == 0, contract.stderr
    payload = json.loads(contract.stdout)
    assert payload["entrypoint"] == "modules/environment/main.py"
    assert payload["scripts_are_private_backend"] is True
    assert set(payload["actions"]) == {"deploy_from_plan", "chat", "status"}

    env = os.environ.copy()
    env.pop("ENVIRONMENT_PUBLIC_ENTRYPOINT_ACTIVE", None)
    private = subprocess.run(
        [sys.executable, str(ENVIRONMENT_ROOT / "scripts" / "orchestration" / "autonomous_deploy.py"), "--help"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert private.returncode != 0
    assert "Use modules/environment/main.py" in private.stderr + private.stdout


def test_prompt_truncation_uses_valid_json_reference(tmp_path):
    value = {"evidence": ["x" * 200]}
    source = tmp_path / "evidence.json"
    marker_text = autonomous_deploy._prompt_json(value, 40, source_path=source)
    marker = json.loads(marker_text)

    assert marker == {
        "_prompt_truncated": True,
        "must_read_full_json": str(source.resolve()),
        "original_json_chars": len(json.dumps(value, ensure_ascii=False, indent=2)),
    }
    assert json.loads(source.read_text(encoding="utf-8")) == value


def test_environment_plan_rejects_invalid_generated_conda_name(tmp_path):
    issues = autonomous_deploy.validate_environment_plan(
        {
            "status": "ready_to_execute",
            "env_name": "invalid env/name",
            "commands": [{"phase": "verify", "command": ["python", "-c", "print(1)"], "required": True}],
        },
        require_full_reproduction=False,
        repo_path=tmp_path,
        run_dir=tmp_path,
    )
    assert "环境计划 env_name 只能包含字母、数字、点、下划线和连字符" in issues


def test_environment_controller_keeps_one_session_and_reports_only_real_queue(monkeypatch, tmp_path):
    project_root = _controller_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(claude_runner, "_invoke_controller", _fake_controller_call)
    queue_events: list[dict] = []

    first = claude_runner.run_controller_message(
        project="demo",
        project_root=project_root,
        message="first",
        on_queued=queue_events.append,
    )
    second = claude_runner.run_controller_message(
        project="demo",
        project_root=project_root,
        message="second",
        on_queued=queue_events.append,
    )

    assert queue_events == []
    assert first["session_id"] == second["session_id"]
    assert [turn["instruction"] for turn in second["conversation"]] == ["first", "second"]
    assert "queued_timeout" not in inspect_source(claude_runner.run_controller_message)


def inspect_source(function) -> str:
    import inspect

    return inspect.getsource(function)


def test_environment_controller_busy_message_has_no_wait_deadline(monkeypatch, tmp_path):
    project_root = _controller_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(claude_runner, "_invoke_controller", _fake_controller_call)
    controller_dir = claude_runner._controller_dir("demo")
    with claude_runner._state_lock(controller_dir):
        state = claude_runner._controller_state(controller_dir, "demo", project_root)
        state["busy"] = True
        claude_runner._save_controller_state(controller_dir, state)
    queue_events: list[dict] = []

    result = claude_runner.run_controller_message(
        project="demo",
        project_root=project_root,
        message="wait for the same controller",
        on_queued=queue_events.append,
    )

    assert result["status"] == "completed"
    assert queue_events and queue_events[0]["status"] == "queued"


def test_environment_controller_cancellation_removes_queued_message(monkeypatch, tmp_path):
    project_root = _controller_workspace(tmp_path, monkeypatch)
    controller_dir = claude_runner._controller_dir("demo")
    message_id = "cancel-me"
    with claude_runner._state_lock(controller_dir):
        state = claude_runner._controller_state(controller_dir, "demo", project_root)
        state["queue"] = [{
            "message_id": message_id,
            "project": "demo",
            "message": "do not run this",
            "status": "queued",
            "created_at": claude_runner.utc_now(),
            "was_queued": True,
        }]
        claude_runner._save_controller_state(controller_dir, state)

    claude_runner._cancel_controller_message(controller_dir, "demo", project_root, message_id)

    state = claude_runner.read_json(controller_dir / "controller.json", {})
    result = claude_runner.read_json(controller_dir / "messages" / f"{message_id}.json", {})
    assert state["queue"][0]["status"] == "cancelled"
    assert result["status"] == "cancelled"
    assert result["return_code"] == 130


def test_environment_interrupt_does_not_leave_stale_marker(monkeypatch, tmp_path):
    project_root = _controller_workspace(tmp_path, monkeypatch)
    controller_dir = claude_runner._controller_dir("demo")
    with claude_runner._state_lock(controller_dir):
        state = claude_runner._controller_state(controller_dir, "demo", project_root)
        state["active_pid"] = 999999
        claude_runner._save_controller_state(controller_dir, state)
    monkeypatch.setattr(claude_runner, "_terminate_controller_pid", lambda _pid: False)

    assert claude_runner._interrupt_active(controller_dir, "priority-message") is False
    state = claude_runner.read_json(controller_dir / "controller.json", {})
    assert state.get("interrupt_requested_by") == ""


def test_environment_hard_command_gate_remains_active():
    assert command_is_dangerous(["rm", "-rf", "/"])
    assert command_is_dangerous(["bash", "-c", "echo unsafe"])
    assert command_is_dangerous(["python", "-c", "print('safe')"]) == ""


def test_environment_run_pruning_preserves_project_handoff(monkeypatch, tmp_path):
    module = _load_environment_main()
    workspace = tmp_path / "workspace"
    runs_root = tmp_path / "runtime" / "runs"
    project = workspace / "projects" / "demo"
    (project / "state").mkdir(parents=True)
    runs_root.mkdir(parents=True)
    monkeypatch.setattr(module, "ROOT", workspace)
    monkeypatch.setattr(module, "RUNTIME_ROOT", runs_root.parent)
    monkeypatch.setattr(module, "RUNTIME_RUNS_ROOT", runs_root)
    monkeypatch.setattr(module, "LATEST_RUN_REVIEW_DIR", runs_root.parent / "latest_run")

    run_dirs = []
    for index in range(8):
        run_dir = runs_root / f"run_{index}"
        run_dir.mkdir()
        module._write_json(run_dir / "run_meta.json", {
            "requested_project": "demo",
            "action": "deploy_from_plan",
            "status": "complete",
        })
        module._write_json(run_dir / "environment_deployment_decision.json", {"decision": "continue_repair"})
        os.utime(run_dir, (index + 1, index + 1))
        run_dirs.append(run_dir)
    module.LATEST_RUN_REVIEW_DIR.symlink_to(run_dirs[-1], target_is_directory=True)
    module._write_json(project / "state" / "environment_handoff.json", {
        "environment_run_id": run_dirs[0].name,
        "module_run_dir": str(run_dirs[0]),
    })

    module._prune_completed_runs()

    remaining = {path.name for path in runs_root.iterdir() if path.is_dir()}
    assert remaining == {"run_0", "run_3", "run_4", "run_5", "run_6", "run_7"}


def test_environment_status_reports_handoff_state_not_file_read_success(monkeypatch, tmp_path, capsys):
    module = _load_environment_main()
    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "demo"
    project.mkdir(parents=True)
    runs_root = tmp_path / "runtime" / "runs"
    run_dir = runs_root / "blocked_run"
    run_dir.mkdir(parents=True)
    module._write_json(run_dir / "run_meta.json", {
        "requested_project": "demo",
        "action": "deploy_from_plan",
        "status": "complete",
    })
    module._write_json(run_dir / "environment_deployment_decision.json", {
        "decision": "continue_repair",
        "environment_handoff": {
            "ready_for_experimenting": False,
            "handoff_gate": {"passed": False, "missing": ["conda"]},
        },
    })
    monkeypatch.setattr(module, "ROOT", workspace)
    monkeypatch.setattr(module, "RUNTIME_ROOT", runs_root.parent)
    monkeypatch.setattr(module, "RUNTIME_RUNS_ROOT", runs_root)
    monkeypatch.setattr(module, "LATEST_RUN_REVIEW_DIR", runs_root.parent / "latest_run")

    assert module._print_status(["--project", "demo"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["read_status"] == "ok"
    assert payload["status"] == "not_ready_for_experimenting"


def test_framework_sync_closes_generated_conda_handoff(tmp_path):
    projects_root = tmp_path / "projects"
    project_root = projects_root / "demo"
    (project_root / "state").mkdir(parents=True)
    (project_root / "project.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
    environment_root = tmp_path / "environment"
    run_dir = environment_root / ".runtime" / "runs" / "env_run"
    prefix = run_dir / "conda_envs" / "generated_env"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "python").write_text("", encoding="utf-8")
    repo = run_dir / "repos" / "repo"
    repo.mkdir(parents=True)
    decision = {
        "decision": "environment_ready",
        "exit_code": 30,
        "environment_handoff": {
            "policy_version": environment_bridge.ENVIRONMENT_POLICY_VERSION,
            "run_id": run_dir.name,
            "ready_for_experimenting": True,
            "repo": {"repo_url": "https://github.com/example/repo", "repo_path": str(repo), "head_commit": "abc"},
            "conda": {"env_name": "generated_env", "prefix": str(prefix), "python": str(prefix / "bin" / "python")},
            "paper": {},
            "handoff_gate": {
                "policy_version": environment_bridge.ENVIRONMENT_POLICY_VERSION,
                "passed": True,
                "missing": [],
                "checks": [{"name": "required_commands", "passed": True}],
            },
        },
    }
    (run_dir / "environment_deployment_decision.json").write_text(json.dumps(decision), encoding="utf-8")

    result = environment_bridge.sync_environment_outputs(
        "demo",
        action="deploy_from_plan",
        result_payload={"run_dir": str(run_dir)},
        projects_root=projects_root,
        environment_root=environment_root,
    )
    config = json.loads((project_root / "project.json").read_text(encoding="utf-8"))

    assert result["ready_for_experimenting"] is True
    assert config["conda_env"] == "generated_env"
    assert config["runtime"]["conda_env_prefix"] == str(prefix)
    assert config["runtime"]["experiment_python"] == str(prefix / "bin" / "python")


def test_web_saved_conda_name_overrides_stale_runtime_value(tmp_path):
    projects_root = tmp_path / "projects"
    project_root = projects_root / "demo"
    project_root.mkdir(parents=True)
    (project_root / "project.json").write_text(json.dumps({
        "conda_env": "web_saved_env",
        "runtime": {"conda_env": "stale_runtime_env"},
    }), encoding="utf-8")

    assert environment_bridge.project_environment_conda_name("demo", projects_root=projects_root) == "web_saved_env"

    (project_root / "project.json").write_text(json.dumps({
        "conda_env": "",
        "runtime": {"conda_env": "stale_runtime_env"},
    }), encoding="utf-8")
    assert environment_bridge.project_environment_conda_name("demo", projects_root=projects_root) == ""


def test_framework_experiment_uses_environment_handoff_prefix(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    state = project_root / "state"
    state.mkdir(parents=True)
    prefix = tmp_path / "environment_run" / "conda_envs" / "paper_env"
    (prefix / "bin").mkdir(parents=True)
    python_path = prefix / "bin" / "python"
    python_path.write_text("", encoding="utf-8")
    config = {
        "conda_env": "paper_env",
        "runtime": {"conda_env_prefix": str(prefix), "experiment_python": str(python_path)},
    }
    config_path = project_root / "project.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (state / "environment_handoff.json").write_text(json.dumps({
        "valid": True,
        "repo_path": str(project_root),
        "environment_handoff": {
            "ready_for_experimenting": True,
            "conda": {"env_name": "paper_env", "prefix": str(prefix), "python": str(python_path)},
            "handoff_gate": {"passed": True, "missing": []},
        },
    }), encoding="utf-8")
    paths = type("Paths", (), {"root": project_root, "state": state, "config": config_path})()
    calls: list[list[str]] = []

    monkeypatch.setattr(run_project, "locate_conda_executable", lambda *_args, **_kwargs: "/fake/conda")
    monkeypatch.setattr(run_project.subprocess, "run", lambda command, **_kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0, "", ""))
    result, _display, mode = run_project.execute_trial_process(
        "demo",
        paths,
        ROOT / "framework" / "scripts",
        str(project_root),
        "paper_env",
        {"command_argv": ["python", "-c", "print(1)"]},
        {},
    )

    assert result.returncode == 0
    assert mode == "conda-prefix-run-argv"
    assert calls == [["/fake/conda", "run", "-p", str(prefix), str(python_path), "-c", "print(1)"]]


def test_framework_syncs_complete_environment_conversation(tmp_path):
    projects_root = tmp_path / "projects"
    project_root = projects_root / "demo"
    (project_root / "state").mkdir(parents=True)
    environment_root = tmp_path / "environment"
    run_dir = environment_root / ".runtime" / "runs" / "chat_run"
    run_dir.mkdir(parents=True)
    turns = [
        {"message_id": "one", "instruction": "first", "response_markdown": "reply one", "status": "completed", "session_id": "session", "web_visible_response": True},
        {"message_id": "two", "instruction": "second", "response_markdown": "reply two", "status": "completed", "session_id": "session", "web_visible_response": True},
    ]
    (run_dir / "environment_chat_result.json").write_text(json.dumps({
        "status": "completed",
        "message_id": "two",
        "controller_result": {**turns[-1], "conversation": turns},
    }), encoding="utf-8")

    environment_bridge.sync_environment_outputs(
        "demo",
        action="chat",
        result_payload={"run_dir": str(run_dir)},
        projects_root=projects_root,
        environment_root=environment_root,
    )
    history = json.loads((project_root / "state" / "environment_controller_history.json").read_text(encoding="utf-8"))
    assert [turn["instruction"] for turn in history["turns"]] == ["first", "second"]


def test_web_accepts_environment_reject_as_blocked(monkeypatch, tmp_path):
    project_root = tmp_path / "projects" / "demo"
    project_root.mkdir(parents=True)
    monkeypatch.setattr(project_bridge, "PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(project_bridge, "build_command", lambda _payload: ("demo", [sys.executable, "-c", "raise SystemExit(20)"]))
    monkeypatch.setattr(project_bridge, "interactive_env", lambda *_args, **_kwargs: os.environ.copy())
    monkeypatch.setattr(project_bridge, "upsert_agent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(project_bridge, "append_agent_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(project_bridge, "project_summary", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(project_bridge, "_framework_public_status_for_project", lambda *_args, **_kwargs: {})
    progress: list[tuple[str, int, int, str]] = []

    result = project_bridge.run_action(
        {"project": "demo", "action": "environment"},
        lambda _message: None,
        lambda: False,
        lambda *args: progress.append(args),
    )

    assert result["returncode"] == 20
    assert result["status"] == "blocked"
    assert any(item[0] == "blocked" for item in progress)


def test_web_process_output_loop_remains_cancel_responsive_without_output():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    started = time.monotonic()
    iterator = project_bridge._responsive_process_lines(proc)
    try:
        assert next(iterator) == ""
        assert time.monotonic() - started < 1.0
    finally:
        proc.terminate()
        proc.wait(timeout=5)
