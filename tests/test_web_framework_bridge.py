from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from path_helpers import ensure_script_paths

ensure_script_paths()

from auto_research.web import project_bridge

ROOT = Path(__file__).resolve().parents[1]


def _make_project(projects: Path, name: str = "demo") -> Path:
    root = projects / name
    (root / "state").mkdir(parents=True)
    (root / "repos" / "selected" / "repo").mkdir(parents=True)
    (root / "project.json").write_text(json.dumps({"name": name, "topic": "demo topic", "conda_env": "demo_env", "target_venue": "ICLR"}), encoding="utf-8")
    (root / "state" / "experiment_plan.json").write_text(json.dumps({"project": name, "title": "demo plan", "repo_url": "https://github.com/example/repo"}), encoding="utf-8")
    (root / "state" / "active_repo.json").write_text(json.dumps({"repo_path": str(root / "repos" / "selected" / "repo"), "name": "example/repo"}), encoding="utf-8")
    return root


def test_web_environment_action_uses_framework_single_stage(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    _make_project(projects, "demo")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "project_target_venue", lambda project, default="": default or "ICLR")
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")

    project, cmd = project_bridge.build_command({"project": "demo", "action": "environment", "venue": "ICLR"})

    assert project == "demo"
    assert cmd[:3] == ["/env/bin/python", str(ROOT / "framework" / "scripts" / "orchestration" / "run_taste_framework.py"), "run"]
    assert "--only-stage" in cmd
    assert cmd[cmd.index("--only-stage") + 1] == "environment"
    module_arg = cmd[cmd.index("--module-arg") + 1]
    assert module_arg.startswith("environment=--plan ")
    assert "modules/environment/main.py" not in " ".join(cmd)


def test_web_experiment_action_uses_framework_and_module_runtime(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    repo = root / "repos" / "selected" / "repo"
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "project_target_venue", lambda project, default="": default or "ICLR")
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")
    monkeypatch.setattr(project_bridge, "_literature_recommendation_gate_is_blocked", lambda project: False)
    monkeypatch.setattr(project_bridge, "_fresh_base_data_is_blocked", lambda project: False)

    project, cmd = project_bridge.build_command({"project": "demo", "action": "experiment", "venue": "ICLR", "iterations": 2, "skip_claude": True})

    assert project == "demo"
    assert "--only-stage" in cmd
    assert cmd[cmd.index("--only-stage") + 1] == "experimenting"
    module_arg = cmd[cmd.index("--module-arg") + 1]
    assert module_arg.startswith("experimenting=--plan ")
    assert f"--repo-path {repo}" in module_arg
    assert "--conda-env demo_env" in module_arg
    assert "--output-root " in module_arg and "modules/experimenting/runtime/web/demo" in module_arg


