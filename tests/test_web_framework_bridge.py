from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
import os
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from path_helpers import ensure_script_paths

ensure_script_paths()

from bridges import project_bridge
from project import project_config
from reporting import paper_state

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


def test_ideation_framework_owns_normalized_input_and_explicit_run_sync(tmp_path):
    from bridges.ideation_bridge import prepare_current_find_ideation_input, sync_current_find_ideation_outputs

    projects = tmp_path / "projects"
    project_root = projects / "demo"
    finding = project_root / "planning" / "finding"
    state = project_root / "state"
    finding.mkdir(parents=True)
    state.mkdir(parents=True)
    run_id = "find-run-1"
    (finding / "find_results.json").write_text(json.dumps({
        "run_id": run_id,
        "strong_recommendations": [{"title": "Evidence A", "url": "https://example.org/a", "summary": "auditable evidence"}],
    }), encoding="utf-8")
    (finding / "read_results.json").write_text(json.dumps({
        "run_id": run_id,
        "public_final_artifact_present": True,
        "readings": [{"title": "Evidence A", "url": "https://example.org/a", "summary": "deep reading"}],
    }), encoding="utf-8")
    (finding / "read.md").write_text("# 论文精读\n\n## Evidence A\n\n正文证据。\n", encoding="utf-8")
    (state / "current_find_claude_reading_validation.json").write_text(json.dumps({"run_id": run_id, "valid": True}), encoding="utf-8")
    (state / "current_find_research_plan.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")

    prepared = prepare_current_find_ideation_input(
        "demo",
        requested_run_id=run_id,
        projects_root=projects,
        runtime_root=tmp_path / "framework-runtime",
    )
    bundle = json.loads(Path(prepared["input_json"]).read_text(encoding="utf-8"))
    assert bundle["schema_version"] == "taste.ideation_input.v1"
    assert bundle["run_id"] == run_id
    assert [item["title"] for item in bundle["items"]] == ["Evidence A"]
    assert bundle["items"][0]["source"] == "read"
    assert bundle["items"][0]["summary"] == "deep reading"
    assert bundle["items"][0]["url"] == "https://example.org/a"
    assert "read_results" not in bundle
    assert bundle["read_markdown"].startswith("# 论文精读")

    ideation_root = tmp_path / "ideation"
    timestamp_id = "20260710T010203123456Z"
    module_run = ideation_root / ".runtime" / "output" / timestamp_id
    module_run.mkdir(parents=True)
    idea_markdown = "# Ideation 生成的新论文想法\n\n## 1. Idea A\n"
    (module_run / "idea.md").write_text(idea_markdown, encoding="utf-8")
    (module_run / "ideas.json").write_text(json.dumps({
        "run_id": run_id,
        "source_run_id": run_id,
        "ideation_run_id": timestamp_id,
        "machine_projection_from": "idea.md",
        "ideas": [{"id": "idea-001", "title": "Idea A", "new_method": "m" * 50, "initial_experiment": "e" * 50, "inspired_by": [{"title": "Evidence A"}]}],
    }), encoding="utf-8")
    (module_run / "manifest.json").write_text(json.dumps({
        "run_id": timestamp_id,
        "source_run_id": run_id,
        "public_final_artifact": "idea.md",
    }), encoding="utf-8")

    result = sync_current_find_ideation_outputs(
        "demo",
        result_payload={"result": {"run_dir": str(module_run)}},
        projects_root=projects,
        ideation_root=ideation_root,
    )
    project_run = finding / "ideation_runs" / timestamp_id
    assert result["ideation_run_id"] == timestamp_id
    assert (finding / "idea.md").read_text(encoding="utf-8") == idea_markdown
    assert (project_run / "idea.md").read_text(encoding="utf-8") == idea_markdown
    assert "ideas" not in json.loads((state / "current_find_research_plan.json").read_text(encoding="utf-8"))
    assert sorted(path.name for path in (finding / "ideation_runs").iterdir()) == [timestamp_id]


def test_ideation_edit_rejects_artifact_from_stale_find(tmp_path):
    from bridges.ideation_bridge import current_find_ideation_run_dir

    projects = tmp_path / "projects"
    project_root = projects / "demo"
    finding = project_root / "planning" / "finding"
    state = project_root / "state"
    finding.mkdir(parents=True)
    state.mkdir(parents=True)
    timestamp_id = "20260710T010203123456Z"
    (state / "current_find_research_plan.json").write_text(json.dumps({"run_id": "find-new"}), encoding="utf-8")
    (finding / "ideas.json").write_text(json.dumps({
        "run_id": "find-old",
        "ideation_run_id": timestamp_id,
    }), encoding="utf-8")
    ideation_root = tmp_path / "ideation"
    (ideation_root / ".runtime" / "output" / timestamp_id).mkdir(parents=True)

    with pytest.raises(ValueError, match="artifact is stale"):
        current_find_ideation_run_dir(
            "demo",
            requested_run_id="find-new",
            projects_root=projects,
            ideation_root=ideation_root,
        )


