from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from path_helpers import ensure_script_paths

ensure_script_paths()

from auto_research.web import project_bridge
import project_config

ROOT = Path(__file__).resolve().parents[1]


def _make_project(projects: Path, name: str = "demo") -> Path:
    root = projects / name
    (root / "state").mkdir(parents=True)
    (root / "repos" / "selected" / "repo").mkdir(parents=True)
    (root / "project.json").write_text(json.dumps({"name": name, "topic": "demo topic", "conda_env": "demo_env", "target_venue": "ICLR"}), encoding="utf-8")
    (root / "state" / "experiment_plan.json").write_text(json.dumps({"project": name, "title": "demo plan", "repo_url": "https://github.com/example/repo"}), encoding="utf-8")
    (root / "state" / "active_repo.json").write_text(json.dumps({"repo_path": str(root / "repos" / "selected" / "repo"), "name": "example/repo"}), encoding="utf-8")
    return root


def _source_status_fixture_markdown() -> str:
    return "\n".join([
        "# 来源状态",
        "",
        "## ICML 2026",
        "- 状态: normal / 标题总数: 6431 / 分类后: 6431 / 来源适配器: icml_downloads / 请求年份: 2026 / 有效年份: 2026 / 官方标题索引已核验 / 元数据完整 / 有官方分类 / 标题索引含摘要",
        "",
        "## ICLR 2026",
        "- 状态: normal / 标题总数: 5352 / 分类后: 5352 / 来源适配器: openreview_reference / 请求年份: 2026 / 有效年份: 2026 / OpenReview 官方元数据已核验 / 元数据完整 / 有官方分类 / 标题索引含摘要",
        "",
        "## NeurIPS 2025",
        "- 状态: normal / 标题总数: 5823 / 分类后: 5823 / 来源适配器: neurips_virtual / 请求年份: 2025 / 有效年份: 2025 / 官方标题索引已核验 / 元数据完整 / 有官方分类 / 标题索引含摘要",
        "",
        "## SIGKDD 2026",
        "- 状态: limited / 标题总数: 256 / 分类后: 256 / 来源适配器: dblp / 请求年份: 2026 / 有效年份: 2026 / 官方标题索引已核验 / 受限 / 无官方分类 / 无摘要",
        "",
    ])


def _ready_environment_handoff(repo_path: Path, conda_prefix: Path, *, data_dir: Path | None = None, run_id: str = "env_run", selected: dict | None = None) -> dict:
    checks = [
        {"name": name, "passed": True, "reason": "pytest handoff fixture"}
        for name in [
            "repository_source",
            "conda_environment",
            "machine_fit",
            "dataset_runtime",
            "required_commands",
            "runtime_smoke",
            "paper_config_alignment",
            "workspace_write_audit",
        ]
    ]
    policy = project_bridge.ENVIRONMENT_HANDOFF_POLICY_VERSION
    data_dir = data_dir or repo_path.parent / "data"
    payload = {
        "status": "ready_for_experimenting",
        "valid": True,
        "repo_path": str(repo_path),
        "conda_env_prefix": str(conda_prefix),
        "experiment_python": str(conda_prefix / "bin" / "python"),
        "environment_handoff": {
            "policy_version": policy,
            "ready_for_experimenting": True,
            "run_id": run_id,
            "repo": {"repo_path": str(repo_path), "repo_url": "https://github.com/example/repo", "head_commit": "abc"},
            "conda": {"prefix": str(conda_prefix), "env_name": conda_prefix.name, "python": str(conda_prefix / "bin" / "python")},
            "data": {"run_data_dir": str(data_dir)},
            "handoff_gate": {"policy_version": policy, "passed": True, "missing": [], "checks": checks},
        },
    }
    if selected:
        payload["selected"] = selected
    return payload


def test_runtime_projection_keeps_configured_conda_env_without_current_handoff(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    cfg = json.loads((root / "project.json").read_text(encoding="utf-8"))
    cfg["python_executable"] = "python3"
    cfg["runtime"] = {"conda_base": str(tmp_path / "miniforge"), "management_python": "", "experiment_python": "/stale/env/bin/python"}
    (root / "project.json").write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)

    import runtime_env

    runtime = runtime_env.project_runtime_config("demo", cfg)
    assert runtime["conda_env"] == "demo_env"
    assert Path(runtime["management_python"]).is_absolute()
    assert runtime["management_python"] != "python3"

    public = project_bridge._public_runtime_with_valid_handoff(root, runtime)
    assert public["conda_env"] == "demo_env"
    assert public["conda_base"] == str(tmp_path / "miniforge")
    assert "experiment_python" not in public