def test_missing_plan_returns_human_readable_blocker(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = projects / "demo"
    root.mkdir(parents=True)
    (root / "project.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "project_target_venue", lambda project, default="": default)
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")

    _project, cmd = project_bridge.build_command({"project": "demo", "action": "environment"})

    assert cmd[:2] == ["/env/bin/python", "-c"]
    assert "缺少可执行实验计划" in cmd[2]


def test_web_experiment_action_prefers_environment_handoff(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    handoff_repo = tmp_path / "environment" / "repo"
    handoff_env = tmp_path / "environment" / "conda_envs" / "rigid"
    handoff_repo.mkdir(parents=True)
    (handoff_env / "bin").mkdir(parents=True)
    (handoff_env / "bin" / "python").write_text("", encoding="utf-8")
    (root / "state" / "environment_handoff.json").write_text(json.dumps({
        "status": "ready_for_experimenting",
        "valid": True,
        "repo_path": str(handoff_repo),
        "conda_env_prefix": str(handoff_env),
        "environment_handoff": {
            "repo": {"repo_path": str(handoff_repo), "repo_url": "https://github.com/example/repo"},
            "conda": {"prefix": str(handoff_env), "env_name": "rigid"},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "project_target_venue", lambda project, default="": default or "ICLR")
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")
    monkeypatch.setattr(project_bridge, "_literature_recommendation_gate_is_blocked", lambda project: False)
    monkeypatch.setattr(project_bridge, "_fresh_base_data_is_blocked", lambda project: False)

    _project, cmd = project_bridge.build_command({"project": "demo", "action": "experiment", "venue": "ICLR", "iterations": 1, "skip_claude": True})
    module_arg = cmd[cmd.index("--module-arg") + 1]
    assert f"--repo-path {handoff_repo}" in module_arg
    assert f"--conda-env {handoff_env}" in module_arg


def test_web_public_state_prefers_environment_handoff_runtime(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    handoff_repo = tmp_path / "environment" / "repo"
    handoff_env = tmp_path / "environment" / "conda_envs" / "rigid"
    handoff_repo.mkdir(parents=True)
    (handoff_env / "bin").mkdir(parents=True)
    (handoff_env / "bin" / "python").write_text("", encoding="utf-8")
    (root / "state" / "environment_handoff.json").write_text(json.dumps({
        "status": "ready_for_experimenting",
        "valid": True,
        "repo_path": str(handoff_repo),
        "conda_env_prefix": str(handoff_env),
        "experiment_python": str(handoff_env / "bin" / "python"),
        "selected": {
            "repo_path": str(handoff_repo),
            "local_path": str(handoff_repo),
            "fresh_find_run_id": "find_current",
            "selected_plan_id": "plan_current",
            "selection_stage": "environment_claude_code",
        },
        "environment_handoff": {
            "ready_for_experimenting": True,
            "run_id": "env_run",
            "repo": {"repo_path": str(handoff_repo), "repo_url": "https://github.com/example/repo", "head_commit": "abc"},
            "conda": {"prefix": str(handoff_env), "env_name": "rigid", "python": str(handoff_env / "bin" / "python")},
            "data": {"run_data_dir": str(tmp_path / "environment" / "data")},
            "pending_downstream_metrics": [{"metric": "designability", "status": "pending_experimenting_evaluation"}],
        },
    }), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)

    prefs = project_bridge._public_run_preferences("demo", root, {"conda_env": "old_env", "target_venue": "ICLR"}, selection={})
    merged_runtime = project_bridge._runtime_with_environment_handoff(root, {"conda_env": "old_env", "experiment_python": "/old/bin/python"})
    old_env_bin = "/home/fmh/workspace/miniforge/envs/protein_rigidssl_sm120/bin"
    monkeypatch.setattr(project_bridge, "project_runtime_config", lambda project, cfg: {"conda_env": "old_env", "experiment_python": "/old/bin/python"})
    monkeypatch.setattr(
        project_bridge,
        "interactive_env",
        lambda project, cfg: {"PATH": f"{old_env_bin}:{handoff_env / 'bin'}:/usr/bin"},
    )
    diagnostics = project_bridge._runtime_diagnostics_light("demo", {})
    env = project_bridge._current_environment_selection(root)

    assert prefs["conda_env"] == str(handoff_env)
    assert prefs["runtime"]["experiment_python"] == str(handoff_env / "bin" / "python")
    assert merged_runtime["conda_env"] == str(handoff_env)
    assert merged_runtime["experiment_python"] == str(handoff_env / "bin" / "python")
    assert diagnostics["runtime"]["conda_env"] == str(handoff_env)
    assert diagnostics["runtime"]["experiment_python"] == str(handoff_env / "bin" / "python")
    assert diagnostics["checks"]["experiment_python"]["path"] == str(handoff_env / "bin" / "python")
    assert diagnostics["path_head"][0] == str(handoff_env / "bin")
    assert old_env_bin not in diagnostics["path_head"]
    environment_stage = project_bridge._public_environment_stage(
        status="selected",
        env=env,
        selected=env.get("selected", {}),
        active_repo={},
        repo_name="example/repo",
        repo_url="https://github.com/example/repo",
        repo_path=str(handoff_repo),
        ref_gate={},
    )

    assert env["valid"] is True
    assert env["reason"] == "environment_handoff_ready_for_experimenting"
    assert env["selected"]["repo_path"] == str(handoff_repo)
    assert env["conda_env"] == str(handoff_env)
    assert environment_stage["status"] == "ready_for_experimenting"
    assert environment_stage["repo_status"] == "ready_for_experimenting"
    assert environment_stage["loader_status"] == "passed"
    assert "论文指标仍由实验阶段验证" in environment_stage["summary_zh"]



def test_web_pid_alive_does_not_spawn_ps(monkeypatch):
    def fail_subprocess_run(*_args, **_kwargs):
        raise AssertionError("_pid_alive must not spawn ps for each process")

    monkeypatch.setattr(project_bridge.subprocess, "run", fail_subprocess_run)
    assert project_bridge._pid_alive(os.getpid()) is True
    assert project_bridge._pid_alive(-1) is False
