from __future__ import annotations

import json
import os
import sys
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
    (root / "state" / "full_research_cycle.json").write_text(json.dumps({"status": "blocked_fresh_base_data_required"}), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "project_target_venue", lambda project, default="": default or "ICLR")
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")
    monkeypatch.setattr(project_bridge, "_literature_recommendation_gate_is_blocked", lambda project: False)
    monkeypatch.setattr(project_bridge, "_fresh_base_data_is_blocked", lambda project: False)

    _project, cmd = project_bridge.build_command({"project": "demo", "action": "experiment", "venue": "ICLR", "iterations": 1, "skip_claude": True})
    module_arg = cmd[cmd.index("--module-arg") + 1]
    assert "blocked_fresh_base_gate_required" not in " ".join(cmd)
    assert f"--repo-path {handoff_repo}" in module_arg
    assert f"--conda-env {handoff_env}" in module_arg




def test_web_summary_does_not_report_not_started_after_synthetic_experiment(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    repo = root / "repos" / "selected" / "repo"
    env_prefix = tmp_path / "environment" / "conda_envs" / "protdis_env"
    (env_prefix / "bin").mkdir(parents=True)
    (env_prefix / "bin" / "python").write_text("", encoding="utf-8")
    (root / "state" / "environment_handoff.json").write_text(json.dumps({
        "status": "ready_for_experimenting",
        "valid": True,
        "repo_path": str(repo),
        "conda_env_prefix": str(env_prefix),
        "environment_handoff": {
            "ready_for_experimenting": True,
            "repo": {"repo_path": str(repo), "repo_url": "https://github.com/example/repo"},
            "conda": {"prefix": str(env_prefix), "python": str(env_prefix / "bin" / "python")},
            "data": {"run_data_dir": str(tmp_path / "environment" / "data")},
        },
    }), encoding="utf-8")
    (root / "state" / "full_research_cycle.json").write_text(json.dumps({"status": "not_started", "summary": "旧 not_started"}), encoding="utf-8")
    (root / "state" / "experiment_registry.json").write_text(json.dumps([
        {
            "timestamp": "2026-06-21T07:48:10Z",
            "run_id": "demo_run",
            "experiment_id": "plan_5",
            "status": "success",
            "method": "experiment",
            "dataset": "synthetic_demo",
            "repo_path": str(repo),
            "artifact_path": str(tmp_path / "runtime" / "iteration_01"),
            "metrics": {"best_monitor_loss": 16.5881},
            "acceptance_status": "accepted",
            "decision": "synthetic_only",
        }
    ], ensure_ascii=False), encoding="utf-8")
    (root / "state" / "experiment_record_table.json").write_text(json.dumps({
        "row_count": 1,
        "columns": ["数据集", "指标"],
        "rows": [{"数据集": "synthetic demo", "指标": "best_monitor_loss=16.5881"}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "project_runtime_config", lambda project, cfg: {"conda_env": str(env_prefix), "experiment_python": str(env_prefix / "bin" / "python")})
    monkeypatch.setattr(project_bridge, "_framework_public_status_for_project", lambda project: {})
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda project, root: {})

    summary = project_bridge._fast_project_summary("demo", root, json.loads((root / "project.json").read_text(encoding="utf-8")))

    assert summary["status"] == "blocked_real_data_experiment_required"
    assert "实验 smoke 已完成" in summary["summary"]
    assert summary["current_blocker"]["category"] == "real_data_experiment_required"
    assert summary["current_blocker"]["title"] == "需要真实数据实验"
    assert "真实数据实验" in summary["current_blocker"]["next_action"]
    assert "投稿准备度" not in summary["current_blocker"]["next_action"]
    assert summary["state"]["show_synthetic_smoke_warning"] is True
    assert summary["state"]["experiment_count"] == 1

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

def test_web_full_cycle_job_logs_hide_stale_reference_goal():
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    sys.path.insert(0, str(reading_scripts))
    sys.modules.pop("pipeline", None)
    from auto_research.web import server as web_server

    rows = web_server._public_full_cycle_job_logs(
        [
            "当前目标：当前科研门控未通过，需继续补齐证据。",
            "下一步：Run audited Rigidity-Aware reference reproduction before paper writing.",
        ],
        {"phase": "ready_for_experimenting", "message": "环境已交付：真实仓库、run-local Conda、数据准备和 loader/model smoke 已通过；论文指标仍由实验阶段验证。"},
        {"project": "demo", "status": "ready_for_experimenting", "summary": "环境已交付：真实仓库、run-local Conda、数据准备和 loader/model smoke 已通过；论文指标仍由实验阶段验证。", "process_alive": False},
    )

    text = "\n".join(rows)
    assert "Run audited Rigidity-Aware" not in text
    assert "使用 handoff repo/env 进入 experimenting" in text


def test_web_handoff_experiment_launch_ignores_environment_worker(monkeypatch, tmp_path):
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    sys.path.insert(0, str(reading_scripts))
    sys.modules.pop("pipeline", None)
    from auto_research.web import server as web_server

    web_server._JOB_LIST_PROJECT_SUMMARY_CACHE.clear()
    monkeypatch.setattr(web_server, "_safe_project_root", lambda project: tmp_path)
    monkeypatch.setattr(
        web_server,
        "_active_project_child_processes",
        lambda project, root, phase_hint="": [
            {"pid": "123", "phase": "environment", "kind": "environment_stage", "elapsed": "01:00", "cmd": "run_environment_stage.py"}
        ],
    )
    monkeypatch.setattr(
        web_server,
        "project_summary",
        lambda project, compact=True: {
            "status": "ready_for_experimenting",
            "stages": {"environment": {"status": "ready_for_experimenting"}},
        },
    )

    assert web_server._project_stage_running_blocker({"project": "demo", "action": "experiment"}, "experiment") is None
    assert web_server._project_stage_running_blocker({"project": "demo", "action": "environment"}, "environment") is not None


def test_web_environment_worker_uses_command_run_id_instead_of_current_find(monkeypatch, tmp_path):
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    sys.path.insert(0, str(reading_scripts))
    sys.modules.pop("pipeline", None)
    from auto_research.web import server as web_server

    monkeypatch.setattr(web_server, "_pid_alive_local", lambda pid: True)
    row = web_server._active_project_worker_job(
        "demo",
        tmp_path,
        {
            "pid": "123",
            "phase": "environment",
            "kind": "environment_stage",
            "elapsed": "00:10",
            "cmd": "python modules/environment/main.py --action deploy_from_plan --run-id web_environment_demo_20260621T054118Z",
        },
        {},
        {"run_id": "find_current"},
        {"run_id": "find_current"},
    )

    assert row["stage"] == "environment"
    assert row["run_id"] == "web_environment_demo_20260621T054118Z"
    assert row["result"]["run_id"] == "web_environment_demo_20260621T054118Z"
    assert "find_current" not in json.dumps(row, ensure_ascii=False)


def test_web_environment_decision_does_not_fallback_when_explicit_run_missing(monkeypatch, tmp_path):
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    sys.path.insert(0, str(reading_scripts))
    sys.modules.pop("pipeline", None)
    from auto_research.web import server as web_server

    monkeypatch.setattr(web_server, "WORKSPACE_ROOT", tmp_path)
    old_run = tmp_path / "modules" / "environment" / "runs" / "web_environment_demo_20260621T053000Z"
    old_run.mkdir(parents=True)
    (old_run / "environment_deployment_decision.json").write_text(json.dumps({
        "run_id": "web_environment_demo_20260621T053000Z",
        "decision": "continue_repair",
        "exit_code": 30,
    }), encoding="utf-8")

    decision = web_server._environment_decision_for_job(
        "web_environment_demo_20260621T054118Z",
        {"project": "demo"},
        "2026-06-21T05:41:18Z",
    )

    assert decision == {}


def test_web_jobs_merges_live_environment_run_id_into_persisted_running_job(monkeypatch):
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    sys.path.insert(0, str(reading_scripts))
    sys.modules.pop("pipeline", None)
    from auto_research.web import server as web_server

    web_server._LIVE_JOBS_CACHE.clear()
    monkeypatch.setattr(web_server, "_reconcile_detached_launcher_jobs", lambda dynamic=None: None)
    monkeypatch.setattr(web_server, "_reconcile_stale_cancelling_jobs", lambda: None)
    monkeypatch.setattr(web_server, "_find_run_history_jobs_from_runs", lambda *args, **kwargs: [])
    monkeypatch.setattr(web_server, "_current_find_downstream_stage_history_jobs", lambda *args, **kwargs: [])
    monkeypatch.setattr(web_server, "_environment_decision_public_projection", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        web_server,
        "_live_jobs_from_projects",
        lambda compact=True: [{
            "job_id": "project-worker_demo_123",
            "stage": "environment",
            "status": "running",
            "run_id": "web_environment_demo_20260621T060831Z",
            "result": {"project": "demo", "phase": "environment", "kind": "environment_stage", "status": "running", "run_id": "web_environment_demo_20260621T060831Z"},
            "progress": {"phase": "environment", "message": "environment worker running"},
        }],
    )
    job = web_server.JobState("environment_web", "environment")
    job.status = "running"
    job.created_at = "2026-06-21T06:08:31Z"
    job.result = {"project": "demo", "status": "running", "action": "environment"}
    monkeypatch.setattr(web_server, "JOBS", {job.job_id: job})

    rows = web_server.api_jobs(compact=True, limit=10, include_history=True, project="demo")

    env_rows = [row for row in rows if row.get("job_id") == "environment_web"]
    assert len(env_rows) == 1
    assert env_rows[0]["run_id"] == "web_environment_demo_20260621T060831Z"
    assert env_rows[0]["result"]["run_id"] == "web_environment_demo_20260621T060831Z"


def test_web_jobs_hides_stale_environment_history_when_live_environment_running(monkeypatch):
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    sys.path.insert(0, str(reading_scripts))
    sys.modules.pop("pipeline", None)
    from auto_research.web import server as web_server

    web_server._LIVE_JOBS_CACHE.clear()
    monkeypatch.setattr(web_server, "_reconcile_detached_launcher_jobs", lambda dynamic=None: None)
    monkeypatch.setattr(web_server, "_reconcile_stale_cancelling_jobs", lambda: None)
    monkeypatch.setattr(web_server, "_find_run_history_jobs_from_runs", lambda *args, **kwargs: [])
    monkeypatch.setattr(web_server, "_current_find_downstream_stage_history_jobs", lambda *args, **kwargs: [])
    monkeypatch.setattr(web_server, "_environment_decision_public_projection", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        web_server,
        "project_summary",
        lambda project, compact=True: {"status": "environment_running", "stages": {"environment": {"status": "running"}}},
    )
    live = {
        "job_id": "project-worker_demo_123",
        "stage": "environment",
        "status": "running",
        "created_at": "2026-06-21T05:41:18Z",
        "run_id": "web_environment_demo_20260621T054118Z",
        "result": {"project": "demo", "phase": "environment", "kind": "environment_stage", "status": "running", "process_alive": True},
        "progress": {"phase": "environment", "message": "environment worker running"},
        "logs": ["project=demo", "stage=environment"],
    }
    monkeypatch.setattr(web_server, "_live_jobs_from_projects", lambda compact=True: [live])

    stale = web_server.JobState("environment_old", "environment")
    stale.status = "blocked"
    stale.created_at = "2026-06-21T05:21:29Z"
    stale.run_id = "web_environment_demo_20260621T052135Z"
    stale.result = {"project": "demo", "status": "blocked", "action": "environment"}
    monkeypatch.setattr(web_server, "JOBS", {stale.job_id: stale})

    rows = web_server.api_jobs(compact=True, limit=10, include_history=True, project="demo")

    assert [row["job_id"] for row in rows] == ["project-worker_demo_123"]
    assert rows[0]["run_id"] == "web_environment_demo_20260621T054118Z"


def test_web_jobs_lists_handoff_environment_worker_as_nonexclusive(monkeypatch):
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    sys.path.insert(0, str(reading_scripts))
    sys.modules.pop("pipeline", None)
    from auto_research.web import server as web_server

    web_server._JOB_LIST_PROJECT_SUMMARY_CACHE.clear()
    monkeypatch.setattr(
        web_server,
        "project_summary",
        lambda project, compact=True: {
            "status": "ready_for_experimenting",
            "stages": {"environment": {"status": "ready_for_experimenting"}, "experiment": {"status": "ready_for_experimenting"}},
        },
    )

    row = web_server._compact_job_for_list({
        "job_id": "project-worker_demo_123",
        "stage": "environment",
        "status": "running",
        "created_at": "2026-06-20T22:00:00Z",
        "logs": [
            "project=demo",
            "stage=environment",
            "process_alive=true",
            "worker_kind=environment_stage",
            "full_cycle_log=/tmp/full_research_cycle.log",
        ],
        "log_count": 4,
        "result": {
            "project": "demo",
            "pid": "123",
            "phase": "environment",
            "raw_stage": "environment_stage",
            "kind": "environment_stage",
            "summary": "项目后台 worker 正在运行。",
            "status": "running",
            "process_alive": True,
            "not_full_cycle_controller": True,
        },
        "progress": {"phase": "environment", "message": "environment worker running; PID=123"},
    })

    assert row["stage"] == "handoff_monitor"
    assert row["stage"] not in web_server.PROJECT_STAGE_EXCLUSIVE_PHASES
    assert row["status"] == "running"
    assert row["result"]["phase"] == "environment"
    assert row["result"]["exclusive_stage"] is False
    assert "不阻塞实验入口" in row["progress"]["message"]


def test_web_environment_decision_projection_supports_environment_ready(monkeypatch):
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    sys.path.insert(0, str(reading_scripts))
    sys.modules.pop("pipeline", None)
    from auto_research.web import server as web_server

    monkeypatch.setattr(web_server, "_environment_decision_for_job", lambda *args, **kwargs: {
        "run_id": "web_environment_demo_20260621T061924Z",
        "decision": "environment_ready",
        "exit_code": 0,
        "allow_next_module": False,
        "ready_for_experimenting": True,
        "environment_handoff": {"ready_for_experimenting": True},
        "workspace_write_audit": {"status": "passed"},
    })

    projection = web_server._environment_decision_public_projection("environment_demo", "", {"project": "demo"}, "2026-06-21T06:19:22Z")

    assert projection["status"] == "ready_for_experimenting"
    assert projection["decision"] == "environment_ready"
    assert projection["exit_code"] == 0
    assert "环境已交付" in projection["summary"]
    assert "停在可修复真实门控" not in projection["summary"]


def test_web_jobs_maps_handoff_ready_status_to_done_for_frontend():
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    sys.path.insert(0, str(reading_scripts))
    sys.modules.pop("pipeline", None)
    from auto_research.web import server as web_server

    row = web_server._compact_job_for_list({
        "job_id": "full-cycle_demo",
        "stage": "full-cycle",
        "status": "ready_for_experimenting",
        "created_at": "2026-06-20T22:00:00Z",
        "logs": ["当前目标：使用 handoff repo/env 进入 experimenting"],
        "log_count": 1,
        "result": {
            "project": "demo",
            "status": "ready_for_experimenting",
            "summary": "环境已交付：真实仓库、run-local Conda、数据准备和 loader/model smoke 已通过；论文指标仍由实验阶段验证。",
            "process_alive": False,
        },
        "progress": {"phase": "ready_for_experimenting", "current": 1, "total": 1, "percent": 100},
    })

    assert row["status"] == "done"
    assert row["result"]["status"] == "ready_for_experimenting"
    assert row["progress"]["phase"] == "ready_for_experimenting"


def test_web_jobs_maps_experiment_acceptance_blocker_to_blocked_status():
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    sys.path.insert(0, str(reading_scripts))
    sys.modules.pop("pipeline", None)
    from auto_research.web import server as web_server

    message = "实验迭代被验收门控阻断：Claude Code 未获准执行必要 Bash/Python 命令；本轮不得计为科研成功。"
    row = web_server._compact_job_for_list({
        "job_id": "experiment_demo",
        "stage": "experiment",
        "status": "blocked",
        "created_at": "2026-06-20T23:10:00Z",
        "logs": ["当前状态：" + message],
        "log_count": 1,
        "result": {
            "project": "demo",
            "panel_stage": "experiment",
            "status": "blocked_claude_permission_denied",
            "acceptance_status": "blocked_claude_permission_denied",
            "summary": message,
        },
        "progress": {"phase": "blocked", "current": 1, "total": 1, "percent": 100, "message": message},
    })

    assert row["stage"] == "experiment"
    assert row["status"] == "blocked"
    assert row["result"]["status"] == "blocked"
    assert row["result"]["acceptance_status"] == "blocked_claude_permission_denied"
    assert "不得计为科研成功" in row["progress"]["message"]



def test_web_jobs_projects_generic_experiment_error_from_registry(monkeypatch, tmp_path):
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    sys.path.insert(0, str(reading_scripts))
    sys.modules.pop("pipeline", None)
    from auto_research.web import server as web_server

    monkeypatch.setattr(web_server, "WORKSPACE_ROOT", tmp_path)
    registry_dir = tmp_path / "modules" / "experimenting" / "runtime" / "web" / "demo" / "state"
    registry_dir.mkdir(parents=True)
    artifact_dir = tmp_path / "modules" / "experimenting" / "runtime" / "web" / "demo" / "runs" / "demo_run" / "iteration_01"
    artifact_dir.mkdir(parents=True)
    (registry_dir / "experiment_registry.json").write_text(
        json.dumps(
            [
                {
                    "timestamp": "2026-06-20T23:55:38Z",
                    "run_id": "demo_run",
                    "project": "demo",
                    "status": "failed",
                    "artifact_path": str(artifact_dir),
                    "acceptance_status": "blocked_generation_evaluation_pipeline_missing",
                    "acceptance_blockers": [
                        {"code": "missing_generation_pipeline", "message": "No generation script."},
                        {"code": "missing_evaluation_pipeline", "message": "No evaluation script."},
                    ],
                    "experiment_iteration_summary_status": "completed",
                    "experiment_iteration_summary_acceptance_status": "partial_with_generation_blocker",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    row = web_server._compact_job_for_list(
        {
            "job_id": "experiment_demo",
            "stage": "experiment",
            "status": "error",
            "created_at": "2026-06-20T23:48:41Z",
            "logs": ["当前状态：research action failed with exit code 1"],
            "log_count": 1,
            "result": {"project": "demo", "panel_stage": "experiment", "status": "running", "action": "experiment"},
            "progress": {"phase": "error", "current": 0, "total": 1, "percent": 0, "message": "research action failed with exit code 1"},
            "error": "research action failed with exit code 1",
        }
    )

    assert row["stage"] == "experiment"
    assert row["status"] == "blocked"
    assert row["result"]["status"] == "blocked"
    assert row["result"]["acceptance_status"] == "blocked_generation_evaluation_pipeline_missing"
    assert "缺少生成/采样和评估流水线" in row["progress"]["message"]

def test_web_source_status_marks_partial_openreview_as_limited():
    row = {
        "source": "ICLR",
        "ok": True,
        "adapter": "openreview_cache",
        "metadata_completeness_status": "partial",
        "metadata_completeness_ok": False,
        "metadata_completeness_limited": True,
        "title_index_completeness_ok": False,
        "title_index_complete": False,
        "has_official_categories": True,
        "has_abstracts": True,
        "has_abstracts_in_title_index": True,
        "source_verified": True,
        "source_scope": "official_openreview_metadata",
    }

    assert project_bridge._venue_source_public_limited(row) is True


def test_web_jobs_keeps_only_latest_persisted_environment_history(monkeypatch):
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    sys.path.insert(0, str(reading_scripts))
    sys.modules.pop("pipeline", None)
    from auto_research.web import server as web_server

    web_server._LIVE_JOBS_CACHE.clear()
    monkeypatch.setattr(web_server, "_reconcile_detached_launcher_jobs", lambda dynamic=None: None)
    monkeypatch.setattr(web_server, "_reconcile_stale_cancelling_jobs", lambda: None)
    monkeypatch.setattr(web_server, "_live_jobs_from_projects", lambda compact=True: [])
    monkeypatch.setattr(web_server, "_find_run_history_jobs_from_runs", lambda *args, **kwargs: [])
    monkeypatch.setattr(web_server, "_current_find_downstream_stage_history_jobs", lambda *args, **kwargs: [])
    monkeypatch.setattr(web_server, "_environment_decision_public_projection", lambda *args, **kwargs: {})

    old = web_server.JobState("environment_old", "environment")
    old.status = "blocked"
    old.created_at = "2026-06-21T05:21:29Z"
    old.run_id = "web_environment_demo_20260621T052135Z"
    old.result = {"project": "demo", "status": "blocked", "summary": "旧 success_criteria 空数组"}
    new = web_server.JobState("environment_new", "environment")
    new.status = "blocked"
    new.created_at = "2026-06-21T05:55:29Z"
    new.run_id = "web_environment_demo_20260621T054118Z"
    new.result = {"project": "demo", "status": "blocked", "summary": "新 import biopython blocker"}
    monkeypatch.setattr(web_server, "JOBS", {old.job_id: old, new.job_id: new})

    rows = web_server.api_jobs(compact=True, limit=10, include_history=True, project="demo")

    env_rows = [row for row in rows if row.get("stage") == "environment"]
    assert [row["job_id"] for row in env_rows] == ["environment_new"]
    assert "success_criteria 空数组" not in json.dumps(rows, ensure_ascii=False)


def test_web_current_find_pending_read_blocker_is_not_environment_ready():
    blocker = project_bridge._current_find_pipeline_public_blocker({
        "status": "pending_current_find_read",
        "recommended_count": 20,
        "recommended_reading_count": 20,
        "full_text_evidence_count": 0,
        "pending_full_text_reading_count": 20,
    })

    assert blocker["category"] == "pending_current_find_read"
    assert "Read 精读尚未运行" in blocker["summary"]
    assert "环境阶段" not in blocker["summary"]
    assert "Read 完成前不能进入 Idea、Plan、环境、实验或写作" in blocker["next_action"]


def test_web_current_find_read_history_supersedes_stale_blocked_read_job(monkeypatch, tmp_path):
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    sys.path.insert(0, str(reading_scripts))
    sys.modules.pop("pipeline", None)
    from auto_research.web import server as web_server

    projects = tmp_path / "projects"
    root = projects / "demo"
    (root / "state").mkdir(parents=True)
    (root / "planning" / "finding").mkdir(parents=True)
    run_id = "find_current"
    (root / "state" / "current_find_research_plan.json").write_text(json.dumps({
        "status": "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection",
        "run_id": run_id,
        "generated_at": "2026-06-21T05:00:00Z",
        "current_find_reading_count": 20,
        "reading_validation": {
            "valid": True,
            "recommended_reading_count": 20,
            "full_text_reading_count": 20,
            "pending_full_text_reading_count": 0,
        },
    }), encoding="utf-8")
    (root / "planning" / "finding" / "read_results.json").write_text(json.dumps({
        "run_id": run_id,
        "generated_at": "2026-06-21T05:01:00Z",
        "readings": [{"title": f"paper {idx}", "full_text_available": True} for idx in range(20)],
    }), encoding="utf-8")

    monkeypatch.setattr(web_server, "PROJECT_IDS_ROOT", projects)
    stale = {
        "job_id": "read_stale",
        "stage": "read",
        "status": "blocked",
        "created_at": "2026-06-21T02:00:00Z",
        "run_id": run_id,
        "result": {"project": "demo", "run_id": run_id, "status": "blocked_current_find_claude_read_failed"},
        "progress": {"message": "当前 Find 仍有 20 篇缺少同篇全文证据"},
    }

    synthetic = web_server._current_find_downstream_stage_history_jobs("demo", existing_items=[stale])
    read_rows = [row for row in synthetic if row.get("stage") == "read"]
    assert len(read_rows) == 1
    assert read_rows[0]["status"] == "done"
    assert "全文精读合格 20 篇，待补 0 篇" in read_rows[0]["progress"]["message"]

    collapsed = web_server._collapse_current_find_read_retry_jobs([stale] + synthetic, project_hint="demo")
    collapsed_read_rows = [row for row in collapsed if row.get("stage") == "read"]
    assert len(collapsed_read_rows) == 1
    assert collapsed_read_rows[0]["job_id"].startswith("current-find-read_")
    assert collapsed_read_rows[0]["status"] == "done"
    assert "缺少同篇全文证据" not in json.dumps(collapsed_read_rows[0], ensure_ascii=False)