def test_planning_framework_owns_approved_input_and_explicit_run_sync(tmp_path):
    from bridges.planning_bridge import (
        prepare_current_find_planning_input,
        prepare_planning_refresh_after_idea_change,
        sync_current_find_planning_outputs,
    )

    projects = tmp_path / "projects"
    project_root = projects / "demo"
    finding = project_root / "planning" / "finding"
    state = project_root / "state"
    finding.mkdir(parents=True)
    state.mkdir(parents=True)
    run_id = "find-run-planning"
    (finding / "find_progress.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    (finding / "read_results.json").write_text(json.dumps({"run_id": run_id, "readings": [{"title": "Paper"}]}), encoding="utf-8")
    (state / "current_find_claude_reading_validation.json").write_text(json.dumps({"run_id": run_id, "valid": True}), encoding="utf-8")
    (state / "current_find_research_plan.json").write_text(json.dumps({"run_id": run_id, "current_find_idea_count": 3}), encoding="utf-8")
    ideas = [
        {"id": "idea-a", "title": "Idea A", "status": "approved", "new_method": "method a", "initial_experiment": "experiment a"},
        {"id": "idea-b", "title": "Idea B", "approved_for_planning": True, "new_method": "method b", "initial_experiment": "experiment b"},
        {"id": "idea-c", "title": "Idea C", "status": "pending", "new_method": "method c", "initial_experiment": "experiment c"},
    ]
    (finding / "ideas.json").write_text(json.dumps({"run_id": run_id, "source": "taste_ideation", "ideas": ideas}), encoding="utf-8")
    (finding / "idea.md").write_text("# Ideas\n\n## Idea A\n\n## Idea B\n", encoding="utf-8")

    prepared = prepare_current_find_planning_input(
        "demo",
        action="plan",
        requested_run_id=run_id,
        projects_root=projects,
        runtime_root=tmp_path / "planning-inputs",
    )
    bundle = json.loads(Path(prepared["input_json"]).read_text(encoding="utf-8"))
    assert bundle["schema_version"] == "taste.planning_input.v1"
    assert prepared["approved_idea_ids"] == ["idea-a", "idea-b"]
    assert [row["id"] for row in bundle["ideas"]["ideas"]] == ["idea-a", "idea-b"]
    assert "approved_ideas" not in bundle
    assert "idea_markdown" not in bundle
    selected = prepare_current_find_planning_input(
        "demo",
        action="plan",
        requested_run_id=run_id,
        requested_idea_ids=["idea-a"],
        projects_root=projects,
        runtime_root=tmp_path / "planning-inputs",
    )
    selected_bundle = json.loads(Path(selected["input_json"]).read_text(encoding="utf-8"))
    assert selected["selected_idea_ids"] == ["idea-a"]
    assert [row["id"] for row in selected_bundle["ideas"]["ideas"]] == ["idea-a"]
    with pytest.raises(ValueError, match="invalid IDs: idea-c"):
        prepare_current_find_planning_input(
            "demo",
            action="plan",
            requested_run_id=run_id,
            requested_idea_ids=["idea-c"],
            projects_root=projects,
            runtime_root=tmp_path / "planning-inputs",
        )

    planning_root = tmp_path / "planning-module"
    planning_run_id = "20260710T010203123456Z_plan_pid42"
    module_run = planning_root / ".runtime" / "runs" / planning_run_id
    module_run.mkdir(parents=True)
    plan_markdown = """# Research Plans

## 1. Plan A

- **Plan ID**: `plan-idea-a`
- **Idea ID**: `idea-a`
- **Latest Version**: `v1`
- **Selected for Execution**: false
- **Completed**: false

### New Method
Use a falsifiable candidate method.

### Initial Experiment
Compare the candidate, baseline, and ablation under one protocol.

### 启发来源
- No external source was supplied in the approved Idea.

### Step-by-step Plan
1. Implement the candidate and matched baseline.
2. Run the ablation and audit failure cases.

### Risks
- The candidate may not improve the matched baseline.

### Metrics
- Primary task score and runtime cost.
"""
    planned_ideas = [ideas[0]]
    idea_revision = hashlib.sha256(json.dumps(planned_ideas, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    plan_rows = [{
        "plan_id": "plan-idea-a",
        "idea_id": "idea-a",
        "order": 1,
        "selected_for_execution": False,
        "completed": False,
        "versions": [{"version_id": "v1", "source": "claude_code_direct"}],
    }]
    (module_run / "plans.json").write_text(json.dumps({
        "run_id": run_id,
        "source": "plan_md_projection",
        "machine_projection_from": "plan.md",
        "public_final_artifact": "plan.md",
        "planned_idea_ids": ["idea-a"],
        "idea_revision": idea_revision,
        "plans": plan_rows,
        "selected_plan_id": "",
        "plan_markdown_generation": {
            "source": "claude_code_direct",
            "sha256": hashlib.sha256(plan_markdown.encode("utf-8")).hexdigest(),
            "audit": {"status": "pass", "issues": []},
        },
    }), encoding="utf-8")
    (module_run / "plan.md").write_text(plan_markdown, encoding="utf-8")
    (module_run / "experiment_plan.json").write_text(json.dumps({"run_id": run_id, "status": "blocked_missing_plan_selection"}), encoding="utf-8")
    (module_run / "taste_plan_bridge.json").write_text(json.dumps({"run_id": run_id, "selected_plan_id": ""}), encoding="utf-8")
    (module_run / "run_meta.json").write_text(json.dumps({"planning_run_id": planning_run_id}), encoding="utf-8")

    result = sync_current_find_planning_outputs(
        "demo",
        result_payload={"result": {"planning_run_dir": str(module_run)}},
        projects_root=projects,
        planning_root=planning_root,
    )
    synced_state = json.loads((state / "current_find_research_plan.json").read_text(encoding="utf-8"))
    assert result["planning_run_id"] == planning_run_id
    assert result["plan_count"] == 1
    assert (finding / "plan.md").read_text(encoding="utf-8").startswith("# Research Plans")
    assert (finding / "planning_runs" / planning_run_id / "plan.md").is_file()
    assert synced_state["current_find_plan_count"] == 1
    assert synced_state["current_find_approved_idea_count"] == 1
    assert synced_state["read_idea_plan_ready"] is True
    assert synced_state["claude_current_find_ready"] is False
    assert synced_state["status"] == "awaiting_plan_selection"
    follow_up = prepare_current_find_planning_input(
        "demo",
        action="select",
        requested_run_id=run_id,
        projects_root=projects,
        runtime_root=tmp_path / "planning-inputs",
    )
    follow_up_bundle = json.loads(Path(follow_up["input_json"]).read_text(encoding="utf-8"))
    assert follow_up_bundle["plan_markdown"] == plan_markdown

    unaffected = prepare_planning_refresh_after_idea_change("demo", changed_idea_id="idea-b", projects_root=projects)
    assert unaffected == {"required": False, "reason": "changed_idea_has_no_plan"}
    refresh = prepare_planning_refresh_after_idea_change("demo", changed_idea_id="idea-a", projects_root=projects)
    assert refresh["required"] is True
    assert refresh["idea_ids"] == ["idea-a"]
    assert not (finding / "plan.md").exists()
    assert not (finding / "plans.json").exists()
    assert (finding / "planning_runs" / planning_run_id / "plan.md").is_file()


def test_planning_claude_writes_canonical_markdown_with_exact_repair_rounds(monkeypatch, tmp_path):
    module_path = ROOT / "modules" / "planning" / "scripts" / "core" / "plan_pipeline.py"
    spec = importlib.util.spec_from_file_location("test_plan_pipeline", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    run_id = "find-plan-direct"
    idea = {
        "id": "idea-a",
        "title": "Candidate A",
        "status": "approved",
        "new_method": "Use a falsifiable intervention.",
        "initial_experiment": "Compare candidate, baseline, and ablation.",
    }
    (tmp_path / "ideas.json").write_text(json.dumps({"run_id": run_id, "ideas": [idea]}), encoding="utf-8")
    calls: list[str] = []

    def fake_claude_writer(prompt, directory, target_path, label, log):
        calls.append(label)
        if not target_path.exists():
            target_path.write_text(module.render_plan_markdown([idea]), encoding="utf-8")
        else:
            target_path.write_text(target_path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")
        return {"status": "ok_file_written"}

    monkeypatch.setenv("PLANNING_PUBLIC_ENTRYPOINT_ACTIVE", "1")
    monkeypatch.setenv("PLAN_BACKEND", "claude_code")
    monkeypatch.setattr(module, "_run_claude_markdown_writer", fake_claude_writer)
    result = module.run_plan_at_directory(
        tmp_path,
        module.PlanRequest(run_id=run_id, idea_ids=["idea-a"], repair_rounds=3),
        module.PlanningConfig(),
    )

    assert calls == ["plan_md_initial", "plan_md_repair_1", "plan_md_repair_2", "plan_md_repair_3"]
    assert result["machine_projection_from"] == "plan.md"
    assert result["plan_markdown_generation"]["repair_rounds"] == 3
    assert result["plan_markdown_generation"]["audit"]["status"] == "pass"
    assert result["plans"][0]["versions"][-1]["version_id"] == "v1"
    assert not ({"title", "new_method", "initial_experiment", "steps", "risks", "metrics"} & set(result["plans"][0]))
    assert (tmp_path / "plan.md").read_text(encoding="utf-8").startswith("# Research Plans")


def test_framework_ideation_patch_regenerates_existing_plan(monkeypatch, tmp_path):
    from orchestration import run_module

    planning_calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(run_module, "_project_module_lock", lambda stage, project: nullcontext())
    monkeypatch.setattr(run_module, "current_find_ideation_run_dir", lambda project, requested_run_id="": tmp_path / "idea-run")
    monkeypatch.setattr(run_module, "_run_streaming", lambda cmd, env, input_text="": (0, '{"status":"ok"}'))
    monkeypatch.setattr(run_module, "sync_current_find_ideation_outputs", lambda project, result_payload: {"run_id": "find-1"})
    monkeypatch.setattr(run_module, "prepare_planning_refresh_after_idea_change", lambda project, changed_idea_id="": {
        "required": True,
        "run_id": "find-1",
        "idea_ids": ["idea-a"],
    })

    def fake_planning(action, args, **kwargs):
        assert kwargs == {"_lock": False}
        planning_calls.append((action, list(args)))
        return 0

    monkeypatch.setattr(run_module, "_run_current_find_planning_bridge", fake_planning)
    rc = run_module._run_current_find_ideation_bridge(
        "patch",
        ["--project", "demo", "--run-id", "find-1", "--idea-id", "idea-a"],
    )

    assert rc == 0
    assert planning_calls == [("plan", [
        "--project", "demo",
        "--run-id", "find-1",
        "--repair-rounds", "3",
        "--idea-id", "idea-a",
    ])]


def test_web_planning_is_framework_only_and_restores_editor_artifact_layout(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    _make_project(projects, "demo")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")
    _project, command = project_bridge.build_command({"project": "demo", "action": "current-find-selection"})

    server_text = (ROOT / "web" / "backend" / "auto_research" / "web" / "server.py").read_text(encoding="utf-8")
    app_text = (ROOT / "web" / "frontend" / "client" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert command[:6] == ["/env/bin/python", str(ROOT / "framework" / "scripts" / "main.py"), "module", "planning", "--action", "select"]
    assert "modules/planning/main.py" not in server_text
    assert '"planning",\n        "--action"' in server_text
    assert '@app.put("/api/runs/{run_id}/plan-markdown")' in server_text
    assert 'data-testid="plan-human-editor"' in app_text
    assert 'data-testid={tab === "plan" ? "plan-artifact-panel" : undefined}' in app_text
    assert "planFinalMarkdownPanel" not in app_text
    assert "planIdeaIds" in app_text
    assert '"plan": (["plan.md"], ["ideas.json", "plans.json"])' in server_text
    assert 'cmd.extend(["--idea-id", idea_id])' in server_text
    assert "planWorkspaceGrid" not in app_text
    assert "planMarkdownSourceEditor" not in app_text
    assert "planTitlesFromMarkdown(planMarkdownText)" in app_text
    assert "rejectHistoricalRunMutation()" in app_text
    assert 'return selected.startsWith("find_")' not in app_text
    assert "prepare_planning_refresh_after_idea_change" in (ROOT / "framework" / "scripts" / "orchestration" / "run_module.py").read_text(encoding="utf-8")


def test_ideation_module_has_one_decoupled_input_pipeline():
    module_root = ROOT / "modules" / "ideation"
    main_text = (module_root / "main.py").read_text(encoding="utf-8")
    pipeline_text = (module_root / "scripts" / "idea_pipeline.py").read_text(encoding="utf-8")
    render_text = (module_root / "scripts" / "ideation_quality" / "render.py").read_text(encoding="utf-8")
    schema_text = (module_root / "scripts" / "ideation_quality" / "schema.py").read_text(encoding="utf-8")
    workspace_text = (module_root / "scripts" / "artifact_io" / "workspace.py").read_text(encoding="utf-8")
    app_text = (ROOT / "web" / "frontend" / "client" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert 'parser.add_argument("--input-json", required=True' in main_text
    assert "STANDALONE_ACTIONS" not in main_text
    assert "FINALIZE_ACTIONS" not in main_text
    assert "auto_research" not in pipeline_text
    assert not (module_root / "__init__.py").exists()
    assert not (module_root / "scripts" / "ideation_tools.py").exists()
    assert not (module_root / "scripts" / "core" / "standalone_pipeline.py").exists()
    assert "禁止输出 `### 自检`" in pipeline_text
    assert '"### 自检",' not in render_text
    assert '"### 坏例切片",' not in render_text
    assert '"### 重点验证场景",' not in render_text
    assert 'expected_headings = ("新方法", "机制细节", "初步实验", "启发来源", "风险与停止标准")' in schema_text
    assert "bad_case_slice" not in schema_text
    assert "validation_scenarios" not in schema_text
    assert "validation_scenarios" not in pipeline_text
    assert "idea?.validation_scenarios" not in app_text
    assert "idea?.bad_case_slice" not in app_text
    assert "无法对应就删除" in pipeline_text
    assert "无直接风险证据时只写提案的停止规则" in pipeline_text
    assert 'bundle.get("read_results")' not in pipeline_text
    assert "MAX_INPUT_BYTES" in pipeline_text
    assert "fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)" in workspace_text
    assert "shutil.copytree(source, tmp)" in workspace_text


def test_web_ideation_is_markdown_first_and_framework_only():
    from pydantic import ValidationError

    from contracts.web_models import IdeaPatch, IdeaRequest

    server_text = (ROOT / "web" / "backend" / "auto_research" / "web" / "server.py").read_text(encoding="utf-8")
    app_text = (ROOT / "web" / "frontend" / "client" / "src" / "App.tsx").read_text(encoding="utf-8")
    framework_bridge = ROOT / "framework" / "scripts" / "bridges" / "project_bridge.py"

    assert framework_bridge.exists()
    assert not (ROOT / "web" / "backend" / "auto_research" / "web" / "project_bridge.py").exists()
    assert "from bridges.project_bridge import" in server_text
    assert "modules/ideation/main.py" not in server_text
    assert 'str(WORKSPACE_ROOT / "framework" / "scripts" / "main.py")' in server_text
    assert 'currentArtifact.name === "idea.md" ? "idea-artifact-markdown"' in app_text
    assert "markdownRenderer.render(artifactPanelContent(currentArtifact))" in app_text
    assert "markdownRenderer.render(ideaMarkdownText)" not in app_text
    assert 'if name != "plan.md"' in server_text
    assert "saveIdeaFields(ideaId)" in app_text
    assert "updateIdeaMarkdown(ideaRunId, ideaMarkdownDraft" in app_text
    assert "startIdea(ideaRunId, maxIdeas, researchProject)" in app_text
    assert "with JOBS_LOCK:" in server_text
    assert "_ideation_framework_env" in server_text
    assert "start_new_session=True" in server_text
    assert 'project: str = Query(..., min_length=1' in server_text
    with pytest.raises(ValidationError):
        IdeaRequest(run_id="find-run", max_ideas=4)
    request = IdeaRequest(run_id="find-run", project="demo", max_ideas=4)
    assert request.project == "demo"
    patch = IdeaPatch(title="Title", new_method="Method", initial_experiment="Experiment")
    assert patch.model_dump(exclude_none=True) == {
        "title": "Title",
        "new_method": "Method",
        "initial_experiment": "Experiment",
    }


def test_web_framework_process_cancellation_does_not_wait_for_child_output():
    from auto_research.web import server as web_server

    started = time.monotonic()
    with pytest.raises(web_server.JobCancelled):
        web_server._run_framework_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            os.environ.copy(),
            lambda _message: None,
            lambda: time.monotonic() - started > 0.3,
        )
    assert time.monotonic() - started < 5


def test_web_ideation_error_message_hides_traceback_and_local_paths():
    from auto_research.web import server as web_server

    stdout = "\n".join([
        "Traceback (most recent call last):",
        '  File "/private/workspace/modules/ideation/main.py", line 10, in main',
        "ValueError: idea.md failed its Markdown contract: missing section",
    ])
    message = web_server._framework_user_error(stdout, "fallback")

    assert message == "idea.md failed its Markdown contract: missing section"
    assert "Traceback" not in message
    assert "/private/workspace" not in message


def test_web_idea_and_plan_results_keep_project_and_plan_error_is_sanitized(monkeypatch):
    from contracts.web_models import AppConfig, IdeaRequest, PlanRequest
    from auto_research.web import server as web_server

    monkeypatch.setattr(web_server, "_cleruntime_caches", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(web_server, "_clerun_caches", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(web_server, "_run_framework_process", lambda *_args, **_kwargs: (0, "", {}))
    config = AppConfig()

    idea = web_server._run_ideation_framework_job(
        "demo",
        IdeaRequest(run_id="find_demo", project="demo", max_ideas=2),
        config,
        lambda _message: None,
        lambda: False,
        lambda *_args: None,
    )
    plan = web_server._run_planning_module_job(
        "demo",
        PlanRequest(run_id="find_demo", idea_ids=["idea-1"], repair_rounds=1),
        config,
        "plan",
        lambda _message: None,
        lambda: False,
        lambda *_args: None,
    )

    assert (idea["project"], idea["run_id"]) == ("demo", "find_demo")
    assert (plan["project"], plan["run_id"]) == ("demo", "find_demo")

    stdout = "\n".join([
        "Traceback (most recent call last):",
        '  File "/private/workspace/modules/planning/main.py", line 10, in main',
        "ValueError: plan.md failed its contract: missing experiment section",
    ])
    monkeypatch.setattr(web_server, "_run_framework_process", lambda *_args, **_kwargs: (1, stdout, {}))
    with pytest.raises(RuntimeError) as exc_info:
        web_server._run_planning_module_job(
            "demo",
            PlanRequest(run_id="find_demo", idea_ids=["idea-1"], repair_rounds=1),
            config,
            "plan",
            lambda _message: None,
            lambda: False,
            lambda *_args: None,
        )
    assert str(exc_info.value) == "plan.md failed its contract: missing experiment section"
    assert "Traceback" not in str(exc_info.value)
    assert "/private/workspace" not in str(exc_info.value)


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

    from runtime import runtime_env

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

    from runtime import runtime_env

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
    assert cmd[:4] == ["/env/bin/python", str(ROOT / "framework" / "scripts" / "main.py"), "workflow", "run"]
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
    monkeypatch.setenv("TIMEOUT_SEC", "3600")

    project, cmd = project_bridge.build_command({
        "project": "demo",
        "action": "find",
        "web_job_id": "find_test_web_action",
        "max_papers": 7,
        "max_ideas": 3,
        "skip_arxiv": True,
        "deep_survey": True,
        "queries": ["protein design"],
        "selection": {
            "venue_ids": ["dblp_icml"],
            "years": [2026],
            "venue_years": [{"venue_id": "dblp_icml", "year": 2026}],
            "include_arxiv": True,
        },
    })

    assert project == "demo"
    assert cmd[:5] == ["/env/bin/python", str(project_bridge.SCRIPTS / "main.py"), "find", "--project", "demo"]
    assert "--max-papers" in cmd and cmd[cmd.index("--max-papers") + 1] == "7"
    assert "--max-ideas" in cmd and cmd[cmd.index("--max-ideas") + 1] == "3"
    assert "--web-job-id" in cmd and cmd[cmd.index("--web-job-id") + 1] == "find_test_web_action"
    assert "--skip-arxiv" in cmd
    assert "--deep-survey" in cmd
    assert "--query" in cmd and cmd[cmd.index("--query") + 1] == "protein design"
    assert "--timeout-sec" not in cmd
    explicit_selection = json.loads(cmd[cmd.index("--selection-json") + 1])
    assert explicit_selection["venue_ids"] == ["dblp_icml"]
    assert explicit_selection["venue_years"] == [{"venue_id": "dblp_icml", "year": 2026}]
    assert explicit_selection["include_arxiv"] is True


def test_web_find_request_passes_project_selection_to_framework(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    project_root = tmp_path / "projects" / "demo"
    (project_root / "state").mkdir(parents=True)
    (project_root / "project.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
    selection = {
        "venue_ids": ["dblp_icml"],
        "years": [2026],
        "venue_years": [{"venue_id": "dblp_icml", "year": 2026}],
        "include_arxiv": True,
    }
    request = web_server.FindRequest(
        project="demo",
        config=web_server.AppConfig(
            provider="mock",
            research_interest="protein design",
            arxiv_queries=["protein inverse folding"],
            nonvenue_fetch_limit=2400,
            title_abstract_scoring_limit=1800,
            venue_title_scan_limit=0,
        ),
        selection=selection,
    )
    captured: dict[str, object] = {}
    persisted_config: dict[str, object] = {}

    monkeypatch.setattr(web_server, "_safe_project_root", lambda project: project_root)
    monkeypatch.setattr(web_server, "_active_web_stage_job_blocker", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_server, "_new_find_guard_blocker", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_server, "_request_config_with_persisted_secrets", lambda config: config)
    monkeypatch.setattr(web_server, "save_canonical_source_selection", lambda value, project_config_path=None: value)
    monkeypatch.setattr(web_server, "_persist_local_llm_config_from_find_request", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_server, "_sync_project_research_preferences_from_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        web_server,
        "_sync_project_finding_config_from_request",
        lambda config, _project_path: persisted_config.update(config.model_dump()),
    )
    monkeypatch.setattr(web_server, "_persist_jobs", lambda: None)

    def fake_run_action(payload, _log, _should_cancel, _progress):
        captured.update(payload)
        return {"status": "done"}

    class FakeJob:
        job_id = "find_test"
        result = None

        def as_dict(self):
            return {"job_id": self.job_id, "result": self.result}

    def fake_start_job(_stage, fn, job_id=None, initial_result=None):
        job = FakeJob()
        job.job_id = job_id or job.job_id
        job.result = initial_result
        job.result = fn(lambda _line: None, lambda: False, lambda *_args: None)
        return job

    monkeypatch.setattr(web_server, "run_action", fake_run_action)
    monkeypatch.setattr(web_server, "start_job", fake_start_job)

    web_server.api_find(request)

    assert captured["project"] == "demo"
    assert str(captured["web_job_id"]).startswith("find_")
    assert captured["selection"] == web_server.normalize_source_selection(selection)
    assert captured["queries"] == ["protein inverse folding"]
    assert "deep_survey" not in captured
    assert persisted_config["nonvenue_fetch_limit"] == 2400
    assert persisted_config["title_abstract_scoring_limit"] == 1800
    assert persisted_config["venue_title_scan_limit"] == 0


def test_project_job_start_is_atomic_per_project_but_allows_other_accounts(monkeypatch):
    from auto_research.web import server as web_server
    from auto_research.web.auth import AuthUser

    monkeypatch.setattr(web_server, "JOBS", {})
    monkeypatch.setattr(web_server, "_persist_jobs", lambda: None)
    monkeypatch.setattr(web_server, "_reconcile_stale_cancelling_jobs", lambda: None)
    monkeypatch.setattr(web_server, "action_gate_blocker", lambda payload: None)
    monkeypatch.setattr(web_server, "job_stage", lambda payload: "custom")
    monkeypatch.setattr(web_server, "_project_stage_running_blocker", lambda payload, stage: None)
    monkeypatch.setattr(
        web_server,
        "_account_project_id",
        lambda value, require_exists=False: f"u_{web_server._current_account().id}__{value}",
    )
    release = threading.Event()

    def fake_run_action(payload, _log, _should_cancel, _progress):
        release.wait(timeout=10)
        return {"status": "done", "project": payload["project"]}

    monkeypatch.setattr(web_server, "run_action", fake_run_action)
    alice = AuthUser(id="a" * 32, username="alice")
    bob = AuthUser(id="b" * 32, username="bob")
    project_job_endpoint = next(route.endpoint for route in web_server.app.routes if getattr(route, "path", "") == "/api/jobs/project")

    def launch(account):
        token = web_server._CURRENT_ACCOUNT.set(account)
        try:
            return project_job_endpoint({"project": "demo", "action": "custom"})
        finally:
            web_server._CURRENT_ACCOUNT.reset(token)

    barrier = threading.Barrier(3)
    same_project_results = []

    def simultaneous_alice_launch():
        barrier.wait(timeout=5)
        same_project_results.append(launch(alice))

    first = threading.Thread(target=simultaneous_alice_launch)
    second = threading.Thread(target=simultaneous_alice_launch)
    first.start()
    second.start()
    barrier.wait(timeout=5)
    first.join(timeout=5)
    second.join(timeout=5)

    assert sorted(getattr(result, "status_code", 200) for result in same_project_results) == [200, 409]
    bob_result = launch(bob)
    assert getattr(bob_result, "status_code", 200) == 200
    assert len(web_server.JOBS) == 2
    assert {job.owner_id for job in web_server.JOBS.values()} == {alice.id, bob.id}

    release.set()
    for job in web_server.JOBS.values():
        assert job.done.wait(timeout=5)


def test_web_paper_chat_action_uses_writing_module(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    _make_project(projects, "demo")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "project_target_venue", lambda project, default="": default or "ICLR")
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")

    payload = {"project": "demo", "action": "writing-chat", "stage": "paper", "message": "检查当前论文状态", "venue": "ICLR"}
    project, cmd = project_bridge.build_command(payload)

    assert project == "demo"
    assert cmd[:6] == ["/env/bin/python", str(ROOT / "framework" / "scripts" / "main.py"), "module", "writing", "--action", "chat"]
    assert "--project" in cmd and cmd[cmd.index("--project") + 1] == "demo"
    assert "--message" in cmd and cmd[cmd.index("--message") + 1] == "检查当前论文状态"
    assert "--venue" in cmd and cmd[cmd.index("--venue") + 1] == "ICLR"
    assert "--queue-if-busy" in cmd
    assert project_bridge.job_stage(payload) == "writing-chat"
    assert project_bridge._requested_panel_stage(payload, "writing-chat") == ("paper", "paper")


def test_web_paper_work_uses_framework_and_no_run_action(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    _make_project(projects, "demo")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "project_target_venue", lambda project, default="": default or "ICLR")
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")

    project, cmd = project_bridge.build_command({"project": "demo", "action": "paper", "venue": "ICLR"})

    assert project == "demo"
    assert cmd[:6] == ["/env/bin/python", str(ROOT / "framework" / "scripts" / "main.py"), "module", "writing", "--action", "work"]
    assert "--project" in cmd and cmd[cmd.index("--project") + 1] == "demo"
    assert "--queue-if-busy" in cmd
    assert "run" not in cmd


def test_legacy_claude_message_environment_stage_routes_to_environment_chat(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    _make_project(projects, "demo")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")

    payload = {"project": "demo", "action": "claude-message", "stage": "environment", "message": "检查环境门控"}
    project, cmd = project_bridge.build_command(payload)

    assert project == "demo"
    assert project_bridge.job_stage(payload) == "environment-chat"
    assert cmd[:6] == ["/env/bin/python", str(ROOT / "framework" / "scripts" / "main.py"), "module", "environment", "--action", "chat"]
    assert "--project" in cmd and cmd[cmd.index("--project") + 1] == "demo"
    assert "--message" in cmd and cmd[cmd.index("--message") + 1] == "检查环境门控"


def test_legacy_claude_message_paper_stage_routes_to_writing_chat(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    _make_project(projects, "demo")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "project_target_venue", lambda project, default="": default or "ICLR")
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")

    payload = {"project": "demo", "action": "claude-message", "stage": "paper", "message": "检查论文证据", "venue": "ICLR"}
    project, cmd = project_bridge.build_command(payload)

    assert project == "demo"
    assert project_bridge.job_stage(payload) == "writing-chat"
    assert cmd[:6] == ["/env/bin/python", str(ROOT / "framework" / "scripts" / "main.py"), "module", "writing", "--action", "chat"]
    assert "--message" in cmd and cmd[cmd.index("--message") + 1] == "检查论文证据"
    assert "--venue" in cmd and cmd[cmd.index("--venue") + 1] == "ICLR"


def test_legacy_claude_message_experiment_stage_routes_to_experimenting_chat(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    _make_project(projects, "demo")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")

    payload = {"project": "demo", "action": "claude-message", "stage": "experiment", "message": "检查实验 blocker"}
    project, cmd = project_bridge.build_command(payload)

    assert project == "demo"
    assert project_bridge.job_stage(payload) == "experimenting-chat"
    assert cmd[:6] == ["/env/bin/python", str(ROOT / "framework" / "scripts" / "main.py"), "module", "experimenting", "--action", "chat"]
    assert "--project" in cmd and cmd[cmd.index("--project") + 1] == "demo"
    assert "--stage" not in cmd
    assert "--queue-if-busy" in cmd
    assert "--message" in cmd and cmd[cmd.index("--message") + 1] == "检查实验 blocker"


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

    service_env = {
        "OPENAI_API_KEY": "service-openai-key",
        "OPENAI_BASE_URL": "https://service-openai.test/v1",
        "LLM_API_KEY": "service-llm-key",
        "LLM_MODEL": "service-model",
        "ANTHROPIC_API_KEY": "shared-claude-key",
        "PATH": "/usr/bin",
    }
    isolated = project_bridge._apply_runtime_env_overrides(service_env, {
        "runtime_env": {"FINDING_LLM_CONFIG": "/accounts/alice/llm.local.json"},
    })
    assert isolated["FINDING_LLM_CONFIG"] == "/accounts/alice/llm.local.json"
    assert isolated["ANTHROPIC_API_KEY"] == "shared-claude-key"
    assert isolated["PATH"] == "/usr/bin"
    assert "OPENAI_API_KEY" not in isolated
    assert "OPENAI_BASE_URL" not in isolated
    assert "LLM_API_KEY" not in isolated
    assert "LLM_MODEL" not in isolated


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
    text = (ROOT / "framework" / "scripts" / "orchestration" / "run_frontend.py").read_text(encoding="utf-8")

    assert '"api_key": "",' in text
    assert '"api_key": api_key' not in text


def test_web_find_settings_do_not_implicitly_force_deep_survey():
    text = (ROOT / "web" / "backend" / "auto_research" / "web" / "server.py").read_text(encoding="utf-8")

    assert 'payload["deep_survey"]' not in text
    assert "venue_scan_limit >=" not in text


def test_run_frontend_transmits_project_finding_budget_contract():
    text = (ROOT / "framework" / "scripts" / "orchestration" / "run_frontend.py").read_text(encoding="utf-8")

    assert 'nonvenue_fetch_limit = config_positive_int("nonvenue_fetch_limit", 5000)' in text
    assert 'title_abstract_scoring_limit = config_positive_int("title_abstract_scoring_limit", 1000)' in text
    assert 'venue_scan_limit = config_nonnegative_int("venue_title_scan_limit", 0)' in text
    assert '"nonvenue_fetch_limit": nonvenue_fetch_limit,' in text
    assert '"title_abstract_scoring_limit": title_abstract_scoring_limit,' in text
    assert '"venue_title_scan_limit": venue_scan_limit,' in text
    assert '"venue_title_scan_limit": _env_nonnegative_int(' in text
    assert "reproducible code dataset" not in text
    assert "recent benchmark method" not in text
    assert "executable research idea" not in text


def test_find_has_no_default_overall_3600_second_timeout():
    frontend_text = (ROOT / "framework" / "scripts" / "orchestration" / "run_frontend.py").read_text(encoding="utf-8")
    project_text = (ROOT / "framework" / "scripts" / "orchestration" / "run_project.py").read_text(encoding="utf-8")
    bridge_text = (ROOT / "framework" / "scripts" / "bridges" / "project_bridge.py").read_text(encoding="utf-8")

    assert 'parser.add_argument("--timeout-sec", type=int, default=0' in frontend_text
    assert 'if args.timeout_sec > 0:' in frontend_text
    assert 'timeout_sec = max(60, int(payload.get("timeout_sec")' not in bridge_text
    assert "os.environ.get('TIMEOUT_SEC', '3600')" not in project_text
    assert "run(taste_cmd, paths.root, paths.logs / '05a_finding_frontend.log', timeout=None)" in project_text


def test_run_frontend_runtime_tuning_replaces_stale_abstract_scoring_values():
    text = (ROOT / "framework" / "scripts" / "orchestration" / "run_frontend.py").read_text(encoding="utf-8")

    assert "elif name not in runtime_tuning and default is not None:" in text
    assert 'abstract_scoring_max_workers = env_int("ABSTRACT_SCORING_MAX_WORKERS", config_positive_int("abstract_scoring_max_workers"' in text
    assert 'abstract_scoring_batch_size = env_int("ABSTRACT_SCORING_BATCH_SIZE", config_positive_int("abstract_scoring_batch_size"' in text
    assert 'runtime_tuning["ABSTRACT_SCORING_BATCH_SIZE"] = str(abstract_scoring_batch_size)' in text
    assert 'runtime_tuning["ABSTRACT_SCORING_MAX_BATCH_SIZE"] = str(max(1, abstract_scoring_batch_size))' in text
    assert 'runtime_tuning["ABSTRACT_SCORING_MAX_WORKERS"] = str(abstract_scoring_max_workers)' in text
    assert 'runtime_tuning["ABSTRACT_SCORING_WORKER_CAP"] = str(max(1, abstract_scoring_max_workers))' in text
    assert 'runtime_tuning["ABSTRACT_SCORING_TIMEOUT_SEC"] = str(abstract_scoring_timeout_sec)' in text
    assert 'runtime_tuning["ARXIV_FULL_SCAN"] = str(os.environ.get("ARXIV_FULL_SCAN") or "0")' in text
    assert 'runtime_tuning["ARXIV_MAX_QUERIES"] = str(arxiv_max_queries)' in text
    assert 'runtime_tuning["ARXIV_TIMEOUT_SEC"] = str(arxiv_timeout_sec)' in text


def test_find_llm_concurrency_defaults_and_web_descriptions_are_ten():
    app_text = (ROOT / "web" / "frontend" / "client" / "src" / "App.tsx").read_text(encoding="utf-8")
    runtime_text = (ROOT / "modules" / "finding" / "scripts" / "core" / "finding_runtime.py").read_text(encoding="utf-8")
    models_text = (ROOT / "framework" / "scripts" / "contracts" / "web_models.py").read_text(encoding="utf-8")
    frontend_text = (ROOT / "framework" / "scripts" / "orchestration" / "run_frontend.py").read_text(encoding="utf-8")

    assert "llm_concurrency: 10," in app_text
    assert "abstract_scoring_max_workers: 10," in app_text
    assert "abstract_scoring_batch_size: 10," in app_text
    assert 'llmConcurrency: "标题 LLM 预筛并发数"' in app_text
    assert "最终标题+摘要评分使用独立并发，默认同为 10" in app_text
    assert "llm_concurrency: int = 10" in runtime_text
    assert "abstract_scoring_max_workers: int = 10" in runtime_text
    assert "llm_concurrency: int = 10" in models_text
    assert "abstract_scoring_max_workers: int = 10" in models_text
    assert 'config_positive_int("llm_concurrency", 10)' in frontend_text
    assert 'config_positive_int("abstract_scoring_max_workers", 10)' in frontend_text


def test_find_bridge_public_contract_has_no_legacy_budget_fields_or_env():
    paths = [
        ROOT / "framework" / "scripts" / "contracts" / "web_models.py",
        ROOT / "framework" / "scripts" / "orchestration" / "run_frontend.py",
        ROOT / "web" / "backend" / "auto_research" / "web" / "server.py",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    for field in ("max_fetch_papers", "find_recall_count", "detail_fetch_count", "arxiv_per_query_limit"):
        assert field not in text
    for env_name in ("FIND_RECALL_COUNT", "DETAIL_FETCH_COUNT", "ARXIV_PER_QUERY_LIMIT"):
        assert env_name not in text
    assert "MIN_TITLE_CANDIDATES" not in text
    assert "MIN_DETAIL_CANDIDATES" not in text


def test_web_biorxiv_category_input_preserves_official_multiword_subjects():
    text = (ROOT / "web" / "frontend" / "client" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "function splitCategoryList(value: string)" in text
    assert "value.split(/[,，;；\\n]+/)" in text
    assert 'updateConfig("biorxiv_categories", splitCategoryList(e.target.value))' in text


def test_web_manual_search_input_preserves_multiword_phrases():
    text = (ROOT / "web" / "frontend" / "client" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "function splitPhraseList(value: string)" in text
    assert 'updateConfig("arxiv_queries", splitPhraseList(e.target.value))' in text


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


def test_finding_main_streams_run_binding_before_pipeline_returns(monkeypatch, tmp_path):
    module_path = ROOT / "modules" / "finding" / "main.py"
    spec = importlib.util.spec_from_file_location("finding_main_stream_test", module_path)
    assert spec and spec.loader
    finding_main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(finding_main)

    class FakeConfig:
        def __init__(self, **_kwargs):
            pass

    class FakeSelection:
        def __init__(self, **_kwargs):
            pass

    class FakeFindRequest:
        def __init__(self, **kwargs):
            self.config = kwargs.get("config")
            self.selection = kwargs.get("selection")

    stderr = io.StringIO()

    def run_find(_request):
        print("Created run find_streamed")
        assert "Created run find_streamed" in stderr.getvalue()
        return {"run_id": "find_streamed"}

    modules = {
        "finding_runtime.models": SimpleNamespace(
            AppConfig=FakeConfig,
            VenueSelection=FakeSelection,
            FindRequest=FakeFindRequest,
            apply_runtime_tuning_env=lambda _config: {},
        ),
        "finding_runtime.source_selection": SimpleNamespace(
            default_source_selection=lambda: {},
            normalize_source_selection=lambda payload: payload,
        ),
        "flow.pipeline": SimpleNamespace(run_find=run_find),
        "finding_runtime.storage": SimpleNamespace(run_dir=lambda _run_id: tmp_path),
    }
    monkeypatch.setattr(finding_main, "_private_import", lambda name: modules[name])
    monkeypatch.setattr(finding_main, "_find_request_payloads", lambda **_kwargs: ({}, {}))
    monkeypatch.setattr(finding_main, "_result_summary", lambda _result, _run_dir: {})
    monkeypatch.setattr(finding_main.sys, "stderr", stderr)

    assert finding_main._run_find([]) == 0
    assert "Created run find_streamed" in stderr.getvalue()


def test_user_facing_find_markdown_is_canonical():
    sync_text = (ROOT / "framework" / "scripts" / "bridges" / "sync_outputs.py").read_text(encoding="utf-8")
    frontend_text = (ROOT / "framework" / "scripts" / "orchestration" / "run_frontend.py").read_text(encoding="utf-8")
    server_text = (ROOT / "web" / "backend" / "auto_research" / "web" / "server.py").read_text(encoding="utf-8")
    finding_text = (ROOT / "modules" / "finding" / "main.py").read_text(encoding="utf-8")
    pipeline_text = (ROOT / "modules" / "finding" / "scripts" / "flow" / "pipeline.py").read_text(encoding="utf-8")

    assert '"find.md"' in sync_text
    assert '"find.md"' in frontend_text
    assert '"find.md"' in server_text
    assert '(directory / "find.md").write_text' not in server_text
    assert '"find.md"' in finding_text
    assert 'run_dir / "find.md"' in pipeline_text
    old_find_markdown = "article" + ".md"
    for text in [sync_text, frontend_text, server_text, finding_text, pipeline_text]:
        assert old_find_markdown not in text


def test_find_web_project_artifacts_do_not_expose_maintainer_status():
    bridge_text = (ROOT / "framework" / "scripts" / "bridges" / "project_bridge.py").read_text(encoding="utf-8")
    frontend_text = (ROOT / "framework" / "scripts" / "orchestration" / "run_frontend.py").read_text(encoding="utf-8")
    sync_text = (ROOT / "framework" / "scripts" / "bridges" / "sync_outputs.py").read_text(encoding="utf-8")

    assert '("工作状态.txt", ROOT / "工作状态.txt"' not in bridge_text
    assert 'root / "planning" / "finding_frontend.md"' in bridge_text
    assert "# Find Frontend" in frontend_text
    assert "# Find Frontend" in sync_text
    assert "# native Frontend" not in frontend_text
    assert "# native Frontend" not in sync_text


def test_web_framework_do_not_import_finding_private_backend():
    files = [
        ROOT / "web" / "backend" / "auto_research" / "web" / "server.py",
        ROOT / "framework" / "scripts" / "bridges" / "project_bridge.py",
        ROOT / "framework" / "scripts" / "orchestration" / "run_frontend.py",
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
    assert cmd[:4] == ["/env/bin/python", str(ROOT / "framework" / "scripts" / "main.py"), "workflow", "run"]
    assert "--only-stage" in cmd
    assert cmd[cmd.index("--only-stage") + 1] == "environment"
    module_arg = cmd[cmd.index("--module-arg") + 1]
    assert module_arg.startswith("environment=--plan ")
    assert "modules/experimenting/main.py" not in " ".join(cmd)



def test_web_experiment_action_uses_framework_module_controller(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "project_target_venue", lambda project, default="": default or "ICLR")
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")
    monkeypatch.setattr(project_bridge, "_literature_recommendation_gate_is_blocked", lambda project: False)
    monkeypatch.setattr(project_bridge, "_fresh_base_data_is_blocked", lambda project: False)

    project, cmd = project_bridge.build_command({"project": "demo", "action": "experiment", "venue": "ICLR", "iterations": 2, "skip_claude": True})

    assert project == "demo"
    assert cmd[:6] == ["/env/bin/python", str(ROOT / "framework" / "scripts" / "main.py"), "module", "experimenting", "--action", "work"]
    assert cmd[cmd.index("--project") + 1] == "demo"
    assert cmd[cmd.index("--iterations") + 1] == "2"
    assert "--queue-if-busy" in cmd
    assert "--plan" not in cmd
    assert "--repo-path" not in cmd
    assert "--conda-env" not in cmd


def test_web_rejects_claude_message_without_module_stage(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    _make_project(projects, "demo")
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)

    with pytest.raises(ValueError, match="explicit module stage"):
        project_bridge.build_command({"project": "demo", "action": "claude-message", "message": "检查当前实验 blocker"})


def test_experiment_panel_never_falls_back_to_legacy_project_session(tmp_path):
    from auto_research.web import server as web_server

    root = tmp_path / "demo"
    state = root / "state"
    state.mkdir(parents=True)
    legacy = {
        "stage": "experiment",
        "status": "completed",
        "response_markdown": "legacy project session response",
        "web_visible_response": True,
    }
    (state / "claude_project_session_last_result.json").write_text(json.dumps(legacy), encoding="utf-8")

    assert project_bridge._latest_claude_receipt_for_stage(root, "experiment") == {}
    result, session_key, reason = web_server._latest_claude_stage_last_result(root, "experiment")
    assert result == {}
    assert session_key == "experimenting_controller"
    assert reason == "stage_receipt_not_found"

    controller = {**legacy, "response_markdown": "module controller response", "session_id": "session-1"}
    (state / "experimenting_controller_last_result.json").write_text(json.dumps(controller), encoding="utf-8")
    assert project_bridge._latest_claude_receipt_for_stage(root, "experiment")["response_markdown"] == "module controller response"
    result, session_key, reason = web_server._latest_claude_stage_last_result(root, "experiment")
    assert result["response_markdown"] == "module controller response"
    assert session_key == "experimenting_controller"
    assert reason == ""


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


def test_web_experiment_action_leaves_handoff_consumption_to_module(monkeypatch, tmp_path):
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
    assert "blocked_fresh_base_gate_required" not in " ".join(cmd)
    assert cmd[:6] == ["/env/bin/python", str(ROOT / "framework" / "scripts" / "main.py"), "module", "experimenting", "--action", "work"]
    assert "--repo-path" not in cmd
    assert "--conda-env" not in cmd


def test_full_cycle_only_sequences_the_seven_framework_actions():
    source = (ROOT / "framework" / "scripts" / "orchestration" / "run_full_research_cycle.py").read_text(encoding="utf-8")
    bridge_source = (ROOT / "framework" / "scripts" / "bridges" / "project_bridge.py").read_text(encoding="utf-8")

    assert 'STAGE_ACTIONS = ("find", "read", "idea", "plan", "environment", "experiment", "paper")' in source
    assert "build_command(payload)" in source
    assert 'action == "plan"' in source
    assert '"current-find-selection"' in source
    assert 'default=3' in source
    assert "claude_project_session.py" not in source
    assert "trajectory" not in source
    assert "audit_" not in source
    assert "experiment_registry" not in source
    assert "use_existing_literature_packet" not in source
    assert "_full_cycle_llm_readiness_block" not in bridge_source


def test_full_cycle_is_not_preempted_by_a_stage_specific_literature_gate(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    (projects / "demo").mkdir(parents=True)
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/env/bin/python")
    monkeypatch.setattr(project_bridge, "project_target_venue", lambda project, default="": default or "ICLR")
    monkeypatch.setattr(project_bridge, "_live_full_cycle_guard", lambda project: {})
    monkeypatch.setattr(project_bridge, "_literature_recommendation_gate_is_blocked", lambda project: True)
    payload = {"project": "demo", "action": "full-cycle", "venue": "ICLR"}

    assert project_bridge.action_gate_blocker(payload) is None
    _project, command = project_bridge.build_command(payload)
    joined = " ".join(command)
    assert "run_full_research_cycle.py" in joined
    assert "use-existing-literature-packet" not in joined
    assert "force-discovery" not in joined


def test_framework_live_process_detection_matches_run_id_case_insensitively(monkeypatch):
    class Result:
        returncode = 0
        stdout = "1234 S /env/bin/python framework/scripts/main.py workflow run --run-id web_environment_demo_20260621T104334Z"

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
    run_dir = tmp_path / "modules" / "environment" / ".runtime" / "runs" / "web_environment_demo_20260621T000000Z"
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
    (run_dir / "run_meta.json").write_text(json.dumps({
        "requested_project": "demo",
        "requested_run_id": "web_environment_demo_20260621T000000Z",
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
    run_dir = tmp_path / "modules" / "environment" / ".runtime" / "runs" / "web_environment_demo_20260621T000000Z"
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
    (run_dir / "run_meta.json").write_text(json.dumps({
        "requested_project": "demo",
        "requested_run_id": "web_environment_demo_20260621T000000Z",
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

    assert prefs["conda_env"] == "old_env"
    assert prefs["runtime"]["conda_env"] == "old_env"
    assert prefs["runtime"]["experiment_python"] == str(handoff_env / "bin" / "python")
    assert prefs["runtime"]["conda_env_prefix"] == str(handoff_env)
    assert merged_runtime["conda_env"] == "rigid"
    assert merged_runtime["conda_env_prefix"] == str(handoff_env)
    assert merged_runtime["experiment_python"] == str(handoff_env / "bin" / "python")
    assert diagnostics["runtime"]["conda_env"] == "rigid"
    assert diagnostics["runtime"]["conda_env_prefix"] == str(handoff_env)
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


def test_web_job_finished_at_round_trips_and_survives_compaction(monkeypatch):
    from auto_research.web import server as web_server

    monkeypatch.setattr(web_server, "JOBS", {})
    monkeypatch.setattr(web_server, "_persist_jobs", lambda: None)

    def outcome(result_status: str):
        if result_status == "error":
            raise RuntimeError("duration sentinel")
        return {"status": result_status}

    for stage in ["find", "read", "idea", "plan"]:
        for suffix, result_status, expected_status in [
            ("done", "done", "done"),
            ("blocked", "blocked_test_gate", "blocked"),
            ("error", "error", "error"),
        ]:
            job = web_server.start_job(
                stage,
                lambda _log, _cancel, _progress, status=result_status: outcome(status),
                job_id=f"{stage}_duration_{suffix}",
            )
            assert job.done.wait(2)
            assert job.status == expected_status
            assert job.finished_at
            assert job.finished_at >= job.created_at

            payload = job.as_dict(compact=False)
            restored = web_server.JobState.from_dict(payload)
            compact = web_server._compact_job_for_list(payload)
            assert restored.finished_at == job.finished_at
            assert compact["finished_at"] == job.finished_at


def test_web_job_persistence_acquires_registry_lock_before_persistence_lock(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    events = []

    class RecordingRLock:
        def __init__(self, name):
            self.name = name
            self.lock = threading.RLock()

        def __enter__(self):
            self.lock.acquire()
            events.append(("enter", self.name))
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            events.append(("exit", self.name))
            self.lock.release()

    monkeypatch.setattr(web_server, "JOBS", {})
    monkeypatch.setattr(web_server, "JOBS_PATH", tmp_path / "web_jobs.json")
    monkeypatch.setattr(web_server, "JOBS_LOCK", RecordingRLock("registry"))
    monkeypatch.setattr(web_server, "JOBS_PERSIST_LOCK", RecordingRLock("persistence"))

    web_server._persist_jobs()

    assert events[:3] == [
        ("enter", "registry"),
        ("enter", "persistence"),
        ("enter", "registry"),
    ]


def test_web_find_read_idea_plan_error_compaction_keeps_specific_exception():
    from auto_research.web import server as web_server

    for stage in ["find", "read", "idea", "plan"]:
        raw_logs = [
            "Traceback (most recent call last):",
            '  File "/zssd/private/TASTE/module.py", line 10, in main',
            f"RuntimeError: {stage} concrete failure sentinel.",
            "research action failed with exit code 1",
            "Traceback (most recent call last):\nRuntimeError: research action failed with exit code 1",
        ]
        progress = {"phase": "error", "current": 0, "total": 1, "percent": 0, "message": "research action failed with exit code 1"}
        result = {"status": "error", "run_id": "find_demo"}

        logs = web_server._public_job_logs(stage, raw_logs, progress, result)
        assert any(f"错误详情：RuntimeError: {stage} concrete failure sentinel." in line for line in logs)
        assert not any("错误详情：RuntimeError: research action failed with exit code 1" in line for line in logs)
        assert not any("Traceback" in line or "/zssd/private" in line for line in logs)

        repeated = web_server._public_job_logs(stage, logs, progress, result)
        assert any(f"错误详情：RuntimeError: {stage} concrete failure sentinel." in line for line in repeated)


def test_web_find_worker_tree_collapses_through_unclassified_wrappers():
    from auto_research.web import server as web_server

    frontend = {"pid": "100", "ppid": "10", "phase": "literature", "kind": "frontend_recovery"}
    driver = {"pid": "400", "ppid": "300", "phase": "literature", "kind": "driver_recovery"}
    process_rows = [
        {"pid": "100", "ppid": "10"},
        {"pid": "200", "ppid": "100"},
        {"pid": "300", "ppid": "200"},
        {"pid": "400", "ppid": "300"},
    ]

    rows = web_server._suppress_same_phase_descendant_workers([frontend, driver], process_rows)

    assert rows == [frontend]


def test_web_find_progress_projection_is_end_to_end_and_monotonic():
    from auto_research.web import server as web_server

    web_server._FIND_OVERALL_PROGRESS_CACHE.clear()
    base = {
        "run_id": "find_demo",
        "selection": {"venue_ids": ["iclr"], "include_arxiv": True, "include_biorxiv": True},
        "counts": {"raw_title_index": 5000, "evaluated_candidates": 0},
    }
    external = web_server._find_progress_projection({
        **base,
        "phase": "arxiv",
        "live_progress": {"phase": "arxiv", "current": 1, "total": 1, "percent": 100, "message": "arXiv complete"},
    })
    final_detail = web_server._find_progress_projection({
        **base,
        "phase": "final_detail_fetch",
        "live_progress": {"phase": "final_detail_fetch", "current": 1, "total": 2, "percent": 50, "message": "ICLR: fetching selected paper details"},
    })
    scoring = web_server._find_progress_projection({
        **base,
        "phase": "abstract_scoring",
        "live_progress": {"phase": "abstract_scoring", "current": 2, "total": 4, "percent": 50, "message": "Scoring batch 2/4"},
    })
    repeated_earlier_subphase = web_server._find_progress_projection({
        **base,
        "phase": "final_ranking_prepare",
        "live_progress": {"phase": "final_ranking_prepare", "current": 0, "total": 10, "percent": 0, "message": "Preparing next source"},
    })

    assert external == {
        "raw_phase": "arxiv",
        "raw_current": 1,
        "raw_total": 1,
        "stage_index": 3,
        "stage_total": 6,
        "stage_key": "extended_sources",
        "stage_label": "扩展渠道检索",
        "overall_percent": 51,
        "stage_percent": 50,
        "message": "arXiv complete",
        "counts": {"raw_title_index": 5000, "evaluated_candidates": 0},
    }
    assert final_detail["stage_key"] == "llm_evaluation"
    assert final_detail["overall_percent"] >= external["overall_percent"]
    assert scoring["overall_percent"] >= external["overall_percent"]
    assert repeated_earlier_subphase["overall_percent"] == scoring["overall_percent"]

    web_server._FIND_OVERALL_PROGRESS_CACHE.clear()
    venue_start = web_server._find_progress_projection({
        **base,
        "selection": {"venue_ids": ["iclr", "neurips", "icml"]},
        "venue_health_report": [{"venue_id": "iclr", "ok": True}],
        "live_progress": {"phase": "llm_title_filter", "current": 50, "total": 100, "percent": 50, "message": "ICLR: scored title batch 50/100"},
    })
    venue_later = web_server._find_progress_projection({
        **base,
        "selection": {"venue_ids": ["iclr", "neurips", "icml"]},
        "venue_health_report": [{"venue_id": "iclr", "ok": True}],
        "live_progress": {"phase": "detail_fetch", "current": 80, "total": 100, "percent": 80, "message": "ICLR: fetching selected paper details"},
    })
    assert venue_start["stage_key"] == "venue_pipeline"
    assert 0 < venue_start["stage_percent"] < venue_later["stage_percent"] < 34
    assert venue_later["overall_percent"] >= venue_start["overall_percent"]


def test_web_find_worker_waits_for_current_web_job_run_binding(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    web_server._FIND_OVERALL_PROGRESS_CACHE.clear()
    job = web_server.JobState("find_web", "find")
    job.status = "running"
    job.result = {"project": "demo", "action": "find"}
    monkeypatch.setattr(web_server, "JOBS", {job.job_id: job})
    monkeypatch.setattr(web_server, "_pid_alive_local", lambda _pid: True)
    monkeypatch.setattr(
        web_server,
        "_latest_find_run_id_from_runs",
        lambda *_args, **_kwargs: pytest.fail("a Web Find without a run binding must not scan global runs"),
    )
    old_complete = {
        "run_id": "find_old",
        "phase": "complete",
        "live_progress": {"phase": "complete", "current": 1, "total": 1, "percent": 100, "message": "complete"},
    }

    row = web_server._active_project_worker_job(
        "demo",
        tmp_path,
        {"pid": "100", "phase": "literature", "kind": "frontend_recovery", "cmd": "main.py find --project demo --web-job-id find_web"},
        {},
        {"run_id": "find_old"},
        old_complete,
    )

    assert row["run_id"] == ""
    assert row["result"]["find_progress"]["raw_phase"] == "initializing"
    assert row["progress"]["percent"] == 0


def test_web_find_worker_reads_only_current_web_job_bound_run(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    web_server._FIND_OVERALL_PROGRESS_CACHE.clear()
    job = web_server.JobState("find_web", "find")
    job.status = "running"
    job.run_id = "find_new"
    job.result = {"project": "demo", "action": "find"}
    monkeypatch.setattr(web_server, "JOBS", {job.job_id: job})
    monkeypatch.setattr(web_server, "_pid_alive_local", lambda _pid: True)
    run_directory = tmp_path / "find_new"
    run_directory.mkdir()
    (run_directory / "find_progress.json").write_text(json.dumps({
        "run_id": "find_new",
        "phase": "llm_title_filter",
        "live_progress": {"phase": "llm_title_filter", "current": 2, "total": 10, "percent": 20, "message": "scored title batch 2/10"},
    }), encoding="utf-8")
    monkeypatch.setattr(web_server, "run_dir", lambda _run_id: run_directory)

    row = web_server._active_project_worker_job(
        "demo",
        tmp_path,
        {"pid": "100", "phase": "literature", "kind": "frontend_recovery", "cmd": "main.py find --project demo --web-job-id find_web"},
        {},
        {"run_id": "find_old"},
        {"run_id": "find_old", "phase": "complete"},
    )

    assert row["run_id"] == "find_new"
    assert row["result"]["find_progress"]["raw_phase"] == "llm_title_filter"
    assert row["result"]["find_progress"]["message"] == "scored title batch 2/10"


def test_web_find_worker_rejects_another_projects_execution_id(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    job_a = web_server.JobState("find_a", "find")
    job_a.status = "running"
    job_a.run_id = "run_a"
    job_a.result = {"project": "project_a", "action": "find"}
    job_b = web_server.JobState("find_b", "find")
    job_b.status = "running"
    job_b.run_id = "run_b"
    job_b.result = {"project": "project_b", "action": "find"}
    monkeypatch.setattr(web_server, "JOBS", {job_a.job_id: job_a, job_b.job_id: job_b})
    monkeypatch.setattr(web_server, "_pid_alive_local", lambda _pid: True)

    row = web_server._active_project_worker_job(
        "project_a",
        tmp_path,
        {"pid": "100", "phase": "literature", "kind": "frontend_recovery", "cmd": "main.py find --project project_a --web-job-id find_b"},
        {},
        {},
        {"run_id": "run_old", "phase": "complete"},
    )

    assert row["run_id"] == ""
    assert row["result"]["find_progress"]["raw_phase"] == "initializing"


def test_web_find_merge_rejects_unbound_stale_worker_progress():
    from auto_research.web import server as web_server

    old_projection = {
        "raw_phase": "complete",
        "stage_label": "Find 完成",
        "overall_percent": 100,
        "message": "complete",
    }
    dynamic = [{
        "job_id": "project-worker_demo_100",
        "stage": "literature",
        "status": "running",
        "run_id": "find_old",
        "result": {
            "project": "demo",
            "run_id": "find_old",
            "kind": "frontend_recovery",
            "process_alive": True,
            "web_job_id": "find_web",
            "find_progress": old_projection,
        },
        "progress": {"phase": "find", "current": 100, "total": 100, "percent": 100, "message": "Find 完成：complete"},
    }]
    persisted = [{
        "job_id": "find_web",
        "stage": "find",
        "status": "running",
        "created_at": "2026-07-14T10:54:44Z",
        "run_id": "",
        "result": {"project": "demo", "action": "find", "web_job_id": "find_web"},
        "logs": ["Find started"],
        "progress": {"phase": "running", "current": 0, "total": 0, "percent": 0, "message": "Find running"},
    }]

    visible_dynamic, merged = web_server._merge_live_find_workers_into_web_jobs(dynamic, persisted)

    assert visible_dynamic == []
    assert merged[0]["run_id"] == ""
    assert merged[0]["result"]["find_progress"]["raw_phase"] == "initializing"
    assert merged[0]["progress"]["percent"] == 0


def test_web_jobs_merges_live_find_worker_and_run_progress_into_primary_job(monkeypatch):
    from auto_research.web import server as web_server

    web_server._LIVE_JOBS_CACHE.clear()
    monkeypatch.setattr(web_server, "_reconcile_detached_launcher_jobs", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_server, "_reconcile_stale_cancelling_jobs", lambda: None)
    monkeypatch.setattr(web_server, "_current_find_downstream_stage_history_jobs", lambda *args, **kwargs: [])
    monkeypatch.setattr(web_server, "_environment_decision_public_projection", lambda *args, **kwargs: {})
    run_id = "find_test_web_merge_130408"
    find_projection = {
        "raw_phase": "abstract_scoring",
        "raw_current": 2,
        "raw_total": 4,
        "stage_index": 4,
        "stage_total": 6,
        "stage_label": "摘要校验与 LLM 综合评估",
        "overall_percent": 74,
        "stage_percent": 55,
        "message": "NeurIPS: scoring batch 2/4",
        "counts": {"evaluated_candidates": 120},
    }
    dynamic = [
        {
            "job_id": "project-worker_demo_100",
            "stage": "literature",
            "status": "running",
            "created_at": "2026-07-13T13:04:03Z",
            "run_id": run_id,
            "logs": ["worker_kind=frontend_recovery", "find_live_progress=NeurIPS: scoring batch 2/4"],
            "result": {"project": "demo", "run_id": run_id, "web_job_id": "find_web", "kind": "frontend_recovery", "pid": "100", "process_alive": True, "find_progress": find_projection},
            "progress": {"phase": "find", "current": 74, "total": 100, "percent": 74, "message": "摘要校验与 LLM 综合评估：NeurIPS: scoring batch 2/4"},
        },
        {
            "job_id": "project-worker_demo_400",
            "stage": "literature",
            "status": "running",
            "created_at": "2026-07-13T13:04:04Z",
            "run_id": run_id,
            "logs": ["worker_kind=driver_recovery"],
            "result": {"project": "demo", "run_id": run_id, "web_job_id": "find_web", "kind": "driver_recovery", "pid": "400", "process_alive": True},
            "progress": {"phase": "find", "current": 74, "total": 100, "percent": 74, "message": "Find running"},
        },
    ]
    monkeypatch.setattr(web_server, "_live_jobs_from_projects", lambda **kwargs: dynamic)
    history_existing_run_ids = []

    def find_history(existing_run_ids, **kwargs):
        history_existing_run_ids.append(set(existing_run_ids))
        if run_id not in existing_run_ids:
            return [{
                "job_id": f"find-run-{run_id}",
                "stage": "find",
                "status": "cancelled",
                "run_id": run_id,
                "result": {"project": "demo", "run_id": run_id},
            }]
        return []

    monkeypatch.setattr(web_server, "_find_run_history_jobs_from_runs", find_history)
    job = web_server.JobState("find_web", "find")
    job.status = "cancelled"
    job.created_at = "2026-07-13T13:04:02Z"
    job.run_id = run_id
    job.logs = ["Find started"]
    job.result = {"project": "demo", "action": "find", "run_id": run_id, "web_job_id": "find_web"}
    job.cancelled_at = "2026-07-13T13:20:00Z"
    monkeypatch.setattr(web_server, "JOBS", {job.job_id: job})

    rows = web_server.api_jobs(compact=True, limit=10, include_history=True, project="demo")

    assert [row["job_id"] for row in rows] == ["find_web"]
    assert rows[0]["status"] == "running"
    assert rows[0]["run_id"] == run_id
    assert rows[0]["result"]["run_id"] == run_id
    assert rows[0]["result"]["find_progress"] == find_projection
    assert rows[0]["progress"]["percent"] == 74
    assert any("scoring batch 2/4" in line for line in rows[0]["logs"])
    assert history_existing_run_ids == [{run_id}]

    detail = web_server.api_job("find_web", compact=False)
    assert detail["run_id"] == run_id
    assert detail["result"]["find_progress"]["overall_percent"] == 74


def test_web_find_websocket_keeps_live_merged_snapshot(monkeypatch):
    from auto_research.web import server as web_server
    from auto_research.web.auth import AuthUser, SESSION_COOKIE

    projection = {
        "raw_phase": "abstract_scoring",
        "raw_current": 3,
        "raw_total": 4,
        "stage_index": 4,
        "stage_total": 6,
        "stage_key": "llm_evaluation",
        "stage_label": "摘要校验与 LLM 综合评估",
        "overall_percent": 80,
        "stage_percent": 75,
        "message": "NeurIPS: scoring batch 3/4",
        "counts": {"abstract_scored_papers": 90},
    }
    worker = {
        "job_id": "project-worker_demo_100",
        "stage": "literature",
        "status": "running",
        "run_id": "find_live",
        "logs": ["find_live_progress=NeurIPS: scoring batch 3/4"],
        "result": {
            "project": "demo",
            "run_id": "find_live",
            "kind": "frontend_recovery",
            "pid": "100",
            "process_alive": True,
            "web_job_id": "find_web",
            "find_progress": projection,
        },
        "progress": {"phase": "find", "current": 80, "total": 100, "percent": 80, "message": "NeurIPS: scoring batch 3/4"},
    }
    monkeypatch.setattr(web_server, "_live_jobs_from_projects", lambda **kwargs: [worker])
    monkeypatch.setattr(web_server.AUTH_STORE, "user_for_session", lambda token: AuthUser(id="a" * 32, username="alice") if token == "session" else None)
    monkeypatch.setattr(web_server, "_account_owns_job_payload", lambda payload: True)
    monkeypatch.setattr(web_server, "_account_owns_job_state", lambda state: True)
    job = web_server.JobState("find_web", "find")
    job.status = "cancelled"
    job.run_id = "find_live"
    job.result = {"project": "demo", "action": "find", "run_id": "find_live", "web_job_id": "find_web"}
    monkeypatch.setattr(web_server, "JOBS", {job.job_id: job})

    class FakeWebSocket:
        def __init__(self):
            self.messages = []
            self.cookies = {SESSION_COOKIE: "session"}

        async def accept(self):
            return None

        async def send_json(self, payload):
            self.messages.append(payload)

    socket = FakeWebSocket()

    async def stop_after_snapshot(_seconds):
        raise web_server.WebSocketDisconnect()

    monkeypatch.setattr(web_server.asyncio, "sleep", stop_after_snapshot)
    asyncio.run(web_server.ws_job(socket, "find_web"))

    snapshots = [message["job"] for message in socket.messages if message.get("type") == "snapshot"]
    assert len(snapshots) == 1
    assert snapshots[0]["status"] == "running"
    assert snapshots[0]["run_id"] == "find_live"
    assert snapshots[0]["result"]["find_progress"]["overall_percent"] == 80


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


def test_web_read_job_compaction_preserves_phase_progress_when_repeated():
    from auto_research.web import server as web_server

    source = {
        "job_id": "read_demo",
        "stage": "read",
        "status": "running",
        "created_at": "2026-07-10T14:00:00Z",
        "logs": [
            "Full-text acquisition phase: 3 papers, 2 workers",
            "Finished full-text acquisition 1/3: full_text=true - Paper One",
            "Finished full-text acquisition 2/3: full_text=false - Paper Two",
            "Finished full-text acquisition 3/3: full_text=true - Paper Three",
            "Reading subagent phase: 2 papers, 2 workers",
        ],
        "log_count": 5,
        "result": {"run_id": "find_demo", "status": "running"},
        "progress": {"phase": "full_text", "current": 1, "total": 3, "percent": 33, "message": "running"},
    }

    first = web_server._compact_job_for_list(source)
    second = web_server._compact_job_for_list(first)

    first_phase = first["progress"]["read_progress"]["phases"]["full_text"]
    second_phase = second["progress"]["read_progress"]["phases"]["full_text"]
    assert (first_phase["current"], first_phase["total"], first_phase["workers"], first_phase["status"]) == (3, 3, 2, "complete")
    assert second_phase == first_phase
    assert first["progress"]["read_progress"]["current_stage"] == "deep_read"
    assert "阶段进度：读文章 0/2" in second["logs"]


def test_web_read_startup_is_human_facing_and_deduplicated():
    from auto_research.web import server as web_server

    queued = web_server._read_job_progress_from_logs(
        [],
        {"phase": "queued", "current": 0, "total": 0, "percent": 0, "message": "Queued"},
        {"status": "running", "run_id": "find_demo"},
        status="running",
    )
    started = web_server._read_job_progress_from_logs(
        [
            "Full-text acquisition phase: 90 papers",
            "Full-text acquisition phase: 90 papers, 16 workers",
        ],
        {"phase": "full_text", "message": "running"},
        {"status": "running", "run_id": "find_demo"},
        status="running",
    )

    assert queued["current_action"] == "等待爬文章开始。"
    assert queued["recent_details"] == ["等待爬文章开始。"]
    assert started["current_action"] == "爬文章启动：共 90 篇，并发 16"
    assert started["recent_details"] == ["爬文章启动：共 90 篇，并发 16"]


def test_web_read_scoring_is_a_label_only_third_phase():
    from auto_research.web import server as web_server

    logs = [
        "Full-text acquisition phase: 2 papers, 2 workers",
        "Finished full-text acquisition 1/2: full_text=true - Paper One",
        "Finished full-text acquisition 2/2: full_text=true - Paper Two",
        "Reading subagent phase: 2 papers, 2 workers",
        "Finished reading subagent 1/2: complete / deep_read=True - Paper One",
        "Finished reading subagent 2/2: complete / deep_read=True - Paper Two",
        "Final Reading scoring phase: 2 completed reading artifacts",
    ]

    progress = web_server._read_job_progress_from_logs(
        logs,
        {"phase": "deep_read"},
        {"status": "running", "run_id": "find_demo"},
        status="running",
    )
    public_logs = web_server._public_read_job_logs(
        logs,
        {"phase": "deep_read"},
        {"status": "running", "run_id": "find_demo"},
    )

    assert progress["current_stage"] == "scoring"
    assert progress["current_action"] == "重新打分"
    assert progress["overall_percent"] == 100
    assert "scoring" not in progress["phases"]
    assert all("重新打分" not in line for line in public_logs)


def test_web_cancelled_read_keeps_completed_phase_and_current_run_logs():
    from auto_research.web import server as web_server

    job = web_server.JobState("read_demo", "read")
    job.status = "cancelled"
    job.run_id = "find_current"
    job.logs = [
        "Full-text acquisition phase: 3 papers, 2 workers",
        "Finished full-text acquisition 1/3: full_text=true - Paper One",
        "Finished full-text acquisition 2/3: full_text=true - Paper Two",
        "Finished full-text acquisition 3/3: full_text=true - Paper Three",
        "Reading subagent phase: 3 papers, 2 workers",
        "Finished reading subagent 1/3: complete / deep_read=True - Paper One",
        "Finished reading subagent 2/3: complete / deep_read=True - Paper Two",
        "Cancellation requested.",
    ]
    job.progress = {"phase": "cancelled", "current": 0, "total": 1, "percent": 0, "message": "Task cancelled by user."}
    job.result = {
        "status": "current_find_deep_read_complete_with_warnings",
        "run_id": "find_current",
        "summary": "旧 run 已完成精读 87/87",
        "warnings": [{"stage": "read", "title": "Old Paper", "status": "blocked_full_text_unavailable"}],
    }

    payload = job.as_dict(compact=True)
    phases = payload["progress"]["read_progress"]["phases"]

    assert (phases["full_text"]["current"], phases["full_text"]["total"], phases["full_text"]["status"]) == (3, 3, "complete")
    assert (phases["deep_read"]["current"], phases["deep_read"]["total"], phases["deep_read"]["status"]) == (2, 3, "cancelled")
    assert payload["progress"]["read_progress"]["current_stage"] == "deep_read"
    assert payload["logs"][0] == "当前状态：任务已取消：读文章停在 2/3。"
    assert any("Paper Two" in line for line in payload["logs"])
    assert all("87/87" not in line and "Old Paper" not in line for line in payload["logs"])


def test_web_completed_read_uses_attempted_and_eligible_phase_totals(monkeypatch):
    from auto_research.web import server as web_server

    monkeypatch.setattr(
        web_server,
        "_read_job_artifact_progress",
        lambda result, progress: {
            "total": 90,
            "full_text_current": 87,
            "deep_read_current": 87,
            "deep_read_attempted": 87,
            "pending_full_text": 3,
            "pending_deep_read": 3,
            "public_read_md_present": True,
            "validation_valid": True,
            "warning_count": 3,
        },
    )

    read_progress = web_server._read_job_progress_from_logs(
        [],
        {"phase": "complete"},
        {"status": "framework_synced_reading_outputs"},
        status="done",
    )

    full_text = read_progress["phases"]["full_text"]
    deep_read = read_progress["phases"]["deep_read"]
    assert (full_text["current"], full_text["total"], full_text["status"]) == (90, 90, "warning")
    assert (deep_read["current"], deep_read["total"], deep_read["status"]) == (87, 87, "complete")
    assert read_progress["current_stage"] == "deep_read"
    assert read_progress["overall_percent"] == 100


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
    registry_dir = tmp_path / "projects" / "demo" / "state"
    registry_dir.mkdir(parents=True)
    artifact_dir = tmp_path / "projects" / "demo" / "experiments" / "experimenting_runs" / "demo_run" / "iteration_01"
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


def test_api_artifacts_allows_read_only_find_scope_for_matching_live_project_run(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    (root / "state" / "finding_frontend.json").write_text(json.dumps({"run_id": "find_previous"}), encoding="utf-8")
    run_id = "find_live"
    run_directory = tmp_path / "runs" / run_id
    run_directory.mkdir(parents=True)
    (run_directory / "find_progress.json").write_text(json.dumps({
        "run_id": run_id,
        "phase": "llm_title_filter",
        "live_progress": {"phase": "llm_title_filter", "current": 4, "total": 10, "percent": 40},
    }), encoding="utf-8")
    worker = {
        "job_id": "project-worker_demo_100",
        "stage": "literature",
        "status": "running",
        "run_id": run_id,
        "result": {
            "project": "demo",
            "run_id": run_id,
            "kind": "frontend_recovery",
            "pid": "100",
            "process_alive": True,
        },
    }
    monkeypatch.setattr(web_server, "PROJECT_IDS_ROOT", projects)
    monkeypatch.setattr(web_server, "run_dir", lambda _run_id: run_directory)
    monkeypatch.setattr(web_server, "_live_jobs_from_projects", lambda **kwargs: [worker])
    web_server._RUN_ARTIFACTS_CACHE.clear()

    payload = web_server.api_artifacts(run_id, light=True, scope="find", project="demo")

    assert payload["project"] == "demo"
    assert payload["scope"] == "find"
    progress = next(item for item in payload["artifacts"] if item["name"] == "find_progress.json")
    assert progress["content"]["run_id"] == run_id


def test_api_artifacts_ideas_scope_reads_only_ideation_files(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    projects = tmp_path / "projects"
    root = _make_project(projects, "demo")
    finding = root / "planning" / "finding"
    finding.mkdir(parents=True, exist_ok=True)
    run_id = "find_ideas_scope_demo"
    (root / "state" / "finding_frontend.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    (finding / "idea.md").write_text(
        "# Ideation 生成的新论文想法\n\n[Paper](https://papers.nips.cc/paper_files/paper/2025/hash/demo.html)\n",
        encoding="utf-8",
    )
    (finding / "ideas.json").write_text(json.dumps({"run_id": run_id, "ideas": []}), encoding="utf-8")
    (finding / "read_results.json").write_text("not valid json", encoding="utf-8")
    run_directory = tmp_path / "runs" / run_id
    run_directory.mkdir(parents=True)
    monkeypatch.setattr(web_server, "run_dir", lambda _run_id: run_directory)
    monkeypatch.setattr(web_server, "PROJECT_IDS_ROOT", projects)
    monkeypatch.setattr(web_server, "_current_find_public_paper_ref_index", lambda _root: (_ for _ in ()).throw(AssertionError("paper_files URL must not hydrate paper ids")))
    web_server._RUN_ARTIFACTS_CACHE.clear()

    payload = web_server.api_artifacts(run_id, light=True, scope="ideas", project="demo")

    assert payload["project"] == "demo"
    assert payload["scope"] == "ideas"
    assert [item["name"] for item in payload["artifacts"]] == ["idea.md", "ideas.json"]


def test_api_artifacts_cache_is_scoped_by_explicit_project(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    projects = tmp_path / "projects"
    run_id = "find_shared_id"
    for project, title in (("alpha", "Alpha Idea"), ("beta", "Beta Idea")):
        root = _make_project(projects, project)
        finding = root / "planning" / "finding"
        finding.mkdir(parents=True, exist_ok=True)
        (root / "state" / "finding_frontend.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        (finding / "idea.md").write_text(f"# Ideation 生成的新论文想法\n\n## 1. {title}\n", encoding="utf-8")
        (finding / "ideas.json").write_text(json.dumps({"run_id": run_id, "ideas": [{"title": title}]}), encoding="utf-8")
    run_directory = tmp_path / "runs" / run_id
    run_directory.mkdir(parents=True)
    monkeypatch.setattr(web_server, "run_dir", lambda _run_id: run_directory)
    monkeypatch.setattr(web_server, "PROJECT_IDS_ROOT", projects)
    web_server._RUN_ARTIFACTS_CACHE.clear()

    alpha = web_server.api_artifacts(run_id, light=True, scope="ideas", project="alpha")
    beta = web_server.api_artifacts(run_id, light=True, scope="ideas", project="beta")

    alpha_markdown = next(item["content"] for item in alpha["artifacts"] if item["name"] == "idea.md")
    beta_markdown = next(item["content"] for item in beta["artifacts"] if item["name"] == "idea.md")
    assert "Alpha Idea" in alpha_markdown
    assert "Beta Idea" in beta_markdown


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


def test_active_paper_state_projects_canonical_writing_workspace(tmp_path):
    root = _make_project(tmp_path / "projects", "demo")
    workspace = root / "paper" / "writing"
    (workspace / "workspace" / "final").mkdir(parents=True)
    (workspace / "workspace" / "audits").mkdir(parents=True)
    (workspace / "venue").mkdir(parents=True)
    (workspace / "workspace" / "final" / "paper.tex").write_text("\\section{Demo}", encoding="utf-8")
    (workspace / "workspace" / "final" / "paper.pdf").write_text("%PDF-1.4", encoding="utf-8")
    (workspace / "workspace" / "refs.bib").write_text("@inproceedings{demo,title={Demo}}", encoding="utf-8")
    (workspace / "audit_repair_loop.json").write_text(json.dumps({"final_audit_status": "pass", "repair_history": [{"round": 1}]}), encoding="utf-8")
    (workspace / "workspace" / "audits" / "claude_quality_audit.json").write_text(json.dumps({"status": "pass", "blockers": []}), encoding="utf-8")
    (workspace / "venue" / "venue_requirements.json").write_text(json.dumps({"venue": "ICLR"}), encoding="utf-8")
    (workspace / "venue" / "template_source.json").write_text(json.dumps({"source": "official"}), encoding="utf-8")
    metadata = root / "paper" / "metadata"
    metadata.mkdir(parents=True)
    (metadata / "paper_pipeline.json").write_text(json.dumps({"writing_workspace": str(workspace), "writing_status": "generated"}), encoding="utf-8")

    state = paper_state.active_paper_state(root, "demo", {"target_venue": "ICLR"})

    assert state["writing_workspace"] == str(workspace)
    assert state["conference_preview_ready"] is True
    assert state["pdf_path"].endswith("workspace/final/paper.pdf")
    assert state["paper_citation_render_status"] == "pass"
    assert state["venue_requirements_status"] == "pass"


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


def test_api_artifacts_does_not_scan_writing_run_directories(monkeypatch, tmp_path):
    from auto_research.web import server as web_server

    run_id = "20260709T010203000000Z_chat-demo_pid123"
    projects = tmp_path / "projects"
    run_root = projects / "demo" / "paper" / "writing_chat_runs" / run_id
    run_root.mkdir(parents=True)

    monkeypatch.setattr(web_server, "PROJECT_IDS_ROOT", projects)
    monkeypatch.setattr(web_server, "FINDING_RUNS_DIR", tmp_path / "missing-finding-runs")
    monkeypatch.setattr(web_server, "FRAMEWORK_RUNS_DIR", tmp_path / "missing-framework-runs")
    monkeypatch.setattr(web_server, "ENVIRONMENT_RUNS_DIR", tmp_path / "missing-environment-runs")
    monkeypatch.setattr(web_server, "run_dir", lambda _run_id: (_ for _ in ()).throw(FileNotFoundError(_run_id)))
    web_server._RUN_ARTIFACTS_CACHE.clear()

    with pytest.raises(FileNotFoundError):
        web_server._run_artifact_roots(run_id)


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


def test_web_completed_read_job_keeps_detailed_logs_after_history_refresh():
    from auto_research.web import server as web_server

    run_id = "find_current"
    completed = {
        "job_id": "read_completed",
        "stage": "read",
        "status": "done",
        "created_at": "2026-06-21T05:00:00Z",
        "run_id": run_id,
        "logs": ["阶段进度：读文章 20/20", "细节：已完成精读 20/20"],
        "result": {"project": "demo", "run_id": run_id},
    }
    history = {
        "job_id": "current-find-read_demo_find_current",
        "stage": "read",
        "status": "done",
        "created_at": "2026-06-21T05:01:00Z",
        "run_id": run_id,
        "logs": ["当前状态：Read 阶段已完成。"],
        "result": {
            "project": "demo",
            "run_id": run_id,
            "kind": "current_find_downstream_artifact_history",
        },
    }

    collapsed = web_server._collapse_current_find_read_retry_jobs([completed, history], project_hint="demo")

    assert collapsed == [completed]
    assert collapsed[0]["logs"] == ["阶段进度：读文章 20/20", "细节：已完成精读 20/20"]


def test_web_read_machine_warnings_are_human_facing(monkeypatch):
    from auto_research.web import server as web_server

    monkeypatch.setattr(web_server, "_read_job_project_read_payload", lambda _result: {
        "warning_details": [
            {
                "phase": "full_text_acquisition",
                "title": "Paper A",
                "status": "blocked_full_text_unavailable",
            },
            {
                "phase": "read",
                "title": "Paper A",
                "status": "blocked_full_text_unavailable",
            },
        ],
        "warnings": ["1 篇当前 Find 推荐仍缺少同篇全文证据；错误/警告只进入任务日志和 read_results.json。"],
    })

    lines = web_server._read_job_machine_warning_lines({"project": "demo", "run_id": "find_current"})

    assert lines == [
        "警告：Paper A 的全文未就绪，因此未进入读文章阶段。",
        "警告：1 篇论文仍缺少同篇全文证据。",
    ]
    assert "blocked_" not in " ".join(lines)
    assert "read_results.json" not in " ".join(lines)


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