def test_environment_launch_runtime_excludes_stale_experiment_python_path(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    stale_python = tmp_path / "old_env" / "bin" / "python"
    stale_python.parent.mkdir(parents=True)
    stale_python.write_text("#!/bin/sh\n", encoding="utf-8")
    cfg = json.loads((root / "project.json").read_text(encoding="utf-8"))
    cfg["runtime"] = {
        "node_bin": "/opt/node/bin",
        "conda_base": str(tmp_path / "miniforge"),
        "experiment_python": str(stale_python),
    }
    (root / "project.json").write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.delenv("EXPERIMENT_PYTHON", raising=False)
    monkeypatch.delenv("PROJECT_PYTHON", raising=False)

    import runtime_env

    env = runtime_env.interactive_env("demo", cfg, include_experiment_python=False)

    assert str(stale_python.parent) not in env["PATH"].split(os.pathsep)[:8]
    assert "EXPERIMENT_PYTHON" not in env
    assert "PROJECT_PYTHON" not in env


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


def test_web_find_action_uses_framework_run_frontend(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    _make_project(projects, "demo")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")

    project, cmd = project_bridge.build_command({
        "project": "demo",
        "action": "find",
        "max_papers": 7,
        "max_ideas": 3,
        "skip_arxiv": True,
        "deep_survey": True,
        "queries": ["protein design"],
    })

    assert project == "demo"
    assert cmd[:4] == ["/env/bin/python", str(project_bridge.SCRIPTS / "run_frontend.py"), "--project", "demo"]
    assert "--max-papers" in cmd and cmd[cmd.index("--max-papers") + 1] == "7"
    assert "--max-ideas" in cmd and cmd[cmd.index("--max-ideas") + 1] == "3"
    assert "--skip-arxiv" in cmd
    assert "--deep-survey" in cmd
    assert "--query" in cmd and cmd[cmd.index("--query") + 1] == "protein design"


def test_project_bridge_runtime_env_overrides_are_llm_only():
    env = project_bridge._runtime_env_overrides_from_payload({
        "runtime_env": {
            "LLM_API_KEY": "secret",
            "OPENAI_API_KEY": "secret2",
            "LLM_API_BASE": "https://llm.example/v1",
            "PATH": "/tmp/unsafe",
            "PYTHONPATH": "/tmp/unsafe",
        }
    })

    assert env["LLM_API_KEY"] == "secret"
    assert env["OPENAI_API_KEY"] == "secret2"
    assert env["LLM_API_BASE"] == "https://llm.example/v1"
    assert "PATH" not in env
    assert "PYTHONPATH" not in env


def test_web_config_saves_llm_secret_to_local_finding_config(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    runtime_config = tmp_path / "runtime" / ".config.json"
    local_llm_config = tmp_path / "modules" / "finding" / "config" / "llm.local.json"
    monkeypatch.setenv("FINDING_LLM_CONFIG", str(local_llm_config))
    monkeypatch.setattr(web_server, "CONFIG_PATH", runtime_config)
    monkeypatch.setattr(web_server, "project_config_path", lambda: None)
    monkeypatch.setattr(web_server, "canonical_source_selection", lambda project_config_path=None: {})
    monkeypatch.setattr(web_server, "save_canonical_source_selection", lambda selection, project_config_path=None: {})

    config = web_server.AppConfig(
        provider="openai_compatible",
        base_url="https://llm.example.test/v1",
        model="generic-model",
        api_key="local-secret",
        temperature=0.2,
    )

    web_server.save_config(config)

    local_payload = json.loads(local_llm_config.read_text(encoding="utf-8"))
    runtime_payload = json.loads(runtime_config.read_text(encoding="utf-8"))
    assert local_payload["api_key"] == "local-secret"
    assert local_payload["provider"] == "openai_compatible"
    assert runtime_payload["api_key"] == ""

    loaded = web_server.load_config()
    public = web_server._public_config_response(loaded)
    assert loaded.api_key == "local-secret"
    assert public["api_key"] == ""
    assert public["api_key_saved"] is True


def test_web_find_mock_request_does_not_overwrite_local_llm_config(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    local_llm_config = tmp_path / "modules" / "finding" / "config" / "llm.local.json"
    local_llm_config.parent.mkdir(parents=True)
    local_llm_config.write_text(json.dumps({
        "provider": "openai_compatible",
        "base_url": "https://llm.example.test/v1",
        "model": "real-model",
        "api_key": "local-secret",
        "temperature": 0.3,
    }), encoding="utf-8")
    monkeypatch.setenv("FINDING_LLM_CONFIG", str(local_llm_config))

    mock_run_config = web_server.AppConfig(
        provider="mock",
        base_url="",
        model="mock-model",
        api_key="",
        temperature=0.2,
    )
    web_server._persist_local_llm_config_from_find_request(mock_run_config, mock_run_config)

    unchanged = json.loads(local_llm_config.read_text(encoding="utf-8"))
    assert unchanged["provider"] == "openai_compatible"
    assert unchanged["model"] == "real-model"
    assert unchanged["api_key"] == "local-secret"

    real_run_config = web_server.AppConfig(
        provider="openai_compatible",
        base_url="https://new.example.test/v1",
        model="new-model",
        api_key="new-secret",
        temperature=0.1,
    )
    web_server._persist_local_llm_config_from_find_request(real_run_config, real_run_config)

    updated = json.loads(local_llm_config.read_text(encoding="utf-8"))
    assert updated["provider"] == "openai_compatible"
    assert updated["base_url"] == "https://new.example.test/v1"
    assert updated["model"] == "new-model"
    assert updated["api_key"] == "new-secret"


def test_run_frontend_finding_input_snapshot_omits_api_key():
    text = (ROOT / "framework" / "scripts" / "run_frontend.py").read_text(encoding="utf-8")

    assert '"api_key": "",' in text
    assert '"api_key": api_key' not in text


def test_web_find_small_budget_does_not_force_deep_survey():
    text = (ROOT / "web" / "backend" / "auto_research" / "web" / "server.py").read_text(encoding="utf-8")

    assert "venue_title_scan_limit or 0) > 0" not in text
    assert "venue_scan_limit >= 1000" in text
    assert "find_recall_count >= 1000" in text
    assert "detail_fetch_count >= 200" in text


def test_run_frontend_uses_project_finding_budget_defaults():
    text = (ROOT / "framework" / "scripts" / "run_frontend.py").read_text(encoding="utf-8")

    assert 'venue_scan_limit = env_int("VENUE_TITLE_SCAN_LIMIT", config_positive_int("venue_title_scan_limit"' in text
    assert 'find_recall_count = env_int("FIND_RECALL_COUNT", config_positive_int("find_recall_count"' in text
    assert 'detail_fetch_count = env_int("DETAIL_FETCH_COUNT", config_positive_int("detail_fetch_count"' in text


def test_run_frontend_runtime_tuning_preserves_web_scoring_budget():
    text = (ROOT / "framework" / "scripts" / "run_frontend.py").read_text(encoding="utf-8")

    assert "elif name not in runtime_tuning and default is not None:" in text
    assert 'abstract_scoring_max_workers = env_int("ABSTRACT_SCORING_MAX_WORKERS", config_positive_int("abstract_scoring_max_workers"' in text
    assert 'abstract_scoring_batch_size = env_int("ABSTRACT_SCORING_BATCH_SIZE", config_positive_int("abstract_scoring_batch_size"' in text
    assert 'runtime_default("ABSTRACT_SCORING_BATCH_SIZE", str(abstract_scoring_batch_size))' in text
    assert 'runtime_default("ABSTRACT_SCORING_MAX_WORKERS", str(abstract_scoring_max_workers))' in text
    assert 'runtime_default("ABSTRACT_SCORING_TIMEOUT_SEC", str(abstract_scoring_timeout_sec))' in text


def test_single_job_api_defaults_to_compact_find_status():
    text = (ROOT / "web" / "backend" / "auto_research" / "web" / "server.py").read_text(encoding="utf-8")

    assert "def api_job(job_id: str, compact: bool = Query(True))" in text
    assert 'result_payload["run_id"] = self.run_id' in text


def test_finding_main_public_cli_uses_real_private_module_paths():
    text = (ROOT / "modules" / "finding" / "main.py").read_text(encoding="utf-8")

    assert '_private_import("flow.pipeline")' in text
    assert '_private_import("flow.support")' in text
    assert '_private_import("pipeline.find_pipeline")' not in text
    assert '_private_import("support.find_support")' not in text


def test_user_facing_find_markdown_is_canonical():
    sync_text = (ROOT / "framework" / "scripts" / "sync_outputs.py").read_text(encoding="utf-8")
    frontend_text = (ROOT / "framework" / "scripts" / "run_frontend.py").read_text(encoding="utf-8")
    server_text = (ROOT / "web" / "backend" / "auto_research" / "web" / "server.py").read_text(encoding="utf-8")
    finding_text = (ROOT / "modules" / "finding" / "main.py").read_text(encoding="utf-8")
    pipeline_text = (ROOT / "modules" / "finding" / "scripts" / "flow" / "pipeline.py").read_text(encoding="utf-8")

    assert '"find.md"' in sync_text
    assert '"find.md"' in frontend_text
    assert '"find.md"' in server_text
    assert '(directory / "find.md").write_text(article_text' in server_text
    assert '"find.md"' in finding_text
    assert 'run_dir / "find.md"' in pipeline_text
    old_find_markdown = "article" + ".md"
    for text in [sync_text, frontend_text, server_text, finding_text, pipeline_text]:
        assert old_find_markdown not in text


def test_find_web_project_artifacts_do_not_expose_maintainer_status():
    bridge_text = (ROOT / "web" / "backend" / "auto_research" / "web" / "project_bridge.py").read_text(encoding="utf-8")
    frontend_text = (ROOT / "framework" / "scripts" / "run_frontend.py").read_text(encoding="utf-8")
    sync_text = (ROOT / "framework" / "scripts" / "sync_outputs.py").read_text(encoding="utf-8")

    assert '("工作状态.txt", ROOT / "工作状态.txt"' not in bridge_text
    assert 'root / "planning" / "finding_frontend.md"' in bridge_text
    assert "# Find Frontend" in frontend_text
    assert "# Find Frontend" in sync_text
    assert "# native Frontend" not in frontend_text
    assert "# native Frontend" not in sync_text


def test_web_framework_do_not_import_finding_private_backend():
    files = [
        ROOT / "web" / "backend" / "auto_research" / "web" / "server.py",
        ROOT / "web" / "backend" / "auto_research" / "web" / "project_bridge.py",
        ROOT / "framework" / "scripts" / "run_frontend.py",
    ]
    forbidden = [
        "from finding_runtime",
        "import finding_runtime",
        "from pipeline.find_pipeline",
        "from support.find_support",
        "modules/finding/scripts",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in text, f"{path} still references Finding private backend marker {marker!r}"


def test_web_environment_init_alias_uses_framework_single_stage(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    _make_project(projects, "demo")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "project_target_venue", lambda project, default="": default or "ICLR")
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")

    assert project_bridge.job_stage({"project": "demo", "action": "init"}) == "environment"
    project, cmd = project_bridge.build_command({"project": "demo", "action": "init", "venue": "ICLR", "conda_env": "demo_env"})

    assert project == "demo"
    assert cmd[:3] == ["/env/bin/python", str(ROOT / "framework" / "scripts" / "orchestration" / "run_taste_framework.py"), "run"]
    assert "--only-stage" in cmd
    assert cmd[cmd.index("--only-stage") + 1] == "environment"
    module_arg = cmd[cmd.index("--module-arg") + 1]
    assert module_arg.startswith("environment=--plan ")
    assert "modules/experimenting/main.py" not in " ".join(cmd)



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
    (root / "state" / "environment_handoff.json").write_text(json.dumps(_ready_environment_handoff(handoff_repo, handoff_env)), encoding="utf-8")
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


def test_framework_live_process_detection_matches_run_id_case_insensitively(monkeypatch):
    class Result:
        returncode = 0
        stdout = "1234 S /env/bin/python framework/scripts/orchestration/run_taste_framework.py run --run-id web_environment_demo_20260621T104334Z"

    monkeypatch.setattr(project_bridge.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(project_bridge, "_pid_alive", lambda pid: pid == "1234")

    assert project_bridge._framework_run_has_live_process("web_environment_demo_20260621t104334z") is True


def test_web_rejects_stale_environment_handoff_policy(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    handoff_repo = tmp_path / "environment" / "repo"
    handoff_env = tmp_path / "environment" / "conda_envs" / "rigid"
    handoff_repo.mkdir(parents=True)
    (handoff_env / "bin").mkdir(parents=True)
    (handoff_env / "bin" / "python").write_text("", encoding="utf-8")
    stale = _ready_environment_handoff(handoff_repo, handoff_env)
    stale["environment_handoff"]["policy_version"] = "environment.deployment_decision.v78"
    stale["environment_handoff"]["handoff_gate"]["policy_version"] = "environment.deployment_decision.v78"
    (root / "state" / "environment_handoff.json").write_text(json.dumps(stale), encoding="utf-8")
    (root / "state" / "evidence_ready_repo_selection.json").write_text(json.dumps({
        "status": "ready_for_experimenting",
        "selection_gate": "environment_handoff_ready_for_experimenting",
        "accepted_by_claude": True,
        "fresh_find_run_id": "find_current",
        "selected_plan_id": "plan_current",
        "selected": stale["selected"] if isinstance(stale.get("selected"), dict) else {
            "repo_path": str(handoff_repo),
            "local_path": str(handoff_repo),
            "selection_gate": "environment_handoff_ready_for_experimenting",
            "fresh_find_run_id": "find_current",
            "selected_plan_id": "plan_current",
        },
    }), encoding="utf-8")
    (root / "state" / "active_repo.json").write_text(json.dumps({
        "repo_path": str(handoff_repo),
        "local_path": str(handoff_repo),
        "selection_gate": "environment_handoff_ready_for_experimenting",
    }), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "project_target_venue", lambda project, default="": default or "ICLR")
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")
    monkeypatch.setattr(project_bridge, "_literature_recommendation_gate_is_blocked", lambda project: False)

    assert project_bridge._environment_handoff_ready_for_experimenting(root) is False
    assert project_bridge._runtime_with_environment_handoff(root, {"conda_env": "old_env", "experiment_python": "/old/bin/python"}) == {"conda_env": "old_env", "experiment_python": "/old/bin/python"}
    env = project_bridge._current_environment_selection(root)
    assert env.get("valid") is False
    assert env.get("reason") == "stale_environment_handoff_projection"
    _project, cmd = project_bridge.build_command({"project": "demo", "action": "experiment", "venue": "ICLR", "iterations": 1, "skip_claude": True})
    assert "blocked_fresh_base_gate_required" in " ".join(cmd)



def test_web_run_preferences_show_current_environment_plan_runtime_without_handoff(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    run_dir = tmp_path / "modules" / "environment" / "runs" / "web_environment_demo_20260621T000000Z"
    round_dir = run_dir / "round_01"
    env_prefix = run_dir / "conda_envs" / "protdis_env"
    (env_prefix / "bin").mkdir(parents=True)
    (env_prefix / "bin" / "python").write_text("", encoding="utf-8")
    round_dir.mkdir(parents=True)
    (round_dir / "claude_environment_plan_round_01.json").write_text(json.dumps({
        "status": "ready_to_execute",
        "env_name": "protdis_env",
        "python_version": "3.11",
        "commands": [],
    }), encoding="utf-8")
    (run_dir / "environment_deployment_decision.json").write_text(json.dumps({
        "run_id": run_dir.name,
        "decision": "continue_repair",
        "ready_for_experimenting": False,
    }), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "ROOT", tmp_path)

    prefs = project_bridge._public_run_preferences("demo", root, {"conda_env": "", "target_venue": "ICLR"}, selection={})

    assert prefs["conda_env"] == "protdis_env"
    assert prefs["runtime"]["conda_env"] == "protdis_env"
    assert prefs["runtime"]["conda_env_prefix"] == str(env_prefix)
    assert prefs["runtime"]["experiment_python"] == str(env_prefix / "bin" / "python")
    assert prefs["runtime"]["environment_decision"] == "continue_repair"


def test_web_run_preferences_project_conda_env_overrides_stale_environment_plan(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    run_dir = tmp_path / "modules" / "environment" / "runs" / "web_environment_demo_20260621T000000Z"
    round_dir = run_dir / "round_01"
    stale_prefix = run_dir / "conda_envs" / "taste_protein_env"
    (stale_prefix / "bin").mkdir(parents=True)
    (stale_prefix / "bin" / "python").write_text("", encoding="utf-8")
    round_dir.mkdir(parents=True)
    (round_dir / "claude_environment_plan_round_01.json").write_text(json.dumps({
        "status": "ready_to_execute",
        "env_name": "taste_protein_env",
        "commands": [],
    }), encoding="utf-8")
    (run_dir / "environment_deployment_decision.json").write_text(json.dumps({
        "run_id": run_dir.name,
        "decision": "continue_repair",
        "ready_for_experimenting": False,
    }), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "ROOT", tmp_path)

    prefs = project_bridge._public_run_preferences(
        "demo",
        root,
        {"conda_env": "protdis_env", "target_venue": "ICLR"},
        runtime_public={"conda_env": "protdis_env", "conda_base": "/opt/miniforge"},
        selection={},
    )
    path_head = project_bridge._runtime_path_head_with_environment_handoff(root, prefs["runtime"], ["/usr/bin"])

    assert prefs["conda_env"] == "protdis_env"
    assert prefs["runtime"]["conda_env"] == "protdis_env"
    assert prefs["runtime"].get("conda_env_prefix") is None
    assert prefs["runtime"].get("experiment_python") is None
    assert prefs["runtime"]["environment_run_id"] == run_dir.name
    assert "taste_protein_env" not in json.dumps(prefs, ensure_ascii=False)
    assert path_head == ["/usr/bin"]



def test_web_summary_filters_cached_experiment_runtime_without_current_handoff(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    stale_env = tmp_path / "old" / "conda_envs" / "protdis_env"
    (stale_env / "bin").mkdir(parents=True)
    stale_python = stale_env / "bin" / "python"
    stale_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "_framework_public_status_for_project", lambda project: {})
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda project, root: {})
    monkeypatch.setattr(project_bridge, "_cached_runtime_diagnostics", lambda project, cfg=None: {
        "project": project,
        "runtime": {
            "conda_env": str(stale_env),
            "conda_env_prefix": str(stale_env),
            "experiment_python": str(stale_python),
            "conda_base": str(tmp_path / "miniforge"),
        },
        "checks": {
            "experiment_python": {"path": str(stale_python), "ok": True, "version": "Python 3.11", "reason": "ok"},
        },
        "path_head": [str(stale_env / "bin"), "/usr/bin"],
        "status": "ok",
    })

    summary = project_bridge._fast_project_summary("demo", root, json.loads((root / "project.json").read_text(encoding="utf-8")))

    assert "experiment_python" not in summary["runtime"]["runtime"]
    assert "conda_env" not in summary["runtime"]["runtime"]
    assert summary["runtime"]["checks"]["experiment_python"] == {
        "path": "",
        "ok": False,
        "version": "",
        "reason": "waiting for current environment handoff",
    }
    assert str(stale_env / "bin") not in summary["runtime"]["path_head"]
    assert "experiment_python" not in summary["run_preferences"].get("runtime", {})



def test_web_summary_does_not_report_not_started_after_synthetic_experiment(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    repo = root / "repos" / "selected" / "repo"
    env_prefix = tmp_path / "environment" / "conda_envs" / "protdis_env"
    (env_prefix / "bin").mkdir(parents=True)
    (env_prefix / "bin" / "python").write_text("", encoding="utf-8")
    (root / "state" / "environment_handoff.json").write_text(json.dumps(_ready_environment_handoff(repo, env_prefix, data_dir=tmp_path / "environment" / "data")), encoding="utf-8")
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


def test_web_summary_keeps_paper_blocked_after_accepted_real_data_probe(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    repo = root / "repos" / "selected" / "repo"
    env_prefix = tmp_path / "environment" / "conda_envs" / "protdis_env"
    (env_prefix / "bin").mkdir(parents=True)
    (env_prefix / "bin" / "python").write_text("", encoding="utf-8")
    (root / "state" / "environment_handoff.json").write_text(json.dumps(_ready_environment_handoff(repo, env_prefix)), encoding="utf-8")
    (root / "state" / "full_research_cycle.json").write_text(json.dumps({"status": "not_started", "summary": "旧 not_started"}), encoding="utf-8")
    (root / "state" / "experiment_registry.json").write_text(json.dumps([
        {
            "timestamp": "2026-06-21T07:48:10Z",
            "run_id": "demo_synthetic",
            "experiment_id": "plan_5",
            "status": "failed",
            "dataset": "synthetic_demo",
            "repo_path": str(repo),
            "acceptance_status": "blocked_real_data_experiment_required",
            "acceptance_blockers": [{"code": "real_data_experiment_required", "message": "synthetic smoke only"}],
            "evidence_status": "blocked_not_promotable",
        },
        {
            "timestamp": "2026-06-21T09:30:00Z",
            "run_id": "proteinshake_probe",
            "experiment_id": "plan_5_proteinshake_realdata_probe",
            "status": "success",
            "dataset": "ProteinShake ProteinFamilyTask",
            "method": "proteinshake_realdata_probe",
            "repo_path": str(repo),
            "metrics": {"proteinshake_test_majority_accuracy": 0.0134},
            "acceptance_status": "accepted_real_data_probe",
            "audit_ready": False,
            "claim_verdict": "unsupported",
            "next_action": "接入 ProtDiS/GP 后继续真实数据实验。",
        },
    ], ensure_ascii=False), encoding="utf-8")
    (root / "state" / "experiment_record_table.json").write_text(json.dumps({
        "row_count": 2,
        "columns": ["时间", "实验ID", "数据集", "审计状态"],
        "rows": [
            {"时间": "2026-06-21T07:48:10Z", "实验ID": "plan_5", "数据集": "synthetic demo", "审计状态": "验收阻断：blocked_real_data_experiment_required", "证据路径": str(repo)},
            {"时间": "2026-06-21T09:30:00Z", "实验ID": "plan_5_proteinshake_realdata_probe", "数据集": "ProteinShake ProteinFamilyTask", "审计状态": "真实数据探针已验收：弱证据，不支撑主论文结论", "证据路径": str(repo)},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "project_runtime_config", lambda project, cfg: {"conda_env": str(env_prefix), "experiment_python": str(env_prefix / "bin" / "python")})
    monkeypatch.setattr(project_bridge, "_framework_public_status_for_project", lambda project: {})
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda project, root: {})

    summary = project_bridge._fast_project_summary("demo", root, json.loads((root / "project.json").read_text(encoding="utf-8")))

    assert summary["status"] == "blocked_paper_level_real_data_experiment_required"
    assert "真实数据探针已完成" in summary["summary"]
    assert summary["current_blocker"]["category"] == "paper_level_real_data_experiment_required"
    assert summary["current_blocker"]["title"] == "需要论文级真实数据实验"
    assert "audit_ready=0" in summary["current_blocker"]["summary"]
    experiment_stage = summary["stages"]["experiment"]
    assert experiment_stage["status"] == "blocked_paper_level_real_data_experiment_required"
    assert experiment_stage["real_experiment_count"] == 1
    assert experiment_stage["audit_ready_completed_experiment_count"] == 0
    assert "最新真实数据探针已验收" in experiment_stage["summary"]
    assert "审计就绪论文级实验 0 条" in experiment_stage["module_summary"]
    assert "已完成或审计就绪" not in experiment_stage["module_summary"]
    assert "synthetic smoke only" not in experiment_stage["summary"]
    assert summary["state"].get("show_synthetic_smoke_warning") is False

def test_web_project_title_comes_only_from_paper_namespace(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    cfg = json.loads((root / "project.json").read_text(encoding="utf-8"))
    cfg["title"] = "legacy project title"
    cfg["paper"] = {"target_venue": "ICLR", "title": ""}
    (root / "project.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)

    prefs = project_bridge._public_run_preferences("demo", root, cfg, selection={})

    assert prefs["title"] == ""
    assert prefs["paper"]["title"] == ""


def test_project_config_title_patch_moves_into_paper_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(project_config, "ROOT", tmp_path)
    cfg = {
        "name": "demo",
        "topic": "demo topic",
        "title": "legacy project title",
        "paper": {"target_venue": "ICLR"},
    }

    updated, _ = project_config._apply_project_patch(cfg, {"title": "Explicit Paper Title"})

    assert "title" not in updated
    assert updated["paper"]["title"] == "Explicit Paper Title"

    cleared, _ = project_config._apply_project_patch(updated, {"title": ""})

    assert "title" not in cleared
    assert "title" not in cleared.get("paper", {})


def test_web_public_state_prefers_environment_handoff_runtime(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    handoff_repo = tmp_path / "environment" / "repo"
    handoff_env = tmp_path / "environment" / "conda_envs" / "rigid"
    handoff_repo.mkdir(parents=True)
    (handoff_env / "bin").mkdir(parents=True)
    (handoff_env / "bin" / "python").write_text("", encoding="utf-8")
    (root / "state" / "environment_handoff.json").write_text(json.dumps(_ready_environment_handoff(
        handoff_repo,
        handoff_env,
        data_dir=tmp_path / "environment" / "data",
        selected={
            "repo_path": str(handoff_repo),
            "local_path": str(handoff_repo),
            "fresh_find_run_id": "find_current",
            "selected_plan_id": "plan_current",
            "selection_stage": "environment_claude_code",
        },
    )), encoding="utf-8")
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


def test_web_source_status_keeps_complete_dblp_title_index_limited_but_unblocked(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    finding = root / "planning" / "finding"
    finding.mkdir(parents=True)
    row = {
        "source_kind": "venue",
        "source": "SIGKDD",
        "venue": "SIGKDD",
        "venue_id": "sigkdd",
        "ok": True,
        "adapter": "dblp_cache",
        "requested_years": [2026],
        "effective_years": [2026],
        "raw_title_index_count": 256,
        "candidate_count": 256,
        "count": 256,
        "metadata_completeness_status": "title_index_only",
        "metadata_completeness_ok": False,
        "metadata_completeness_limited": True,
        "title_index_completeness_status": "complete",
        "title_index_completeness_ok": True,
        "title_index_complete": True,
        "has_official_categories": False,
        "has_abstracts": False,
        "source_scope": "dblp_current_index_not_official_accepted_list",
        "official_title_index_verified": False,
    }
    progress = {
        "run_id": "find_demo",
        "source_status": [row],
        "venue_health_report": [row],
        "counts": {"strong_recommendations": 5, "recommendation_target_count": 5, "recommendation_shortfall": 0},
    }
    (finding / "find_progress.json").write_text(json.dumps(progress), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "_current_verified_venue_metadata_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(project_bridge, "_current_health_check_source_status_rows", lambda *args, **kwargs: [])

    summary = project_bridge._current_find_pipeline_summary(root)

    assert project_bridge._venue_source_integrity_blocker(row) == ""
    assert project_bridge._source_integrity_blocked_rows([row]) == []
    assert project_bridge._venue_source_public_limited(row) is True
    assert project_bridge._derive_source_scope("neurips_official_papers_cache") == "official_neurips_papers_index"
    assert summary["source_integrity_gate"] == {"status": "passed", "blocked_count": 0, "blockers": []}
    assert summary["status"] != "source_integrity_blocked"


def test_web_source_status_does_not_promote_missing_verified_cache_rows():
    missing_rows = [
        {
            "source": "ICLR",
            "source_kind": "venue",
            "venue_id": "openreview_iclr",
            "venue": "ICLR",
            "ok": False,
            "limited": True,
            "count": 0,
            "raw_title_index_count": 0,
            "candidate_count": 0,
            "metadata_completeness_status": "missing",
            "message": "verified local venue metadata cache missing",
        },
        {
            "source": "NeurIPS",
            "source_kind": "venue",
            "venue_id": "ccf_ai_conference_a_neurips_conference_on_neural_information_processing_systems",
            "venue": "NeurIPS",
            "ok": False,
            "limited": True,
            "count": 0,
            "raw_title_index_count": 0,
            "candidate_count": 0,
            "metadata_completeness_status": "missing",
            "message": "verified local venue metadata cache missing",
        },
    ]

    assert project_bridge._merge_verified_venue_metadata_rows([], missing_rows) == []

    current_rows = [
        {
            "source": "ICLR",
            "source_kind": "venue",
            "venue_id": "openreview_iclr",
            "venue": "ICLR",
            "ok": True,
            "limited": False,
            "count": 5352,
            "raw_title_index_count": 5352,
            "candidate_count": 5352,
            "metadata_completeness_status": "complete",
            "metadata_completeness_ok": True,
        }
    ]
    merged = project_bridge._merge_verified_venue_metadata_rows(current_rows, missing_rows)

    assert len(merged) == 1
    assert merged[0]["source"] == "ICLR"
    assert merged[0]["raw_title_index_count"] == 5352


def test_web_find_pipeline_summary_blocks_suspicious_one_paper_core_venue(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    finding = root / "planning" / "finding"
    finding.mkdir(parents=True)
    progress = {
        "run_id": "find_demo",
        "source_status": [
            {
                "source_kind": "venue",
                "source": "ICLR",
                "venue": "ICLR",
                "venue_id": "openreview_iclr_2026",
                "ok": True,
                "adapter": "openreview_cache",
                "requested_years": [2026],
                "effective_years": [2026],
                "raw_title_index_count": 1,
                "candidate_count": 1,
                "count": 1,
                "metadata_completeness_status": "partial",
                "metadata_completeness_ok": False,
                "metadata_completeness_limited": True,
                "title_index_completeness_status": "partial",
                "title_index_completeness_ok": False,
                "title_index_complete": False,
                "has_official_categories": True,
                "has_abstracts": True,
                "source_scope": "official_openreview_metadata",
            }
        ],
        "counts": {"strong_recommendations": 5, "recommendation_target_count": 5, "recommendation_shortfall": 0},
    }
    (finding / "find_progress.json").write_text(json.dumps(progress), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "_current_verified_venue_metadata_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(project_bridge, "_current_health_check_source_status_rows", lambda *args, **kwargs: [])

    summary = project_bridge._current_find_pipeline_summary(root)

    assert summary["status"] == "source_integrity_blocked"
    assert summary["recommendation_shortfall"] == 1
    assert summary["source_integrity_gate"]["blocked_count"] == 1
    assert summary["content_ready"] is False
    assert summary["execution_ready"] is False
    assert summary["takeover_ready"] is False
    assert project_bridge._venue_source_public_limited(progress["source_status"][0]) is True


def test_web_project_summary_projects_source_integrity_blocker(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    finding = root / "planning" / "finding"
    finding.mkdir(parents=True, exist_ok=True)
    run_id = "find_demo"
    progress = {
        "run_id": run_id,
        "source_status": [
            {
                "source_kind": "venue",
                "source": "ICLR",
                "venue": "ICLR",
                "venue_id": "openreview_iclr_2026",
                "ok": True,
                "adapter": "openreview_cache",
                "requested_years": [2026],
                "effective_years": [2026],
                "raw_title_index_count": 1,
                "candidate_count": 1,
                "count": 1,
                "metadata_completeness_status": "partial",
                "metadata_completeness_ok": False,
                "metadata_completeness_limited": True,
                "title_index_completeness_status": "partial",
                "title_index_completeness_ok": False,
                "title_index_complete": False,
                "has_official_categories": True,
                "has_abstracts": True,
                "source_scope": "official_openreview_metadata",
            }
        ],
        "counts": {"strong_recommendations": 5, "recommendation_target_count": 5, "recommendation_shortfall": 0},
    }
    (finding / "find_progress.json").write_text(json.dumps(progress), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "_PROJECT_SUMMARY_CACHE", {})
    monkeypatch.setattr(project_bridge, "_current_verified_venue_metadata_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(project_bridge, "_current_health_check_source_status_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda *args, **kwargs: {})
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])

    summary = project_bridge.project_summary("demo")

    assert summary["status"] == "source_integrity_blocked"
    assert summary["literature_survey"]["status"] == "source_integrity_blocked"
    assert summary["literature_survey"]["recommendation_gate_status"] == "source_integrity_blocked"
    assert summary["current_find_pipeline"]["status"] == "source_integrity_blocked"
    assert summary["current_find_pipeline"]["source_integrity_gate"]["blocked_count"] == 1
    assert summary["main_route"]["base_selection_status"] == "source_integrity_blocked"
    assert summary["stages"]["environment"]["status"] == "source_integrity_blocked"
    assert "Find 来源完整性失败" in summary["stages"]["environment"]["summary"]
    assert summary["current_blocker"]["category"] == "source_integrity_blocked"
    assert "不能作为真实会议语料" in summary["current_blocker"]["summary"]


def test_api_artifacts_light_compacts_large_current_find_progress(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    finding = root / "planning" / "finding"
    finding.mkdir(parents=True, exist_ok=True)
    run_id = "find_large_progress_demo"
    (root / "state" / "finding_frontend.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    progress = {
        "run_id": run_id,
        "phase": "complete",
        "counts": {"raw_title_index": 240, "strong_recommendations": 20},
        "source_status": [{"source": "NeurIPS", "count": 240, "status": "normal"}],
        "raw_title_index": [{"title": f"paper {index}", "abstract": "x" * 200} for index in range(240)],
    }
    (finding / "find_progress.json").write_text(json.dumps(progress), encoding="utf-8")
    run_directory = tmp_path / "runs" / run_id
    run_directory.mkdir(parents=True)
    monkeypatch.setattr(web_server, "run_dir", lambda _run_id: run_directory)
    monkeypatch.setattr(web_server, "PROJECT_IDS_ROOT", projects)
    monkeypatch.setattr(web_server, "LARGE_JSON_ARTIFACT_LIMIT_BYTES", 1024)
    web_server._RUN_ARTIFACTS_CACHE.clear()

    payload = web_server.api_artifacts(run_id, light=True)

    progress_artifact = next(item for item in payload["artifacts"] if item["name"] == "find_progress.json")
    assert progress_artifact["content_truncated"] is True
    assert progress_artifact["size_bytes"] > 1024
    content = progress_artifact["content"]
    assert content["counts"]["raw_title_index"] == 240
    assert content["source_status"] == [{"source": "NeurIPS", "count": 240, "status": "normal"}]
    assert "raw_title_index" in content["omitted_keys"]
    assert "raw_title_index" not in content
    assert len(json.dumps(payload, ensure_ascii=False)) < 20000


def test_api_artifacts_compact_find_results_preserves_dynamic_source_status(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    finding = root / "planning" / "finding"
    finding.mkdir(parents=True, exist_ok=True)
    run_id = "find_large_results_dynamic_sources_demo"
    (root / "state" / "finding_frontend.json").write_text(json.dumps({"taste_run_id": run_id, "counts": {"recommended": 3}}), encoding="utf-8")
    progress = {
        "run_id": run_id,
        "phase": "complete",
        "source_status": [
            {"source": "nature", "ok": True, "limited": False, "count": 331, "prefiltered_count": 331, "date_coverage": {"oldest": "2026-04-08", "newest": "2026-07-02"}},
            {"source": "arxiv", "ok": True, "limited": True, "count": 8565, "raw_count": 8565, "prefiltered_count": 2000, "message": "arXiv rate limited after 94 pages"},
            {"source": "biorxiv", "ok": True, "limited": False, "count": 16602, "raw_count": 16602, "prefiltered_count": 2000},
        ],
    }
    (finding / "find_progress.json").write_text(json.dumps(progress), encoding="utf-8")
    (finding / "find_results.json").write_text(json.dumps({"run_id": run_id, "padding": "x" * 5000}), encoding="utf-8")
    run_directory = tmp_path / "runs" / run_id
    run_directory.mkdir(parents=True)
    (run_directory / "find_progress.json").write_text(json.dumps(progress), encoding="utf-8")

    monkeypatch.setattr(web_server, "run_dir", lambda _run_id: run_directory)
    monkeypatch.setattr(web_server, "PROJECT_IDS_ROOT", projects)
    monkeypatch.setattr(web_server, "LARGE_JSON_ARTIFACT_LIMIT_BYTES", 1024)
    web_server._RUN_ARTIFACTS_CACHE.clear()

    payload = web_server.api_artifacts(run_id, light=True)

    find_results_artifact = next(item for item in payload["artifacts"] if item["name"] == "find_results.json")
    rows = {row["source"]: row for row in find_results_artifact["content"]["source_status"]}
    assert rows["nature"]["ok"] is True
    assert rows["nature"]["limited"] is False
    assert rows["nature"]["count"] == 331
    assert rows["arxiv"]["limited"] is True
    assert rows["arxiv"]["raw_count"] == 8565
    assert rows["biorxiv"]["ok"] is True
    assert rows["biorxiv"]["limited"] is False
    assert rows["biorxiv"]["prefiltered_count"] == 2000


def test_api_artifacts_resolves_environment_framework_and_module_roots(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    run_id = "web_environment_demo_20260621T220925Z"
    framework_run = tmp_path / "framework" / "workspace" / "runs" / run_id.lower()
    module_run = tmp_path / "modules" / "environment" / "runs" / run_id
    (framework_run / "public").mkdir(parents=True)
    module_run.mkdir(parents=True)
    (framework_run / "public" / "workflow_status.md").write_text("# 环境状态\nblocked", encoding="utf-8")
    (framework_run / "public" / "frontend_status.json").write_text(json.dumps({"status": "blocked"}), encoding="utf-8")
    (module_run / "environment_deployment_decision.json").write_text(json.dumps({"run_id": run_id, "decision": "continue_repair"}), encoding="utf-8")
    (module_run / "repo_info.json").write_text(json.dumps({"repo_candidates": ["https://github.com/example/repo"]}), encoding="utf-8")

    monkeypatch.setattr(web_server, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(web_server, "FRAMEWORK_RUNS_DIR", tmp_path / "framework" / "workspace" / "runs")
    monkeypatch.setattr(web_server, "ENVIRONMENT_RUNS_DIR", tmp_path / "modules" / "environment" / "runs")
    monkeypatch.setattr(web_server, "run_dir", lambda _run_id: (_ for _ in ()).throw(FileNotFoundError(_run_id)))
    web_server._RUN_ARTIFACTS_CACHE.clear()

    payload = web_server.api_artifacts(run_id, light=False)

    by_name = {item["name"]: item for item in payload["artifacts"]}
    assert "workflow_status.md" in by_name
    assert by_name["workflow_status.md"]["path"].endswith("public/workflow_status.md")
    assert by_name["frontend_status.json"]["content"] == {"status": "blocked"}
    assert by_name["environment_deployment_decision.json"]["content"]["decision"] == "continue_repair"
    assert by_name["repo_info.json"]["content"]["repo_candidates"] == ["https://github.com/example/repo"]
    assert str(framework_run) in payload["artifact_roots"]
    assert str(module_run) in payload["artifact_roots"]


def test_api_artifacts_resolves_finding_runtime_runs(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    run_id = "find_20260703_205019_627705"
    finding_runs = tmp_path / "modules" / "finding" / ".runtime" / "runs"
    run_root = finding_runs / run_id
    run_root.mkdir(parents=True)
    (run_root / "find.md").write_text("# Find\n\n- 推荐论文 A", encoding="utf-8")
    (run_root / "source_status.md").write_text("# 来源状态\n\n## arXiv\n- 状态: normal", encoding="utf-8")
    (run_root / "find_progress.json").write_text(json.dumps({"run_id": run_id, "phase": "complete"}), encoding="utf-8")
    (run_root / "find_results.json").write_text(json.dumps({"run_id": run_id, "strong_recommendations": [{"title": "A"}]}), encoding="utf-8")

    monkeypatch.setattr(web_server, "PROJECT_IDS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(web_server, "FINDING_RUNS_DIR", finding_runs)
    monkeypatch.setattr(web_server, "run_dir", lambda _run_id: (_ for _ in ()).throw(FileNotFoundError(_run_id)))
    web_server._RUN_ARTIFACTS_CACHE.clear()

    payload = web_server.api_artifacts(run_id, light=True)

    by_name = {item["name"]: item for item in payload["artifacts"]}
    assert str(run_root) in payload["artifact_roots"]
    assert by_name["find.md"]["content"].startswith("# Find")
    assert "来源状态" in by_name["source_status.md"]["content"]
    assert by_name["find_progress.json"]["content"]["phase"] == "complete"
    assert by_name["find_results.json"]["content"]["strong_recommendations"] == [{"title": "A"}]


def test_api_artifacts_falls_back_to_project_current_find_read_md(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    finding = root / "planning" / "finding"
    finding.mkdir(parents=True, exist_ok=True)
    run_id = "find_project_current"
    (root / "state" / "current_find_research_plan.json").write_text(json.dumps({
        "run_id": run_id,
        "status": "current_find_deep_read_complete",
        "current_find_reading_count": 1,
        "current_find_idea_count": 0,
        "current_find_plan_count": 0,
        "read_idea_plan_ready": False,
    }), encoding="utf-8")
    (root / "state" / "current_find_claude_reading_validation.json").write_text(json.dumps({
        "run_id": run_id,
        "valid": True,
        "status": "current_find_deep_read_complete",
        "expected_recommendation_count": 1,
        "actual_reading_count": 1,
        "full_text_reading_count": 1,
        "pending_full_text_reading_count": 0,
        "pending_deep_read_synthesis_count": 0,
    }), encoding="utf-8")
    (finding / "find_results.json").write_text(json.dumps({
        "run_id": run_id,
        "strong_recommendations": [{"title": "Paper A"}],
    }), encoding="utf-8")
    (finding / "find_progress.json").write_text(json.dumps({"run_id": run_id, "phase": "complete"}), encoding="utf-8")
    (finding / "read.md").write_text("# 论文精读\n\n## 逐篇精读\n\n### 1. Paper A\n", encoding="utf-8")
    (finding / "read_results.json").write_text(json.dumps({
        "run_id": run_id,
        "source": "claude_code_current_find_takeover",
        "status": "current_find_deep_read_complete",
        "readings": [{"title": "Paper A", "full_text_available": True, "deep_read_complete": True}],
        "public_final_artifact_present": True,
    }), encoding="utf-8")

    monkeypatch.setattr(web_server, "PROJECT_IDS_ROOT", projects)
    monkeypatch.setattr(web_server, "run_dir", lambda _run_id: (_ for _ in ()).throw(FileNotFoundError(_run_id)))
    web_server._RUN_ARTIFACTS_CACHE.clear()
    web_server._RUN_PROJECT_CACHE.clear()

    payload = web_server.api_artifacts(run_id, light=False)

    by_name = {item["name"]: item for item in payload["artifacts"]}
    assert str(finding) in payload["artifact_roots"]
    assert "read.md" in by_name
    assert by_name["read.md"]["content"].startswith("# 论文精读")
    assert "read_results.json" in by_name
    assert by_name["read_results.json"]["content"]["status"] == "current_find_deep_read_complete"


def test_api_artifacts_light_recovers_current_find_source_status_from_markdown(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    finding = root / "planning" / "finding"
    finding.mkdir(parents=True, exist_ok=True)
    run_id = "find_large_progress_demo"
    (root / "state" / "finding_frontend.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    (root / "state" / "current_find_recommendation_projection.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "counts": {
                    "recommended": 20,
                    "raw_title_index_papers": 6434,
                    "venue_total_papers_available": 6434,
                    "venue_title_filter_input_papers": 6434,
                    "category_filtered_papers": 6434,
                    "tfidf_screened_papers": 6434,
                    "llm_title_scored_papers": 5204,
                    "venue_final_title_candidates": 1503,
                },
                "survey_stats": {
                    "raw_title_index_papers": 6434,
                    "category_filtered_papers": 6434,
                    "tfidf_screened_papers": 6434,
                    "llm_title_scored_papers": 5204,
                    "venue_final_title_candidates": 1503,
                },
                "strong_recommendations": [{"title": "paper", "id": "p1"}],
                "read_candidates": [{"title": "paper", "id": "p1"}],
            }
        ),
        encoding="utf-8",
    )
    (finding / "find_progress.json").write_text('{"padding":"' + ('x' * (6 * 1024 * 1024)) + '"}', encoding="utf-8")
    (finding / "find_results.json").write_text('{"padding":"' + ('x' * (6 * 1024 * 1024)) + '"}', encoding="utf-8")
    (finding / "source_status.md").write_text(_source_status_fixture_markdown(), encoding="utf-8")
    run_directory = tmp_path / "runs" / run_id
    run_directory.mkdir(parents=True)
    monkeypatch.setattr(web_server, "run_dir", lambda _run_id: run_directory)
    monkeypatch.setattr(web_server, "PROJECT_IDS_ROOT", projects)
    monkeypatch.setattr(web_server, "LARGE_JSON_ARTIFACT_LIMIT_BYTES", 1024)
    web_server._RUN_ARTIFACTS_CACHE.clear()

    payload = web_server.api_artifacts(run_id, light=True)

    by_name = {item["name"]: item for item in payload["artifacts"]}
    for name in ["find_progress.json", "find_results.json"]:
        content = by_name[name]["content"]
        rows = content["source_status"]
        assert [row["source"] for row in rows] == ["ICML", "ICLR", "NeurIPS", "SIGKDD"]
        assert [row["count"] for row in rows] == [6431, 5352, 5823, 256]
        assert content["source_status_totals"]["raw_title_index_papers"] == 17862
        assert content["source_status_totals"]["venue_title_filter_input_papers"] == 17862
        assert content["counts"]["raw_title_index_papers"] == 6434
        assert content["counts"]["venue_total_papers_available"] == 6434
        assert content["counts"]["category_filtered_papers"] == 6434
        assert content["counts"]["llm_title_scored_papers"] == 5204
        assert content["survey_stats"]["raw_title_index_papers"] == 6434
        assert content["survey_stats"]["venue_final_title_candidates"] == 1503


def test_project_summary_recovers_source_status_from_markdown_when_progress_is_large(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    finding = root / "planning" / "finding"
    finding.mkdir(parents=True, exist_ok=True)
    run_id = "find_large_progress_demo"
    (root / "state" / "finding_frontend.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    (root / "state" / "current_find_recommendation_projection.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "source_run_id": run_id,
                "counts": {
                    "recommended": 20,
                    "recommendation_target_count": 20,
                    "recommendation_shortfall": 0,
                    "strong_recommendations": 20,
                    "raw_title_index_papers": 6434,
                    "venue_title_filter_input_papers": 6434,
                    "category_filtered_papers": 6434,
                    "tfidf_screened_papers": 6434,
                    "llm_title_scored_papers": 5204,
                    "venue_final_title_candidates": 1503,
                },
                "survey_stats": {
                    "raw_title_index_papers": 6434,
                    "venue_title_filter_input_papers": 6434,
                    "category_filtered_papers": 6434,
                    "tfidf_screened_papers": 6434,
                    "llm_title_scored_papers": 5204,
                    "venue_final_title_candidates": 1503,
                },
                "strong_recommendations": [],
                "read_candidates": [],
            }
        ),
        encoding="utf-8",
    )
    (finding / "find_progress.json").write_text('{"padding":"' + ('x' * (6 * 1024 * 1024)) + '"}', encoding="utf-8")
    (finding / "source_status.md").write_text(_source_status_fixture_markdown(), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "_PROJECT_SUMMARY_CACHE", {})
    monkeypatch.setattr(project_bridge, "_current_verified_venue_metadata_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(project_bridge, "_current_health_check_source_status_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(project_bridge, "filter_source_status_by_selection", lambda rows, selection: rows)
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda *args, **kwargs: {})

    summary = project_bridge.project_summary("demo")
    rows = summary["literature_survey"]["source_status"]

    assert [row["source"] for row in rows] == ["ICML", "ICLR", "NeurIPS", "SIGKDD"]
    assert [row["count"] for row in rows] == [6431, 5352, 5823, 256]
    pipeline_rows = summary["current_find_pipeline"]["source_status"]
    assert [row["source"] for row in pipeline_rows] == ["ICML", "ICLR", "NeurIPS", "SIGKDD"]
    assert [row["count"] for row in pipeline_rows] == [6431, 5352, 5823, 256]
    stage_rows = summary["stages"]["find"]["source_status"]
    assert [row["source"] for row in stage_rows] == ["ICML", "ICLR", "NeurIPS", "SIGKDD"]
    assert [row["count"] for row in stage_rows] == [6431, 5352, 5823, 256]
    assert summary["literature_survey"]["source_status_totals"]["raw_title_index_papers"] == 17862
    assert summary["literature_survey"]["counts"]["raw_title_index_papers"] == 6434
    assert summary["literature_survey"]["counts"]["category_filtered_papers"] == 6434
    assert summary["literature_survey"]["counts"]["llm_title_scored_papers"] == 5204
    assert summary["literature_survey"]["source_integrity_gate"]["blocked_count"] == 0
    assert summary["literature_survey"]["status"] == "current_find_packet_ready"


def test_dynamic_source_status_is_not_normalized_as_venue_metadata():
    source_status = [
        {
            "source": "nature",
            "ok": True,
            "limited": False,
            "count": 331,
            "prefiltered_count": 331,
            "message": "ok; date coverage 2026-04-08 to 2026-07-02",
            "date_coverage": {"oldest": "2026-04-08", "newest": "2026-07-02"},
        },
        {
            "source": "arxiv",
            "ok": True,
            "limited": True,
            "count": 8565,
            "raw_count": 8565,
            "prefiltered_count": 2000,
            "message": "arXiv rate limited after 94 pages; kept 8565 papers.",
            "date_coverage": {"oldest": "2026-04-03", "newest": "2026-07-02"},
        },
        {
            "source": "biorxiv",
            "ok": True,
            "limited": False,
            "count": 16602,
            "raw_count": 16602,
            "prefiltered_count": 2000,
            "message": "ok; complete native date window scanned",
            "date_coverage": {"oldest": "2026-04-03", "newest": "2026-07-03"},
        },
    ]
    venue_health = [
        {
            "venue_id": "openreview_iclr",
            "venue": "ICLR",
            "ok": True,
            "adapter": "openreview_reference",
            "corpus_count": 5352,
            "candidate_count": 1381,
            "effective_years": [2026],
            "requested_years": [2026],
            "metadata_completeness_ok": True,
            "metadata_completeness_status": "complete",
            "title_index_completeness_ok": True,
            "has_official_categories": True,
            "has_abstracts": True,
        }
    ]

    expanded = project_bridge._expand_source_status_rows(source_status, venue_health)
    rows = {row["source"]: row for row in project_bridge._merge_verified_venue_metadata_rows(expanded, [])}

    assert rows["nature"]["ok"] is True
    assert rows["nature"]["limited"] is False
    assert rows["nature"]["count"] == 331
    assert rows["nature"]["prefiltered_count"] == 331
    assert rows["nature"]["date_coverage"]["oldest"] == "2026-04-08"
    assert rows["arxiv"]["ok"] is True
    assert rows["arxiv"]["limited"] is True
    assert rows["arxiv"]["raw_count"] == 8565
    assert rows["arxiv"]["prefiltered_count"] == 2000
    assert rows["biorxiv"]["ok"] is True
    assert rows["biorxiv"]["limited"] is False
    assert rows["biorxiv"]["raw_count"] == 16602


def test_web_jobs_keeps_only_latest_persisted_environment_history(monkeypatch):
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
    assert "Find 已完成" in blocker["summary"]
    assert "Read 精读、Idea 和 Plan 尚未运行" in blocker["summary"]
    assert "环境阶段" not in blocker["summary"]
    assert "启动 Read" in blocker["next_action"]
    assert "唯一研究计划" in blocker["next_action"]


def test_web_find_only_completion_is_pending_read_not_claude_gate(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    finding = root / "planning" / "finding"
    finding.mkdir(parents=True, exist_ok=True)
    run_id = "find_demo"
    (finding / "find_progress.json").write_text(json.dumps({
        "run_id": run_id,
        "counts": {
            "strong_recommendations": 3,
            "recommendation_target_count": 3,
            "recommendation_shortfall": 0,
        },
        "recommendation_target_count": 3,
        "recommendation_shortfall": 0,
        "source_status": [],
        "venue_health_report": [],
    }), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "_PROJECT_SUMMARY_CACHE", {})
    monkeypatch.setattr(project_bridge, "_framework_public_status_for_project", lambda project: {})
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda *args, **kwargs: {})
    monkeypatch.setattr(project_bridge, "_current_verified_venue_metadata_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(project_bridge, "_current_health_check_source_status_rows", lambda *args, **kwargs: [])

    summary = project_bridge._current_find_pipeline_summary(root)
    blocker = project_bridge._current_find_pipeline_public_blocker(summary)
    public_text = json.dumps({"summary": summary, "blocker": blocker}, ensure_ascii=False)

    assert summary["status"] == "pending_current_find_read"
    assert summary["next_required_action"] == "run_read_for_current_find"
    assert blocker["title"] == "Find 已完成，等待运行 Read/Idea/Plan"
    assert "Find 已完成" in blocker["summary"]
    assert "Claude 接管 gate" not in public_text
    assert "等待当前 Find 后处理" not in public_text

    cfg = json.loads((root / "project.json").read_text(encoding="utf-8"))
    project_summary = project_bridge._fast_project_summary("demo", root, cfg)
    projected = project_summary["literature_survey"]["current_find_pipeline"]
    projected_blocker = project_summary["human_gate_summary"]
    projected_text = json.dumps({"pipeline": projected, "blocker": projected_blocker}, ensure_ascii=False)
    assert projected["status"] == "pending_current_find_read"
    assert projected["next_required_action"] == "run_read_for_current_find"
    assert "Find 已完成" in projected["summary_zh"]
    assert "Claude 接管 gate" not in projected_text
    assert "等待当前 Find 后处理" not in projected_text


def test_web_current_find_read_history_supersedes_stale_blocked_read_job(monkeypatch, tmp_path):
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
    assert "当前展示 20/20 篇" in read_rows[0]["progress"]["message"]
    assert "同篇全文证据 20/20 篇" in read_rows[0]["progress"]["message"]
    assert "精读完成 20/20 篇" in read_rows[0]["progress"]["message"]

    collapsed = web_server._collapse_current_find_read_retry_jobs([stale] + synthetic, project_hint="demo")
    collapsed_read_rows = [row for row in collapsed if row.get("stage") == "read"]
    assert len(collapsed_read_rows) == 1
    assert collapsed_read_rows[0]["job_id"].startswith("current-find-read_")
    assert collapsed_read_rows[0]["status"] == "done"
    assert "缺少同篇全文证据" not in json.dumps(collapsed_read_rows[0], ensure_ascii=False)


def test_web_project_summary_lists_read_md_before_read_results(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    run_id = "find_current"
    finding = root / "planning" / "finding"
    finding.mkdir(parents=True, exist_ok=True)
    (finding / "find_results.json").write_text(json.dumps({
        "run_id": run_id,
        "recommendations": [{"title": "Paper A"}],
        "articles": [{"title": "Paper A"}],
    }), encoding="utf-8")
    (finding / "read.md").write_text("# 精读\n\nPaper A\n", encoding="utf-8")
    (finding / "read_results.json").write_text(json.dumps({
        "run_id": run_id,
        "readings": [{"title": "Paper A", "full_text_available": True}],
    }), encoding="utf-8")
    (root / "state" / "current_find_claude_reading_validation.json").write_text(json.dumps({
        "run_id": run_id,
        "valid": True,
        "expected_recommendation_count": 1,
        "actual_reading_count": 1,
    }), encoding="utf-8")
    (root / "state" / "current_find_research_plan.json").write_text(json.dumps({
        "run_id": run_id,
        "read_idea_plan_ready": True,
        "current_find_reading_count": 1,
    }), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "_PROJECT_SUMMARY_CACHE", {})
    monkeypatch.setattr(project_bridge, "_framework_public_status_for_project", lambda project: {})
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda *args, **kwargs: {})

    summary = project_bridge.project_summary("demo")
    names = [item["name"] for item in summary["artifacts"]]

    assert "read.md" in names
    assert "read_results.json" in names
    assert names.index("read.md") < names.index("read_results.json")


def test_web_project_summary_hides_unfinalized_read_results(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    run_id = "find_current"
    finding = root / "planning" / "finding"
    finding.mkdir(parents=True, exist_ok=True)
    (finding / "find_results.json").write_text(json.dumps({
        "run_id": run_id,
        "recommendations": [{"title": "Paper A"}],
        "articles": [{"title": "Paper A"}],
    }), encoding="utf-8")
    (finding / "read_results.json").write_text(json.dumps({
        "run_id": run_id,
        "readings": [{"title": "Paper A", "full_text_available": True}],
        "public_final_artifact_present": False,
        "reading_validation": {
            "run_id": run_id,
            "valid": False,
            "blockers": ["final read.md is missing"],
        },
    }), encoding="utf-8")
    (root / "state" / "current_find_claude_reading_validation.json").write_text(json.dumps({
        "run_id": run_id,
        "valid": False,
        "expected_recommendation_count": 1,
        "actual_reading_count": 1,
        "blockers": ["final read.md is missing"],
    }), encoding="utf-8")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "_PROJECT_SUMMARY_CACHE", {})
    monkeypatch.setattr(project_bridge, "_framework_public_status_for_project", lambda project: {})
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda *args, **kwargs: {})

    summary = project_bridge.project_summary("demo")
    names = [item["name"] for item in summary["artifacts"]]

    assert "read.md" not in names
    assert "read_results.json" not in names
