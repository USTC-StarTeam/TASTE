import importlib
import json
import time
from datetime import date
from pathlib import Path

import pytest

from path_helpers import ensure_script_paths

from auto_research.auto_plan.pipeline import run_plan
from auto_research.auto_plan.pipeline import finish_plan, polish_plan, render_plan_markdown
from auto_research.emailer import build_run_email_html
from auto_research.jobs import JobCancelled
from auto_research.models import AppConfig, EmailJobRequest, FindRequest, PlanPolishRequest, PlanRequest, ReadRequest, VenueHealthRequest
from auto_research.storage import create_run_dir, delete_run, read_json, write_json, write_text
from auto_research.web import server
from auto_research.web.server import JOBS, api_artifacts, start_job


@pytest.fixture(autouse=True)
def isolate_web_job_state(tmp_path, monkeypatch):
    """Keep tests from writing the real runtime/state/web_jobs.json."""
    original_jobs = dict(server.JOBS)
    server.JOBS.clear()
    monkeypatch.setattr(server, "JOBS_PATH", tmp_path / "web_jobs.json")
    yield
    server.JOBS.clear()
    server.JOBS.update(original_jobs)


def test_script_llm_config_reads_runtime_env_overrides(monkeypatch):
    llm_client = importlib.import_module("llm_client")
    for key in ["LLM_API_KEY", "DEEPSEEK_API_KEY"]:
        monkeypatch.delenv(key, raising=False)
    cfg = {
        "llm": {"provider": "deepseek", "api_base": "https://example.test/v1", "model": "model", "api_key_env": "DEEPSEEK_API_KEY"},
        "runtime": {"env_overrides": {"DEEPSEEK_API_KEY": "secret-key", "LLM_RESPONSE_FORMAT": "json_object"}},
    }

    settings = llm_client.get_llm_config(cfg)

    assert settings["api_key"] == "secret-key"
    assert settings["response_format"] == "json_object"


def test_check_llm_ready_accepts_json_ok_response():
    check_llm_ready = importlib.import_module("check_llm_ready")

    assert check_llm_ready.readiness_response_ok('{"ok": true}') is True
    assert check_llm_ready.readiness_response_ok('{"status": "ready"}') is True
    assert check_llm_ready.readiness_response_ok('ok') is True
    assert check_llm_ready.readiness_response_ok('{"ok": false}') is False


def test_run_frontend_default_uses_management_python(monkeypatch, tmp_path):
    run_frontend = importlib.import_module("run_frontend")
    management_python = tmp_path / "python"
    management_python.write_text("#!/bin/sh\n", encoding="utf-8")
    management_python.chmod(0o755)
    monkeypatch.setenv("MANAGEMENT_PYTHON", str(management_python))
    args = type("Args", (), {"env_name": ""})()
    driver = tmp_path / "run_driver.py"

    cmd = run_frontend.driver_python_command(args, {}, driver)

    assert cmd == [str(management_python), str(driver)]


def recommended_paper(paper_id: str, title: str) -> dict:
    abstract = (
        f"{title} studies a concrete recommendation method with a reusable benchmark, detailed evaluation protocol, "
        "and auditable experimental setting for downstream full-paper reading. The abstract is intentionally long enough "
        "to satisfy the real-abstract guard used by the web recommendation contract."
    )
    return {
        "id": paper_id,
        "title": title,
        "url": f"https://example.test/{paper_id}",
        "abstract": abstract,
        "abstract_zh": f"{title} 的中文摘要已经由 Find 阶段生成，说明该论文包含可复用方法、基准和评测协议。",
        "fit_score": 8.0,
        "llm_fit_score": 8.0,
        "score": 8.0,
        "score_source": "llm_title_abstract_score_only",
        "reason_source": "llm abstract evaluation",
        "find_recommendation": True,
        "recommended_by_llm_ranking": True,
        "_user_visible_recommendation": True,
        "evidence_tier": "strong_recommendation",
    }



def test_sync_ars_uses_defined_source_lists(monkeypatch, tmp_path):
    syncer = importlib.import_module("sync_third_party_research_stack")
    monkeypatch.setattr(syncer, "THIRD_PARTY", tmp_path / "third_party")
    monkeypatch.setattr(syncer, "PROVENANCE_ROOT", tmp_path / "provenance")
    monkeypatch.setattr(syncer, "SKILL_ROOT", tmp_path / "skills")
    repo = tmp_path / "third_party" / "academic-research-skills"
    (repo / "academic-pipeline").mkdir(parents=True)
    (repo / "academic-pipeline" / "SKILL.md").write_text("---\ndescription: Academic pipeline.\n---\n# Academic Pipeline\n", encoding="utf-8")
    (repo / "shared").mkdir()
    (repo / "shared" / syncer.ARS_SHARED[0]).write_text("shared protocol", encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts" / syncer.ARS_SCRIPTS[0]).write_text("print('audit')\n", encoding="utf-8")

    modules, skills = syncer.sync_ars({"name": "academic", "repository": "example", "local_path": "third_party/academic-research-skills"})

    assert any(row["name"] == "academic-pipeline" and row["available"] for row in modules)
    assert skills and skills[0]["path"].endswith("SKILL.md")
    assert (tmp_path / skills[0]["path"]).exists()




def test_missing_source_method_sync_does_not_rewrite_existing_adapters(monkeypatch, tmp_path):
    syncer = importlib.import_module("sync_third_party_research_stack")
    monkeypatch.setattr(syncer, "THIRD_PARTY", tmp_path / "third_party")
    monkeypatch.setattr(syncer, "PROVENANCE_ROOT", tmp_path / "provenance")
    trajectory = tmp_path / "provenance" / "method-source-trajectory-system" / "SKILL.md"
    paper = tmp_path / "provenance" / "method-source-paper-production" / "SKILL.md"
    trajectory.parent.mkdir(parents=True)
    paper.parent.mkdir(parents=True)
    trajectory.write_text("existing trajectory adapter", encoding="utf-8")
    paper.write_text("existing paper adapter", encoding="utf-8")

    evo_modules, evo_skills = syncer.sync_evoscientist({"name": "EvoScientist"})
    paper_modules, paper_skills = syncer.sync_paper_orchestra({"name": "PaperOrchestra"})

    assert evo_modules and all(not row["available"] for row in evo_modules)
    assert paper_modules and all(not row["available"] for row in paper_modules)
    assert evo_skills == []
    assert paper_skills == []
    assert trajectory.read_text(encoding="utf-8") == "existing trajectory adapter"
    assert paper.read_text(encoding="utf-8") == "existing paper adapter"


def test_api_catalog_merges_sigkdd_kdd_aliases():
    full_name = "ACM SIGKDD Conference on Knowledge Discovery and Data Mining"
    rows = [row for row in server.api_catalog() if row.get("full_name") == full_name]

    assert len(rows) == 1
    assert rows[0]["name"] == "SIGKDD"
    assert rows[0]["rank"] == "A"
    assert any(alias.get("id") == "dblp_kdd" for alias in rows[0].get("aliases", []))


def test_venue_health_result_keeps_requested_venue_and_year(monkeypatch):
    monkeypatch.setattr(server, "fetch_venue_sample", lambda _venue, _year, _limit: {
        "venue_id": "",
        "year": "",
        "ok": True,
        "sample_count": "2",
        "source_adapter": "openreview",
        "message": "ok",
        "samples": [{"title": "paper"}],
    })

    result = server._fetch_venue_sample_with_timeout({"name": "ICLR"}, "openreview_iclr_2026", 2026, 2)

    assert result["venue_id"] == "openreview_iclr_2026"
    assert result["year"] == 2026
    assert result["ok"] is True
    assert result["sample_count"] == 2


def test_venue_health_check_updates_project_source_status(monkeypatch, tmp_path):
    from auto_research.web import project_bridge

    project = "demo_health"
    root = tmp_path / project
    (root / "state").mkdir(parents=True)
    (root / "project.json").write_text(json.dumps({"name": project, "topic": "demo"}), encoding="utf-8")
    monkeypatch.setattr(server, "PROJECT_IDS_ROOT", tmp_path)
    monkeypatch.setattr(project_bridge, "PROJECTS", tmp_path)
    monkeypatch.setattr(server, "fetch_venue_sample", lambda _venue, _year, _limit: {
        "venue_id": "",
        "year": "",
        "ok": True,
        "sample_count": "2",
        "source_adapter": "openreview",
        "message": "ok",
        "samples": [{"title": "paper"}],
    })

    response = server.api_venue_health(VenueHealthRequest(
        project=project,
        venue_ids=["openreview_iclr_2026"],
        years=[2026],
        venue_years=[{"venue_id": "openreview_iclr_2026", "year": 2026}],
        sample_limit=2,
    ))

    assert response["results"][0]["venue_id"] == "openreview_iclr_2026"
    payload = json.loads((root / "state" / "venue_health_status.json").read_text(encoding="utf-8"))
    row = payload["source_status"][0]
    assert row["source_kind"] == "venue_health"
    assert row["ok"] is True
    assert row["sample_count"] == 2
    rows = project_bridge._current_health_check_source_status_rows(
        project,
        root,
        {"venue_ids": ["openreview_iclr_2026"], "years": [2026], "venue_years": [{"venue_id": "openreview_iclr_2026", "year": 2026}]},
    )
    assert rows and rows[0]["venue_id"] == "openreview_iclr_2026"


def test_venue_health_rows_match_year_specific_and_base_venue_ids(tmp_path):
    from auto_research.web import project_bridge

    project = "demo_health_alias"
    root = tmp_path / project
    (root / "state").mkdir(parents=True)
    write_json(root / "state" / "venue_health_status.json", {
        "source_status": [{
            "source": "ICLR 2026",
            "source_kind": "venue_health",
            "venue_id": "openreview_iclr",
            "venue": "ICLR",
            "year": 2026,
            "status": "ok",
            "ok": True,
            "sample_count": 1,
        }]
    })

    rows = project_bridge._current_health_check_source_status_rows(
        project,
        root,
        {"venue_ids": ["openreview_iclr_2026"], "years": [2026], "venue_years": [{"venue_id": "openreview_iclr_2026", "year": 2026}]},
    )

    assert rows and rows[0]["venue_id"] == "openreview_iclr"


def test_venue_health_rows_respect_explicit_venue_year_pairs(tmp_path):
    from auto_research.web import project_bridge

    project = "demo_health_pair_filter"
    root = tmp_path / project
    (root / "state").mkdir(parents=True)
    write_json(root / "state" / "venue_health_status.json", {
        "source_status": [
            {"source": "ICLR 2026", "source_kind": "venue_health", "venue_id": "openreview_iclr", "venue": "ICLR", "year": 2026, "status": "ok", "ok": True, "sample_count": 1},
            {"source": "ICLR 2025", "source_kind": "venue_health", "venue_id": "openreview_iclr", "venue": "ICLR", "year": 2025, "status": "ok", "ok": True, "sample_count": 1},
        ]
    })

    rows = project_bridge._current_health_check_source_status_rows(
        project,
        root,
        {"venue_ids": ["openreview_iclr_2026"], "years": [2026, 2025], "venue_years": [{"venue_id": "openreview_iclr_2026", "year": 2026}]},
    )

    assert [row["year"] for row in rows] == [2026]


def test_hollow_done_find_without_run_id_is_hidden_from_taskbar():
    item = {
        "job_id": "find_empty",
        "stage": "find",
        "status": "done",
        "created_at": "2026-06-07T19:00:47Z",
        "logs": [],
        "run_id": "",
        "result": {"run_id": None},
    }

    assert server._job_is_hollow_route(item) is True


def test_multiple_jobs_are_visible_and_soft_cancelled():
    JOBS.clear()

    first = start_job("find", lambda _log, _should_cancel, _progress: {"run_id": "first"})
    second = start_job("find", lambda _log, _should_cancel, _progress: {"run_id": "second"})
    first.done.wait(2)
    second.done.wait(2)

    assert first.job_id in JOBS
    assert second.job_id in JOBS
    assert first.job_id != second.job_id

    def cancellable(_log, should_cancel, progress):
        progress("loop", 0, 100, "looping")
        for _ in range(100):
            if should_cancel():
                raise JobCancelled("cancelled in test")
            time.sleep(0.01)
        return {"run_id": "late"}

    job = start_job("read", cancellable)
    job.request_cancel()
    job.done.wait(3)

    assert job.status == "cancelled"
    assert job.cancel_requested is True
    assert job.cancelled_at


def test_live_find_blocks_duplicate_project_find_launch():
    JOBS.clear()
    job = server.JobState("find_live", "find")
    job.status = "running"
    job.result = {"project": "demo_project"}
    job.set_progress("venue_title_index", 0, 4, "Starting venue title index fetch")
    JOBS[job.job_id] = job

    blocker = server._active_web_stage_job_blocker("demo_project", "find")

    assert blocker
    assert blocker["status"] == "blocked_existing_project_stage_running"
    assert blocker["existing_job_id"] == "find_live"


def test_persisted_running_find_is_cancelled_after_server_restart(tmp_path, monkeypatch):
    JOBS.clear()
    jobs_path = tmp_path / "web_jobs.json"
    monkeypatch.setattr(server, "JOBS_PATH", jobs_path)
    write_json(jobs_path, {
        "jobs": [{
            "job_id": "find_stale",
            "stage": "find",
            "status": "running",
            "created_at": "2026-06-13T21:33:35Z",
            "logs": ["find started"],
            "run_id": "find_stale_run",
            "result": {"project": "demo_project", "action": "find"},
            "progress": {"phase": "venue_title_index", "current": 0, "total": 4, "percent": 0, "message": "Starting venue title index fetch"},
        }]
    })

    server._load_persisted_jobs()

    job = JOBS["find_stale"]
    assert job.status == "cancelled"
    assert job.cancelled_at
    assert job.progress["phase"] == "interrupted"
    assert "不是当前运行错误" in job.progress["message"]


def test_venue_title_scan_limit_zero_means_full_fetch(monkeypatch):
    from auto_research.auto_find import pipeline as find_pipeline

    monkeypatch.delenv("VENUE_TITLE_SCAN_LIMIT", raising=False)
    monkeypatch.delenv("FIND_VENUE_TITLE_SCAN_LIMIT", raising=False)

    assert find_pipeline._venue_title_fetch_limit(AppConfig()) is None
    assert find_pipeline._venue_title_fetch_limit(AppConfig(venue_title_scan_limit=0)) is None
    assert find_pipeline._venue_title_fetch_limit(AppConfig(venue_title_scan_limit=200)) == 200

    monkeypatch.setenv("VENUE_TITLE_SCAN_LIMIT", "0")
    assert find_pipeline._venue_title_fetch_limit(AppConfig(venue_title_scan_limit=200)) is None
    monkeypatch.setenv("VENUE_TITLE_SCAN_LIMIT", "50")
    assert find_pipeline._venue_title_fetch_limit(AppConfig()) == 50


def test_default_full_venue_fetch_does_not_cap_recall_to_max_fetch_papers():
    from auto_research.auto_find import pipeline as find_pipeline

    config = AppConfig(
        venue_title_scan_limit=0,
        max_fetch_papers=120,
        find_recall_count=1000,
        detail_fetch_count=160,
        max_recommended_papers=20,
    )

    assert find_pipeline._venue_recall_result_limit(config, 2000) == 1000


def test_final_recommendation_target_respects_configured_top_n():
    from auto_research.auto_find import pipeline as find_pipeline

    config = AppConfig(max_recommended_papers=4)

    assert find_pipeline._strong_recommendation_target_count(config, source_count=1) == 4
    assert find_pipeline._strong_recommendation_target_count(config, source_count=4) == 4


def test_final_recommendation_max_count_never_expands_visible_target(monkeypatch):
    from auto_research.auto_find import pipeline as find_pipeline

    monkeypatch.setenv("STRONG_RECOMMENDATION_MAX_COUNT", "50")
    config = AppConfig(max_recommended_papers=20)

    assert find_pipeline._strong_recommendation_target_count(config, source_count=5) == 20
    assert find_pipeline._strong_recommendation_output_count(config, source_count=5) == 20


def test_arxiv_empty_date_window_defaults_to_recent_half_year():
    from auto_research.auto_find import sources

    start_date, end_date, source = sources._arxiv_date_window("", "", today=date(2026, 6, 10))

    assert start_date == "2025-12-12"
    assert end_date == "2026-06-10"
    assert source == "default_recent_180_days"


def test_arxiv_configured_date_window_is_preserved():
    from auto_research.auto_find import sources

    start_date, end_date, source = sources._arxiv_date_window("2026/01/02", "", today=date(2026, 6, 10))

    assert start_date == "2026-01-02"
    assert end_date == ""
    assert source == "configured"


def test_run_frontend_reports_half_year_arxiv_window_by_default():
    source = (Path(__file__).resolve().parents[1] / "framework" / "scripts" / "run_frontend.py").read_text(encoding="utf-8")

    assert "DEFAULT_ARXIV_WINDOW_DAYS = 180" in source
    assert "secondary_window_days" not in source[source.index("arxiv_window_days = env_int"):source.index("venue_scan_limit = env_int")]


def test_run_project_uses_single_downstream_project_agent_route():
    source = (Path(__file__).resolve().parents[1] / "framework" / "scripts" / "run_project.py").read_text(encoding="utf-8")
    assert "05aa_current_find_read_idea_plan_route.log" in source
    assert "10_downstream_compile_route.log" in source
    assert "13c_project_agent_reflection_route.log" in source
    assert "run_" + "llm_" + "research_team.py" not in source


def test_run_loop_topic_ignores_project_id_placeholder(tmp_path):
    import importlib.util
    from types import SimpleNamespace

    script_path = Path(__file__).resolve().parents[1] / "modules" / "experimenting" / "scripts" / "run_loop.py"
    spec = importlib.util.spec_from_file_location("run_loop_topic_under_test", script_path)
    run_loop = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(run_loop)

    planning = tmp_path / "planning"
    state = tmp_path / "state"
    (planning / "finding").mkdir(parents=True)
    state.mkdir()
    write_json(planning / "finding" / "plans.json", {
        "selected_plan_id": "plan-1",
        "plans": [{"plan_id": "plan-1", "title": "Selected current Find experiment plan"}],
    })
    paths = SimpleNamespace(planning=planning, state=state)

    topic = run_loop.effective_loop_topic("demo_project", "demo_project", "", {"topic": "demo_project"}, paths)

    assert topic == "Selected current Find experiment plan"


def test_run_project_topic_ignores_project_id_placeholder(tmp_path):
    import importlib.util
    from types import SimpleNamespace

    script_path = Path(__file__).resolve().parents[1] / "framework" / "scripts" / "run_project.py"
    spec = importlib.util.spec_from_file_location("run_project_topic_under_test", script_path)
    run_project = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(run_project)

    planning = tmp_path / "planning"
    state = tmp_path / "state"
    (planning / "finding").mkdir(parents=True)
    state.mkdir()
    write_json(planning / "finding" / "plans.json", {
        "selected_plan_id": "plan-1",
        "plans": [{"plan_id": "plan-1", "title": "Evidence grounded paper agent benchmark"}],
    })
    paths = SimpleNamespace(planning=planning, state=state)
    cfg = {"topic": "demo_project", "queries": ["demo_project"]}

    topic = run_project.effective_project_topic("demo_project", "", cfg, paths)
    queries = run_project.planned_discovery_queries(cfg, paths, topic, project="demo_project")

    assert topic == "Evidence grounded paper agent benchmark"
    assert queries == ["Evidence grounded paper agent benchmark"]


def test_experiment_command_skips_discovery_by_default(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project = "demo_project"
    (tmp_path / project).mkdir(parents=True)
    monkeypatch.setattr(project_bridge, "PROJECTS", tmp_path)
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/py")
    monkeypatch.setattr(project_bridge, "_literature_recommendation_gate_is_blocked", lambda _project: False)
    monkeypatch.setattr(project_bridge, "_fresh_base_data_is_blocked", lambda _project: False)

    _, cmd = project_bridge.build_command({"project": project, "action": "experiment", "venue": "ICLR"})

    assert "--skip-discovery" in cmd
    assert "--topic" not in cmd


def test_default_find_selection_starts_with_no_sources():
    from auto_research.source_selection import default_source_selection, normalize_source_selection, source_enabled

    default_selection = default_source_selection()
    normalized_empty = normalize_source_selection({})
    assert default_selection["venue_ids"] == []
    assert default_selection["venue_years"] == []
    assert normalized_empty["venue_ids"] == []
    assert normalized_empty["venue_years"] == []
    for key in ["include_arxiv", "include_biorxiv", "include_huggingface", "include_github", "include_nature", "include_science"]:
        assert default_selection[key] is False
        assert normalized_empty[key] is False
    for source in ["venues", "arxiv", "biorxiv", "huggingface", "github", "nature", "science"]:
        assert source_enabled({}, source) is False


def test_project_list_orders_recent_activity_first(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    older = tmp_path / "aaa_old_project"
    newer = tmp_path / "zzz_recent_project"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    write_json(older / "project.json", {"name": "aaa_old_project", "topic": "older"})
    time.sleep(0.05)
    write_json(newer / "project.json", {"name": "zzz_recent_project", "topic": "newer"})
    state = newer / "state"
    state.mkdir()
    write_json(state / "current_find_progress.json", {"status": "running"})
    monkeypatch.setattr(project_bridge, "PROJECTS", tmp_path)

    rows = project_bridge.list_projects()

    assert [row["id"] for row in rows[:2]] == ["zzz_recent_project", "aaa_old_project"]
    assert rows[0]["updated_at"]


def test_source_selection_uses_workspace_root_env_for_project_config(tmp_path, monkeypatch):
    from auto_research.source_selection import canonical_source_selection

    project_root = tmp_path / "projects" / "demo_project"
    project_root.mkdir(parents=True)
    write_json(project_root / "project.json", {
        "discovery": {
            "canonical_source_selection": {
                "venue_ids": ["openreview_iclr_2026"],
                "years": [2026, 2025],
                "include_arxiv": False,
            }
        }
    })
    empty_config = tmp_path / "runtime" / ".config.json"
    empty_config.parent.mkdir(parents=True)
    write_json(empty_config, {})
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("PROJECT_ID", "demo_project")

    selection = canonical_source_selection(config_path=empty_config)

    assert selection["venue_ids"] == ["openreview_iclr_2026"]
    assert selection["years"] == [2026, 2025]
    assert selection["venue_years"] == [
        {"venue_id": "openreview_iclr_2026", "year": 2026},
        {"venue_id": "openreview_iclr_2026", "year": 2025},
    ]


def test_project_summary_run_preferences_keep_project_selection_over_stale_progress(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project = "demo_project_selection"
    root = tmp_path / project
    planning = root / "planning" / "finding"
    planning.mkdir(parents=True)
    write_json(root / "project.json", {"name": project, "topic": "Demo topic", "target_venue": "ICLR"})
    write_json(planning / "find_progress.json", {
        "run_id": "find_current",
        "phase": "complete",
        "selection": {"venue_ids": ["openreview_iclr_2026"], "years": [2026, 2025]},
    })
    project_selection = {
        "venue_ids": ["openreview_iclr_2026"],
        "years": [2026],
        "venue_years": [{"venue_id": "openreview_iclr_2026", "year": 2026}],
        "include_arxiv": False,
        "include_biorxiv": False,
        "include_huggingface": False,
        "include_github": False,
        "include_nature": False,
        "include_science": False,
    }
    monkeypatch.setattr(project_bridge, "project_source_selection", lambda _project: project_selection)
    monkeypatch.setattr(project_bridge, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])

    summary = project_bridge._fast_project_summary(project, root, {"name": project, "topic": "Demo topic", "target_venue": "ICLR"})

    prefs_selection = summary["run_preferences"]["default_find_selection"]
    assert prefs_selection["years"] == [2026]
    assert prefs_selection["venue_years"] == [{"venue_id": "openreview_iclr_2026", "year": 2026}]
    assert summary["literature_survey"]["selection"]["years"] == [2026, 2025]


def test_find_request_uses_project_research_profile_when_api_config_is_partial(tmp_path, monkeypatch):
    project_root = tmp_path / "projects" / "demo_project"
    project_root.mkdir(parents=True)
    project_path = project_root / "project.json"
    write_json(project_path, {
        "research_interest": "autonomous scientific workflow agents",
        "researcher_profile": "prefer reproducible evaluation evidence",
    })
    config_path = tmp_path / "runtime" / ".config.json"
    config_path.parent.mkdir(parents=True)
    write_json(config_path, {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
        "api_key": "sk-test-secret",
    })
    monkeypatch.setattr(server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(server, "project_config_path", lambda: project_path)

    cfg = server._request_config_with_persisted_secrets(AppConfig(provider="deepseek", model="deepseek-v4-flash"))

    assert cfg.research_interest == "autonomous scientific workflow agents"
    assert cfg.researcher_profile == "prefer reproducible evaluation evidence"
    assert cfg.api_key == "sk-test-secret"


def test_find_request_preserves_explicit_empty_research_profile(tmp_path, monkeypatch):
    project_root = tmp_path / "projects" / "demo_project"
    project_root.mkdir(parents=True)
    project_path = project_root / "project.json"
    write_json(project_path, {"research_interest": "old project topic", "researcher_profile": "old profile"})
    config_path = tmp_path / "runtime" / ".config.json"
    config_path.parent.mkdir(parents=True)
    write_json(config_path, {"provider": "mock", "model": "mock", "api_key": ""})
    monkeypatch.setattr(server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(server, "project_config_path", lambda: project_path)

    cfg = server._request_config_with_persisted_secrets(AppConfig(research_interest="", researcher_profile="", provider="mock", model="mock"))

    assert cfg.research_interest == ""
    assert cfg.researcher_profile == ""


def test_find_request_uses_project_topic_when_web_config_has_empty_profile(tmp_path, monkeypatch):
    project_root = tmp_path / "projects" / "demo_project"
    project_root.mkdir(parents=True)
    project_path = project_root / "project.json"
    write_json(project_path, {"name": "demo_project", "topic": "adaptive retrieval benchmark"})
    config_path = tmp_path / "runtime" / ".config.json"
    config_path.parent.mkdir(parents=True)
    write_json(config_path, {"provider": "mock", "model": "mock", "api_key": ""})
    monkeypatch.setattr(server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(server, "project_config_path", lambda: project_path)

    cfg = server._request_config_with_persisted_secrets(AppConfig(research_interest="", researcher_profile="", provider="mock", model="mock"))

    assert cfg.research_interest == "adaptive retrieval benchmark"
    assert cfg.researcher_profile == ""


def test_save_config_syncs_nonempty_research_profile_to_project(tmp_path, monkeypatch):
    project_root = tmp_path / "projects" / "demo_project"
    project_root.mkdir(parents=True)
    project_path = project_root / "project.json"
    write_json(project_path, {})
    config_path = tmp_path / "runtime" / ".config.json"
    config_path.parent.mkdir(parents=True)
    monkeypatch.setattr(server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(server, "project_config_path", lambda: project_path)

    server.save_config(AppConfig(research_interest="new topic", researcher_profile="new profile", provider="mock", model="mock"))

    saved_project = read_json(project_path, {})
    assert saved_project["research_interest"] == "new topic"
    assert saved_project["researcher_profile"] == "new profile"


def test_save_config_keeps_source_selection_in_project_config_only(tmp_path, monkeypatch):
    project_path = tmp_path / "projects" / "demo_project" / "project.json"
    project_path.parent.mkdir(parents=True)
    write_json(project_path, {"name": "demo_project"})
    config_path = tmp_path / "runtime" / ".config.json"
    config_path.parent.mkdir(parents=True)
    write_json(config_path, {"provider": "mock", "model": "mock", "default_find_selection": {"include_arxiv": True}})
    monkeypatch.setattr(server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(server, "project_config_path", lambda: project_path)

    selection = {"venue_ids": ["openreview_iclr_2026"], "years": [2026], "include_arxiv": False}
    server.save_config(AppConfig(provider="mock", model="mock", default_find_selection=selection))

    saved_project = read_json(project_path, {})
    saved_runtime = read_json(config_path, {})
    assert saved_project["default_find_selection"]["venue_ids"] == ["openreview_iclr_2026"]
    assert saved_project["discovery"]["canonical_source_selection"]["venue_ids"] == ["openreview_iclr_2026"]
    assert "default_find_selection" not in saved_runtime


def test_claude_code_launch_does_not_derive_account_from_taste_llm_config():
    from auto_research.web import project_bridge

    bridge_source = Path(project_bridge.__file__).read_text()
    session_source = (Path(__file__).resolve().parents[1] / "framework" / "scripts" / "claude_project_session.py").read_text()

    assert "_inject_saved_llm_env" not in bridge_source
    assert "ANTHROPIC_AUTH_TOKEN" not in bridge_source
    assert "ANTHROPIC_API_KEY" not in bridge_source
    assert "ANTHROPIC_BASE_URL" not in bridge_source
    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in bridge_source
    assert "os.environ.get('LLM_MODEL')" not in session_source
    assert "os.environ.get('ANTHROPIC_MODEL')" not in session_source


def test_read_request_for_historical_run_uses_current_find_wrapper_when_current_read_is_pending(tmp_path):
    root = tmp_path / "projects" / "demo_project"
    taste_dir = root / "planning" / "finding"
    state_dir = root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    write_json(taste_dir / "find_results.json", {"run_id": "find_current"})
    write_json(taste_dir / "read_results.json", {"run_id": "find_current", "source": "pending_new_find_read", "status": "pending", "readings": []})
    write_json(taste_dir / "ideas.json", {"run_id": "find_current", "source": "pending_new_find_idea", "ideas": []})
    write_json(taste_dir / "plans.json", {"run_id": "find_current", "source": "pending_new_find_plan", "plans": []})
    write_json(
        state_dir / "current_find_research_plan.json",
        {"run_id": "find_current", "status": "pending_current_find_read", "next_required_action": "run_read_for_current_find"},
    )

    request = ReadRequest(run_id="find_historical", max_papers=0)

    assert server._read_request_should_use_current_find_wrapper(request, "demo_project", root) is True


def test_read_request_for_historical_run_can_use_ordinary_read_after_current_find_is_complete(tmp_path):
    root = tmp_path / "projects" / "demo_project"
    taste_dir = root / "planning" / "finding"
    state_dir = root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    write_json(taste_dir / "find_results.json", {"run_id": "find_current"})
    write_json(taste_dir / "read_results.json", {"run_id": "find_current", "source": "claude_code_current_find_takeover", "readings": [{"title": "Current Paper"}]})
    write_json(taste_dir / "ideas.json", {"run_id": "find_current", "source": "claude_code_current_find_takeover", "ideas": [{"id": "idea-1"}]})
    write_json(taste_dir / "plans.json", {"run_id": "find_current", "source": "claude_code_current_find_takeover", "plans": [{"plan_id": "plan-1"}]})
    write_json(state_dir / "current_find_claude_reading_validation.json", {"run_id": "find_current", "valid": True})
    write_json(state_dir / "current_find_research_plan.json", {"run_id": "find_current", "status": "claude_takeover_ready"})

    request = ReadRequest(run_id="find_historical", max_papers=0)

    assert server._read_request_should_use_current_find_wrapper(request, "demo_project", root) is False


def test_current_find_read_is_incomplete_when_idea_or_plan_contract_is_empty(tmp_path):
    root = tmp_path / "projects" / "demo_project"
    taste_dir = root / "planning" / "finding"
    state_dir = root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    write_json(taste_dir / "find_results.json", {"run_id": "find_current"})
    write_json(taste_dir / "read_results.json", {"run_id": "find_current", "source": "claude_code_current_find_takeover", "readings": [{"title": "Current Paper"}]})
    write_json(taste_dir / "ideas.json", {"run_id": "find_current", "source": "claude_code_current_find_takeover", "ideas": []})
    write_json(taste_dir / "plans.json", {"run_id": "find_current", "source": "claude_code_current_find_takeover", "plans": []})
    write_json(state_dir / "current_find_claude_reading_validation.json", {"run_id": "find_current", "valid": True})

    assert server._current_find_read_is_incomplete(root, "find_current", idea_count=2) is True

    write_json(taste_dir / "ideas.json", {"run_id": "find_current", "source": "claude_code_current_find_takeover", "ideas": [{"id": "idea-1"}, {"id": "idea-2"}]})
    write_json(taste_dir / "plans.json", {"run_id": "find_current", "source": "claude_code_current_find_takeover", "plans": [{"plan_id": "plan-1"}, {"plan_id": "plan-2"}]})

    assert server._current_find_read_is_incomplete(root, "find_current", idea_count=2) is False


def test_current_find_read_validation_requires_repair_only_for_pending_current_run(tmp_path):
    root = tmp_path / "projects" / "demo_project"
    state_dir = root / "state"
    state_dir.mkdir(parents=True)

    write_json(
        state_dir / "current_find_claude_reading_validation.json",
        {
            "run_id": "find_demo",
            "valid": False,
            "full_text_reading_count": 15,
            "pending_full_text_reading_count": 5,
            "pending_full_text_reading_titles": ["Pending Paper"],
        },
    )

    assert server._current_find_read_validation_requires_repair(root, "find_demo") is True

    write_json(
        state_dir / "current_find_claude_reading_validation.json",
        {"run_id": "find_demo", "valid": True, "pending_full_text_reading_count": 0},
    )
    assert server._current_find_read_validation_requires_repair(root, "find_demo") is False

    write_json(
        state_dir / "current_find_claude_reading_validation.json",
        {"run_id": "find_old", "valid": False, "pending_full_text_reading_count": 5},
    )
    assert server._current_find_read_validation_requires_repair(root, "find_demo") is False


def test_current_find_downstream_artifacts_appear_as_idea_plan_job_history(monkeypatch, tmp_path):
    project = "demo_project"
    projects_root = tmp_path / "projects"
    root = projects_root / project
    finding = root / "planning" / "finding"
    state_dir = root / "state"
    finding.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    write_json(state_dir / "finding_frontend.json", {"taste_run_id": "find_current", "status": "find_completed"})
    write_json(
        state_dir / "current_find_research_plan.json",
        {
            "run_id": "find_current",
            "status": "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection",
            "selected_idea_id": "idea-1",
            "selected_plan_id": "plan-1",
        },
    )
    write_json(
        finding / "ideas.json",
        {
            "run_id": "find_current",
            "ideas": [
                {"id": "idea-1", "title": "Idea 1", "score": 8, "selected_for_execution": True},
                {"id": "idea-2", "title": "Idea 2", "objective_scores": {"novelty": 7}},
            ],
        },
    )
    write_json(
        finding / "plans.json",
        {
            "run_id": "find_current",
            "plans": [
                {"plan_id": "plan-1", "title": "Plan 1", "ready_for_gate": True, "selected_for_execution": True},
                {"plan_id": "plan-2", "title": "Plan 2"},
            ],
        },
    )
    monkeypatch.setattr(server, "PROJECT_IDS_ROOT", projects_root)

    jobs = server._current_find_downstream_stage_history_jobs(project, existing_items=[])

    by_stage = {row["stage"]: row for row in jobs}
    assert set(by_stage) == {"idea", "plan"}
    assert by_stage["idea"]["status"] == "done"
    assert by_stage["idea"]["run_id"] == "find_current"
    assert by_stage["idea"]["result"]["idea_count"] == 2
    assert by_stage["idea"]["result"]["selected_idea_id"] == "idea-1"
    assert "idea-1" not in by_stage["idea"]["progress"]["message"]
    assert by_stage["plan"]["result"]["plan_count"] == 2
    assert by_stage["plan"]["result"]["selected_plan_id"] == "plan-1"
    assert "plan-1" not in by_stage["plan"]["progress"]["message"]

    jobs_with_existing_idea = server._current_find_downstream_stage_history_jobs(
        project,
        existing_items=[{"stage": "idea", "run_id": "find_current", "result": {"project": project}}],
    )
    assert {row["stage"] for row in jobs_with_existing_idea} == {"plan"}


def test_current_find_read_retry_history_keeps_latest_public_row():
    project = "demo_project"
    rows = [
        {
            "job_id": "read_old",
            "stage": "read",
            "status": "blocked",
            "created_at": "2026-06-14T01:00:00Z",
            "run_id": "find_current",
            "result": {"project": project, "run_id": "find_current"},
        },
        {
            "job_id": "read_latest",
            "stage": "read",
            "status": "done",
            "created_at": "2026-06-14T02:00:00Z",
            "run_id": "find_current",
            "result": {"project": project, "run_id": "find_current"},
        },
        {
            "job_id": "idea_history",
            "stage": "idea",
            "status": "done",
            "created_at": "2026-06-14T02:01:00Z",
            "run_id": "find_current",
            "result": {"project": project, "run_id": "find_current"},
        },
        {
            "job_id": "read_old_run",
            "stage": "read",
            "status": "blocked",
            "created_at": "2026-06-13T02:00:00Z",
            "run_id": "find_previous",
            "result": {"project": project, "run_id": "find_previous"},
        },
    ]

    collapsed = server._collapse_current_find_read_retry_jobs(rows, project_hint=project)

    assert {row["job_id"] for row in collapsed} == {"read_latest", "idea_history", "read_old_run"}


def test_current_find_read_job_passes_configured_idea_count(tmp_path, monkeypatch):
    root = tmp_path / "projects" / "demo_project"
    taste_dir = root / "planning" / "finding"
    taste_dir.mkdir(parents=True)
    write_json(taste_dir / "find_results.json", {"run_id": "find_current"})
    captured: dict[str, list[str]] = {}

    class FakePopen:
        def __init__(self, cmd, **_kwargs):
            captured["cmd"] = list(cmd)
            self.stdout = iter(['{"status":"current_find_claude_read_complete"}\n'])

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            return None

    monkeypatch.setattr(server, "load_config", lambda: AppConfig(max_ideas=2))
    monkeypatch.setattr(server.subprocess, "Popen", FakePopen)

    result = server._run_current_find_claude_read_job(
        "demo_project",
        root,
        ReadRequest(run_id="find_current", max_papers=0),
        lambda _line: None,
        lambda: False,
        lambda *_args: None,
    )

    assert captured["cmd"][captured["cmd"].index("--idea-count") + 1] == "2"
    assert result["idea_count"] == 2


def test_repair_deep_read_fragment_source_is_current_and_preferred(tmp_path):
    import importlib.util

    module_path = Path(__file__).resolve().parents[1] / "modules" / "reading" / "scripts" / "ensure_current_find_research_plan.py"
    spec = importlib.util.spec_from_file_location("ensure_current_find_research_plan_test", module_path)
    ensure_plan = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(ensure_plan)

    taste_dir = tmp_path / "planning" / "finding"
    fragment_dir = taste_dir / "current_find_deep_read_fragments"
    fragment_dir.mkdir(parents=True)
    run_id = "find_demo"
    title = "Repair Fragment Selection Paper"

    def reading(limitations: str) -> dict:
        return {
            "paper_id": "paper-1",
            "title": title,
            "abstract_zh": "中文摘要" * 120,
            "motivation_zh": "研究动机" * 90,
            "method_details_zh": "方法细节" * 260,
            "experiments_zh": "实验结果" * 150,
            "limitations_zh": limitations,
            "method_advantages_zh": ["优势说明" * 40, "另一个优势" * 35],
            "method_disadvantages_zh": ["不足说明" * 40, "另一个不足" * 35],
            "full_text_available": True,
            "full_text_status": "pdf_text_read",
            "pdf_text_chars": 12000,
            "subagent_deep_read": True,
            "deep_read_audit": {"mode": "task_subagent", "subagent_used": True, "status": "completed"},
        }

    write_json(
        fragment_dir / "01_paper-1.json",
        {"run_id": run_id, "source": ensure_plan.CURRENT_FIND_DEEP_READ_FRAGMENT_SOURCE, "reading": reading("短局限")},
    )
    write_json(
        fragment_dir / "01_paper-1_repair_attempt2.json",
        {"run_id": run_id, "source": ensure_plan.CURRENT_FIND_DEEP_READ_FRAGMENT_REPAIR_SOURCE, "reading": reading("返修后的详细局限" * 90)},
    )

    rows = ensure_plan._current_find_deep_read_fragment_rows(taste_dir, run_id)
    selected = ensure_plan._select_current_find_readings_from_candidates(
        rows,
        {"run_id": run_id, "strong_recommendations": [{"id": "paper-1", "title": title}]},
        {},
    )

    assert len(rows) == 2
    assert selected[0]["deep_read_audit"]["fragment_path"].endswith("01_paper-1_repair_attempt2.json")
    assert selected[0]["deep_read_source"] == ensure_plan.CURRENT_FIND_DEEP_READ_FRAGMENT_REPAIR_SOURCE


def test_nested_plan_selection_is_lifted_into_current_find_contract():
    import importlib.util

    module_path = Path(__file__).resolve().parents[1] / "modules" / "reading" / "scripts" / "ensure_current_find_research_plan.py"
    spec = importlib.util.spec_from_file_location("ensure_current_find_research_plan_test_selection", module_path)
    ensure_plan = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(ensure_plan)

    idea = {
        "id": "idea-1",
        "title": "Idea One",
        "new_method": "new method",
        "initial_experiment": "initial experiment",
    }
    raw_plan = {
        "plan_id": "plan-1",
        "idea_id": "idea-1",
        "title": "Plan One",
        "steps": ["specific step with measurable protocol"],
        "plans_selection": {
            "selected": True,
            "selected_by": "main_claude_code_after_deep_read",
            "reason": "best evidence alignment",
        },
    }

    plan = ensure_plan._sanitize_plan(raw_plan, idea, 1, [], [], [])
    selection = ensure_plan.apply_current_find_execution_selection([idea], [plan], executable=True)

    assert plan["selected_for_execution"] is True
    assert plan["execute_next"] is True
    assert plan["execution_selection"]["reason"] == "best evidence alignment"
    assert selection["selected_plan_id"] == "plan-1"
    assert selection["selection_issue"] == ""




def test_current_find_plan_bridge_gate_blocks_stale_plan(tmp_path):
    from types import SimpleNamespace

    full_cycle = importlib.import_module("run_full_research_cycle")
    paths = SimpleNamespace(state=tmp_path / "state", planning=tmp_path / "planning")
    finding = paths.planning / "finding"
    finding.mkdir(parents=True)
    paths.state.mkdir(parents=True)

    write_json(finding / "find_results.json", {"run_id": "find_new", "strong_recommendations": [recommended_paper("p1", "Paper 1")]})
    write_json(finding / "read_results.json", {"run_id": "find_old", "readings": [{"title": "Paper 1"}]})
    write_json(finding / "ideas.json", {"run_id": "find_old", "ideas": []})
    write_json(finding / "plans.json", {"run_id": "find_old", "plans": []})
    write_json(paths.state / "current_find_research_plan.json", {"run_id": "find_old", "status": "ready", "read_idea_plan_ready": True, "claude_current_find_ready": True})
    write_json(paths.state / "current_find_claude_reading_validation.json", {"run_id": "find_old", "valid": True, "policy_version": full_cycle.CURRENT_FIND_FULL_TEXT_POLICY_VERSION, "actual_reading_count": 1, "full_text_reading_count": 1, "pending_full_text_reading_count": 0, "blockers": []})

    gate = full_cycle.current_find_plan_bridge_gate_status(paths, {"bridge_return_code": 0})

    assert gate["blocking"] is True
    assert gate["status"] == "blocked_current_find_plan_bridge"
    assert any("stale" in blocker for blocker in gate["blockers"])


def test_current_find_plan_bridge_gate_passes_current_ready_plan(tmp_path):
    from types import SimpleNamespace

    full_cycle = importlib.import_module("run_full_research_cycle")
    paths = SimpleNamespace(state=tmp_path / "state", planning=tmp_path / "planning")
    finding = paths.planning / "finding"
    finding.mkdir(parents=True)
    paths.state.mkdir(parents=True)
    recommendations = [recommended_paper(f"p{idx}", f"Paper {idx}") for idx in range(1, 6)]
    readings = [{"title": row["title"], "full_text_evidence": {"available": True}} for row in recommendations]
    ideas = [{"id": f"idea-{idx}", "title": f"Idea {idx}"} for idx in range(1, 6)]
    plans = [
        {
            "plan_id": f"plan-{idx}",
            "idea_id": f"idea-{idx}",
            "title": f"Plan {idx}",
            "selected_for_execution": idx == 1,
            "execute_next": idx == 1,
        }
        for idx in range(1, 6)
    ]
    write_json(finding / "find_results.json", {"run_id": "find_new", "strong_recommendations": recommendations})
    write_json(finding / "read_results.json", {"run_id": "find_new", "readings": readings})
    write_json(finding / "ideas.json", {"run_id": "find_new", "ideas": ideas})
    write_json(finding / "plans.json", {"run_id": "find_new", "plans": plans})
    write_json(paths.state / "current_find_research_plan.json", {"run_id": "find_new", "status": "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection", "read_idea_plan_ready": True, "claude_current_find_ready": True, "selected_plan_id": "plan-1", "ideas": ideas, "plans": plans})
    write_json(paths.state / "current_find_claude_reading_validation.json", {"run_id": "find_new", "valid": True, "policy_version": full_cycle.CURRENT_FIND_FULL_TEXT_POLICY_VERSION, "actual_reading_count": 5, "full_text_reading_count": 5, "pending_full_text_reading_count": 0, "blockers": []})

    gate = full_cycle.current_find_plan_bridge_gate_status(paths, {"bridge_return_code": 0})

    assert gate["blocking"] is False
    assert gate["status"] == "pass"
    assert gate["selected_plan_id"] == "plan-1"

def test_ensure_claude_plan_state_respects_configured_idea_count(tmp_path, monkeypatch):
    import importlib.util
    from types import SimpleNamespace

    module_path = Path(__file__).resolve().parents[1] / "modules" / "reading" / "scripts" / "ensure_current_find_research_plan.py"
    spec = importlib.util.spec_from_file_location("ensure_current_find_research_plan_test_state", module_path)
    ensure_plan = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(ensure_plan)
    monkeypatch.setattr(ensure_plan, "load_project_config", lambda _project: {"target_venue": "ICLR"})

    paths = SimpleNamespace(root=tmp_path, state=tmp_path / "state", planning=tmp_path / "planning")
    finding = paths.planning / "finding"
    finding.mkdir(parents=True)
    paths.state.mkdir(parents=True)
    write_json(finding / "find_results.json", {"run_id": "find_demo", "strong_recommendations": [{"title": "Paper 1"}, {"title": "Paper 2"}]})
    write_json(finding / "read_results.json", {"run_id": "find_demo", "source": ensure_plan.CLAUDE_TAKEOVER_SOURCE, "readings": [{"title": "Paper 1"}, {"title": "Paper 2"}], "targeted_search_queries": ["query one", "query two", "query three"]})
    write_json(paths.state / "current_find_claude_reading_validation.json", {"run_id": "find_demo", "valid": True, "policy_version": ensure_plan.FULL_TEXT_READ_POLICY_VERSION, "actual_reading_count": 2, "full_text_reading_count": 2, "pending_full_text_reading_count": 0, "blockers": []})

    def idea(idx: int) -> dict:
        return {
            "id": f"idea-{idx}",
            "title": f"Idea {idx}",
            "new_method": "new method " * 8,
            "initial_experiment": "initial experiment " * 8,
            "inspired_by": [{"title": "Paper 1", "inspiration": "method"}],
            "supporting_papers": [{"title": "Paper 1"}],
            "objective_scores": {key: 7.5 for key in ensure_plan.IDEA_OBJECTIVE_SCORE_KEYS},
            "score": 7.5,
            "idea_score": 7.5,
            "idea_score_audit": {"mode": "task_subagent", "subagent_used": True, "status": "completed"},
        }

    ideas = [idea(1), idea(2)]
    plans = [
        {"plan_id": "plan-1", "idea_id": "idea-1", "title": "Plan 1", "steps": ["specific step"], "selected_for_execution": True, "execute_next": True, "execution_selection": {"selected": True, "selected_by": "main_claude_code_after_deep_read", "reason": "best"}},
        {"plan_id": "plan-2", "idea_id": "idea-2", "title": "Plan 2", "steps": ["specific step"], "selected_for_execution": False, "execute_next": False, "execution_selection": {"selected": False, "selected_by": "not_selected_candidate_backlog", "reason": "backlog"}},
    ]
    write_json(finding / "ideas.json", {"run_id": "find_demo", "source": ensure_plan.CLAUDE_TAKEOVER_SOURCE, "ideas": ideas, "targeted_search_queries": ["query one", "query two", "query three"]})
    write_json(finding / "plans.json", {"run_id": "find_demo", "source": ensure_plan.CLAUDE_TAKEOVER_SOURCE, "plans": plans, "targeted_search_queries": ["query one", "query two", "query three"]})

    payload = ensure_plan.ensure_claude_plan_state("demo_project", paths, "find_demo", [{"title": "Paper 1"}, {"title": "Paper 2"}], ideas, plans, {"status": "completed", "return_code": 0}, idea_count=2)

    assert payload["status"] == "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection"
    assert payload["current_find_idea_count"] == 2
    assert payload["selected_plan_id"] == "plan-1"
    assert not payload.get("idea_contract_issues")


def test_current_find_payload_currentness_uses_file_mtime_when_generated_at_is_stale(tmp_path):
    module_path = Path(__file__).resolve().parents[1] / 'modules' / 'reading' / 'scripts' / 'ensure_current_find_research_plan.py'
    spec = importlib.util.spec_from_file_location('ensure_current_find_research_plan_test_mtime', module_path)
    ensure_plan = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(ensure_plan)

    artifact = tmp_path / 'ideas.json'
    write_json(
        artifact,
        {
            'run_id': 'find_current',
            'source': ensure_plan.CLAUDE_TAKEOVER_SOURCE,
            'generated_at': '2000-01-01T00:00:00+00:00',
            'ideas': [],
        },
    )
    current_revision = ensure_plan.dt.datetime.fromtimestamp(artifact.stat().st_mtime, ensure_plan.dt.timezone.utc) - ensure_plan.dt.timedelta(seconds=1)

    assert ensure_plan.claude_output_payloads_or_files_are_current([read_json(artifact)], [artifact], current_revision) is True


def test_structured_artifacts_preserve_same_run_claude_ideas_when_refresh_is_empty(tmp_path, monkeypatch):
    import importlib.util
    from types import SimpleNamespace

    module_path = Path(__file__).resolve().parents[1] / "modules" / "reading" / "scripts" / "ensure_current_find_research_plan.py"
    spec = importlib.util.spec_from_file_location("ensure_current_find_research_plan_test_preserve", module_path)
    ensure_plan = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(ensure_plan)

    paths = SimpleNamespace(root=tmp_path, state=tmp_path / "state", planning=tmp_path / "planning")
    finding = paths.planning / "finding"
    finding.mkdir(parents=True)
    paths.state.mkdir(parents=True)
    run_id = "find_demo"

    def idea(idx: int) -> dict:
        return {
            "id": f"idea-{idx}",
            "title": f"Idea {idx}",
            "new_method": "specific current-find method " * 4,
            "initial_experiment": "specific minimum experiment " * 4,
            "inspired_by": [{"title": "Paper 1", "inspiration": "method"}],
            "supporting_papers": [{"title": "Paper 1"}],
            "objective_scores": {key: 7.5 for key in ensure_plan.IDEA_OBJECTIVE_SCORE_KEYS},
            "score": 7.5,
            "idea_score": 7.5,
            "idea_score_audit": {"mode": "task_subagent", "subagent_used": True, "status": "completed"},
        }

    ideas = [idea(1), idea(2)]
    plans = [
        {"plan_id": "plan-1", "idea_id": "idea-1", "title": "Plan 1", "steps": ["specific step"], "selected_for_execution": True, "execute_next": True, "execution_selection": {"selected": True, "selected_by": "main_claude_code_after_deep_read", "reason": "best"}},
        {"plan_id": "plan-2", "idea_id": "idea-2", "title": "Plan 2", "steps": ["specific step"], "selected_for_execution": False, "execute_next": False, "execution_selection": {"selected": False, "selected_by": "not_selected_candidate_backlog", "reason": "backlog"}},
    ]
    write_json(finding / "ideas.json", {"run_id": run_id, "source": ensure_plan.CLAUDE_TAKEOVER_SOURCE, "ideas": ideas})
    write_json(finding / "plans.json", {"run_id": run_id, "source": ensure_plan.CLAUDE_TAKEOVER_SOURCE, "plans": plans})

    ensure_plan.write_current_find_structured_artifacts(
        paths,
        finding,
        run_id,
        readings=[{"title": "Paper 1"}],
        ideas=[],
        plans=[],
        takeover={"status": "completed", "return_code": 0},
        validation={"valid": True},
    )

    preserved_ideas = read_json(finding / "ideas.json")["ideas"]
    preserved_plans = read_json(finding / "plans.json")["plans"]
    assert len(preserved_ideas) == 2
    assert len(preserved_plans) == 2
    assert read_json(finding / "plans.json")["selected_plan_id"] == "plan-1"


def test_repair_accepts_find_current_artifacts_even_when_before_repair_start(tmp_path, monkeypatch):
    import importlib.util
    from types import SimpleNamespace

    module_path = Path(__file__).resolve().parents[1] / "modules" / "reading" / "scripts" / "ensure_current_find_research_plan.py"
    spec = importlib.util.spec_from_file_location("ensure_current_find_research_plan_test_repair_current", module_path)
    ensure_plan = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(ensure_plan)

    paths = SimpleNamespace(root=tmp_path, state=tmp_path / "state", planning=tmp_path / "planning")
    finding = paths.planning / "finding"
    finding.mkdir(parents=True)
    paths.state.mkdir(parents=True)
    run_id = "find_demo"
    find_revision = ensure_plan.parse_iso_time("2026-06-10T21:17:00+00:00")
    artifact_time = "2026-06-10T21:37:00+00:00"

    def idea(idx: int) -> dict:
        return {
            "id": f"idea-{idx}",
            "title": f"Idea {idx}",
            "new_method": "specific current-find method " * 4,
            "initial_experiment": "specific minimum experiment " * 4,
            "inspired_by": [{"title": "Paper 1", "inspiration": "method"}],
            "supporting_papers": [{"title": "Paper 1"}],
            "objective_scores": {key: 7.5 for key in ensure_plan.IDEA_OBJECTIVE_SCORE_KEYS},
            "score": 7.5,
            "idea_score": 7.5,
            "idea_score_audit": {"mode": "task_subagent", "subagent_used": True, "status": "completed"},
        }

    readings = [{"title": "Paper 1"}, {"title": "Paper 2"}]
    ideas = [idea(1), idea(2)]
    plans = [
        {"plan_id": "plan-1", "idea_id": "idea-1", "title": "Plan 1", "steps": ["specific step"], "selected_for_execution": True, "execute_next": True, "execution_selection": {"selected": True, "selected_by": "main_claude_code_after_deep_read", "reason": "best"}},
        {"plan_id": "plan-2", "idea_id": "idea-2", "title": "Plan 2", "steps": ["specific step"], "selected_for_execution": False, "execute_next": False, "execution_selection": {"selected": False, "selected_by": "not_selected_candidate_backlog", "reason": "backlog"}},
    ]
    validation = {
        "run_id": run_id,
        "valid": True,
        "policy_version": ensure_plan.FULL_TEXT_READ_POLICY_VERSION,
        "generated_at": artifact_time,
        "actual_reading_count": 2,
        "full_text_reading_count": 2,
        "pending_full_text_reading_count": 0,
        "blockers": [],
    }
    write_json(finding / "ideas.json", {"run_id": run_id, "source": ensure_plan.CLAUDE_TAKEOVER_SOURCE, "generated_at": artifact_time, "ideas": ideas})
    write_json(finding / "plans.json", {"run_id": run_id, "source": ensure_plan.CLAUDE_TAKEOVER_SOURCE, "generated_at": artifact_time, "plans": plans})
    write_json(finding / "read_results.json", {"run_id": run_id, "source": ensure_plan.CLAUDE_TAKEOVER_SOURCE, "generated_at": artifact_time, "readings": readings})

    def fail_takeover(*_args, **_kwargs):
        raise AssertionError("valid same-run artifacts should not trigger another repair")

    monkeypatch.setattr(ensure_plan, "run_claude_current_find_takeover", fail_takeover)
    takeover = {
        "status": "completed",
        "return_code": 0,
        "started_at": "2026-06-10T21:42:00+00:00",
        "finished_at": "2026-06-10T21:46:00+00:00",
        "prompt_path": "prompt.md",
    }

    result = ensure_plan.maybe_repair_current_find_takeover(
        "demo_project",
        paths,
        finding,
        run_id,
        {"run_id": run_id, "strong_recommendations": [{"title": "Paper 1"}, {"title": "Paper 2"}]},
        takeover,
        readings,
        ideas,
        plans,
        ["query one", "query two", "query three"],
        validation,
        effective_read_limit=2,
        min_required_readings=2,
        idea_count=2,
        find_revision=find_revision,
    )

    updated_takeover, _readings, updated_ideas, updated_plans, _queries, _validation, changed_run = result
    assert changed_run == ""
    assert updated_takeover["contract_validation_valid"] is True
    assert len(updated_ideas) == 2
    assert len(updated_plans) == 2


def test_compact_read_job_result_does_not_project_paper_status(monkeypatch):
    monkeypatch.setattr(server, "_paper_stage_from_project_snapshot", lambda _project: {"status": "preview_available", "pdf_path": "paper/output/iclr/paper.pdf"})

    compact = server._compact_job_result(
        {
            "project": "demo_project",
            "run_id": "find_demo",
            "status": "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection",
            "target_venue": "ICLR",
        },
        stage="read",
        job_id="read_demo",
    )

    assert compact["status"] == "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection"
    assert "paper_stage" not in compact
    assert compact.get("pdf_path") is None


def test_compact_claude_job_preserves_panel_stage():
    compact = server._compact_job_result(
        {
            "project": "demo_project",
            "action": "claude-message",
            "agent_id": "main",
            "requested_stage": "paper",
            "panel_stage": "paper",
            "status": "done",
        },
        stage="claude-message",
        job_id="claude-message_demo",
    )

    assert compact["action"] == "claude-message"
    assert compact["agent_id"] == "main"
    assert compact["requested_stage"] == "paper"
    assert compact["panel_stage"] == "paper"

    listed = server._compact_job_for_list({
        "job_id": "claude-message_demo",
        "stage": "claude-message",
        "status": "done",
        "created_at": "2026-06-09T00:00:00Z",
        "logs": [],
        "result": compact,
        "progress": {"phase": "complete", "current": 1, "total": 1, "percent": 100, "message": "done"},
    })
    assert listed["result"]["requested_stage"] == "paper"
    assert listed["result"]["panel_stage"] == "paper"
    assert listed["stage"] == "paper"

    env_compact = server._compact_job_result(
        {
            "project": "demo_project",
            "action": "claude-message",
            "agent_id": "main",
            "requested_stage": "environment",
            "panel_stage": "environment",
            "status": "done",
        },
        stage="claude-message",
        job_id="claude-message_env",
        logs=["conference-preview paper evidence should not override panel stage"],
    )

    listed_env = server._compact_job_for_list({
        "job_id": "claude-message_env",
        "stage": "claude-message",
        "status": "preview_available",
        "created_at": "2026-06-09T00:00:00Z",
        "logs": ["conference-preview paper evidence should not override panel stage"],
        "result": env_compact,
        "progress": {"phase": "complete", "current": 1, "total": 1, "percent": 100, "message": "done"},
    })
    assert listed_env["stage"] == "environment"
    assert listed_env["status"] == "done"
    assert listed_env["result"]["requested_stage"] == "environment"
    assert listed_env["result"]["panel_stage"] == "environment"
    assert server._is_paper_job("claude-message", "claude-message_env", env_compact, listed_env["logs"]) is False





def test_running_claude_environment_job_uses_panel_stage_and_public_progress():
    initial = server._initial_project_agent_job_result({"action": "claude-message", "project": "demo", "stage": "environment", "agent_id": "main"}, "claude-message")
    assert initial["panel_stage"] == "environment"
    row = server._compact_job_for_list({
        "job_id": "claude-message_demo",
        "stage": "claude-message",
        "status": "running",
        "created_at": "2026-06-10T00:00:00Z",
        "progress": {
            "phase": "running",
            "current": 5,
            "total": 0,
            "percent": 0,
            "message": "Claude: 调用工具: Read file=/tmp/taste-local/TASTE/projects/demo/state/repo_data_requirements.json",
        },
        "logs": [
            "claude-message started",
            "Workflow command: /tmp/taste-local/miniforge/bin/python /tmp/taste-local/TASTE/framework/scripts/claude_project_session.py --project demo --stage environment --message secret details",
            "claude: executable=/tmp/taste-local/.nvm/versions/node/v22/bin/claude",
            "Claude: 调用工具: Read file=/tmp/taste-local/TASTE/projects/demo/state/repo_data_requirements.json",
        ],
        "result": initial,
    })

    assert row["stage"] == "environment"
    assert row["progress"]["message"] == "项目代理正在读取/修改当前项目证据以处理环境配置门控。"
    text = "\n".join(row["logs"])
    assert "Workflow command" not in text
    assert "claude_project_session.py" not in text
    assert "/tmp/taste-local" not in text
    assert "executable=" not in text


def test_environment_loader_passed_progress_maps_to_reference_probe(monkeypatch):
    message = "环境阶段已选择当前候选基底：example/repo；真实数据/loader 已通过，等待参考协议/环境 manifest 探针。"
    monkeypatch.setattr(
        server,
        "project_summary",
        lambda _project: {
            "status": "blocked_fresh_base_reference_probe_required",
            "full_research_cycle": {"status": "blocked_fresh_base_reference_probe_required", "summary": message},
            "stages": {"environment": {"summary": message}},
        },
    )

    row = server._compact_job_for_list({
        "job_id": "claude-message_demo",
        "stage": "environment",
        "status": "blocked",
        "created_at": "2026-06-10T00:00:00Z",
        "progress": {"phase": "blocked_fresh_base_data_required", "current": 1, "total": 1, "percent": 100, "message": message},
        "logs": [message],
        "result": {"project": "demo", "action": "claude-message", "requested_stage": "environment", "panel_stage": "environment", "status": "blocked_fresh_base_data_required"},
    })

    assert row["stage"] == "environment"
    assert row["status"] == "blocked_fresh_base_reference_probe_required"
    assert row["progress"]["phase"] == "blocked_fresh_base_reference_probe_required"
    assert "真实数据/loader 已通过" in row["progress"]["message"]



def test_read_public_logs_hide_agent_transcript_details():
    logs = server._public_job_logs(
        "read",
        [
            "Claude: 精读状态（20篇 + 2篇不可用）",
            "- VENOMREC -> `boundary_audit`",
            "`idea_score_audit`记录为 subagent评分。",
            "read blocked",
        ],
        {"phase": "blocked", "message": "read stopped at an evidence gate"},
        {"run_id": "find_demo", "status": "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection"},
        limit=8,
    )
    text = "\n".join(logs)
    assert "精读、Idea 和 Plan 已完成" in text
    assert "运行编号：find_demo" in text
    assert "详细日志" in text
    assert "Claude:" not in text
    assert "boundary_audit" not in text
    assert "idea_score_audit" not in text
    assert "read blocked" not in text



def test_read_public_logs_hide_running_wrapper_command():
    logs = server._public_job_logs(
        "read",
        [
            "read started",
            "Delegating current Find Read/Idea/Plan repair to wrapper: /home/fmh/workspace/miniforge/envs/ar_taste/bin/python3.11 /home/fmh/workspace/TASTE/modules/reading/scripts/ensure_current_find_research_plan.py --project demo --force",
        ],
        {"phase": "current find claude takeover", "message": "主控 Claude Code 正在接管当前 Find 的全文精读、idea 和 plan。"},
        {"run_id": "find_demo", "status": "running"},
        limit=8,
    )
    text = "\n".join(logs)
    assert "正在生成当前 Find 的全文精读、Idea 和 Plan" in text
    assert "详细日志" in text
    assert "Claude Code" not in text
    assert "Delegating" not in text
    assert "ensure_current_find_research_plan.py" not in text
    assert "/home/fmh" not in text

    progress = server._public_job_api_payload({"phase": "current_find_claude_takeover", "message": "主控 Claude Code 正在接管当前 Find 的全文精读、idea 和 plan。"})
    rendered_progress = json.dumps(progress, ensure_ascii=False)
    assert "正在生成当前 Find 的全文精读、Idea 和 Plan" in rendered_progress
    assert "Claude Code" not in rendered_progress


def test_find_public_logs_hide_internal_batch_details():
    logs = server._public_job_logs(
        "find",
        [
            "ICLR: requested years [2026] had no usable papers via none; no fallback year was used",
            "NeurIPS: dynamic title prune 2025 / other ratio=0.9% strictness=niche selected=17 kept=3",
            "SIGKDD: starting LLM title prefilter for 256 titles in 3 uncached batches with 8 workers; cache_hits=226; per-batch timeout=120s",
        ],
        {"phase": "complete", "current": 1, "total": 1, "percent": 100, "message": "find complete"},
        {
            "run_id": "find_20260613_165905_201680",
            "survey_stats": {
                "title_total_papers": 17326,
                "category_filtered_papers": 13994,
                "tfidf_screened_papers": 10690,
                "llm_title_scored_papers": 10588,
                "abstract_scored_papers": 594,
                "recommended_papers": 20,
            },
            "recommendation_quality": {"status": "ok", "recommendation_count": 20},
        },
        limit=8,
    )
    text = "\n".join(logs)
    assert "Find 已完成" in text
    assert "标题总数 17326" in text
    assert "推荐 20" in text
    assert "摘要和推荐理由检查通过" in text
    assert "cache_hits" not in text
    assert "dynamic title prune" not in text
    assert "fallback" not in text
    assert "per-batch timeout" not in text



def test_environment_public_logs_fold_raw_command_progress():
    logs = server._public_job_logs(
        "environment",
        [
            "Workflow command: /tmp/taste-local/miniforge/bin/python modules/environment/scripts/run_environment_stage.py --project demo",
            "$ /tmp/taste-local/miniforge/bin/python modules/environment/scripts/select_evidence_ready_repo.py --project demo",
            "Runtime PATH head: /tmp/taste-local/bin | /tmp/taste-local/conda/bin",
        ],
        {"message": "$ /tmp/taste-local/miniforge/bin/python modules/environment/scripts/select_evidence_ready_repo.py --project demo"},
        {},
        limit=8,
    )
    text = "\n".join(logs)
    assert "环境配置正在运行阶段审计命令" in text
    assert "select_evidence_ready_repo.py" not in text
    assert "/tmp/taste-local" not in text
    assert "Runtime PATH head" not in text



def test_environment_compact_job_sanitizes_running_progress_command():
    row = server._compact_job_for_list({
        "job_id": "environment_running",
        "stage": "environment",
        "status": "running",
        "created_at": "2026-06-10T00:00:00Z",
        "progress": {
            "phase": "environment",
            "current": 3,
            "total": 0,
            "percent": 0,
            "message": "$ /tmp/taste-local/miniforge/envs/management/bin/python3.11 modules/environment/scripts/select_evidence_ready_repo.py --project demo",
        },
        "logs": ["$ /tmp/taste-local/miniforge/envs/management/bin/python3.11 modules/environment/scripts/select_evidence_ready_repo.py --project demo"],
        "result": {"project": "demo", "action": "environment", "panel_stage": "environment"},
    })

    assert "环境配置正在运行阶段审计命令" in row["progress"]["message"]
    assert "/tmp/taste-local" not in row["progress"]["message"]
    assert "select_evidence_ready_repo.py" not in row["progress"]["message"]
    joined_logs = "\n".join(row["logs"])
    assert "环境配置正在运行阶段审计命令" in joined_logs
    assert "/tmp/taste-local" not in joined_logs
    assert "select_evidence_ready_repo.py" not in joined_logs


def test_environment_compact_job_promotes_data_blocker_over_done_status():
    row = server._compact_job_for_list({
        "job_id": "environment_done_but_data_blocked",
        "stage": "environment",
        "status": "done",
        "created_at": "2026-06-10T00:00:00Z",
        "progress": {
            "phase": "blocked",
            "current": 1,
            "total": 1,
            "percent": 100,
            "message": "环境阶段已选择当前候选基底：example/repo；但真实数据/loader 尚未通过，不能进入实验或论文证据。",
        },
        "logs": ["environment done"],
        "result": {"project": "demo", "action": "environment", "panel_stage": "environment", "status": "done"},
    })

    assert row["status"] == "blocked_fresh_base_data_required"
    assert row["progress"]["phase"] == "blocked_fresh_base_data_required"
    assert row["result"]["status"] == "blocked_fresh_base_data_required"
    assert "真实数据/loader" in row["progress"]["message"]


def test_environment_compact_job_hides_stale_not_started_history(monkeypatch):
    monkeypatch.setattr(
        server,
        "project_summary",
        lambda _project: {
            "status": "running",
            "full_research_cycle": {"status": "running", "summary_zh": "环境配置任务正在运行。"},
            "stages": {"environment": {"summary": "环境配置任务正在运行。"}},
        },
    )
    row = server._compact_job_for_list({
        "job_id": "environment_old",
        "stage": "environment",
        "status": "blocked",
        "created_at": "2026-06-10T00:00:00Z",
        "progress": {"phase": "blocked", "current": 1, "total": 1, "percent": 100, "message": "项目：demo；状态：not_started。"},
        "logs": ["当前状态：项目：demo；状态：not_started。", "environment blocked"],
        "result": {"project": "demo", "action": "environment", "panel_stage": "environment", "status": "blocked"},
    })

    assert "not_started" not in row["progress"]["message"]
    assert "历史环境配置任务已阻塞" in row["progress"]["message"]
    joined_logs = "\n".join(row["logs"])
    assert "not_started" not in joined_logs
    assert "历史环境配置任务已阻塞" in joined_logs


def test_environment_compact_job_refreshes_blocked_progress_from_project_summary(monkeypatch):
    monkeypatch.setattr(
        server,
        "project_summary",
        lambda _project: {
            "status": "blocked_fresh_base_data_required",
            "full_research_cycle": {
                "status": "blocked_fresh_base_data_required",
                "summary_zh": "环境阶段已选择当前候选基底：example/repo；但真实数据/loader 尚未通过，不能进入实验或论文证据。",
            },
            "stages": {"environment": {"summary": "当前基底已选定，等待真实数据/loader。"}},
        },
    )
    item = {
        "job_id": "environment_old",
        "stage": "environment",
        "status": "blocked",
        "created_at": "2026-06-10T00:00:00Z",
        "progress": {"phase": "blocked", "current": 1, "total": 1, "percent": 100, "message": "项目：demo；状态：not_started。"},
        "logs": ["environment blocked"],
        "result": {"project": "demo_project", "action": "environment", "panel_stage": "environment", "status": "blocked"},
    }

    row = server._compact_job_for_list(item)

    assert row["progress"]["phase"] == "blocked_fresh_base_data_required"
    assert "not_started" not in row["progress"]["message"]
    assert "example/repo" in row["progress"]["message"]
    assert row["result"]["summary"] == row["progress"]["message"]
    joined_logs = "\n".join(row["logs"])
    assert "not_started" not in joined_logs
    assert "未选择可审计基底仓库" not in joined_logs
    assert "example/repo" in joined_logs
    assert "审计进展：审计进展" not in joined_logs

def test_environment_job_list_uses_compact_public_logs(monkeypatch):
    JOBS.clear()
    monkeypatch.setattr(server, "_reconcile_detached_launcher_jobs", lambda: None)
    monkeypatch.setattr(server, "_live_jobs_from_projects", lambda compact=True: [])
    monkeypatch.setattr(server, "_cached_list_runs", lambda: [])
    job = server.JobState("environment_demo", "environment")
    job.status = "blocked"
    job.progress = {"phase": "blocked", "current": 1, "total": 1, "percent": 100, "message": "项目：demo；状态：blocked_environment_base_selection_required。"}
    job.result = {
        "project": "demo",
        "status": "blocked",
        "action": "environment",
        "panel_stage": "environment",
        "summary": {
            "summary": "audit complete; audited=12; ready=0",
            "current_blocker": {"human_summary": "仍未选择 evidence-ready 仓库。"},
        },
    }
    job.logs = [
        "$ /tmp/taste-local/miniforge/envs/management/bin/python3.11 modules/environment/scripts/select_evidence_ready_repo.py --project demo",
        "[literature-base-audit] candidate 1/4 query: \"Noisy historical query\"",
        "selected_active_repo=none",
        "Traceback (most recent call last): File \"/tmp/taste-local/TASTE/framework/scripts/x.py\"",
        "environment blocked",
    ]
    JOBS[job.job_id] = job

    rows = server.api_jobs(compact=True, limit=10, include_history=True)

    listed = next(row for row in rows if row.get("job_id") == job.job_id)
    log_text = "\n".join(listed.get("logs") or [])
    assert "当前状态：项目：demo" in log_text
    assert "详细日志：已保留" in log_text
    assert "/tmp/taste-local" not in log_text
    assert "select_evidence_ready_repo.py" not in log_text
    assert "Noisy historical query" not in log_text
    assert "Traceback" not in log_text


def test_api_jobs_snapshots_jobs_before_compacting(monkeypatch):
    JOBS.clear()
    monkeypatch.setattr(server, "_reconcile_detached_launcher_jobs", lambda: None)
    monkeypatch.setattr(server, "_live_jobs_from_projects", lambda compact=True: [])
    monkeypatch.setattr(server, "_cached_list_runs", lambda: [])
    first = server.JobState("find_existing", "find")
    first.status = "done"
    first.run_id = "find_20260614_000000_000000"
    JOBS[first.job_id] = first
    original_compact = server._compact_job_for_list

    def mutating_compact(item):
        if "find_inserted" not in JOBS:
            inserted = server.JobState("find_inserted", "find")
            inserted.status = "done"
            inserted.run_id = "find_20260614_000001_000001"
            JOBS[inserted.job_id] = inserted
        return original_compact(item)

    monkeypatch.setattr(server, "_compact_job_for_list", mutating_compact)

    rows = server.api_jobs(compact=True, limit=10, include_history=True)

    assert any(row["job_id"] == "find_existing" for row in rows)
    assert "find_inserted" in JOBS


def test_api_jobs_hides_superseded_cancelled_jobs_without_artifacts(monkeypatch):
    JOBS.clear()
    monkeypatch.setattr(server, "_reconcile_detached_launcher_jobs", lambda: None)
    monkeypatch.setattr(server, "_live_jobs_from_projects", lambda compact=True: [])
    monkeypatch.setattr(server, "_cached_list_runs", lambda: [])

    latest_run_id = "find_20260613_193012_583206"
    monkeypatch.setattr(server, "_run_belongs_to_project", lambda run_id, project: str(run_id) == latest_run_id and project == "demo_project")

    old = server.JobState("find_old_cancelled", "find")
    old.status = "cancelled"
    old.created_at = "2026-06-13T19:26:31Z"
    old.run_id = "find_20260613_192633_273325"
    old.result = {"project": "demo_project", "status": "cancelled", "run_id": None}
    old.progress = {"phase": "interrupted", "current": 0, "total": 1, "percent": 0, "message": "Server restarted before this job completed."}
    JOBS[old.job_id] = old

    latest = server.JobState("find_latest_done", "find")
    latest.status = "done"
    latest.created_at = "2026-06-13T19:30:11Z"
    latest.run_id = latest_run_id
    latest.result = {"status": "done", "run_id": latest.run_id, "artifact_dir": "/tmp/find_latest"}
    latest.progress = {"phase": "complete", "current": 1, "total": 1, "percent": 100, "message": "Find 已完成。"}
    JOBS[latest.job_id] = latest

    rows = server.api_jobs(compact=True, limit=10, include_history=True, project="demo_project")
    job_ids = {row.get("job_id") for row in rows}

    assert "find_latest_done" in job_ids
    assert "find_old_cancelled" not in job_ids


def test_api_jobs_hides_superseded_cancelled_jobs_without_project_filter(monkeypatch):
    JOBS.clear()
    monkeypatch.setattr(server, "_reconcile_detached_launcher_jobs", lambda: None)
    monkeypatch.setattr(server, "_live_jobs_from_projects", lambda compact=True: [])
    monkeypatch.setattr(server, "_cached_list_runs", lambda: [])

    latest_run_id = "find_20260613_202357_495811"
    old_run_id = "find_20260613_192633_273325"
    monkeypatch.setattr(server, "_project_id_for_find_run", lambda run_id: "demo_project" if str(run_id) in {latest_run_id, old_run_id} else "")

    old = server.JobState("find_old_cancelled", "find")
    old.status = "cancelled"
    old.created_at = "2026-06-13T19:26:31Z"
    old.run_id = old_run_id
    old.result = {"project": "demo_project", "status": "cancelled", "run_id": None}
    old.progress = {"phase": "interrupted", "current": 0, "total": 1, "percent": 0, "message": "Server restarted before this job completed."}
    JOBS[old.job_id] = old

    latest = server.JobState("find_latest_done", "find")
    latest.status = "done"
    latest.created_at = "2026-06-13T20:36:23Z"
    latest.run_id = latest_run_id
    latest.result = {"status": "done", "run_id": latest.run_id, "artifact_dir": "/tmp/find_latest"}
    latest.progress = {"phase": "complete", "current": 1, "total": 1, "percent": 100, "message": "Find 已完成。"}
    JOBS[latest.job_id] = latest

    rows = server.api_jobs(compact=True, limit=10, include_history=True)
    job_ids = {row.get("job_id") for row in rows}

    assert "find_latest_done" in job_ids
    assert "find_old_cancelled" not in job_ids


def test_api_jobs_keeps_latest_cancelled_job_when_not_superseded(monkeypatch):
    JOBS.clear()
    monkeypatch.setattr(server, "_reconcile_detached_launcher_jobs", lambda: None)
    monkeypatch.setattr(server, "_live_jobs_from_projects", lambda compact=True: [])
    monkeypatch.setattr(server, "_cached_list_runs", lambda: [])

    job = server.JobState("find_latest_cancelled", "find")
    job.status = "cancelled"
    job.created_at = "2026-06-13T19:30:11Z"
    job.result = {"project": "demo_project", "status": "cancelled", "run_id": None}
    job.progress = {"phase": "cancelled", "current": 0, "total": 1, "percent": 0, "message": "Task cancelled by user."}
    JOBS[job.job_id] = job

    rows = server.api_jobs(compact=True, limit=10, include_history=True, project="demo_project")

    assert any(row.get("job_id") == "find_latest_cancelled" for row in rows)


def test_jobs_api_normalizes_failed_running_project_agent_job(monkeypatch):
    JOBS.clear()
    monkeypatch.setattr(server, "_reconcile_detached_launcher_jobs", lambda: None)
    monkeypatch.setattr(server, "_live_jobs_from_projects", lambda compact=True: [])
    job = server.JobState("claude-message_failed", "experiment")
    job.status = "running"
    job.error = "research action failed with exit code 3"
    job.result = {"project": "demo_project", "action": "claude-message", "panel_stage": "experiment", "status": "running"}
    job.progress = {"phase": "error", "current": 0, "total": 1, "percent": 0, "message": "research action failed with exit code 3"}
    JOBS[job.job_id] = job

    rows = server.api_jobs(compact=True, limit=20, include_history=True)

    listed = next(row for row in rows if row.get("job_id") == job.job_id)
    assert listed["stage"] == "experiment"
    assert listed["status"] == "error"
    assert listed["result"]["status"] == "error"
    assert listed["progress"]["phase"] == "error"


def test_agent_guidance_receipt_remains_visible_and_fetchable(monkeypatch):
    JOBS.clear()
    monkeypatch.setattr(server, "_reconcile_detached_launcher_jobs", lambda: None)
    monkeypatch.setattr(server, "_live_jobs_from_projects", lambda compact=True: [])
    job = server.JobState("project-agent-guidance_demo", "project-agent-guidance")
    job.status = "queued"
    job.result = {
        "project": "demo_project",
        "action": "agent-guidance",
        "agent_id": "guidance_demo",
        "target_agent_id": "main",
        "requested_stage": "experiment",
        "panel_stage": "experiment",
        "status": "queued",
        "guidance_receipt": "queued project guidance: guidance_123",
    }
    job.progress = {"phase": "queued", "current": 1, "total": 1, "percent": 100, "message": "queued project guidance: guidance_123"}
    JOBS[job.job_id] = job

    rows = server.api_jobs(compact=True, limit=20, include_history=False)
    listed = next((row for row in rows if row.get("job_id") == job.job_id), None)
    assert listed is not None
    assert listed["stage"] == "experiment"
    assert listed["job_id"] == "project-agent-guidance_demo"
    assert listed["result"]["action"] == "agent-guidance"
    assert listed["result"]["panel_stage"] == "experiment"

    job.status = "done"
    job.result["status"] = "done"
    detail = server.api_job(job.job_id, compact=False)
    assert detail["job_id"] == job.job_id
    assert detail["stage"] == "experiment"
    assert detail["result"]["guidance_receipt"] == "queued project guidance: guidance_123"

def test_public_claude_receipt_advertises_full_response_without_embedding_raw_text():
    from auto_research.web import project_bridge

    receipt = project_bridge._public_claude_receipt(
        {
            "status": "completed",
            "stage": "current-find-claude-read-idea-plan",
            "response_markdown": "完整 Claude 回复正文，应按需读取而不是塞入 compact project summary。",
        }
    )

    assert receipt["content_compacted"] is True
    assert receipt["raw_response_hidden"] is True
    assert receipt["full_response_available"] is True
    assert receipt["response_chcount"] > 0
    assert "完整 Claude 回复正文" not in receipt["response_markdown"]


def test_claude_latest_response_endpoint_returns_full_result(tmp_path, monkeypatch):
    project_root = tmp_path / "demo_project"
    state_dir = project_root / "state"
    state_dir.mkdir(parents=True)
    write_json(
        state_dir / "claude_project_session_last_result.json",
        {
            "status": "completed",
            "stage": "current-find-claude-read-idea-plan",
            "session_id": "session-demo",
            "claude_json": {"result": "## 完整回复\n\n主控已经完成精读、idea 和 plan。"},
            "stdout": "short fallback",
        },
    )
    monkeypatch.setattr(server, "PROJECT_IDS_ROOT", tmp_path)

    payload = server.api_project_claude_latest_response("demo_project")

    assert payload["source"] == "claude_json.result"
    assert "## 完整回复" in payload["response_markdown"]
    assert payload["full_response_available"] is True


def test_runtime_env_separates_management_and_experiment_python(tmp_path, monkeypatch):
    import stat
    from types import SimpleNamespace
    import runtime_env

    def fake_python(path, version):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    project_root = tmp_path / "projects" / "demo_project"
    project_root.mkdir(parents=True)
    conda_base = tmp_path / "miniforge"
    (conda_base / "etc" / "profile.d").mkdir(parents=True)
    (conda_base / "etc" / "profile.d" / "conda.sh").write_text("", encoding="utf-8")
    management_python = fake_python(tmp_path / "env" / "bin" / "python", "Python 3.11.9")
    experiment_python = fake_python(conda_base / "envs" / "experiment_env" / "bin" / "python", "Python 3.10.14")
    project_config = project_root / "project.json"
    write_json(project_config, {
        "name": "demo_project",
        "conda_env": "experiment_env",
        "python_executable": str(management_python),
        "runtime": {"conda_base": str(conda_base)},
    })

    monkeypatch.setattr(runtime_env, "ROOT", tmp_path)
    monkeypatch.setattr(runtime_env, "build_paths", lambda _project: SimpleNamespace(config=project_config))
    monkeypatch.setattr(runtime_env, "load_project_config", lambda _project: read_json(project_config, {}))
    monkeypatch.delenv("EXPERIMENT_PYTHON", raising=False)
    monkeypatch.delenv("PROJECT_PYTHON", raising=False)
    monkeypatch.setenv("MANAGEMENT_PYTHON", str(management_python))

    runtime = runtime_env.project_runtime_config("demo_project")
    assert runtime["management_python"] == str(management_python)
    assert runtime["python_executable"] == str(management_python)
    assert runtime["experiment_python"] == str(experiment_python)

    updated = runtime_env.update_project_runtime("demo_project", {
        "management_python": str(management_python),
        "experiment_python": str(experiment_python),
        "conda_base": str(conda_base),
    })
    saved = read_json(project_config, {})
    assert saved["python_executable"] == str(management_python)
    assert saved["runtime"]["experiment_python"] == str(experiment_python)
    assert saved["environment"]["experiment_python"] == str(experiment_python)
    assert updated["management_python"] == str(management_python)
    assert updated["experiment_python"] == str(experiment_python)

    env = runtime_env.interactive_env("demo_project")
    assert env["WORKSPACE_ROOT"] == str(tmp_path)
    assert env["PROJECT_ID"] == "demo_project"
    assert env["DEFAULT_PROJECT_ID"] == "demo_project"
    assert env["MANAGEMENT_PYTHON"] == str(management_python)
    assert env["EXPERIMENT_PYTHON"] == str(experiment_python)
    assert str(tmp_path / "framework") in env["PYTHONPATH"].split(":")
    assert str(tmp_path / "web" / "backend") in env["PYTHONPATH"].split(":")
    assert str(tmp_path / "modules" / "finding") in env["PYTHONPATH"].split(":")
    assert str(tmp_path / "framework" / "scripts") in env["PYTHONPATH"].split(":")
    assert str(tmp_path / "modules" / "reading" / "scripts") in env["PYTHONPATH"].split(":")


def test_create_project_cli_preserves_prompt_and_conda_env(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from types import SimpleNamespace

    script_path = server.WORKSPACE_ROOT / "framework" / "scripts" / "create_project.py"
    spec = importlib.util.spec_from_file_location("create_project_script_test", script_path)
    create_project = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(create_project)

    root = tmp_path / "root"
    (root / "templates").mkdir(parents=True)
    template = json.loads((server.WORKSPACE_ROOT / "templates" / "project.json").read_text(encoding="utf-8"))
    write_json(root / "templates" / "project.json", template)
    monkeypatch.setattr(create_project, "ROOT", root)
    monkeypatch.setattr(create_project, "TEMPLATE", root / "templates" / "project.json")
    monkeypatch.setattr(create_project.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "create_project.py",
            "--name",
            "demo_project",
            "--topic",
            "demo topic",
            "--prompt",
            "demo goal",
            "--conda-env",
            "demo_env",
            "--query",
            "q1",
            "--query",
            "q2",
        ],
    )

    create_project.main()

    data = read_json(root / "projects" / "demo_project" / "project.json", {})
    assert data["topic"] == "demo topic"
    assert data["user_prompt"] == "demo goal"
    assert data["research_interest"] == "demo goal"
    assert data["startup"]["last_bootstrap_request"] == "demo goal"
    assert data["conda_env"] == "demo_env"
    assert data["queries"] == ["q1", "q2"]
    activate = root / "projects" / "demo_project" / "activate_env.sh"
    assert activate.exists()
    activate_text = activate.read_text(encoding="utf-8")
    assert 'ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"' in activate_text
    assert 'exec "$ROOT/framework/scripts/run_in_conda.sh" "demo_project" "$@"' in activate_text



def test_running_fresh_find_prefers_latest_runtime_run_id(tmp_path, monkeypatch):
    from auto_research.web import server

    runs = tmp_path / "runtime" / "runs"
    (runs / "find_20260610_100000_000000").mkdir(parents=True)
    (runs / "find_20260610_110000_000000").mkdir(parents=True)
    project_root = tmp_path / "projects" / "demo_project"
    state = project_root / "state"
    state.mkdir(parents=True)
    write_json(state / "finding_frontend.json", {"run_id": "find_20260610_100000_000000"})

    monkeypatch.setattr(server, "RUNS_DIR", runs)

    assert server._latest_find_run_id_from_runs(project_root) == "find_20260610_100000_000000"
    assert server._latest_find_run_id_from_runs(project_root, prefer_run_dir=True) == "find_20260610_110000_000000"


def test_compact_job_list_hides_raw_historical_command():
    from auto_research.web import server

    item = {
        "job_id": "full-cycle_demo",
        "stage": "idea",
        "status": "stale",
        "created_at": "2026-06-05T00:00:00Z",
        "logs": [],
        "run_id": "find_demo",
        "result": {
            "project": "demo_project",
            "phase": "idea",
            "command": "/env/bin/python framework/scripts/run_full_research_cycle.py --project demo_project --venue Nature",
            "status": "stale",
            "process_alive": False,
        },
        "progress": {},
    }

    compact = server._compact_job_for_list(item)

    assert compact["stage"] == "idea"
    assert compact["result"]["project"] == "demo_project"
    assert "command" not in compact["result"]



def test_full_cycle_blocked_job_uses_current_reference_dependency_blocker(monkeypatch):
    from auto_research.web import server

    blocker = "assafelovic/gpt-researcher reference protocol/import probe 已运行；代码结构存在，但当前环境依赖缺失（缺失 39/46 个 requirements），首个 import blocker: No module named 'json_repair'。"
    monkeypatch.setattr(
        server,
        "project_summary",
        lambda _project, compact=True: {
            "status": "blocked_fresh_base_reference_probe_required",
            "current_blocker": {"category": "fresh_base_reference_probe_required", "summary": blocker},
        },
    )

    row = server._compact_job_for_list({
        "job_id": "full-cycle_demo",
        "stage": "full-cycle",
        "status": "blocked",
        "created_at": "2026-06-10T00:00:00Z",
        "logs": [
            "Detached full-cycle worker is no longer running",
            "门控阻塞：stale route blocker",
            "summary=stale route blocker",
        ],
        "result": {"project": "demo_project", "summary": "历史 full-cycle 启动器已停止；当前状态以项目摘要和实验模块为准。"},
        "progress": {"phase": "environment", "message": "历史 full-cycle 启动器已停止；当前状态以项目摘要和实验模块为准。"},
    })

    assert row["status"] == "blocked_fresh_base_reference_probe_required"
    assert row["progress"]["phase"] == "blocked_fresh_base_reference_probe_required"
    assert "39/46" in row["progress"]["message"]
    assert "json_repair" in row["result"]["summary"]





def test_full_cycle_blocked_job_uses_current_cycle_summary(monkeypatch):
    from auto_research.web import server

    current_summary = "完整科研自循环已停止；当前状态=blocked_no_viable_reference_base；没有正在运行的 full-cycle。"
    monkeypatch.setattr(
        server,
        "project_summary",
        lambda _project, compact=True: {
            "status": "blocked_no_viable_reference_base",
            "summary": current_summary,
            "full_research_cycle": {"status": "blocked_no_viable_reference_base", "summary": current_summary},
            "current_blocker": {"category": "submission_readiness", "summary": "venue_policy_known"},
        },
    )

    row = server._compact_job_for_list({
        "job_id": "full-cycle_stopped",
        "stage": "full-cycle",
        "status": "blocked",
        "created_at": "2026-06-10T00:00:00Z",
        "logs": ["Detached full-cycle worker is no longer running"],
        "result": {"project": "demo_project", "summary": "历史 full-cycle 启动器已停止；当前状态以项目摘要和实验模块为准。"},
        "progress": {"phase": "environment", "message": "历史 full-cycle 启动器已停止；当前状态以项目摘要和实验模块为准。"},
    })

    assert row["status"] == "blocked_no_viable_reference_base"
    assert row["progress"]["phase"] == "blocked_no_viable_reference_base"
    assert "blocked_no_viable_reference_base" in row["progress"]["message"]
    assert row["result"]["summary"] == current_summary
    assert any("blocked_no_viable_reference_base" in line for line in row["logs"])
    assert not any("当前状态以项目门控摘要为准" in line for line in row["logs"])
    assert not any("stale route blocker" in line for line in row["logs"])


def test_compact_job_list_hides_internal_json_log_chunks():
    from auto_research.web import server

    item = {
        "job_id": "read_demo",
        "stage": "read",
        "status": "blocked",
        "created_at": "2026-06-07T00:00:00Z",
        "logs": [
            "\"idea_contract_issues\": [",
            "{",
            "\"scope\": \"idea_count\",",
            "\"guardrail\": \"internal Claude contract text\"",
            "read blocked",
        ],
        "run_id": "find_demo",
        "result": {},
        "progress": {"phase": "blocked", "message": "read stopped at an evidence gate"},
    }

    compact = server._compact_job_for_list(item)

    rendered = "\n".join(compact["logs"])
    assert "详细日志" in rendered
    assert "read stopped at an evidence gate" not in compact["progress"]["message"]
    assert "idea_contract" not in compact["progress"]["message"]
    assert "read blocked" not in rendered
    assert "idea_contract" not in rendered
    assert "guardrail" not in rendered
    assert "{" not in rendered


def test_compact_job_progress_messages_are_public_chinese():
    from auto_research.web import server

    find_row = server._compact_job_for_list({
        "job_id": "find_demo",
        "stage": "find",
        "status": "done",
        "created_at": "2026-06-07T00:00:00Z",
        "logs": ["find complete"],
        "run_id": "find_demo_run",
        "result": {"run_id": "find_demo_run"},
        "progress": {"phase": "complete", "current": 1, "total": 1, "percent": 100, "message": "find complete"},
    })
    read_row = server._compact_job_for_list({
        "job_id": "read_demo_public",
        "stage": "read",
        "status": "done",
        "created_at": "2026-06-07T00:00:00Z",
        "logs": ["read complete"],
        "run_id": "find_demo_run",
        "result": {"run_id": "find_demo_run"},
        "progress": {"phase": "complete", "current": 1, "total": 1, "percent": 100, "message": "read complete"},
    })
    cancelled_row = server._compact_job_for_list({
        "job_id": "find_cancelled_demo",
        "stage": "find",
        "status": "cancelled",
        "created_at": "2026-06-07T00:00:00Z",
        "logs": ["Task cancelled by user."],
        "run_id": "find_cancelled_run",
        "result": {"run_id": "find_cancelled_run"},
        "progress": {"phase": "cancelled", "current": 0, "total": 1, "percent": 0, "message": "Task cancelled by user."},
    })

    assert find_row["progress"]["message"] == "Find 已完成。"
    assert read_row["progress"]["message"] == "精读阶段已完成。"
    assert cancelled_row["progress"]["message"] == "任务已取消。"
    rendered = "\n".join(find_row["logs"] + read_row["logs"] + cancelled_row["logs"])
    assert "find complete" not in rendered
    assert "read complete" not in rendered
    assert "Task cancelled" not in rendered


def test_job_progress_shape_is_serialized():
    JOBS.clear()
    job = start_job("find", lambda _log, _should_cancel, progress: (progress("phase", 2, 4, "halfway") or {"ok": True}))
    job.done.wait(2)
    data = job.as_dict()
    assert data["progress"]["phase"] == "complete"
    assert data["progress"]["percent"] == 100
    assert data["progress"]["message"] == "find complete"


def test_public_run_preferences_recomputes_paper_template_from_current_venue(tmp_path):
    from auto_research.web import project_bridge

    prefs = project_bridge._public_run_preferences(
        "demo_project",
        tmp_path,
        {
            "name": "demo_project",
            "target_venue": "ICLR",
            "venue": "ICLR",
            "paper": {
                "target_venue": "ICLR",
                "template_family": "iclr",
                "template_source_url": "https://www.springernature.com/gp/authors/campaigns/latex-author-support",
            },
        },
        selection={},
    )

    assert prefs["target_venue"] == "ICLR"
    assert prefs["paper"]["template_family"] == "iclr"
    assert prefs["paper"]["template_source_url"] == "https://github.com/ICLR/Master-Template"


def test_full_cycle_stale_summary_detection_does_not_flag_terminal_text():
    from auto_research.web import project_bridge

    assert project_bridge._full_cycle_summary_claims_live("完整科研自循环正在运行；阶段=paper；PID=123")
    assert not project_bridge._full_cycle_summary_claims_live("完整科研自循环已停止在最大轮次后；没有正在运行的 full-cycle。")


def test_sanitize_stale_full_cycle_summary_rewrites_non_live_pid_text(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    root = tmp_path / "projects" / "demo_project"
    state_dir = root / "state"
    state_dir.mkdir(parents=True)
    payload = {
        "status": "blocked_after_max_cycles",
        "summary": "完整科研自循环正在运行；阶段=paper；PID=123",
        "summary_zh": "完整科研自循环正在运行；阶段=paper；PID=123",
        "latest_step": {"stage": "full-cycle-blocker-repair-final", "phase": "experiment"},
        "current_goal": "需要补齐候选实验证据。",
        "full_cycle_job": {"status": "blocked_after_max_cycles", "pid": "123", "process_alive": False},
    }
    write_json(state_dir / "full_research_cycle.json", payload)
    monkeypatch.setattr(project_bridge, "_pid_alive", lambda _pid: False)

    cleaned = project_bridge._sanitize_stale_full_cycle_summary(payload, payload["full_cycle_job"], root=root, base_title="Demo Base")
    persisted = read_json(state_dir / "full_research_cycle.json", {})

    assert "正在运行；阶段" not in cleaned["summary"]
    assert "PID=123" not in cleaned["summary"]
    assert "没有正在运行的 full-cycle" in cleaned["summary"]
    assert persisted["summary"] == cleaned["summary"]


def test_plan_uses_only_selected_approved_ideas():
    run_id, directory = create_run_dir("plan_test")
    try:
        write_json(
            directory / "ideas.json",
            {
                "run_id": run_id,
                "ideas": [
                    {"id": "approved", "title": "Approved idea", "hypothesis": "H", "status": "approved"},
                    {"id": "pending", "title": "Pending idea", "hypothesis": "P", "status": "pending"},
                    {"id": "deleted", "title": "Deleted idea", "hypothesis": "D", "status": "deleted"},
                ],
            },
        )
        cfg = AppConfig(provider="mock")

        empty = run_plan(PlanRequest(run_id=run_id, idea_ids=["pending", "deleted"]), cfg, log=lambda _msg: None)
        assert empty["plans"] == []

        result = run_plan(PlanRequest(run_id=run_id, idea_ids=["approved", "pending"]), cfg, log=lambda _msg: None)
        assert [plan["idea_id"] for plan in result["plans"]] == ["approved"]
        assert result["plans"][0]["versions"][0]["version_id"] == "v1"
        assert result["plans"][0]["versions"][0]["evaluation_rounds"][0]["repair_summary"]
    finally:
        delete_run(run_id)


def test_plan_reads_project_same_run_ideas_and_syncs_plans(monkeypatch, tmp_path):
    run_id, directory = create_run_dir("project_plan_test")
    project_root = tmp_path / "root"
    taste_dir = project_root / "projects" / "demo_project" / "planning" / "finding"
    taste_dir.mkdir(parents=True)
    try:
        write_json(directory / "ideas.json", {"run_id": run_id, "ideas": []})
        write_json(
            taste_dir / "ideas.json",
            {
                "run_id": run_id,
                "ideas": [
                    {
                        "idea_id": "project-idea",
                        "title": "Project edited idea",
                        "hypothesis": "Human/project agent edit",
                        "approved_for_planning": True,
                    },
                    {"idea_id": "skip-me", "title": "Rejected", "status": "deleted", "pursue": True},
                ],
            },
        )
        monkeypatch.setenv("WORKSPACE_ROOT", str(project_root))
        monkeypatch.setenv("PROJECT_ID", "demo_project")
        cfg = AppConfig(provider="mock")

        result = run_plan(PlanRequest(run_id=run_id, idea_ids=["project-idea"], repair_rounds=1), cfg, log=lambda _msg: None)

        assert [plan["idea_id"] for plan in result["plans"]] == ["project-idea"]
        assert result["plans"][0]["title"] == "Project edited idea"
        synced = read_json(taste_dir / "plans.json", {})
        assert synced["run_id"] == run_id
        assert [plan["idea_id"] for plan in synced["plans"]] == ["project-idea"]
        assert (taste_dir / "plan.md").exists()
    finally:
        delete_run(run_id)



def test_plan_markdown_prefers_initial_experiment_over_generic_gate_steps():
    markdown = render_plan_markdown([
        {
            "plan_id": "plan-specific",
            "idea_id": "idea-specific",
            "title": "Specific plan",
            "new_method": "Semantic gated discrete retrieval planner with LLM evidence embeddings in candidate fading and reconstruction.",
            "initial_experiment": "Implement a minimal PreferGrow-based semantic gated variant, compare against a baseline planner and semantic reranking, and report HR@10, NDCG@10, long-tail slices, and semantic-conflict bad cases.",
            "versions": [
                {
                    "version_id": "v1",
                    "final_plan": {
                        "steps": [
                            "Verify current Find run_id and guarded read/idea/plan outputs.",
                            "Environment-stage Claude Code reads all current strong recommendations and audits candidate repos/data/protocols.",
                            "Accept a base only by writing state/evidence_ready_repo_selection.json.",
                        ]
                    },
                    "evaluation_rounds": [],
                }
            ],
        }
    ])

    assert "### Initial Experiment" in markdown
    assert "Implement a minimal PreferGrow-based semantic gated variant" in markdown
    assert "Use this initial experiment as the execution contract" in markdown
    assert "Verify current Find run_id" not in markdown
    assert "Environment-stage Claude Code" not in markdown

def test_plan_polish_appends_version_without_overwriting():
    run_id, directory = create_run_dir("polish_test")
    try:
        write_json(
            directory / "ideas.json",
            {"run_id": run_id, "ideas": [{"id": "approved", "title": "Approved idea", "hypothesis": "H", "status": "approved"}]},
        )
        cfg = AppConfig(provider="mock")
        result = run_plan(PlanRequest(run_id=run_id, idea_ids=["approved"], repair_rounds=1), cfg, log=lambda _msg: None)
        plan_id = result["plans"][0]["plan_id"]
        polish_plan(PlanPolishRequest(run_id=run_id, plan_id=plan_id, version_id="v1", rounds=1), cfg, log=lambda _msg: None)
        data = read_json(directory / "plans.json", {})
        versions = data["plans"][0]["versions"]
        assert [version["version_id"] for version in versions] == ["v1", "v2"]
    finally:
        delete_run(run_id)


def test_finish_plan_hides_rounds_in_markdown_but_keeps_json():
    run_id, directory = create_run_dir("finish_plan_test")
    try:
        write_json(
            directory / "ideas.json",
            {"run_id": run_id, "ideas": [{"id": "approved", "title": "Approved idea", "hypothesis": "H", "status": "approved"}]},
        )
        cfg = AppConfig(provider="mock")
        result = run_plan(PlanRequest(run_id=run_id, idea_ids=["approved"], repair_rounds=1), cfg, log=lambda _msg: None)
        plan_id = result["plans"][0]["plan_id"]
        data = finish_plan(run_id, plan_id)
        assert data["plans"][0]["completed"] is True
        assert data["plans"][0]["completed_at"]
        markdown = (directory / "plan.md").read_text(encoding="utf-8")
        assert "Evaluation / Repair Rounds" not in markdown
        stored = read_json(directory / "plans.json", {})
        assert stored["plans"][0]["versions"][0]["evaluation_rounds"]
    finally:
        delete_run(run_id)


def test_email_report_renders_markdown_and_ranking_html():
    run_id, directory = create_run_dir("email_test")
    try:
        write_text(directory / "article.md", "# Report\n\n- **Item**: value")
        write_json(
            directory / "find_results.json",
            {
                "screened_ranking": [
                    {
                        "id": "paper_1",
                        "title": "Generative materials discovery",
                        "venue": "ICLR",
                        "year": 2025,
                        "fit_score": 8,
                        "diversity_score": 7,
                        "score": 7.75,
                        "hit_directions": ["生成式AI", "材料物理"],
                        "fit_explanation": "强契合。",
                    }
                ]
            },
        )
        html = build_run_email_html(EmailJobRequest(run_id=run_id, artifact_names=["article.md"]))
        assert "<h1>Report:" in html
        assert "<strong>Item</strong>" in html
        assert "Full Screened Ranking" in html
        assert str(directory / "article.md") in html
    finally:
        delete_run(run_id)


def test_artifact_api_returns_paths():
    run_id, directory = create_run_dir("artifact_path_test")
    try:
        write_text(directory / "article.md", "# Article")
        response = api_artifacts(run_id)
        article = next(item for item in response["artifacts"] if item["name"] == "article.md")
        assert article["path"] == str(directory / "article.md")
    finally:
        delete_run(run_id)


def test_artifact_api_guards_project_downstream_markdown_by_current_run(tmp_path, monkeypatch):
    run_id = "find_current"
    run_root = tmp_path / "runs" / run_id
    project_root = tmp_path / "project"
    finding_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    run_root.mkdir(parents=True)
    finding_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    write_json(state_dir / "current_find_research_plan.json", {"run_id": run_id})
    write_json(finding_dir / "read_results.json", {"run_id": "find_old", "readings": [{"title": "Old"}]})
    write_text(finding_dir / "read.md", "run_id: find_old\n# Old Read")
    monkeypatch.setattr(server, "run_dir", lambda _run_id: run_root)
    monkeypatch.setattr(server, "_project_root_for_find_run", lambda _run_id: project_root)
    monkeypatch.setattr(server, "_current_find_pipeline_summary", lambda _root: {"run_id": run_id, "content_ready": True})
    server._RUN_ARTIFACTS_CACHE.clear()

    assert server._project_taste_artifact_path(project_root, run_id, "read.md") is None
    response = api_artifacts(run_id)
    assert "read.md" not in {item["name"] for item in response["artifacts"]}

    write_json(finding_dir / "read_results.json", {"run_id": run_id, "readings": [{"title": "Current"}]})
    write_text(finding_dir / "read.md", "run_id: find_current\n# Current Read")
    server._RUN_ARTIFACTS_CACHE.clear()

    response = api_artifacts(run_id)
    read_artifact = next(item for item in response["artifacts"] if item["name"] == "read.md")
    assert read_artifact["path"] == str(finding_dir / "read.md")
    assert "# Current Read" in read_artifact["content"]

    monkeypatch.setattr(server, "_current_find_pipeline_summary", lambda _root: {"run_id": run_id, "content_ready": False, "read_idea_plan_ready": False, "takeover_ready": False})
    server._RUN_ARTIFACTS_CACHE.clear()
    response = api_artifacts(run_id)
    assert "read.md" not in {item["name"] for item in response["artifacts"]}

    monkeypatch.setattr(server, "_current_find_pipeline_summary", lambda _root: {"run_id": run_id, "content_ready": True})
    write_text(finding_dir / "read.md", "run_id: find_old\n# Old Read")
    server._RUN_ARTIFACTS_CACHE.clear()

    assert server._project_taste_artifact_path(project_root, run_id, "read.md") is None
    response = api_artifacts(run_id)
    assert "read.md" not in {item["name"] for item in response["artifacts"]}


def test_full_cycle_gate_blocks_duplicate_live_worker(monkeypatch, tmp_path):
    from auto_research.web import project_bridge

    server._LIVE_JOBS_CACHE.clear()
    project = "demo_project"
    root = tmp_path / "projects" / project
    state_dir = root / "state"
    state_dir.mkdir(parents=True)
    write_json(
        state_dir / "full_cycle_job.json",
        {
            "project": project,
            "status": "running",
            "pid": 424242,
            "kind": "full_cycle",
            "stage": "full-cycle-experiment",
            "process_alive": True,
            "command": "/env/bin/python framework/scripts/run_full_research_cycle.py --project demo_project",
            "log_path": str(root / "logs" / "supervision" / "full_research_cycle.log"),
        },
    )
    monkeypatch.setattr(project_bridge, "PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(project_bridge, "_pid_alive", lambda pid: str(pid) == "424242")

    blocker = project_bridge.action_gate_blocker({"action": "full-cycle", "project": project})

    assert blocker is not None
    assert blocker["status"] == "blocked_existing_full_cycle_running"
    assert blocker["existing_full_cycle"]["pid"] == 424242


def test_cancel_prefers_active_project_child_over_stale_full_cycle_pid(monkeypatch, tmp_path):
    from auto_research.web import server

    server._LIVE_JOBS_CACHE.clear()
    project = "demo_project"
    root = tmp_path / "projects" / project
    state_dir = root / "state"
    state_dir.mkdir(parents=True)
    write_json(
        state_dir / "full_cycle_job.json",
        {
            "project": project,
            "web_job_id": "full-cycle_stale",
            "status": "blocked_after_max_cycles",
            "pid": "943425",
            "command": "/env/bin/python framework/scripts/run_full_research_cycle.py --project demo_project",
        },
    )
    monkeypatch.setattr(server, "PROJECT_IDS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(server, "_pid_alive_local", lambda pid: str(pid) == "178540")
    monkeypatch.setattr(
        server,
        "_live_jobs_from_projects",
        lambda compact=False: [
            {
                "job_id": "full-cycle_stale",
                "stage": "experiment",
                "status": "blocked",
                "result": {
                    "project": project,
                    "pid": "943425",
                    "phase": "experiment",
                    "command": "/env/bin/python framework/scripts/run_full_research_cycle.py --project demo_project",
                    "process_alive": False,
                },
                "logs": [],
                "progress": {},
            }
        ],
    )

    def active_child(seen_project, seen_root, phase_hint=""):
        assert seen_project == project
        assert seen_root == root
        return {
            "pid": "178540",
            "phase": "paper",
            "kind": "paper_pipeline",
            "cmd": "/env/bin/python modules/writing/scripts/run_paper_pipeline.py --project demo_project --venue Nature",
        }

    terminated = {}
    monkeypatch.setattr(server, "_active_project_child_process", active_child)
    monkeypatch.setattr(server, "_terminate_process_tree", lambda pid: terminated.setdefault("payload", {"requested_pid": str(pid), "terminated_pids": [str(pid)], "terminated_pgids": []}))

    response = server.api_cancel_job("full-cycle_stale")

    assert response["status"] == "cancelling"
    assert response["stage"] == "paper"
    assert response["result"]["pid"] == "178540"
    assert response["result"]["phase"] == "paper"
    assert response["termination"]["requested_pid"] == "178540"
    assert terminated["payload"]["requested_pid"] == "178540"





def test_cancel_full_cycle_prefers_live_controller_over_worker(monkeypatch, tmp_path):
    from auto_research.web import server

    server._LIVE_JOBS_CACHE.clear()
    project = "demo_project"
    root = tmp_path / "projects" / project
    root.mkdir(parents=True)
    monkeypatch.setattr(server, "PROJECT_IDS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(server, "_pid_alive_local", lambda pid: str(pid) in {"100", "202"})
    live_items = [
        {
            "job_id": "full-cycle_demo",
            "stage": "experiment",
            "status": "running",
            "result": {
                "project": project,
                "pid": "100",
                "phase": "experiment",
                "command": "/env/bin/python framework/scripts/run_full_research_cycle.py --project demo_project",
                "process_alive": True,
            },
            "logs": [],
            "progress": {},
        },
        {
            "job_id": "experiment-worker_demo_project_202",
            "stage": "experiment",
            "status": "running",
            "result": {"project": project, "pid": "202", "phase": "experiment", "process_alive": True},
            "logs": [],
            "progress": {},
        },
    ]
    monkeypatch.setattr(server, "_live_jobs_from_projects", lambda compact=False: live_items)
    monkeypatch.setattr(server, "_active_project_child_process", lambda *_args, **_kwargs: {"pid": "202", "phase": "experiment", "kind": "experiment_training"})
    terminated = {}
    monkeypatch.setattr(server, "_terminate_process_tree", lambda pid: terminated.setdefault("payload", {"requested_pid": str(pid), "terminated_pids": [str(pid)], "terminated_pgids": []}))

    response = server.api_cancel_job("full-cycle_demo")

    assert response["status"] == "cancelling"
    assert response["result"]["pid"] == "100"
    assert response["termination"]["requested_pid"] == "100"
    assert terminated["payload"]["requested_pid"] == "100"


def test_live_jobs_lists_all_active_project_workers(monkeypatch, tmp_path):
    from auto_research.web import server

    server._LIVE_JOBS_CACHE.clear()
    project = "demo_project"
    root = tmp_path / "projects" / project
    state_dir = root / "state"
    state_dir.mkdir(parents=True)
    (root / "planning" / "finding").mkdir(parents=True)
    write_json(root / "project.json", {"name": project, "topic": "demo"})
    write_json(state_dir / "full_cycle_job.json", {
        "project": project,
        "web_job_id": "full-cycle_demo",
        "status": "stale",
        "pid": "100",
        "kind": "full_cycle",
        "stage": "experiment",
        "process_alive": False,
        "stale_reason": "no_matching_live_full_cycle_process",
    })
    write_json(state_dir / "full_research_cycle.json", {
        "project": project,
        "status": "stale_full_research_cycle_snapshot",
        "summary": "完整科研自循环进程已停止；没有正在运行的 full-cycle。",
        "started_at": "2026-06-04T00:00:00+00:00",
        "latest_step": {"stage": "experiment", "status": "stale"},
    })
    write_json(root / "planning" / "finding" / "find_progress.json", {"run_id": "find_demo", "status": "complete"})
    write_json(state_dir / "finding_frontend.json", {"taste_run_id": "find_demo", "status": "find_completed"})
    write_json(state_dir / "current_find_research_plan.json", {"run_id": "find_old", "status": "historical"})
    monkeypatch.setattr(server, "PROJECT_IDS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(server, "list_projects", lambda: [{"id": project, "path": str(root)}])
    monkeypatch.setattr(server, "_pid_alive_local", lambda pid: str(pid) in {"201", "202"})
    monkeypatch.setattr(server, "_active_project_child_processes", lambda seen_project, seen_root, phase_hint="": [
        {"pid": "201", "ppid": "1", "stat": "S", "elapsed": "00:10", "pcpu": "1.0", "pmem": "0.1", "cmd": "python train_diffusion.py --data ATV --model_type CandidateRec --epoch 100", "cwd": str(root / "repos" / "candidates" / "candidaterec"), "kind": "experiment_training", "phase": "experiment"},
        {"pid": "202", "ppid": "1", "stat": "S", "elapsed": "00:11", "pcpu": "1.0", "pmem": "0.1", "cmd": "python train_diffusion.py --data ATV --model_type BaselineRec --epoch 100", "cwd": str(root / "repos" / "candidates" / "candidaterec"), "kind": "experiment_training", "phase": "experiment"},
    ])
    monkeypatch.setattr(server, "_active_detail_lines", lambda *_args, **_kwargs: ([], []))

    jobs = server._live_jobs_from_projects(compact=True)

    worker_rows = [row for row in jobs if str(row.get("job_id", "")).startswith("experiment-worker_")]
    assert {row["result"]["pid"] for row in worker_rows} == {"201", "202"}
    full_rows = [row for row in jobs if row.get("job_id") == "full-cycle_demo"]
    assert full_rows and full_rows[0]["status"] != "running"
    assert full_rows[0]["result"]["process_alive"] is False


def test_live_jobs_lists_active_worker_while_full_cycle_controller_runs(monkeypatch, tmp_path):
    from auto_research.web import server

    server._LIVE_JOBS_CACHE.clear()
    project = "demo_project"
    root = tmp_path / "projects" / project
    state_dir = root / "state"
    state_dir.mkdir(parents=True)
    (root / "planning" / "finding").mkdir(parents=True)
    write_json(root / "project.json", {"name": project, "topic": "demo"})
    write_json(state_dir / "full_cycle_job.json", {
        "project": project,
        "web_job_id": "full-cycle_demo",
        "status": "running",
        "pid": "100",
        "kind": "full_cycle",
        "stage": "experiment",
        "process_alive": True,
        "started_at": "2026-06-05T00:00:00+00:00",
    })
    write_json(state_dir / "full_research_cycle.json", {
        "project": project,
        "status": "running",
        "summary": "完整科研自循环正在运行。",
        "started_at": "2026-06-05T00:00:00+00:00",
        "latest_step": {"stage": "autonomous-research", "status": "running"},
    })
    write_json(root / "planning" / "finding" / "find_progress.json", {"run_id": "find_demo", "status": "complete"})
    monkeypatch.setattr(server, "PROJECT_IDS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(server, "list_projects", lambda: [{"id": project, "path": str(root)}])
    monkeypatch.setattr(server, "_pid_alive_local", lambda pid: str(pid) in {"100", "202"})
    monkeypatch.setattr(server, "_ps_row_for_pid", lambda pid: {"pid": str(pid), "elapsed": "00:20", "cmd": "python framework/scripts/run_full_research_cycle.py --project demo_project"} if str(pid) == "100" else {})
    monkeypatch.setattr(server, "_active_project_child_processes", lambda seen_project, seen_root, phase_hint="": [
        {"pid": "100", "ppid": "1", "stat": "S", "elapsed": "00:20", "pcpu": "1.0", "pmem": "0.1", "cmd": "python framework/scripts/run_full_research_cycle.py --project demo_project", "cwd": str(root), "kind": "full_cycle", "phase": "full-cycle"},
        {"pid": "202", "ppid": "100", "stat": "S", "elapsed": "00:11", "pcpu": "1.0", "pmem": "0.1", "cmd": "python train_nullspace_discrete_diff.py --data ATV --epoch 100", "cwd": str(root / "repos" / "candidates" / "candidaterec"), "kind": "experiment_training", "phase": "experiment"},
    ])
    monkeypatch.setattr(server, "_active_project_child_process", lambda seen_project, seen_root, phase_hint="": {"pid": "202", "kind": "experiment_training", "phase": "experiment", "cmd": "python train_nullspace_discrete_diff.py --data ATV --epoch 100"})
    monkeypatch.setattr(server, "_active_detail_lines", lambda *_args, **_kwargs: ([], []))
    monkeypatch.setattr(server, "_latest_claude_agent_status_lines", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(server, "_tail_file_lines", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(server, "_has_active_experiment_training", lambda seen_root, seen_pid: True)

    jobs = server._live_jobs_from_projects(compact=True)

    full_rows = [row for row in jobs if row.get("job_id") == "full-cycle_demo"]
    worker_rows = [row for row in jobs if str(row.get("job_id", "")).startswith("experiment-worker_")]
    assert len(full_rows) == 1
    assert full_rows[0]["status"] == "running"
    assert full_rows[0]["run_id"] == "find_demo"
    assert full_rows[0]["result"]["pid"] == "100"
    assert {row["result"]["pid"] for row in worker_rows} == {"202"}
    assert worker_rows[0]["result"]["not_full_cycle_controller"] is True
    assert "由当前完整科研循环管理" in worker_rows[0]["result"]["summary"]
    assert "控制器已停止" not in worker_rows[0]["result"]["summary"]


def test_current_find_worker_uses_live_frontend_run_id(monkeypatch, tmp_path):
    from auto_research.web import server

    project = "demo_project"
    root = tmp_path / "projects" / project
    state_dir = root / "state"
    state_dir.mkdir(parents=True)
    (root / "planning" / "finding").mkdir(parents=True)
    write_json(state_dir / "finding_frontend.json", {"taste_run_id": "find_new", "status": "find_completed"})
    write_json(state_dir / "current_find_research_plan.json", {"run_id": "find_old", "status": "historical"})
    find_progress = {"run_id": "find_new", "phase": "complete"}
    monkeypatch.setattr(server, "_pid_alive_local", lambda pid: str(pid) == "202")
    monkeypatch.setattr(server, "_latest_project_log", lambda *_args, **_kwargs: "")

    job = server._active_project_worker_job(
        project,
        root,
        {"pid": "202", "phase": "read", "kind": "current_find_claude_child", "cmd": "python modules/reading/scripts/ensure_current_find_research_plan.py"},
        {},
        {"run_id": "find_old"},
        find_progress,
        compact=True,
        controller_alive=True,
    )

    assert job["run_id"] == "find_new"
    assert job["stage"] == "read"


def test_full_text_repair_replaces_stale_packet_before_first_attempt(monkeypatch, tmp_path):
    repair = importlib.import_module("repair_current_find_full_text_evidence")

    project = "demo_project"
    root = tmp_path / "projects" / project
    planning = root / "planning"
    finding = planning / "finding"
    state = root / "state"
    (finding / "full_text_reading").mkdir(parents=True)
    state.mkdir(parents=True)
    paths = type("Paths", (), {"root": root, "planning": planning, "state": state, "name": project})()
    write_json(
        finding / "find_results.json",
        {"run_id": "find_new", "strong_recommendations": [{"title": "Paper One", "id": "paper-one", "pdf_url": "https://example.test/paper.pdf"}]},
    )
    write_json(
        finding / "full_text_reading" / "full_text_packet.json",
        {"run_id": "find_old", "source": "old", "papers": [{"title": "Old Paper", "text_path": "old.txt", "text_chars": 9000}]},
    )
    write_json(
        state / "current_find_claude_reading_validation.json",
        {"run_id": "find_new", "generated_at": "2026-06-10T00:00:00+00:00", "pending_without_evidence_titles": ["Paper One"]},
    )
    seen_run_ids = []

    def fake_try(paths_arg, paper, rank):
        seen_run_ids.append(repair.load_json(repair.full_text_packet_path(paths_arg), {}).get("run_id"))
        return None, [{"kind": "fake_attempt", "accepted": False}]

    monkeypatch.setattr(repair, "build_paths", lambda _project: paths)
    monkeypatch.setattr(repair, "try_acquire_for_paper", fake_try)
    monkeypatch.setattr(repair, "record_unavailable_full_text_evidence_blocker", lambda *_args, **_kwargs: {"status": "recorded"})

    rc, receipt = repair.repair_current_find_full_text_evidence(project, force=True)

    packet = repair.load_json(repair.full_text_packet_path(paths), {})
    assert rc == 2
    assert receipt["run_id"] == "find_new"
    assert seen_run_ids == ["find_new"]
    assert packet["run_id"] == "find_new"
    assert packet["previous_packet_run_id"] == "find_old"
    assert packet["papers"][0]["title"] == "Paper One"


def test_full_text_packet_path_honors_run_local_full_text_dir(tmp_path):
    repair = importlib.import_module("repair_current_find_full_text_evidence")
    paths = type(
        "Paths",
        (),
        {
            "root": tmp_path,
            "planning": tmp_path / "planning",
            "full_text_reading_dir": tmp_path / "runs" / "find_demo" / "full_text_reading",
        },
    )()

    assert repair.full_text_packet_path(paths) == tmp_path / "runs" / "find_demo" / "full_text_reading" / "full_text_packet.json"


def test_full_text_repair_fetch_retries_retryable_status(monkeypatch):
    repair = importlib.import_module("repair_current_find_full_text_evidence")

    class FakeResponse:
        def __init__(self, status_code, content_type, content, url="https://openreview.net/pdf?id=demo"):
            self.status_code = status_code
            self.headers = {"content-type": content_type}
            self.content = content
            self.url = url

    calls = []
    responses = [
        FakeResponse(503, "text/html", b"busy"),
        FakeResponse(200, "application/pdf", b"%PDF demo"),
    ]

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(repair.requests, "get", fake_get)
    monkeypatch.setattr(repair.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(repair, "FULL_TEXT_FETCH_ATTEMPTS", 3)
    monkeypatch.setattr(repair, "FULL_TEXT_FETCH_RETRY_BASE_DELAY_SEC", 0.0)

    status, content_type, content, final_url = repair.fetch_url("https://openreview.net/pdf?id=demo")

    assert status == 200
    assert content_type == "application/pdf"
    assert content.startswith(b"%PDF")
    assert final_url == "https://openreview.net/pdf?id=demo"
    assert len(calls) == 2


def test_openreview_scan_retries_retryable_status(monkeypatch):
    repair = importlib.import_module("repair_current_find_full_text_evidence")

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.headers = {"content-type": "application/json"}
            self.content = b""
            self.url = "https://api2.openreview.net/notes"
            self._payload = payload

        def json(self):
            return self._payload

    responses = [
        FakeResponse(503, {}),
        FakeResponse(200, {"notes": [{"id": "demo", "forum": "demo", "content": {"title": {"value": "Demo Paper"}}}]}),
    ]

    def fake_get(url, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(repair.requests, "get", fake_get)
    monkeypatch.setattr(repair.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(repair, "FULL_TEXT_FETCH_ATTEMPTS", 3)
    monkeypatch.setattr(repair, "FULL_TEXT_FETCH_RETRY_BASE_DELAY_SEC", 0.0)
    repair._OPENREVIEW_SCAN_CACHE.clear()

    notes = repair._fetch_openreview_scan("demo_scan", {"invitations": "Demo.cc/2026/Conference/-/Submission"})

    assert [note["id"] for note in notes] == ["demo"]


def test_web_find_repair_runs_full_text_evidence_script(monkeypatch, tmp_path):
    root = tmp_path / "projects" / "demo_project"
    (root / "state").mkdir(parents=True)
    seen: dict[str, object] = {}

    class FakeStdout:
        def __iter__(self):
            return iter([
                '{"status":"blocked_full_text_evidence_unavailable","acquired_count":0,"unavailable_count":1,"pending_after_repair":["Paper One"]}\n'
            ])

    class FakeProc:
        stdout = FakeStdout()

        def wait(self, timeout=5):
            seen["wait_timeout"] = timeout
            return 2

        def terminate(self):
            seen["terminated"] = True

    def fake_popen(cmd, cwd, env, text, stdout, stderr, bufsize):
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        seen["env"] = env
        seen["text"] = text
        seen["bufsize"] = bufsize
        return FakeProc()

    logs: list[str] = []
    progress_calls: list[tuple[str, int, int, str]] = []
    monkeypatch.setenv("MANAGEMENT_PYTHON", "/opt/taste/python")
    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)

    result = server._run_current_find_full_text_evidence_repair(
        "demo_project",
        root,
        logs.append,
        lambda: False,
        lambda phase, current, total, message: progress_calls.append((phase, current, total, message)),
    )

    cmd = seen["cmd"]
    env = seen["env"]
    assert cmd[:2] == ["/opt/taste/python", str(server.WORKSPACE_ROOT / "modules" / "reading" / "scripts" / "repair_current_find_full_text_evidence.py")]
    assert cmd[-3:] == ["--project", "demo_project", "--force"]
    assert seen["cwd"] == str(server.WORKSPACE_ROOT)
    assert env["WORKSPACE_ROOT"] == str(server.WORKSPACE_ROOT)
    assert env["PROJECT_ID"] == "demo_project"
    assert env["DEFAULT_PROJECT_ID"] == "demo_project"
    assert str(server.WORKSPACE_ROOT / "framework" / "scripts") in env["PYTHONPATH"].split(server.os.pathsep)
    assert str(server.WORKSPACE_ROOT / "modules" / "reading" / "scripts") in env["PYTHONPATH"].split(server.os.pathsep)
    assert result["returncode"] == 2
    assert result["status"] == "blocked_full_text_evidence_unavailable"
    assert result["pending_after_repair"] == ["Paper One"]
    assert progress_calls[-1][0] == "full_text_evidence_blocked"
    assert any("full-text evidence repair" in line for line in logs)


def test_current_find_selection_command_runs_full_takeover_until_content_ready(monkeypatch, tmp_path):
    from auto_research.web import project_bridge

    projects = tmp_path / "projects"
    project = "demo_project"
    (projects / project).mkdir(parents=True)
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    monkeypatch.setattr(project_bridge, "management_python", lambda: "/opt/taste/python")
    monkeypatch.setattr(project_bridge, "_literature_recommendation_gate_is_blocked", lambda _project: False)
    monkeypatch.setattr(project_bridge, "_current_find_pipeline_summary", lambda _root, **_kwargs: {"content_ready": False, "readings": 0, "ideas": 0, "plans": 0})

    _project, cmd = project_bridge.build_command({"project": project, "action": "current-find-selection"})

    assert cmd[-1] == "--force"

    monkeypatch.setattr(project_bridge, "_current_find_pipeline_summary", lambda _root, **_kwargs: {"content_ready": True, "readings": 4, "ideas": 5, "plans": 5})

    _project, cmd = project_bridge.build_command({"project": project, "action": "current-find-selection"})

    assert cmd[-1] == "--force-selection"


def test_full_text_preflight_ready_does_not_report_pending_full_text():
    ensure_plan = importlib.import_module("ensure_current_find_research_plan")
    validation = ensure_plan._current_find_evidence_preflight_validation(
        "find_demo",
        {
            "expected_recommendation_count": 2,
            "full_text_evidence_count": 2,
            "pending_without_evidence_titles": [],
            "full_text_evidence_titles": ["Paper One", "Paper Two"],
            "full_text_packet": {"run_id": "find_demo", "papers": [{"title": "Paper One"}, {"title": "Paper Two"}]},
        },
        status="current_find_full_text_evidence_ready_pending_claude_deep_read",
        blockers=["Read-stage full-text packet evidence is ready; Claude Code must now synthesize detailed per-paper deep readings"],
    )

    assert validation["full_text_evidence_count"] == 2
    assert validation["pending_without_evidence_count"] == 0
    assert validation["pending_full_text_reading_count"] == 0
    assert validation["pending_full_text_reading_titles"] == []
    assert validation["pending_deep_read_synthesis_count"] == 2
    assert ensure_plan.current_reading_validation_needs_full_text_evidence(validation) is False
    assert ensure_plan.current_reading_validation_needs_claude_rewrite(validation) is True


def test_missing_current_find_reading_with_packet_body_requires_deep_read_not_evidence(tmp_path):
    ensure_plan = importlib.import_module("ensure_current_find_research_plan")
    project_root = tmp_path / "demo_project"
    finding = project_root / "planning" / "finding"
    packet_dir = finding / "full_text_reading"
    text_dir = packet_dir / "texts"
    state_dir = project_root / "state"
    text_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    title_one = "Evidence Ready Paper One"
    title_two = "Evidence Ready Paper Two"
    body = " ".join([
        title_two,
        "Abstract Introduction Methodology Experiments Evaluation Results Conclusion References",
        "This full paper text is intentionally long and repeats section markers for body-shape validation.",
    ])
    (text_dir / "two.txt").write_text((body + "\n") * 900, encoding="utf-8")
    packet = {
        "run_id": "find_demo",
        "papers": [
            {"id": "paper-2", "title": title_two, "text_path": "texts/two.txt", "text_chars": 30000, "pdf_url": "https://example.test/two.pdf"},
        ],
    }
    write_json(packet_dir / "full_text_packet.json", packet)
    find_results = {
        "run_id": "find_demo",
        "strong_recommendations": [recommended_paper("paper-1", title_one), recommended_paper("paper-2", title_two)],
        "articles": [],
    }
    readings = [
        {
            "paper_id": "paper-1",
            "id": "paper-1",
            "title": title_one,
            "support_role": "strong_evidence",
            "verdict": "claim_ready",
            "abstract_zh": "该论文有完整中文摘要，说明研究问题、方法和实验。",
            "motivation_zh": "该论文动机清楚，面向推荐系统中的真实问题。",
            "method_details_zh": "方法包含模型结构、训练目标、输入输出和推理流程。",
            "experiments_zh": "实验覆盖数据集、指标、对照方法、消融和主要结果。",
            "limitations_zh": "局限性包括数据范围和泛化边界。",
            "method_advantages_zh": ["优点一是方法结构清晰。", "优点二是实验协议可复用。"],
            "method_disadvantages_zh": ["不足一是验证场景有限。", "不足二是部分假设需要继续检验。"],
            "full_text_available": True,
            "full_text_status": "pdf_text_read",
            "pdf_text_chars": 24000,
            "source_text_chars": 24000,
            "subagent_deep_read": True,
            "deep_read_audit": {"mode": "task_subagent", "subagent_used": True, "status": "completed", "text_path": "texts/one.txt", "source_text_chars": 24000},
        }
    ]

    class Paths:
        root = project_root
        planning = project_root / "planning"
        state = state_dir

    valid, report = ensure_plan.validate_claude_readings_against_current_find(readings, find_results, 2, Paths, "find_demo")

    assert valid is False
    assert report["pending_without_evidence_count"] == 0
    assert report["pending_full_text_reading_count"] == 0
    assert title_two in report["pending_deep_read_synthesis_titles"]
    assert ensure_plan.current_reading_validation_needs_full_text_evidence(report) is False
    assert ensure_plan.current_reading_validation_needs_claude_rewrite(report) is True


def test_active_launcher_experiment_runs_from_sidecar(monkeypatch, tmp_path):
    from auto_research.web import server

    root = tmp_path / "projects" / "demo_project"
    artifact = root / "artifacts" / "launcher_demo"
    artifact.mkdir(parents=True)
    stdout = artifact / "stdout_stderr.log"
    stdout.write_text("epoch 3 ndcg@10=0.42\n", encoding="utf-8")
    (artifact / "launcher.pid.json").write_text(json.dumps({"pid": 4242, "artifact_dir": str(artifact)}), encoding="utf-8")
    (artifact / "run_contract.json").write_text(
        json.dumps(
            {
                "pid": 4242,
                "artifact_dir": str(artifact),
                "stdout_path": str(stdout),
                "command": ["/project/env/bin/python", "-u", "runner.py"],
                "command_display": "/project/env/bin/python -u runner.py",
                "cwd": str(root / "repos" / "selected" / "demo"),
                "experiment_metadata": {"dataset": "amazon-beauty", "method": "demo_method"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_pid_alive_local", lambda pid: str(pid) == "4242")
    monkeypatch.setattr(server, "_ps_row_for_pid", lambda pid: {"pid": "4242", "ppid": "1", "stat": "R", "elapsed": "00:10", "pcpu": "99", "pmem": "1.0", "cmd": "/project/env/bin/python -u runner.py", "cwd": str(root / "repos" / "selected" / "demo")})

    rows = server._active_launcher_experiment_runs(root)

    assert len(rows) == 1
    assert rows[0]["pid"] == "4242"
    assert rows[0]["dataset"] == "amazon-beauty"
    assert rows[0]["stdout_path"] == str(stdout)
    assert server._has_active_experiment_training(root, "9999") is True


def test_active_detail_lines_include_detached_launcher_training(monkeypatch, tmp_path):
    from auto_research.web import server

    root = tmp_path / "projects" / "demo_project"
    repo = root / "repos" / "selected" / "demo"
    repo.mkdir(parents=True)
    (root / "state" / "evidence_ready_repo_selection.json").parent.mkdir(parents=True, exist_ok=True)
    write_json(root / "state" / "evidence_ready_repo_selection.json", {"selected": {"repo_path": str(repo)}})
    artifact = root / "artifacts" / "launcher_demo"
    artifact.mkdir(parents=True)
    stdout = artifact / "stdout_stderr.log"
    stdout.write_text("[demo] epoch 7 ndcg@10=0.51\n", encoding="utf-8")
    write_json(artifact / "launcher.pid.json", {"pid": 4242, "artifact_dir": str(artifact)})
    write_json(
        artifact / "run_contract.json",
        {
            "pid": 4242,
            "artifact_dir": str(artifact),
            "stdout_path": str(stdout),
            "command_display": "/project/env/bin/python -u runner.py",
            "cwd": str(repo),
            "experiment_metadata": {"dataset": "amazon-beauty", "method": "demo_method"},
        },
    )
    monkeypatch.setattr(server, "_pid_alive_local", lambda pid: str(pid) == "4242")
    monkeypatch.setattr(server, "_ps_row_for_pid", lambda pid: {"pid": "4242", "ppid": "1", "stat": "R", "elapsed": "00:10", "pcpu": "99", "pmem": "1.0", "cmd": "/project/env/bin/python -u runner.py", "cwd": str(repo)})
    monkeypatch.setattr(
        server,
        "_process_tree_rows",
        lambda pid: [{"pid": "4242", "ppid": "1", "stat": "R", "elapsed": "00:10", "pcpu": "99", "pmem": "1.0", "cmd": "/project/env/bin/python -u runner.py", "cwd": str(repo)}],
    )
    monkeypatch.setattr(server, "_all_process_rows", lambda: [])

    logs, artifacts = server._active_detail_lines(root, "4242", "experiment")

    assert any(line.startswith("experiment_cmd=/project/env/bin/python -u runner.py") for line in logs)
    assert any("experiment_run=amazon-beauty" in line and str(stdout) in line for line in logs)
    assert any(str(stdout) in line for line in logs)
    assert str(artifact) in artifacts


def test_active_detail_lines_show_recent_completed_launcher_log(monkeypatch, tmp_path):
    from auto_research.web import server

    root = tmp_path / "projects" / "demo_project"
    artifact = root / "artifacts" / "launcher_demo"
    artifact.mkdir(parents=True)
    stdout = artifact / "stdout_stderr.log"
    stdout.write_text("Testing complete ndcg@10=0.51\n", encoding="utf-8")
    write_json(artifact / "launcher.pid.json", {"pid": 4242, "artifact_dir": str(artifact)})
    write_json(artifact / "run_contract.json", {"pid": 4242, "artifact_dir": str(artifact), "stdout_path": str(stdout), "command_display": "/project/env/bin/python -u runner.py"})
    monkeypatch.setattr(server, "_pid_alive_local", lambda pid: False)
    monkeypatch.setattr(server, "_process_tree_rows", lambda pid: [])
    monkeypatch.setattr(server, "_all_process_rows", lambda: [])

    logs, artifacts = server._active_detail_lines(root, "9999", "experiment")

    assert any("最近一次实验训练已结束" in line for line in logs)
    assert any(line == f"experiment_log={stdout}" for line in logs)
    assert any("Testing complete ndcg@10=0.51" in line for line in logs)
    assert str(stdout) in artifacts


def test_active_detail_lines_recent_completed_launcher_log_reports_registered_audit(monkeypatch, tmp_path):
    from auto_research.web import server

    root = tmp_path / "projects" / "demo_project"
    artifact = root / "artifacts" / "launcher_demo"
    artifact.mkdir(parents=True)
    stdout = artifact / "stdout_stderr.log"
    stdout.write_text("Testing complete ndcg@10=0.49\n", encoding="utf-8")
    write_json(artifact / "launcher.pid.json", {"pid": 4242, "artifact_dir": str(artifact)})
    write_json(artifact / "run_contract.json", {"pid": 4242, "artifact_dir": str(artifact), "stdout_path": str(stdout), "command_display": "/project/env/bin/python -u runner.py"})
    write_json(
        root / "state" / "experiment_registry.json",
        [
            {
                "experiment_id": "launcher_demo",
                "artifact_path": str(artifact),
                "status": "completed",
                "audit_ready": True,
                "metric_name": "ndcg_at_10",
                "metric_value": 0.49,
                "promotion_status": "candidate_observation_only",
                "comparison_status": "not_above_selected_base_reference",
            }
        ],
    )
    monkeypatch.setattr(server, "_pid_alive_local", lambda pid: False)
    monkeypatch.setattr(server, "_process_tree_rows", lambda pid: [])
    monkeypatch.setattr(server, "_all_process_rows", lambda: [])

    logs, artifacts = server._active_detail_lines(root, "9999", "experiment")

    assert any("已登记审计" in line and "未通过科研进展门控" in line for line in logs)
    assert not any("等待 project agent 登记审计" in line for line in logs)
    assert any("Testing complete ndcg@10=0.49" in line for line in logs)
    assert str(stdout) in artifacts


def test_cancel_worker_job_uses_exact_pid_from_job_id(monkeypatch, tmp_path):
    from auto_research.web import server

    project = "demo_project"
    root = tmp_path / "projects" / project
    root.mkdir(parents=True)
    monkeypatch.setattr(server, "PROJECT_IDS_ROOT", tmp_path / "projects")
    live_items = [
        {"job_id": "experiment-worker_demo_project_201", "stage": "experiment", "status": "running", "result": {"project": project, "pid": "201", "phase": "experiment", "process_alive": True}, "logs": [], "progress": {}},
        {"job_id": "experiment-worker_demo_project_202", "stage": "experiment", "status": "running", "result": {"project": project, "pid": "202", "phase": "experiment", "process_alive": True}, "logs": [], "progress": {}},
    ]
    monkeypatch.setattr(server, "_live_jobs_from_projects", lambda compact=False: live_items)
    terminated = {}
    monkeypatch.setattr(server, "_terminate_process_tree", lambda pid: terminated.setdefault("payload", {"requested_pid": str(pid), "terminated_pids": [str(pid)], "terminated_pgids": []}))

    response = server.api_cancel_job("experiment-worker_demo_project_202")

    assert response["status"] == "cancelling"
    assert response["result"]["pid"] == "202"
    assert response["termination"]["requested_pid"] == "202"
    assert terminated["payload"]["requested_pid"] == "202"


def test_cancel_exact_environment_job_does_not_fallback_to_stale_full_cycle(monkeypatch, tmp_path):
    from auto_research.web import server

    server.JOBS.clear()
    monkeypatch.setattr(server, "JOBS_PATH", tmp_path / "web_jobs.json")
    project = "demo_project"
    root = tmp_path / "projects" / project
    root.mkdir(parents=True)
    monkeypatch.setattr(server, "PROJECT_IDS_ROOT", tmp_path / "projects")
    job = server.JobState("environment_exact", "environment")
    job.status = "running"
    job.logs = ["Workflow command: /env/bin/python modules/environment/scripts/run_environment_stage.py --project demo_project --venue ICLR"]
    server.JOBS[job.job_id] = job
    monkeypatch.setattr(
        server,
        "_live_jobs_from_projects",
        lambda compact=False: [
            {
                "job_id": "full-cycle_stale",
                "stage": "experiment",
                "status": "blocked",
                "result": {
                    "project": project,
                    "pid": "100",
                    "phase": "experiment",
                    "command": "/env/bin/python framework/scripts/run_full_research_cycle.py --project demo_project",
                    "process_alive": False,
                },
                "logs": [],
                "progress": {},
            }
        ],
    )

    def active_child(seen_project, seen_root, phase_hint=""):
        assert seen_project == project
        assert seen_root == root
        if phase_hint == "environment":
            return {"pid": "202", "phase": "environment", "kind": "environment_stage", "cmd": "/env/bin/python modules/environment/scripts/run_environment_stage.py --project demo_project"}
        return {}

    terminated = {}
    monkeypatch.setattr(server, "_active_project_child_process", active_child)
    monkeypatch.setattr(server, "_terminate_process_tree", lambda pid: terminated.setdefault("payload", {"requested_pid": str(pid), "terminated_pids": [str(pid)], "terminated_pgids": []}))

    response = server.api_cancel_job("environment_exact")

    assert response["job_id"] == "environment_exact"
    assert response["stage"] == "environment"
    assert response["status"] == "cancelling"
    assert response["cancel_requested"] is True
    assert response["result"]["pid"] == "202"
    assert response["termination"]["requested_pid"] == "202"
    assert job.cancel_requested is True


def test_literature_base_audit_uses_current_find_when_fresh_base_is_stale(monkeypatch, tmp_path):
    ensure_script_paths()
    audit = importlib.import_module("run_literature_base_audit")
    paths = type("Paths", (), {"name": "demo_project", "state": tmp_path / "state", "planning": tmp_path / "planning"})()
    paths.state.mkdir(parents=True)
    (paths.planning / "finding").mkdir(parents=True)
    write_json(paths.planning / "finding" / "find_progress.json", {"run_id": "find_new"})
    write_json(paths.state / "fresh_research_base.json", {"fresh_find_run_id": "find_old", "top_candidates": [{"title": "Old CandidateRec", "rank": 1}]})
    monkeypatch.setattr(audit, "candidates_from_current_find", lambda seen_paths, run_id: [{"title": "Current Diffusion Base", "fresh_find_run_id": run_id}])

    run_id, rows = audit.candidates_from_current_fresh_base(paths)

    assert run_id == "find_new"
    assert rows == [{"title": "Current Diffusion Base", "fresh_find_run_id": "find_new"}]


def test_literature_base_audit_quotes_real_candidate_title(monkeypatch):
    ensure_script_paths()
    audit = importlib.import_module("run_literature_base_audit")

    queries = audit.candidate_search_queries({"title": "Reference Recommender Benchmark Paper"}, 2)

    assert queries[0] == '"Reference Recommender Benchmark Paper"'
    assert " + title +" not in " ".join(queries)


def test_runtime_detect_endpoint_calls_detector(monkeypatch):
    calls = []

    def fake_detect(project: str):
        calls.append(("detect", project))
        return {"node_bin": "/tmp/node/bin", "claude_path": "/tmp/node/bin/claude"}

    def fake_status(project: str):
        calls.append(("status", project))
        return {"checks": {"node": {"ok": True}}, "runtime_status": "ok"}

    monkeypatch.setattr(server, "detect_runtime_config", fake_detect)
    monkeypatch.setattr(server, "runtime_status", fake_status)

    result = server.api_project_runtime_detect("demo_project")

    assert calls == [("detect", "demo_project"), ("status", "demo_project")]
    assert result["runtime"] == {"node_bin": "/tmp/node/bin", "claude_path": "/tmp/node/bin/claude"}
    assert result["checks"]["node"]["ok"] is True


def test_environment_repo_search_ignores_find_source_toggles(monkeypatch):
    ensure_script_paths()
    env_stage = importlib.import_module("run_environment_stage")
    monkeypatch.setattr(env_stage, "project_search_queries", lambda _project: ["retrieval benchmark repo"])
    calls = []
    monkeypatch.setattr(env_stage, "run_optional", lambda cmd, _cwd: calls.append(cmd) or 0)

    env_stage.expand_repo_search("demo_project", 1, limit=4)

    github = next(cmd for cmd in calls if "modules/finding/scripts/discover_github_repos.py" in cmd)
    arxiv = next(cmd for cmd in calls if "modules/finding/scripts/discover_arxiv.py" in cmd)
    assert "--ignore-source-selection" in github
    assert "--ignore-source-selection" in arxiv
    assert any("modules/finding/scripts/ingest_discovery.py" in cmd for cmd in calls)







def test_blocker_action_plan_command_supports_optional_venue(monkeypatch):
    ensure_script_paths()
    blocker = importlib.reload(importlib.import_module("build_blocker_action_plan"))
    monkeypatch.setattr(blocker, "management_python", lambda: "/env/python")

    with_venue = blocker.command("demo", "ICLR", "audit_paper_evidence.py")
    without_venue = blocker.command("demo", "audit_paper_evidence.py")

    assert with_venue == "/env/python modules/writing/scripts/audit_paper_evidence.py --project demo --venue ICLR"
    assert without_venue == "/env/python modules/writing/scripts/audit_paper_evidence.py --project demo"


def test_discover_arxiv_slug_removes_path_separators(monkeypatch):
    ensure_script_paths()
    discover_arxiv = importlib.reload(importlib.import_module("discover_arxiv"))

    slug = discover_arxiv.safe_discovery_slug("LLM user/item R$^2$ec: code dataset")

    assert "/" not in slug
    assert ":" not in slug
    assert slug


def test_environment_stage_explicit_env_name_overrides_strategy_recommendation(monkeypatch):
    ensure_script_paths()
    env_stage = importlib.reload(importlib.import_module("run_environment_stage"))
    strategy = {"env_action": "create_new_project_env", "recommended_env_name": "demo_project"}

    assert env_stage.strategy_env_name(strategy, "llm_diff_rec", explicit_env_name="llm_diff_rec") == "llm_diff_rec"
    assert env_stage.strategy_env_name(strategy, "fallback_env", explicit_env_name="") == "demo_project"


def test_environment_run_optional_timeout_returns_124(monkeypatch, tmp_path, capsys):
    ensure_script_paths()
    env_stage = importlib.import_module("run_environment_stage")

    def fake_run(cmd, cwd, text=True, timeout=None):
        assert timeout == 7
        raise env_stage.subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(env_stage.subprocess, "run", fake_run)

    rc = env_stage.run_optional(["selector", "--project", "demo"], tmp_path, timeout=7)

    assert rc == 124
    assert "optional command timed out after 7s" in capsys.readouterr().out




def test_environment_refresh_reuses_existing_claim_ready_probe(monkeypatch, tmp_path):
    ensure_script_paths()
    env_stage = importlib.import_module("run_environment_stage")
    paths = type("Paths", (), {"state": tmp_path / "state"})()
    paths.state.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    write_json(paths.state / "real_dataset_probe.json", {"repo_path": str(repo), "probes": [{"claim_ready": True, "loader_probe": {"success": True}}]})
    calls = []

    monkeypatch.setattr(env_stage, "build_paths", lambda _project: paths)
    monkeypatch.setattr(env_stage, "run", lambda cmd, cwd, timeout=None: calls.append(cmd) or 0)

    env_stage.refresh_repo_data("demo", str(repo), "demo_env")

    assert any("modules/environment/scripts/build_repo_data_requirements.py" in cmd for cmd in calls)
    assert not any("modules/environment/scripts/probe_repo_dataset.py" in cmd for cmd in calls)




def test_data_policy_normalizes_pool_ready_candidates_without_execution_ready(monkeypatch, tmp_path):
    import sys

    ensure_script_paths()
    data_policy = importlib.reload(importlib.import_module("data_unavailability_policy"))
    paths = type("Paths", (), {"state": tmp_path / "state", "reports": tmp_path / "reports"})()
    paths.state.mkdir(parents=True)
    paths.reports.mkdir(parents=True)
    write_json(paths.state / "active_repo.json", {"name": "Active/Blocked", "repo_path": str(tmp_path / "active")})
    write_json(paths.state / "repo_data_requirements.json", {"blocked_datasets": ["missing-data"], "ready_datasets": []})
    write_json(paths.state / "real_dataset_probe.json", {"probes": []})
    write_json(paths.state / "repo_candidate_pool_audit.json", {
        "evidence_ready_candidates": [{
            "name": "Example/ReadyRepo",
            "url": "https://github.com/example/ready",
            "score": 6.6,
            "repo_path": str(tmp_path / "ready"),
            "data_requirements": {"ready_datasets": ["amazon-beauty"]},
        }]
    })

    monkeypatch.setattr(data_policy, "build_paths", lambda _project: paths)
    monkeypatch.setattr(data_policy, "load_project_config", lambda _project: {"literature": {"repo_candidate_floor": 0}})
    monkeypatch.setattr(sys, "argv", ["data_unavailability_policy.py", "--project", "demo"])

    data_policy.main()

    payload = read_json(paths.state / "data_unavailability_policy.json")
    candidates = payload["evidence_ready_alternative_repo_candidates"]
    assert payload["decision"] == "switch_or_backtrack_to_evidence_ready_repo"
    assert candidates[0]["name"] == "Example/ReadyRepo"
    assert candidates[0]["execution_ready"] is True
    assert "ready=True" in (paths.reports / "data_unavailability_policy.md").read_text(encoding="utf-8")


def test_current_run_environment_selector_allows_active_repo_reaudit(monkeypatch, tmp_path):
    ensure_script_paths()
    env_stage = importlib.import_module("run_environment_stage")
    paths = type("Paths", (), {"state": tmp_path / "state", "planning": tmp_path / "planning"})()
    paths.state.mkdir(parents=True)
    (paths.planning / "finding").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = []

    monkeypatch.setattr(env_stage, "current_find_run_id", lambda _paths: "find_new")
    monkeypatch.setattr(env_stage, "current_env_selection_valid", lambda _paths: True)

    def fake_run_optional(cmd, cwd, timeout=None):
        calls.append(cmd)
        if "modules/environment/scripts/select_evidence_ready_repo.py" in cmd:
            write_json(paths.state / "evidence_ready_repo_selection.json", {"selected": {"repo_path": str(repo)}})
        return 0

    monkeypatch.setattr(env_stage, "run_optional", fake_run_optional)

    selected = env_stage.select_current_run_environment_repo("demo", paths, "demo_env", max_rounds=1)

    selector_cmd = next(cmd for cmd in calls if "modules/environment/scripts/select_evidence_ready_repo.py" in cmd)
    assert selected == str(repo)
    assert "--fresh-find-run-id" in selector_cmd
    assert "--exclude-active-repo" not in selector_cmd


def test_environment_blocker_replaces_stale_implementation_plan(monkeypatch, tmp_path):
    ensure_script_paths()
    env_stage = importlib.import_module("run_environment_stage")
    paths = type("Paths", (), {"state": tmp_path / "state", "planning": tmp_path / "planning"})()
    paths.state.mkdir(parents=True)
    (paths.planning / "finding").mkdir(parents=True)
    write_json(paths.planning / "finding" / "find_progress.json", {"run_id": "find_new"})
    write_json(
        paths.planning / "finding" / "plans.json",
        {
            "run_id": "find_new",
            "selected_plan_id": "plan-current",
            "selected_idea_id": "idea-current",
            "plans": [{"plan_id": "plan-current", "idea_id": "idea-current", "selected_for_execution": True}],
        },
    )

    env_stage.write_repo_selection_blocker(paths, "no current repo", selection={"fresh_find_run_id": "find_new", "selection_gate": "continued_search_required_by_claude_topic_fit", "selection_stage": "environment_claude_code"})
    env_stage.write_fresh_base_implementation_blocker(paths, "find_new", "no current repo")

    blocker = read_json(paths.state / "repo_selection_blocker.json")
    impl = read_json(paths.state / "fresh_base_implementation_plan.json")
    assert blocker["fresh_find_run_id"] == "find_new"
    assert blocker["selected_plan_id"] == "plan-current"
    assert blocker["selected_idea_id"] == "idea-current"
    assert blocker["blocker_type"] == "environment_repo_selection_blocked"
    assert impl["status"] == "blocked_environment_repo_selection_required"
    assert impl["fresh_find_run_id"] == "find_new"
    assert impl["selected_plan_id"] == "plan-current"
    assert impl["selected_idea_id"] == "idea-current"
    assert impl["repo"] == {}



def test_generic_data_plan_without_project_adapter_marks_selected_repo_data_blocked(tmp_path, monkeypatch):
    ensure_script_paths()
    build_req = importlib.reload(importlib.import_module("build_repo_data_requirements"))
    probe = importlib.reload(importlib.import_module("probe_repo_dataset"))
    build_plan = importlib.reload(importlib.import_module("build_fresh_base_implementation_plan"))
    for module in [build_req, probe, build_plan]:
        monkeypatch.setattr(module, "ROOT", tmp_path)

    project = "demo_project"
    root = tmp_path / "projects" / project
    state = root / "state"
    planning = root / "planning" / "finding"
    repo = root / "repos" / "selected" / "example_repo"
    repo.mkdir(parents=True)
    state.mkdir(parents=True)
    planning.mkdir(parents=True)
    write_json(planning / "find_progress.json", {"run_id": "find_current"})
    write_json(
        state / "evidence_ready_repo_selection.json",
        {
            "fresh_find_run_id": "find_current",
            "selection_stage": "environment_claude_code",
            "selection_gate": "accepted_by_claude_transformable_pending_loader_bootstrap",
            "evidence_ready_count": 0,
            "selected": {
                "name": "example/repo",
                "repo_path": str(repo),
                "fresh_find_run_id": "find_current",
                "selection_stage": "environment_claude_code",
                "pending_loader_bootstrap": True,
                "probe_summary": {"claim_ready_datasets": []},
            },
        },
    )
    adapter = root / "scripts" / "adapters" / "build_fresh_base_implementation_plan.py"

    assert build_req.write_generic_requirement(project, str(repo), root / "scripts" / "adapters" / "build_repo_data_requirements.py") == 0
    assert probe.write_generic_probe(project, str(repo), "demo_env", root / "scripts" / "adapters" / "probe_repo_dataset.py") == 0
    assert build_plan.write_generic_plan(project, adapter) == 0

    req = read_json(state / "repo_data_requirements.json")
    real_probe = read_json(state / "real_dataset_probe.json")
    plan = read_json(state / "fresh_base_implementation_plan.json")
    assert req["status"] == "blocked_missing_project_data_adapter"
    assert real_probe["status"] == "blocked_missing_project_dataset_probe_adapter"
    assert plan["status"] == "blocked_fresh_base_data_required"
    assert plan["repo"]["name"] == "example/repo"
    assert plan["ready_datasets"] == []
    assert plan["blocked_datasets"]


def test_selected_pending_environment_repo_summary_is_data_blocked(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project = "demo_project"
    root = tmp_path / project
    state = root / "state"
    planning = root / "planning" / "finding"
    repo = root / "repos" / "selected" / "example_repo"
    repo.mkdir(parents=True)
    state.mkdir(parents=True)
    planning.mkdir(parents=True)
    write_json(root / "project.json", {"name": project, "topic": "Demo topic", "target_venue": "ICLR"})
    write_json(planning / "find_progress.json", {"run_id": "find_current", "phase": "complete", "strong_recommendation_count": 4, "recommendation_target_count": 4, "recommendation_shortfall": 0})
    write_json(planning / "find_results.json", {"run_id": "find_current", "strong_recommendations": [recommended_paper("p1", "Paper One")]})
    write_json(state / "current_find_research_plan.json", {"run_id": "find_current", "status": "claude_takeover_ready"})
    write_json(
        state / "evidence_ready_repo_selection.json",
        {
            "fresh_find_run_id": "find_current",
            "selection_stage": "environment_claude_code",
            "selection_gate": "accepted_by_claude_transformable_pending_loader_bootstrap",
            "evidence_ready_count": 0,
            "selected": {
                "name": "example/repo",
                "url": "https://example.test/repo",
                "repo_path": str(repo),
                "fresh_find_run_id": "find_current",
                "selection_stage": "environment_claude_code",
                "pending_loader_bootstrap": True,
                "probe_summary": {"claim_ready_datasets": []},
            },
        },
    )
    write_json(state / "fresh_base_implementation_plan.json", {"fresh_find_run_id": "find_current", "status": "blocked_fresh_base_data_required", "repo": {"name": "example/repo", "repo_path": str(repo)}, "ready_datasets": []})
    write_json(state / "full_research_cycle.json", {"status": "not_started"})
    monkeypatch.setattr(project_bridge, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda _project, _root: {"venue_ids": ["openreview_iclr_2026"], "years": [2026]})

    summary = project_bridge._fast_project_summary(project, root, {"name": project, "topic": "Demo topic", "target_venue": "ICLR"})

    assert summary["status"] == "blocked_fresh_base_data_required"
    assert "not_started" not in summary["summary"]
    assert "example/repo" in summary["summary"]
    assert summary["main_route"]["repo_name"] == "example/repo"
    assert summary["stages"]["environment"]["status"] == "selected"
    assert summary["stages"]["environment"]["data_status"] == "waiting_for_real_data_loader_evidence"

def test_loader_ready_evidence_advances_selected_base_to_reference_probe(monkeypatch, tmp_path):
    from auto_research.web import project_bridge

    project = "demo_project"
    root = tmp_path / project
    state = root / "state"
    finding = root / "planning" / "finding"
    repo = root / "repos" / "selected" / "example_repo"
    state.mkdir(parents=True)
    finding.mkdir(parents=True)
    repo.mkdir(parents=True)
    run_id = "find_current"
    dataset = "demo_ready_dataset"

    write_json(finding / "find_progress.json", {"run_id": run_id, "phase": "complete"})
    write_json(state / "current_find_research_plan.json", {"run_id": run_id, "status": "ready"})
    write_json(
        state / "evidence_ready_repo_selection.json",
        {
            "fresh_find_run_id": run_id,
            "selection_stage": "environment_claude_code",
            "selection_gate": "accepted_by_claude_transformable_pending_loader_bootstrap",
            "accepted_by_claude": True,
            "selected": {
                "name": "example/repo",
                "url": "https://example.test/repo",
                "repo_path": str(repo),
                "fresh_find_run_id": run_id,
                "probe_summary": {"claim_ready_datasets": []},
            },
        },
    )
    write_json(state / "active_repo.json", {"name": "example/repo", "repo_path": str(repo), "claim_ready_datasets": []})
    write_json(
        state / "fresh_base_implementation_plan.json",
        {
            "fresh_find_run_id": run_id,
            "status": "implementation_ready",
            "repo": {"name": "example/repo", "repo_path": str(repo)},
            "ready_datasets": [dataset],
            "blocked_datasets": [],
            "blocker_reasons": [],
        },
    )
    write_json(
        state / "real_dataset_probe.json",
        {
            "repo_path": str(repo),
            "status": "passed",
            "decision": "loader_probe_complete",
            "ready_datasets": [dataset],
            "blocked_datasets": [],
            "blocker_reasons": [],
            "probes": [{"dataset": dataset}],
        },
    )
    write_json(state / "reference_reproduction_gate.json", {"status": "blocked", "decision": "no_viable_base_switch_route", "human_summary": "stale no viable route"})
    write_json(state / "full_research_cycle.json", {"status": "blocked_no_viable_base_switch_route", "current_goal": "stale no viable route"})
    monkeypatch.setattr(project_bridge, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda _project, _root: {"venue_ids": ["openreview_iclr_2026"], "years": [2026]})

    summary = project_bridge._fast_project_summary(project, root, {"name": project, "topic": "Demo topic", "target_venue": "ICLR"})

    assert summary["status"] == "blocked_fresh_base_reference_probe_required"
    assert "真实数据/loader 已通过" in summary["summary"]
    assert "尚未通过" not in summary["summary"]
    assert summary["main_route"]["dataset"] == dataset
    assert summary["main_route"]["ready_datasets"] == [dataset]
    assert summary["stages"]["environment"]["data_status"] == "real_data_loader_ready"
    assert summary["stages"]["environment"]["reference_reproduction_gate"]["decision"] == "fresh_base_reference_probe_required"
    assert summary["stages"]["experiment"]["reference_reproduction_gate"]["decision"] == "fresh_base_reference_probe_required"
    assert summary["current_blocker"]["category"] == "fresh_base_reference_probe_required"
    assert "reference-protocol/import probe" in summary["current_blocker"]["next_action"]


def test_reference_protocol_import_probe_surfaces_dependency_blocker(monkeypatch, tmp_path):
    from auto_research.web import project_bridge

    project = "demo_project"
    root = tmp_path / project
    state = root / "state"
    finding = root / "planning" / "finding"
    repo = root / "repos" / "selected" / "example_repo"
    state.mkdir(parents=True)
    finding.mkdir(parents=True)
    repo.mkdir(parents=True)
    run_id = "find_current"
    dataset = "demo_ready_dataset"

    write_json(finding / "find_progress.json", {"run_id": run_id, "phase": "complete"})
    write_json(state / "current_find_research_plan.json", {"run_id": run_id, "status": "ready"})
    write_json(
        state / "evidence_ready_repo_selection.json",
        {
            "fresh_find_run_id": run_id,
            "selection_stage": "environment_claude_code",
            "selection_gate": "accepted_by_claude_transformable_pending_loader_bootstrap",
            "accepted_by_claude": True,
            "selected": {"name": "example/repo", "repo_path": str(repo), "fresh_find_run_id": run_id},
        },
    )
    write_json(state / "active_repo.json", {"name": "example/repo", "repo_path": str(repo)})
    write_json(state / "fresh_base_implementation_plan.json", {"fresh_find_run_id": run_id, "status": "implementation_ready", "repo": {"name": "example/repo", "repo_path": str(repo)}, "ready_datasets": [dataset], "blocked_datasets": [], "blocker_reasons": []})
    write_json(state / "real_dataset_probe.json", {"repo_path": str(repo), "status": "passed", "decision": "loader_probe_complete", "ready_datasets": [dataset], "blocked_datasets": [], "blocker_reasons": []})
    write_json(state / "full_research_cycle.json", {"status": "blocked_no_viable_base_switch_route"})
    write_json(state / "reference_protocol_import_probe.json", {"repo_path": str(repo), "verdict": "code_present_deps_missing", "results": {"direct_import": {"status": "failed", "blocker": "No module named 'json_repair'"}, "dependency_audit": {"missing": 32, "total_requirements": 46}}})
    monkeypatch.setattr(project_bridge, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda _project, _root: {"venue_ids": ["openreview_iclr_2026"], "years": [2026]})

    protocol = project_bridge._fresh_base_protocol_probe(root)
    summary = project_bridge._fast_project_summary(project, root, {"name": project, "topic": "Demo topic", "target_venue": "ICLR"})

    assert protocol["status"] == "reference_protocol_probe_blocked"
    assert protocol["decision"] == "dependency_install_required"
    assert "32/46" in summary["summary"]
    assert "json_repair" in summary["summary"]
    assert "等待参考协议/环境 manifest 探针" not in summary["summary"]
    assert summary["human_gate_summary"]["title"] == "参考协议依赖缺失"
    assert summary["human_gate_summary"]["source"] == "deterministic_gate_audit"
    assert summary["human_supervision"]["blocker"]["source"] == "deterministic_gate_audit"
    assert "32/46" in summary["current_blocker"]["summary"]
    assert "json_repair" in summary["current_blocker"]["summary"]
    assert "补齐缺失依赖" in summary["current_blocker"]["next_action"]
    env_gate = summary["stages"]["environment"]["reference_reproduction_gate"]
    experiment_gate = summary["stages"]["experiment"]["reference_reproduction_gate"]
    assert env_gate["decision"] == "dependency_install_required"
    assert "32/46" in env_gate["human_summary"]
    assert experiment_gate["decision"] == "dependency_install_required"
    assert "json_repair" in experiment_gate["human_summary"]

    top_level_probe = project_bridge._reference_protocol_import_probe_public_payload({
        "verdict": "code_present_deps_missing",
        "dependency_audit": {"missing": 39, "total": 46},
        "imports": {"gpt_researcher": {"status": "failed", "error": "No module named 'json_repair'"}},
    })
    assert top_level_probe["decision"] == "dependency_install_required"
    assert "39/46" in top_level_probe["human_summary"]
    assert "json_repair" in top_level_probe["human_summary"]


def test_public_job_payload_hides_paper_agent_repair_diagnostics_from_summaries_only():
    raw = {
        "stage": "paper",
        "logs": ["missing bib entries for cited keys=achiam2023gpt; keep this bounded log detail"],
        "progress": {
            "message": "预览仍需完善：missing bib entries for cited keys=achiam2023gpt, liang2023holistic；Claude Code 自审未通过，项目代理需独立读 PDF/TeX/BibTeX/log/venue contract 后修复并写 receipt。"
        },
        "result": {
            "paper_summary": "missing bib entries for cited keys=foo；self_review_hash_mismatch_pdf: sha256 mismatch for reviewed artifact: /tmp/paper.pdf"
        },
    }

    public = server._public_job_api_payload(raw)
    summary_rendered = json.dumps({"progress": public["progress"], "result": public["result"]}, ensure_ascii=False)
    log_rendered = json.dumps(public["logs"], ensure_ascii=False)

    assert "missing bib entries" not in summary_rendered
    assert "achiam2023gpt" not in summary_rendered
    assert "Claude Code 自审" not in summary_rendered
    assert "PDF/TeX/BibTeX/log" not in summary_rendered
    assert "self_review_hash_mismatch" not in summary_rendered
    assert "sha256 mismatch" not in summary_rendered
    assert "引用/参考文献仍需修复" in summary_rendered
    assert "论文自审未通过" in summary_rendered
    assert "missing bib entries" in log_rendered
    assert "achiam2023gpt" in log_rendered

    compact_logs = server._public_job_logs("paper", raw["logs"], raw["progress"], raw["result"], limit=20)
    rendered_compact_logs = json.dumps(compact_logs, ensure_ascii=False)
    assert "详细日志" in rendered_compact_logs
    assert "missing bib entries" in rendered_compact_logs
    assert "achiam2023gpt" in rendered_compact_logs

    already_public_logs = server._public_job_logs(
        "paper",
        ["当前状态：会议格式论文预览已生成", "详细日志：正文页数：9/9"],
        raw["progress"],
        raw["result"],
        limit=20,
    )
    assert "详细日志：详细日志" not in json.dumps(already_public_logs, ensure_ascii=False)



def test_taskbar_does_not_infer_claude_narrative_from_keywords():
    from auto_research.web import server

    line = "scientific_progress_gate still blocked; build_blocker_action_plan.py wrote deterministic gate output"
    summarized = server._summarize_claude_taskbline(line)

    assert summarized == line
    assert "Claude Code：" not in summarized



def test_select_fresh_base_marks_stale_implementation_plan(monkeypatch, tmp_path):
    ensure_script_paths()
    selector = importlib.import_module("select_fresh_research_base")
    paths = type("Paths", (), {"state": tmp_path / "state"})()
    paths.state.mkdir(parents=True)
    write_json(paths.state / "fresh_base_implementation_plan.json", {"fresh_find_run_id": "find_old", "status": "implementation_ready_for_reference_probe", "repo": {"repo_path": "/old"}})

    plan, status = selector.current_implementation_plan(paths, "find_new")

    assert plan == {}
    assert status == "stale_for_current_find"


def test_pipeline_guard_ignores_stale_active_repo_and_impl(monkeypatch, tmp_path):
    ensure_script_paths()
    guard = importlib.import_module("pipeline_guard")
    paths = type("Paths", (), {"state": tmp_path / "state", "planning": tmp_path / "planning"})()
    paths.state.mkdir(parents=True)
    (paths.planning / "finding").mkdir(parents=True)
    write_json(paths.planning / "finding" / "find_progress.json", {"run_id": "find_new"})
    write_json(paths.state / "evidence_ready_repo_selection.json", {"fresh_find_run_id": "find_new", "selection_stage": "environment_claude_code", "selection_gate": "continued_search_required_by_claude_topic_fit", "selected": {}})
    write_json(paths.state / "active_repo.json", {"fresh_find_run_id": "find_old", "selection_stage": "environment_claude_code", "selection_gate": "accepted_by_claude_topic_fit", "repo_path": "/old-active"})
    write_json(paths.state / "fresh_base_implementation_plan.json", {"fresh_find_run_id": "find_old", "status": "implementation_ready_for_reference_probe", "repo": {"repo_path": "/old-impl"}})

    assert guard._current_impl_repo_path(paths) == ""


def test_pipeline_guard_uses_selected_base_viability_current_route(monkeypatch, tmp_path):
    ensure_script_paths()
    guard = importlib.reload(importlib.import_module("pipeline_guard"))
    paths = type("Paths", (), {"state": tmp_path / "state", "planning": tmp_path / "planning"})()
    paths.state.mkdir(parents=True)
    (paths.planning / "finding").mkdir(parents=True)
    run_id = "find_current"
    reference_repo = "/tmp/demo/repos/selected/example_org_reference_rec"
    candidate_repo = "/tmp/demo/repos/candidates/candidaterec"
    title = "Reference Recommender Benchmark Paper"

    write_json(paths.planning / "finding" / "find_progress.json", {"run_id": run_id})
    write_json(
        paths.state / "evidence_ready_repo_selection.json",
        {
            "fresh_find_run_id": run_id,
            "selection_stage": "environment_claude_code",
            "selection_gate": "continued_search_required_by_claude_topic_fit",
            "selected": {},
        },
    )
    write_json(
        paths.state / "active_repo.json",
        {
            "fresh_find_run_id": "find_old",
            "selection_stage": "environment_claude_code",
            "selection_gate": "accepted_by_deterministic_base_switch_gate",
            "repo_path": candidate_repo,
        },
    )
    write_json(
        paths.state / "fresh_base_implementation_plan.json",
        {
            "fresh_find_run_id": run_id,
            "status": "blocked_environment_repo_selection_required",
            "repo": {},
        },
    )
    write_json(
        paths.state / "selected_base_viability_gate.json",
        {
            "status": "blocked",
            "decision": "base_switch_gate_required",
            "current_selected_repo": "example-org/ReferenceRec",
            "current_selected_repo_path": reference_repo,
            "selected_base_title": title,
        },
    )
    write_json(
        paths.state / "selected_base_route_guard.json",
        {
            "selected_base_find_run_id": run_id,
            "trusted_audit": {"repo_name": "example-org/ReferenceRec", "repo_path": reference_repo, "dataset": "ks"},
        },
    )
    write_json(
        paths.state / "fresh_base_reference_full_reproduction_audit.json",
        {
            "status": "completed_reference_reproduction",
            "repo_name": "example-org/ReferenceRec",
            "repo_path": reference_repo,
            "selected_base": {"repo_path": reference_repo, "literature_base_title": title},
        },
    )
    write_json(paths.state / "reference_reproduction_gate.json", {"status": "pass", "decision": "continue_base"})
    write_json(paths.state / "blocker_action_plan.json", {"status": "blocked", "summary": {}})
    monkeypatch.setattr(guard, "build_paths", lambda _project: paths)

    env = guard.current_environment_selection(paths)

    assert env["valid"] is True
    assert env["reason"] == "selected_base_viability_current_route"
    assert env["selected"]["repo_path"] == reference_repo
    assert guard._current_impl_repo_path(paths) == reference_repo
    assert guard.fresh_base_hard_gate_blocked("demo_project") is False


def test_full_cycle_literature_packet_refresh_is_environment_not_find(tmp_path):
    from auto_research.web import project_bridge
    from auto_research.web import server
    from scripts.run_full_research_cycle import FullCycle
    from scripts.run_supervision_tick import _public_phase_from_stage as tick_phase_from_stage

    environment_stages = [
        "literature-sync-existing",
        "literature-tool-packet-refresh",
        "fresh-research-base-selection-refresh",
        "literature-base-candidate-assessment",
        "method-stack-sync",
    ]
    for stage in environment_stages:
        assert FullCycle.public_phase_from_stage(stage) == "environment"
        assert tick_phase_from_stage(stage) == "environment"
        assert project_bridge._phase_from_stage(stage) == "environment"
        assert project_bridge._public_phase_for_full_cycle(stage, "demo_project", tmp_path) == "environment"
        assert server._phase_from_stage(stage) == "environment"
        assert server._public_taste_stage(stage) == "environment"


def test_true_fresh_literature_survey_still_maps_to_find(tmp_path):
    from auto_research.web import project_bridge
    from auto_research.web import server
    from scripts.run_full_research_cycle import FullCycle
    from scripts.run_supervision_tick import _public_phase_from_stage as tick_phase_from_stage

    fresh_find_stages = ["literature-survey", "run-finding", "run-driver"]
    for stage in fresh_find_stages:
        assert FullCycle.public_phase_from_stage(stage) == "find"
        assert tick_phase_from_stage(stage) == "find"
        assert project_bridge._phase_from_stage(stage) == "literature"
        assert project_bridge._public_phase_for_full_cycle(stage, "demo_project", tmp_path) == "find"
        assert server._phase_from_stage(stage) == "literature"
        assert server._public_taste_stage(stage) == "find"


def test_public_text_strips_legacy_recommendation_cards():
    legacy = """#1 Causal Direct Preference Optimization for Distributionally Robust Generative Recommendation
ICML / 2026 / 推荐 / Fit=9 / Score=9
URL
PDF

## 1. Causal Direct Preference Optimization for Distributionally Robust Generative Recommendation

### 摘要
正文摘要保留。
"""

    cleaned = server._public_text(legacy)

    assert "#1 Causal Direct Preference" not in cleaned
    assert "Fit=9" not in cleaned
    assert "Score=9" not in cleaned
    assert "\nURL\n" not in cleaned
    assert "\nPDF\n" not in cleaned
    assert "## 1. Causal Direct Preference Optimization" in cleaned
    assert "正文摘要保留" in cleaned


def test_public_text_keeps_workflow_stage_words():
    assert server._public_text("idea") == "idea"
    assert server._public_text("find") == "find"


def test_public_text_strips_article_artifact_pointer_lines():
    legacy = """## 推荐文章\n\n完整摘要和推荐理由见 article.md。\n\n### 摘要\n正文摘要保留。\n"""

    cleaned = server._public_text(legacy)

    assert "完整摘要和推荐理由见" not in cleaned
    assert "article.md" not in cleaned
    assert "### 摘要" in cleaned
    assert "正文摘要保留" in cleaned


def test_find_survey_panel_stays_between_source_config_and_task_artifacts():
    from pathlib import Path

    app_path = Path(__file__).resolve().parents[1] / "web" / "frontend" / "client" / "src" / "App.tsx"
    css_path = Path(__file__).resolve().parents[1] / "web" / "frontend" / "client" / "src" / "styles.css"
    app = app_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8")

    config_stage_idx = app.index('data-testid="find-config-stage"')
    grid_idx = app.index('data-testid="find-config-grid"')
    top_row_idx = app.index('data-testid="find-config-top-row"', grid_idx)
    venue_idx = app.index("findVenueConfigPanel", grid_idx)
    source_idx = app.index("findSourceConfigPanel", venue_idx)
    grid_close_idx = app.index('            </div>\n            <div className="findLayoutSentinel"', source_idx)
    source_complete_idx = app.index('data-testid="find-source-configs-complete"', grid_close_idx)
    survey_slot_idx = app.index('data-testid="find-survey-slot"', source_complete_idx)
    survey_call_idx = app.index("{renderFindLiteratureSurveyPanel()}", survey_slot_idx)
    config_close_idx = app.index('          </section>\n          </>', survey_call_idx)
    task_artifact_idx = app.index('data-testid="global-task-artifact"')

    assert config_stage_idx < grid_idx < top_row_idx < venue_idx < source_idx < grid_close_idx < source_complete_idx < survey_slot_idx <= survey_call_idx < config_close_idx < task_artifact_idx
    assert app.count('data-testid="find-survey-slot"') == 1
    assert app.count("{renderFindLiteratureSurveyPanel()}") == 1
    assert "findSurveyPlacement" not in app
    render_idx = app.index("function renderFindLiteratureSurveyPanel")
    survey_return_idx = app.index("return renderSurveyShell((", render_idx)
    source_heading_idx = app.index('data-testid="find-source-status-heading"', survey_return_idx)
    source_empty_idx = app.index('data-testid="find-source-status-empty"', survey_return_idx)
    flow_grid_idx = app.index("surveyFlowGrid compactSurveyFlow", survey_return_idx)
    artifact_loading_idx = app.index("正在加载 Find 验收状态", survey_return_idx)
    task_heading_idx = app.index('data-testid="global-task-heading"')
    artifact_heading_idx = app.index('data-testid="global-artifact-heading"')
    survey_block = app[survey_return_idx:task_heading_idx]
    assert source_heading_idx < source_empty_idx < flow_grid_idx < artifact_loading_idx < task_heading_idx < artifact_heading_idx
    assert "liveFindProgress" not in survey_block
    assert "data-testid=\"find-recommendation-list\"" not in survey_block
    assert "recommendationRows" not in survey_block
    assert "paperRecommendation findRecommendationItem" not in survey_block
    assert "paperLinkRow" not in survey_block
    assert "paperAbstractText" not in survey_block
    assert "paperEvidenceGrid" not in survey_block
    assert ">URL</a>" not in survey_block
    assert ">PDF</a>" not in survey_block
    assert "Fit=" not in survey_block
    assert "Score=" not in survey_block
    assert "function paperMetaText" not in app
    assert "function findRecommendationEvidenceText" not in app
    assert "完整摘要和推荐理由见" not in app
    assert "推荐文章列表在下方" not in app
    assert "正在加载推荐文章列表" not in app
    assert "activeProjectInfo?.literature_survey_preview" in app
    assert "正在加载来源状态" not in app
    assert "Loading source status" not in app
    assert "正在读取当前 Find 来源状态" not in app
    assert "Loading current Find source status" not in app
    assert "literature_survey_preview" in (Path(__file__).resolve().parents[1] / "web" / "frontend" / "client" / "src" / "api.ts").read_text(encoding="utf-8")
    assert "_light_find_survey_from_progress" in (Path(__file__).resolve().parents[1] / "web" / "backend" / "auto_research" / "web" / "project_bridge.py").read_text(encoding="utf-8")
    assert "当前 Find 产物已就绪" not in app
    assert "推荐论文已就绪" not in app
    assert "findSourceReviewSlot" not in app
    assert "findSurveyStage" not in app
    assert 'data-layout-order="after-find-config-source-before-task-artifact"' in app
    assert ".findConfigTopRow" in css
    assert "findSurveyAfterConfig" in app
    assert ".findSurveySlot" in css
    assert ".findSurveyConfigPanel" in css
    assert ".findSurveyAfterConfig" in css
    assert ".findLayoutSentinel" in css
    assert "grid-template-columns: minmax(360px, 1fr) minmax(360px, 1fr)" in css
    assert ".findSurveySlot .findSurveyPanel" in css

def test_frontend_plan_approved_idea_ids_use_schema_compatible_key():
    from pathlib import Path

    app_path = Path(__file__).resolve().parents[1] / "web" / "frontend" / "client" / "src" / "App.tsx"
    css_path = Path(__file__).resolve().parents[1] / "web" / "frontend" / "client" / "src" / "styles.css"
    app = app_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8")

    assert "approvedIdeas.map((idea: any, index: number) => ideaKey(idea, index))" in app
    assert '["deleted", "rejected", "reject", "archived"].includes(status)' in app
    assert "approvedIdeas.map((idea: any) => idea.id)" not in app
    assert ".ideaLargeTextarea" in css
    assert "max-height: 220px" in css
    assert "方法类型：" in app and "方法侧重：" in app
    assert "方法类型" in app and "机制类别" in app
    assert "| # | 论文 | 机制类别 | 主要优点 | 主要局限 |" in app



def test_frontend_plan_page_uses_compact_controls_and_plan_markdown_artifact():
    from pathlib import Path

    app_path = Path(__file__).resolve().parents[1] / "web" / "frontend" / "client" / "src" / "App.tsx"
    css_path = Path(__file__).resolve().parents[1] / "web" / "frontend" / "client" / "src" / "styles.css"
    app = app_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8")

    plan_idx = app.index('{tab === "plan" && (')
    env_idx = app.index('{tab === "environment" && (', plan_idx)
    block = app[plan_idx:env_idx]

    assert 'className="planControlGrid planTopGrid"' in block
    assert 'className="panel planControlPanel planIdeasPanel"' in block
    assert 'className="panel planControlPanel planSettingsPanel"' in block
    assert 'className="panel planControlPanel planCurrentPanel"' in block
    assert 'className="idea ideaEditorCard planCandidateCard"' in block
    assert 'compactPlanCard' not in block
    assert 'plans.map((plan: any, index: number)' in block
    assert 'planTitleText(plan, index)' in block
    assert 'planTitleBox' in block
    assert 'planReadOnlyBox' in block
    assert 'planOverviewBox' in block
    assert 'runPlanPolish(plan.plan_id' in block
    assert 'runPlanFinish(plan.plan_id)' in block
    assert 'preferredArtifactNameForTab' not in app
    assert 'return (preferredName ? renderedRunArtifacts.find' not in app
    assert '下方产物仅在需要审计时手动打开' not in app
    assert '选择一个产物查看' not in app
    assert 'Open artifacts manually only when auditing' not in app
    assert '完整摘要和推荐理由见' not in app
    assert 'legacyMetadataLine' in app
    assert 'legacyScoreLine' in app
    assert 'isLegacyRecommendationCardStart' in app
    assert 'stripLegacyRecommendationCards' in app
    assert '(?:Fit|Score)' in app
    assert 'stripLegacyArtifactPointerLines' in app
    assert 'renderedRunArtifactsSignature' in app
    assert '[tab, renderedRunArtifactsRunId, renderedRunArtifactsSignature]' in app
    assert 'setRunId(id);\n    setActiveArtifact("");\n    setRawArtifacts({});' in app
    assert 'setCurrentFindArtifacts([]);\n      setActiveArtifact("");\n      setRawArtifacts({});' in app
    assert 'if (currentFindArtifactsInFlightRef.current === id) return;\n    setActiveArtifact("");\n    setRawArtifacts({});' in app

    assert '.planControlGrid' in css
    assert '.planCandidateCard' in css
    assert '.planTitleBox' in css
    assert '.planReadOnlyBox' in css
    assert '.planOverviewBox' in css
    assert '.compactPlanCard' not in css


def test_web_current_find_pipeline_reports_pending_read_after_find_gate_passes(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    recommendations = [
        recommended_paper("paper-1", "Paper 1"),
        recommended_paper("paper-2", "Paper 2"),
    ]
    write_json(
        taste_dir / "find_results.json",
        {
            "run_id": "find_ready",
            "strong_recommendations": recommendations,
            "read_candidates": recommendations,
            "recommendation_shortfall": 0,
            "recommendation_target_count": 2,
            "recommendation_quality": {
                "status": "ok",
                "missing_real_abstract_count": 0,
                "missing_chinese_abstract_count": 0,
                "english_abstract_fallback_count": 0,
            },
        },
    )
    write_json(
        state_dir / "current_find_research_plan.json",
        {
            "run_id": "find_ready",
            "source": "web_find_adoption",
            "status": "pending_current_find_read",
            "next_required_action": "run_read_for_current_find",
            "current_find_reading_count": 0,
            "current_find_idea_count": 0,
            "current_find_plan_count": 0,
        },
    )
    monkeypatch.setattr(project_bridge, "_current_find_results_light", lambda root, project: read_json(root / "planning" / "finding" / "find_results.json", {}))

    summary = project_bridge._current_find_pipeline_summary(project_root)

    assert summary["status"] == "pending_current_find_read"
    assert summary["failure_type"] == ""
    assert summary["next_required_action"] == "run_read_for_current_find"
    assert summary["read_artifact_count"] == 0
    assert summary["takeover_ready"] is False
    assert not any("rerun_current_find_claude_takeover_repair" in item for item in summary["blockers"])


def test_human_find_recommendation_requires_final_llm_contract():
    from auto_research.web import project_bridge

    legacy = {
        "id": "legacy",
        "title": "Legacy Strong Tag",
        "abstract": "This paper has a long enough abstract for the legacy row, but it lacks final LLM scoring and explicit recommendation flags, so the web/API layer must not expose it as a user-visible recommendation.",
        "fit_score": 9.0,
        "evidence_tier": "strong_recommendation",
    }
    low_fit = recommended_paper("low-fit", "Low Fit Final Scored Paper")
    low_fit["fit_score"] = 6.0
    low_fit["llm_fit_score"] = 6.0

    assert project_bridge._human_find_recommendation_literature_row(legacy) is False
    assert project_bridge._human_find_recommendation_literature_row(low_fit) is True
    assert project_bridge._human_find_recommendation_literature_row(recommended_paper("ok", "Valid Final Scored Paper")) is True



def test_web_literature_gate_rejects_semantic_tldr_only_abstract():
    from auto_research.web import project_bridge

    tldr = "This long Semantic Scholar TLDR summary should remain auxiliary metadata, not a real abstract for recommendations."
    row = {
        **recommended_paper("paper-tldr", "TLDR Only Paper"),
        "abstract": tldr,
        "abstract_en": tldr,
        "abstract_zh": "",
        "metadata": {"abstract_source": "semantic_scholar_doi_tldr", "tldr": tldr},
    }

    assert project_bridge._human_find_recommendation_literature_row(row) is False


def test_new_find_guard_allows_audited_human_web_restart_for_shortfall(tmp_path, monkeypatch):
    project = "demo_project"
    root = tmp_path / "projects" / project
    state_dir = root / "state"
    state_dir.mkdir(parents=True)
    write_json(
        state_dir / "current_find_research_plan.json",
        {
            "run_id": "find_short",
            "status": "pending_current_find_read",
            "literature_gate": {
                "status": "recommendation_shortfall",
                "strong_recommendations": 13,
                "recommendation_target_count": 20,
                "recommendation_shortfall": 7,
            },
        },
    )
    monkeypatch.setattr(server, "_current_project_for_find_guard", lambda: (project, root))
    monkeypatch.setattr(server, "_live_full_cycle_for_project", lambda _project, _root: {})

    blocked = server._new_find_guard_blocker(FindRequest())
    assert blocked is not None
    assert blocked["status"] == "blocked_new_find_guard"
    assert blocked["recommendation_shortfall"] == 7

    allowed = server._new_find_guard_blocker(
        FindRequest(human_approved_new_find=True, approval_reason="user_explicit_find_run_from_web")
    )

    assert allowed is None
    receipt = read_json(state_dir / "latest_new_find_restart_approval.json", {})
    assert receipt["approved"] is True
    assert receipt["source"] == "api_jobs_find"
    assert receipt["reason"] == "user_explicit_find_run_from_web"


def test_projection_counts_use_filtered_recommendation_contract(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    state_dir = project_root / "state"
    taste_dir = project_root / "planning" / "finding"
    state_dir.mkdir(parents=True)
    taste_dir.mkdir(parents=True)
    legacy_rows = [
        {
            "id": f"legacy-{idx}",
            "title": f"Legacy Paper {idx}",
            "abstract": "This old row has abstract text but no final LLM recommendation flags, so it must stay out of the user-visible recommendation pool.",
            "fit_score": 9.0,
            "evidence_tier": "strong_recommendation",
        }
        for idx in range(19)
    ]
    write_json(taste_dir / "find_progress.json", {"run_id": "find_projection", "phase": "complete", "counts": {}})
    write_json(
        state_dir / "current_find_recommendation_projection.json",
        {
            "run_id": "find_projection",
            "recommendation_target_count": 20,
            "strict_strong_anchor_count": 20,
            "counts": {"recommended": 20, "strong_recommendations": 20},
            "strong_recommendations": [recommended_paper("paper-ok", "Valid Projection Paper"), *legacy_rows],
            "read_candidates": [recommended_paper("paper-ok", "Valid Projection Paper"), *legacy_rows],
        },
    )
    monkeypatch.setattr(project_bridge, "PROJECTS", tmp_path)
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda _project, _root: {"venue_ids": [], "years": []})

    find_light = project_bridge._current_find_results_light(project_root, "demo_project")
    assert len(find_light["strong_recommendations"]) == 1
    assert find_light["counts"]["strong_recommendations"] == 1
    assert find_light["recommendation_shortfall"] == 19

    light_survey = project_bridge._light_find_survey_from_progress(project_root)
    assert light_survey["counts"]["strong_recommendations"] == 1
    assert light_survey["counts"]["recommendation_shortfall"] == 19
    assert light_survey["status"] == "recommendation_shortfall"


def test_light_find_survey_preview_preserves_current_find_screening_counts(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    write_json(
        taste_dir / "find_progress.json",
        {
            "run_id": "find_current",
            "phase": "complete",
            "counts": {
                "raw_title_index": 17332,
                "venue_title_filter_input_papers": 11357,
                "venue_category_selected_papers": 4662,
                "title_candidates": 3856,
                "venue_final_title_candidates": 3856,
                "detail_fetched": 2656,
                "venue_detail_fetched_candidates": 2656,
                "evaluated_candidates": 2656,
                "llm_scored_candidates": 2649,
                "abstract_fetch_failed_candidates": 7,
                "final_llm_scoring_skipped_candidates": 7,
            },
            "strong_recommendation_count": 20,
            "recommendation_target_count": 20,
            "recommendation_shortfall": 0,
        },
    )
    write_json(
        state_dir / "current_find_recommendation_projection.json",
        {
            "run_id": "find_current",
            "recommendation_target_count": 20,
            "recommendation_shortfall": 0,
            "counts": {"recommended": 20, "strong_recommendations": 20, "llm_scored_candidates": 2649},
            "survey_stats": {
                "category_filtered_papers": 11304,
                "tfidf_screened_papers": 10236,
                "venue_title_filter_input_papers": 10236,
                "title_score_input_papers": 10236,
                "llm_title_scored_papers": 6247,
                "abstract_scored_papers": 2649,
                "llm_scored_candidates": 2649,
            },
        },
    )
    monkeypatch.setattr(project_bridge, "PROJECTS", tmp_path)
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda _project, _root: {"venue_ids": [], "years": []})

    preview = project_bridge._light_find_survey_from_progress(project_root)
    counts = preview["counts"]
    assert preview["run_id"] == "find_current"
    assert preview["status"] == "current_find_packet_ready"
    assert counts["raw_title_index_papers"] == 17332
    assert counts["venue_title_filter_input_papers"] == 10236
    assert counts["venue_category_selected_papers"] == 4662
    assert counts["category_filtered_papers"] == 11304
    assert counts["tfidf_screened_papers"] == 10236
    assert counts["title_score_input_papers"] == 10236
    assert counts["llm_title_scored_papers"] == 6247
    assert counts["title_candidates"] == 3856
    assert counts["venue_final_title_candidates"] == 3856
    assert counts["detail_fetched"] == 2656
    assert counts["venue_detail_fetched_candidates"] == 2656
    assert counts["evaluated_candidates"] == 2656
    assert counts["abstract_scored_papers"] == 2649
    assert counts["llm_scored_candidates"] == 2649
    assert counts["abstract_fetch_failed_candidates"] == 7
    assert counts["final_llm_scoring_skipped_candidates"] == 7
    assert counts["recommended"] == 20
    assert counts["read_candidates"] == 20


def test_current_find_summary_ignores_stale_unversioned_frontend_cache(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / 'demo_project'
    state_dir = project_root / 'state'
    taste_dir = project_root / 'planning' / 'finding'
    state_dir.mkdir(parents=True)
    taste_dir.mkdir(parents=True)
    write_json(
        taste_dir / 'find_progress.json',
        {
            'run_id': 'find_current',
            'phase': 'complete',
            'counts': {
                'raw_title_index': 17332,
                'category_filtered_papers': 11304,
                'tfidf_screened_papers': 11304,
                'title_score_input_papers': 10236,
                'title_candidates': 3856,
                'venue_final_title_candidates': 3856,
                'detail_fetched': 2656,
                'venue_detail_fetched_candidates': 2656,
                'evaluated_candidates': 2656,
                'llm_scored_candidates': 2649,
                'abstract_fetch_failed_candidates': 7,
                'final_llm_scoring_skipped_candidates': 7,
            },
            'strong_recommendation_count': 20,
            'recommendation_target_count': 20,
            'recommendation_shortfall': 0,
        },
    )
    write_json(
        state_dir / 'current_find_research_plan.json',
        {
            'run_id': 'find_current',
            'status': 'pending_current_find_read',
            'current_find_reading_count': 0,
            'current_find_idea_count': 0,
            'current_find_plan_count': 0,
        },
    )
    write_json(
        state_dir / 'finding_frontend.json',
        {
            'status': 'old_unversioned_cache',
            'survey_stats': {
                'llm_scored_candidates': 275,
                'venue_final_title_candidates': 299,
                'venue_detail_fetched_candidates': 299,
            },
        },
    )
    write_json(
        state_dir / 'taste_literature_intermediates.json',
        {
            'survey_stats': {
                'llm_scored_candidates': 240,
                'venue_final_title_candidates': 1976,
            },
        },
    )
    write_json(
        state_dir / 'literature_tool_packet.json',
        {
            'run_id': 'find_old',
            'source_run_id': 'find_old',
            'summary': {'strong_paper_anchors': 20, 'recommendation_target_count': 20},
            'coverage': {'llm_scored_candidates': 960, 'venue_final_title_candidates': 1976},
        },
    )
    write_json(
        state_dir / 'supervision_tick.json',
        {
            'literature': {
                'run_id': 'find_old',
                'evaluated_candidates': 960,
                'llm_scored_candidates': 960,
                'articles': 20,
            }
        },
    )
    monkeypatch.setattr(project_bridge, 'PROJECTS', tmp_path)
    monkeypatch.setattr(project_bridge, '_current_project_source_selection', lambda _project, _root: {'venue_ids': [], 'years': []})
    monkeypatch.setattr(project_bridge, '_remote_process_rows', lambda: [])
    monkeypatch.setattr(project_bridge, '_pid_alive', lambda _pid: False)

    light = project_bridge._current_find_results_light(project_root, 'demo_project')
    assert light['counts']['llm_scored_candidates'] == 2649
    assert light['counts']['title_candidates'] == 3856
    assert light['counts']['venue_final_title_candidates'] == 3856
    assert light['counts']['venue_detail_fetched_candidates'] == 2656
    assert light['counts']['abstract_fetch_failed_candidates'] == 7
    assert light['counts']['final_llm_scoring_skipped_candidates'] == 7

    summary = project_bridge._fast_project_summary(
        'demo_project',
        project_root,
        {'name': 'demo_project', 'topic': 'Demo', 'target_venue': 'ICLR', 'venue': 'ICLR'},
    )
    assert summary['target_venue'] == 'ICLR'
    assert summary['venue'] == 'ICLR'
    counts = summary['stages']['find']['counts']
    assert counts['llm_scored_candidates'] == 2649
    assert counts['evaluated_candidates'] == 2656
    assert counts['title_candidates'] == 3856
    assert counts['venue_final_title_candidates'] == 3856
    assert counts['detail_fetched'] == 2656
    assert counts['venue_detail_fetched_candidates'] == 2656
    assert counts['abstract_fetch_failed_candidates'] == 7
    assert counts['final_llm_scoring_skipped_candidates'] == 7
    survey_counts = summary['literature_survey']['counts']
    assert survey_counts['venue_final_title_candidates'] == 3856
    assert survey_counts['venue_detail_fetched_candidates'] == 2656
    assert survey_counts['abstract_fetch_failed_candidates'] == 7
    assert survey_counts['final_llm_scoring_skipped_candidates'] == 7



def test_light_artifacts_include_compact_find_results_for_completed_find_counts():
    assert "find_results.json" in server.LIGHT_ARTIFACT_JSON_NAMES


def test_compact_large_find_results_artifact_promotes_progress_counts_to_survey_stats(tmp_path, monkeypatch):
    run_id = "find_current"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    write_json(
        run_dir / "find_progress.json",
        {
            "run_id": run_id,
            "phase": "complete",
            "counts": {
                "raw_title_index": 17329,
                "raw_title_index_papers": 17329,
                "category_filtered_papers": 11611,
                "tfidf_screened_papers": 10543,
                "venue_title_filter_input_papers": 10543,
                "title_score_input_papers": 10543,
                "llm_title_scored_papers": 6502,
                "title_candidates": 4756,
                "venue_final_title_candidates": 4756,
                "detail_fetched": 640,
                "venue_detail_fetched_candidates": 640,
                "evaluated_candidates": 640,
                "abstract_scored_papers": 587,
                "llm_scored_candidates": 587,
                "abstract_fetch_failed_candidates": 53,
                "final_llm_scoring_skipped_candidates": 53,
            },
            "strong_recommendation_count": 20,
            "recommendation_target_count": 20,
            "recommendation_shortfall": 0,
        },
    )
    monkeypatch.setattr(server, "_project_root_for_find_run", lambda _run_id: None)

    payload = server._compact_large_find_results_artifact(run_dir, run_id, 200_000_000)

    assert payload["survey_stats"]["raw_title_index_papers"] == 17329
    assert payload["survey_stats"]["tfidf_screened_papers"] == 10543
    assert payload["survey_stats"]["title_score_input_papers"] == 10543
    assert payload["survey_stats"]["llm_title_scored_papers"] == 6502
    assert payload["counts"]["tfidf_screened_papers"] == 10543
    assert payload["counts"]["llm_title_scored_papers"] == 6502

def test_compact_large_find_results_artifact_prefers_current_projection_survey_stats(tmp_path, monkeypatch):
    from auto_research.web import server

    run_id = 'find_current'
    run_dir = tmp_path / 'runs' / run_id
    project_root = tmp_path / 'demo_project'
    state_dir = project_root / 'state'
    run_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    write_json(
        run_dir / 'find_progress.json',
        {
            'run_id': run_id,
            'counts': {
                'category_filtered_papers': 11304,
                'tfidf_screened_papers': 11304,
                'title_score_input_papers': 10236,
                'llm_scored_candidates': 577,
            },
        },
    )
    write_json(
        state_dir / 'finding_frontend.json',
        {
            'status': 'old_unversioned_cache',
            'survey_stats': {'tfidf_screened_papers': 999, 'llm_scored_candidates': 275},
        },
    )
    write_json(
        state_dir / 'current_find_recommendation_projection.json',
        {
            'run_id': run_id,
            'source_run_id': run_id,
            'counts': {'strong_recommendations': 20, 'recommended': 20},
            'survey_stats': {
                'category_filtered_papers': 11304,
                'tfidf_screened_papers': 10236,
                'venue_title_filter_input_papers': 10236,
                'title_score_input_papers': 10236,
                'llm_title_scored_papers': 6247,
                'abstract_scored_papers': 580,
                'llm_scored_candidates': 580,
                'recommended_papers': 20,
            },
        },
    )
    monkeypatch.setattr(server, '_project_root_for_find_run', lambda _run_id: project_root)

    payload = server._compact_large_find_results_artifact(run_dir, run_id, 200_000_000)

    assert payload['survey_stats']['tfidf_screened_papers'] == 10236
    assert payload['counts']['category_filtered_papers'] == 11304
    assert payload['counts']['tfidf_screened_papers'] == 10236
    assert payload['counts']['title_score_input_papers'] == 10236
    assert payload['counts']['llm_title_scored_papers'] == 6247
    assert payload['counts']['abstract_scored_papers'] == 580
    assert payload['counts']['llm_scored_candidates'] == 580
    assert payload['counts']['strong_recommendations'] == 20


def test_web_current_find_pipeline_uses_state_contract_when_artifacts_are_stale(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    write_json(taste_dir / "find_results.json", {"run_id": "find_current", "strong_recommendations": []})
    write_json(taste_dir / "read_results.json", {"run_id": "find_stale", "source": "claude_code_current_find_takeover", "readings": [{}]})
    write_json(taste_dir / "ideas.json", {"run_id": "find_stale", "source": "claude_code_current_find_takeover", "ideas": [{}]})
    write_json(taste_dir / "plans.json", {"run_id": "find_stale", "source": "claude_code_current_find_takeover", "plans": [{}]})
    write_json(
        state_dir / "current_find_research_plan.json",
        {
            "run_id": "find_current",
            "source": "claude_code_current_find_takeover",
            "selected_plan_id": "plan-selected",
            "selected_idea_id": "idea-selected",
            "readings": [{"title": "Paper A"}, {"title": "Paper B"}],
            "ideas": [{"id": f"idea-{idx}", "title": f"Idea {idx}"} for idx in range(5)],
            "plans": [
                {
                    "plan_id": "plan-selected" if idx == 0 else f"plan-{idx}",
                    "idea_id": "idea-selected" if idx == 0 else f"idea-{idx}",
                    "title": f"Plan {idx}",
                    "selected_for_execution": idx == 0,
                    "execute_next": idx == 0,
                }
                for idx in range(5)
            ],
            "reading_validation": {
                "valid": True,
                "actual_reading_count": 2,
                "expected_recommendation_count": 2,
                "full_text_reading_count": 2,
                "full_text_evidence_count": 2,
                "pending_full_text_reading_count": 0,
                "pending_without_evidence_count": 0,
                "pending_deep_read_synthesis_count": 0,
                "blockers": [],
            },
        },
    )
    monkeypatch.setattr(project_bridge, "_current_find_results_light", lambda root, project: read_json(root / "planning" / "finding" / "find_results.json", {}))

    summary = project_bridge._current_find_pipeline_summary(project_root)

    assert summary["status"] == "claude_takeover_ready"
    assert summary["takeover_ready"] is True
    assert summary["read_artifact_count"] == 2
    assert summary["full_text_reading_count"] == 2
    assert summary["ideas"] == 5
    assert summary["plans"] == 5
    assert summary["selected_plan_id"] == "plan-selected"
    assert summary["selected_idea_id"] == "idea-selected"


def test_current_find_pipeline_accepts_web_generated_idea_plan_as_current_run_content(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    run_id = "find_web"
    write_json(taste_dir / "find_results.json", {"run_id": run_id, "strong_recommendations": [recommended_paper("p1", "Paper One"), recommended_paper("p2", "Paper Two")]})
    write_json(taste_dir / "read_results.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "readings": [{"title": "Paper One"}, {"title": "Paper Two"}]})
    write_json(
        taste_dir / "ideas.json",
        {
            "run_id": run_id,
            "source": "taste_auto_idea",
            "ideas": [
                {"id": "idea-001", "title": "Approved idea", "status": "approved"},
                {"id": "idea-002", "title": "Backlog idea", "status": "pending"},
            ],
        },
    )
    write_json(
        taste_dir / "plans.json",
        {
            "run_id": run_id,
            "source": "taste_auto_plan",
            "plans": [{"plan_id": "plan-idea-001", "idea_id": "idea-001", "title": "Approved idea plan"}],
            "selection_issue": "missing_selected_plan",
        },
    )
    write_json(
        state_dir / "current_find_research_plan.json",
        {
            "run_id": run_id,
            "reading_validation": {
                "valid": True,
                "actual_reading_count": 2,
                "expected_recommendation_count": 2,
                "full_text_reading_count": 2,
                "full_text_evidence_count": 2,
                "pending_full_text_reading_count": 0,
                "pending_without_evidence_count": 0,
                "pending_deep_read_synthesis_count": 0,
                "blockers": [],
            },
        },
    )
    monkeypatch.setattr(project_bridge, "_current_find_results_light", lambda root, project: read_json(root / "planning" / "finding" / "find_results.json", {}))

    summary = project_bridge._current_find_pipeline_summary(project_root)

    assert summary["content_ready"] is True
    assert summary["status"] == "blocked_missing_selected_plan"
    assert summary["failure_type"] == "missing_selected_plan"
    assert summary["ideas"] == 2
    assert summary["plans"] == 1
    assert not any("below 5" in item for item in summary["blockers"])
    assert not any("source=" in item for item in summary["blockers"])


def test_finish_plan_marks_completed_plan_as_selected_execution(monkeypatch, tmp_path):
    from auto_research.auto_plan import pipeline as plan_pipeline

    run_id = "find_finish"
    run_root = tmp_path / run_id
    run_root.mkdir()
    write_json(
        run_root / "ideas.json",
        {"run_id": run_id, "ideas": [{"id": "idea-001", "title": "Idea", "status": "approved"}]},
    )
    write_json(
        run_root / "plans.json",
        {
            "run_id": run_id,
            "plans": [
                {"plan_id": "plan-1", "idea_id": "idea-001", "title": "Plan 1"},
                {"plan_id": "plan-2", "idea_id": "idea-001", "title": "Plan 2"},
            ],
        },
    )
    monkeypatch.setattr(plan_pipeline, "run_dir", lambda _run_id: run_root)
    monkeypatch.setattr(plan_pipeline, "sync_latest", lambda *args, **kwargs: None)
    monkeypatch.setattr(plan_pipeline, "update_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(plan_pipeline, "_sync_project_plans", lambda *args, **kwargs: None)

    result = plan_pipeline.finish_plan(run_id, "plan-1")

    by_id = {plan["plan_id"]: plan for plan in result["plans"]}
    assert by_id["plan-1"]["completed"] is True
    assert by_id["plan-1"]["selected_for_execution"] is True
    assert by_id["plan-1"]["execute_next"] is True
    assert by_id["plan-2"]["selected_for_execution"] is False
    assert result["selected_plan_id"] == "plan-1"
    assert result["selected_idea_id"] == "idea-001"
    assert result["selection_issue"] == ""


def test_environment_selection_requires_current_selected_plan_id(tmp_path):
    from auto_research.web import project_bridge

    root = tmp_path / "demo_project"
    finding = root / "planning" / "finding"
    state = root / "state"
    repo = root / "repos" / "selected" / "demo_repo"
    finding.mkdir(parents=True)
    state.mkdir(parents=True)
    repo.mkdir(parents=True)
    run_id = "find_env_plan_contract"
    write_json(finding / "find_progress.json", {"run_id": run_id})
    write_json(finding / "ideas.json", {"run_id": run_id, "ideas": [{"id": "idea-current", "title": "Current Idea", "status": "approved"}]})
    write_json(
        finding / "plans.json",
        {
            "run_id": run_id,
            "selected_plan_id": "plan-current",
            "selected_idea_id": "idea-current",
            "plans": [{"plan_id": "plan-current", "idea_id": "idea-current", "title": "Current Plan", "selected_for_execution": True, "execute_next": True}],
        },
    )
    selected_base = {
        "name": "Demo Repo",
        "title": "Selected Base Paper",
        "repo_path": str(repo),
        "local_path": str(repo),
        "fresh_find_run_id": run_id,
        "selection_stage": "environment_claude_code",
    }
    write_json(
        state / "evidence_ready_repo_selection.json",
        {
            "fresh_find_run_id": run_id,
            "selection_stage": "environment_claude_code",
            "selection_gate": "accepted_by_claude_topic_fit",
            "accepted_by_claude": True,
            "selected": dict(selected_base),
        },
    )

    stale = project_bridge._current_environment_selection(root)

    assert stale["valid"] is False
    assert stale["reason"] == "environment_selection_selected_plan_missing_or_stale"
    assert stale["current_selected_plan_id"] == "plan-current"

    selected_base["selected_plan_id"] = "plan-current"
    write_json(
        state / "evidence_ready_repo_selection.json",
        {
            "fresh_find_run_id": run_id,
            "selected_plan_id": "plan-current",
            "selected_idea_id": "idea-current",
            "selection_stage": "environment_claude_code",
            "selection_gate": "accepted_by_claude_topic_fit",
            "accepted_by_claude": True,
            "selected": selected_base,
        },
    )

    current = project_bridge._current_environment_selection(root)

    assert current["valid"] is True
    assert current["selected_plan_id"] == "plan-current"
    assert current["selected_idea_id"] == "idea-current"
    assert current["current_selected_plan_id"] == "plan-current"
    assert current["current_selected_idea_id"] == "idea-current"


def test_current_environment_pending_selection_prefers_current_run_over_stale_viability(tmp_path):
    from auto_research.web import project_bridge

    root = tmp_path / "demo_project"
    finding = root / "planning" / "finding"
    state = root / "state"
    finding.mkdir(parents=True)
    state.mkdir(parents=True)
    run_id = "find_current_env_blocker"
    write_json(finding / "find_progress.json", {"run_id": run_id})
    write_json(
        finding / "plans.json",
        {
            "run_id": run_id,
            "selected_plan_id": "plan-current",
            "selected_idea_id": "idea-current",
            "plans": [{"plan_id": "plan-current", "idea_id": "idea-current", "selected_for_execution": True, "execute_next": True}],
        },
    )
    write_json(
        state / "selected_base_viability_gate.json",
        {
            "status": "blocked",
            "decision": "base_switch_gate_required",
            "fresh_find_run_id": "find_old",
            "current_selected_repo": "old/repo",
            "current_selected_repo_path": "/old/repo",
            "selected_base_title": "Old Base",
        },
    )
    write_json(
        state / "evidence_ready_repo_selection.json",
        {
            "fresh_find_run_id": run_id,
            "selected_plan_id": "plan-current",
            "selected_idea_id": "idea-current",
            "selection_stage": "environment_claude_code",
            "selection_gate": "continued_search_required_by_claude_topic_fit",
            "selected": {},
        },
    )

    pending = project_bridge._current_environment_selection(root)

    assert pending["valid"] is False
    assert pending["fresh_find_run_id"] == run_id
    assert pending["selected_plan_id"] == "plan-current"
    assert pending["selected_idea_id"] == "idea-current"
    assert pending["current_selected_plan_id"] == "plan-current"
    assert pending["current_selected_idea_id"] == "idea-current"
    assert pending["reason"] == "environment_repo_selection_blocked_current_run"


def test_current_find_pipeline_counts_unread_recommendations_as_pending_full_text(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    recommendations = [recommended_paper("paper-1", "Unread Paper One"), recommended_paper("paper-2", "Unread Paper Two")]
    write_json(
        taste_dir / "find_results.json",
        {
            "run_id": "find_unread",
            "strong_recommendations": recommendations,
            "articles": recommendations,
            "recommendation_target_count": 2,
            "recommendation_shortfall": 0,
            "recommendation_quality": {"status": "ok", "missing_real_abstract_count": 0, "missing_chinese_abstract_count": 0, "english_abstract_fallback_count": 0},
        },
    )
    write_json(taste_dir / "read_results.json", {"run_id": "find_unread", "source": "pending_new_find_read", "readings": []})
    write_json(taste_dir / "ideas.json", {"run_id": "find_unread", "source": "pending_new_find_idea", "ideas": []})
    write_json(taste_dir / "plans.json", {"run_id": "find_unread", "source": "pending_new_find_plan", "plans": []})
    write_json(
        state_dir / "current_find_research_plan.json",
        {
            "run_id": "find_unread",
            "status": "pending_current_find_read",
            "next_required_action": "run_read_for_current_find",
            "literature_gate": {"status": "pass", "strong_recommendations": 2, "recommendation_target_count": 2, "recommendation_shortfall": 0},
        },
    )
    monkeypatch.setattr(project_bridge, "_current_find_results_light", lambda root, project: read_json(root / "planning" / "finding" / "find_results.json", {}))

    summary = project_bridge._current_find_pipeline_summary(project_root)

    assert summary["status"] == "pending_current_find_read"
    assert summary["next_required_action"] == "run_read_for_current_find"
    assert summary["pending_full_text_reading_count"] == 2
    assert summary["pending_without_evidence_count"] == 2
    assert "等待 Read 精读" in summary["summary_zh"]




def test_current_find_pipeline_does_not_let_stored_valid_state_override_current_deep_read_gaps(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    packet_dir = taste_dir / "full_text_reading"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    packet_dir.mkdir(parents=True)
    run_id = "find_stale_valid_state"
    recommendation = recommended_paper("paper-1", "Metric Style Paper")
    write_json(
        taste_dir / "find_results.json",
        {
            "run_id": run_id,
            "strong_recommendations": [recommendation],
            "articles": [recommendation],
            "recommendation_target_count": 1,
            "recommendation_shortfall": 0,
            "recommendation_quality": {"status": "ok", "missing_real_abstract_count": 0, "missing_chinese_abstract_count": 0, "english_abstract_fallback_count": 0},
        },
    )
    write_json(
        packet_dir / "full_text_packet.json",
        {"run_id": run_id, "papers": [{"paper_id": "paper-1", "title": "Metric Style Paper", "text_path": "texts/paper-1.txt", "text_chars": 30000}]},
    )
    write_json(
        taste_dir / "read_results.json",
        {
            "run_id": run_id,
            "source": "claude_code_current_find_takeover",
            "readings": [
                {
                    "paper_id": "paper-1",
                    "title": "Metric Style Paper",
                    "full_text_available": True,
                    "full_text_status": "pdf_text_read",
                    "pdf_text_chars": 30000,
                    "source_evidence": {"text_chars": 30000, "text_path": "texts/paper-1.txt"},
                    "abstract_zh": "本文研究推荐模型的排序协议和偏好建模方法，摘要说明问题背景、模型机制、实验协议和主要发现。" * 4,
                    "motivation_zh": "论文动机是解决推荐系统中语义偏好与协同信号不一致的问题，并指出已有方法缺少稳定评测协议。" * 5,
                    "method_details_zh": "方法包含用户编码器、物品编码器、训练目标、推理流程和排序头，并描述输入输出如何连接。实验中学习率为一乘以十的负五次方，批次大小二十四。" * 8,
                    "experiments_zh": "实验覆盖数据集、基线、指标和消融，报告NDCG@10达到零点零四七二，相对提升百分之七十二点二六，并进行了p值小于零点零五的显著性检验。" * 6,
                    "limitations_zh": "局限性包括跨域迁移、长尾用户、在线延迟和负采样协议差异，需要在统一仓库和统一指标下继续复核。" * 5,
                    "method_advantages_zh": ["优点是模型结构、训练目标和推理流程边界清晰，可以直接拆分消融并审计每个模块的贡献。", "优点是实验协议覆盖数据、基线、指标和显著性分析，便于后续复现实验对齐。"],
                    "method_disadvantages_zh": ["不足是训练和推理成本较高，在线服务需要额外评估延迟、显存和吞吐量。", "不足是收益依赖数据划分、负采样和候选集大小，跨数据集结论不能直接外推。"],
                }
            ],
        },
    )
    write_json(taste_dir / "ideas.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "selected_idea_id": "idea-1", "ideas": [{"id": "idea-1", "status": "approved"}]})
    write_json(taste_dir / "plans.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "selected_plan_id": "plan-1", "selected_idea_id": "idea-1", "plans": [{"plan_id": "plan-1", "idea_id": "idea-1", "selected_for_execution": True}]})
    write_json(state_dir / "current_find_research_plan.json", {"run_id": run_id, "read_idea_plan_ready": True, "selected_plan_id": "plan-1", "selected_idea_id": "idea-1"})
    write_json(state_dir / "current_find_claude_reading_validation.json", {"run_id": run_id, "valid": True, "expected_recommendation_count": 1, "actual_reading_count": 1, "full_text_reading_count": 1, "full_text_evidence_count": 1, "pending_deep_read_synthesis_count": 0, "pending_full_text_reading_count": 0, "blockers": []})
    monkeypatch.setattr(project_bridge, "_current_find_results_light", lambda root, project: read_json(root / "planning" / "finding" / "find_results.json", {}))

    summary = project_bridge._current_find_pipeline_summary(project_root)

    assert summary["status"] == "blocked_current_find_deep_read_validation_pending"
    assert summary["content_ready"] is False
    assert summary["full_text_evidence_count"] == 1
    assert summary["full_text_reading_count"] == 0
    assert summary["pending_deep_read_synthesis_count"] == 1
    assert summary["pending_full_text_reading_count"] == 1
    assert any("科学数字" in gap for gap in summary["deep_read_content_gap_details"][0]["missing_or_invalid_fields"])

def test_current_find_pipeline_uses_standalone_preflight_validation_counts(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    recommendations = [
        recommended_paper("paper-1", "Evidence Ready Paper One"),
        recommended_paper("paper-2", "Evidence Ready Paper Two"),
        recommended_paper("paper-3", "Missing Evidence Paper One"),
        recommended_paper("paper-4", "Missing Evidence Paper Two"),
    ]
    write_json(
        taste_dir / "find_results.json",
        {
            "run_id": "find_partial_evidence",
            "strong_recommendations": recommendations,
            "articles": recommendations,
            "recommendation_target_count": 4,
            "recommendation_shortfall": 0,
            "recommendation_quality": {"status": "ok", "missing_real_abstract_count": 0, "missing_chinese_abstract_count": 0, "english_abstract_fallback_count": 0},
        },
    )
    write_json(taste_dir / "read_results.json", {"run_id": "find_partial_evidence", "source": "pending_new_find_read", "readings": []})
    write_json(taste_dir / "ideas.json", {"run_id": "find_partial_evidence", "source": "pending_new_find_idea", "ideas": []})
    write_json(taste_dir / "plans.json", {"run_id": "find_partial_evidence", "source": "pending_new_find_plan", "plans": []})
    write_json(state_dir / "current_find_research_plan.json", {"run_id": "find_partial_evidence", "status": "blocked_current_find_full_text_evidence_pending"})
    write_json(
        state_dir / "current_find_claude_reading_validation.json",
        {
            "run_id": "find_partial_evidence",
            "status": "blocked_current_find_full_text_evidence_pending",
            "preflight": "before_current_find_claude_takeover",
            "valid": False,
            "actual_reading_count": 0,
            "expected_recommendation_count": 4,
            "full_text_evidence_count": 2,
            "pending_without_evidence_count": 2,
            "pending_full_text_reading_count": 2,
            "full_text_reading_count": 0,
            "pending_full_text_reading_titles": ["Missing Evidence Paper One", "Missing Evidence Paper Two"],
        },
    )
    monkeypatch.setattr(project_bridge, "_current_find_results_light", lambda root, project: read_json(root / "planning" / "finding" / "find_results.json", {}))

    summary = project_bridge._current_find_pipeline_summary(project_root)

    assert summary["status"] == "blocked_current_find_full_text_evidence_pending"
    assert summary["full_text_evidence_count"] == 2
    assert summary["pending_without_evidence_count"] == 2
    assert summary["pending_full_text_reading_count"] == 2
    assert summary["pending_full_text_reading_titles"] == ["Missing Evidence Paper One", "Missing Evidence Paper Two"]

def test_current_find_pipeline_ignores_stale_read_artifacts_when_using_preflight_validation(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    recommendations = [recommended_paper(f"paper-{idx}", f"Paper {idx}") for idx in range(1, 5)]
    write_json(
        taste_dir / "find_results.json",
        {
            "run_id": "find_current",
            "strong_recommendations": recommendations,
            "articles": recommendations,
            "recommendation_target_count": 4,
            "recommendation_shortfall": 0,
            "recommendation_quality": {"status": "ok", "missing_real_abstract_count": 0, "missing_chinese_abstract_count": 0, "english_abstract_fallback_count": 0},
        },
    )
    write_json(taste_dir / "read_results.json", {"run_id": "find_old", "source": "claude_code_current_find_takeover", "readings": [{"title": "Old Paper"}]})
    write_json(taste_dir / "ideas.json", {"run_id": "find_old", "source": "claude_code_current_find_takeover", "ideas": [{"id": "old-idea"}]})
    write_json(taste_dir / "plans.json", {"run_id": "find_old", "source": "claude_code_current_find_takeover", "plans": [{"plan_id": "old-plan"}]})
    write_json(state_dir / "current_find_research_plan.json", {"run_id": "find_current", "status": "blocked_current_find_full_text_evidence_pending"})
    write_json(
        state_dir / "current_find_claude_reading_validation.json",
        {
            "run_id": "find_current",
            "status": "blocked_current_find_full_text_evidence_pending",
            "preflight": "before_current_find_claude_takeover",
            "valid": False,
            "actual_reading_count": 0,
            "expected_recommendation_count": 4,
            "full_text_evidence_count": 2,
            "pending_without_evidence_count": 2,
            "pending_full_text_reading_count": 2,
            "full_text_reading_count": 0,
            "pending_full_text_reading_titles": ["Paper 3", "Paper 4"],
            "blockers": ["Read-stage full-text packet still misses packet entries"],
        },
    )
    monkeypatch.setattr(project_bridge, "_current_find_results_light", lambda root, project: read_json(root / "planning" / "finding" / "find_results.json", {}))

    summary = project_bridge._current_find_pipeline_summary(project_root)

    assert summary["status"] == "blocked_current_find_full_text_evidence_pending"
    assert summary["read_artifact_count"] == 0
    assert summary["ideas"] == 0
    assert summary["plans"] == 0
    assert summary["full_text_evidence_count"] == 2
    assert summary["pending_without_evidence_count"] == 2
    assert summary["pending_full_text_reading_count"] == 2
    assert summary["pending_full_text_reading_titles"] == ["Paper 3", "Paper 4"]


def test_web_current_find_pipeline_counts_only_full_text_readings(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    write_json(
        taste_dir / "find_results.json",
        {
            "run_id": "find_demo",
            "strong_recommendations": [recommended_paper("paper-1", "Full Paper")],
        },
    )
    write_json(
        taste_dir / "read_results.json",
        {
            "run_id": "find_demo",
            "source": "claude_code_current_find_takeover",
            "readings": [
                {
                    "paper_id": "paper-1",
                    "title": "Full Paper",
                    "url": "https://example.test/paper",
                    "verdict": "core_reading",
                    "support_role": "core_method_reference",
                    "relevance": "主题相关。",
                    "method": "方法摘要。",
                    "experiments": "实验摘要。",
                    "limitations": "局限摘要。",
                    "critique_reason": "",
                    "full_text_available": False,
                    "full_text_status": "pending_full_text_reading",
                }
            ],
        },
    )
    write_json(taste_dir / "ideas.json", {"run_id": "find_demo", "source": "claude_code_current_find_takeover", "ideas": [{} for _ in range(5)]})
    write_json(taste_dir / "plans.json", {"run_id": "find_demo", "source": "claude_code_current_find_takeover", "plans": [{} for _ in range(5)]})
    write_json(state_dir / "current_find_research_plan.json", {"run_id": "find_demo"})
    monkeypatch.setattr(project_bridge, "_current_find_results_light", lambda root, project: read_json(root / "planning" / "finding" / "find_results.json", {}))

    summary = project_bridge._current_find_pipeline_summary(project_root)

    assert summary["takeover_ready"] is False
    assert summary["status"] == "blocked_current_find_full_text_evidence_pending"
    assert summary["failure_type"] == "full_text_evidence_missing"
    assert summary["next_required_action"] == "acquire_current_find_full_text_evidence"
    assert summary["readings"] == 0
    assert summary["read_artifact_count"] == 1
    assert summary["pending_full_text_reading_count"] == 1
    assert any("full-text evidence" in item for item in summary["blockers"])



def test_web_current_find_pipeline_hides_stale_selected_execution_until_content_ready(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    run_id = "find_demo_pending_read"
    recommendations = [recommended_paper("paper-1", "Paper 1"), recommended_paper("paper-2", "Paper 2")]
    write_json(
        taste_dir / "find_results.json",
        {
            "run_id": run_id,
            "strong_recommendations": recommendations,
            "read_candidates": recommendations,
            "recommendation_shortfall": 0,
            "recommendation_target_count": 2,
            "recommendation_quality": {"status": "ok"},
        },
    )
    write_json(taste_dir / "read_results.json", {"run_id": run_id, "source": "pending_new_find_read", "readings": []})
    write_json(taste_dir / "ideas.json", {"run_id": run_id, "source": "pending_new_find_idea", "ideas": []})
    write_json(taste_dir / "plans.json", {"run_id": run_id, "source": "pending_new_find_plan", "plans": []})
    write_json(
        state_dir / "current_find_research_plan.json",
        {
            "run_id": run_id,
            "status": "pending_current_find_read",
            "next_required_action": "run_read_for_current_find",
            "selected_plan_id": "stale-plan-001",
            "selected_idea_id": "stale-idea-001",
            "selected_execution": {"selected_plan_id": "stale-plan-001"},
        },
    )
    monkeypatch.setattr(project_bridge, "_current_find_results_light", lambda root, project: read_json(root / "planning" / "finding" / "find_results.json", {}))

    summary = project_bridge._current_find_pipeline_summary(project_root)

    assert summary["status"] == "pending_current_find_read"
    assert summary["content_ready"] is False
    assert summary["selected_execution"] == {}
    assert summary["selected_plan_id"] == ""
    assert summary["selected_idea_id"] == ""
    assert summary["selected_execution_status"] == ""
    assert summary["selected_execution_issue"] == ""


def test_web_current_find_pipeline_rejects_abstract_only_no_pdf_evidence(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    write_json(
        taste_dir / "find_results.json",
        {
            "run_id": "find_demo_abstract_only",
            "strong_recommendations": [recommended_paper("paper-1", "Abstract Only Paper")],
        },
    )
    write_json(
        taste_dir / "read_results.json",
        {
            "run_id": "find_demo_abstract_only",
            "source": "claude_code_current_find_takeover",
            "readings": [
                {
                    "paper_id": "paper-1",
                    "title": "Abstract Only Paper",
                    "url": "https://doi.org/10.example/abstract",
                    "verdict": "recommended_reading_boundary",
                    "support_role": "foundation_borrowing",
                    "critique_reason": "只有摘要或题录信息，缺少 PDF/HTML 全文。",
                    "abstract_zh": "摘要说明该论文构建推荐数据集，但这只是摘要级信息。",
                    "motivation_zh": "动机来自摘要级线索，仍缺少论文正文。",
                    "method_details_zh": "方法描述来自摘要或题录，没有论文正文、PDF、HTML全文或正文包 text_path，因此不能算全文精读。",
                    "experiments_zh": "实验描述来自摘要或题录，没有完整实验章节正文、表格或指标细节，不能作为全文精读。",
                    "limitations_zh": "局限说明来自摘要级推断，仍缺少正文证据。",
                    "full_text_available": False,
                    "full_text_status": "no_pdf_available",
                    "source_text_chars": 2400,
                    "source_evidence": "dblp_abstract_only",
                }
            ],
        },
    )
    write_json(taste_dir / "ideas.json", {"run_id": "find_demo_abstract_only", "source": "claude_code_current_find_takeover", "ideas": [{} for _ in range(5)]})
    write_json(taste_dir / "plans.json", {"run_id": "find_demo_abstract_only", "source": "claude_code_current_find_takeover", "plans": [{} for _ in range(5)]})
    write_json(state_dir / "current_find_research_plan.json", {"run_id": "find_demo_abstract_only"})
    monkeypatch.setattr(project_bridge, "_current_find_results_light", lambda root, project: read_json(root / "planning" / "finding" / "find_results.json", {}))

    summary = project_bridge._current_find_pipeline_summary(project_root)

    assert summary["takeover_ready"] is False
    assert summary["full_text_evidence_count"] == 0
    assert summary["full_text_reading_count"] == 0
    assert summary["pending_without_evidence_count"] == 1
    assert summary["pending_full_text_reading_count"] == 1
    assert any("full-text evidence" in item for item in summary["blockers"])


def test_web_current_find_pipeline_requires_chinese_deep_read_fields(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    write_json(
        taste_dir / "find_results.json",
        {
            "run_id": "find_demo_deep_fields",
            "strong_recommendations": [recommended_paper("paper-1", "Deep Field Paper")],
        },
    )
    write_json(
        taste_dir / "read_results.json",
        {
            "run_id": "find_demo_deep_fields",
            "source": "claude_code_current_find_takeover",
            "readings": [
                {
                    "paper_id": "paper-1",
                    "title": "Deep Field Paper",
                    "url": "https://example.test/paper",
                    "verdict": "core_reading",
                    "support_role": "core_method_reference",
                    "critique_reason": "",
                    "abstract_from_find": "这是 Find 摘要，不能替代项目代理基于正文写出的中文精读字段。",
                    "relevance": "direct_target",
                    "method": "The method is described in English only, so the web/API projection must not count it as Chinese full-paper reading.",
                    "experiments": "The experiments are described in English only, so the web/API projection must keep the reading blocked.",
                    "limitations": "The limitations are described in English only, so this is not enough for the Chinese read contract.",
                    "full_text_available": True,
                    "full_text_status": "pdf_text_read",
                    "pdf_text_chars": 2400,
                    "source_evidence": {"text_chars": 2400, "text_path": "texts/deep-field-paper.txt"},
                }
            ],
        },
    )
    write_json(taste_dir / "ideas.json", {"run_id": "find_demo_deep_fields", "source": "claude_code_current_find_takeover", "ideas": [{} for _ in range(5)]})
    write_json(taste_dir / "plans.json", {"run_id": "find_demo_deep_fields", "source": "claude_code_current_find_takeover", "plans": [{} for _ in range(5)]})
    write_json(state_dir / "current_find_research_plan.json", {"run_id": "find_demo_deep_fields"})
    monkeypatch.setattr(project_bridge, "_current_find_results_light", lambda root, project: read_json(root / "planning" / "finding" / "find_results.json", {}))

    summary = project_bridge._current_find_pipeline_summary(project_root)

    assert summary["takeover_ready"] is False
    assert summary["full_text_evidence_count"] == 1
    assert summary["full_text_reading_count"] == 0
    assert summary["pending_deep_read_synthesis_count"] == 1
    assert summary["pending_full_text_reading_count"] == 1
    assert any("required Chinese deep-read JSON fields" in item for item in summary["blockers"])
    gaps = summary["reading_validation"]["deep_read_content_gap_details"][0]["missing_or_invalid_fields"]
    assert any("abstract_zh" in item for item in gaps)
    assert any("motivation_zh" in item for item in gaps)



def test_full_cycle_current_plan_requires_full_text_validation():
    from scripts.run_full_research_cycle import current_find_validation_ready

    old_validation = {
        "run_id": "find_demo",
        "valid": True,
        "expected_recommendation_count": 20,
        "actual_reading_count": 20,
        "full_text_reading_count": 0,
        "pending_full_text_reading_count": 20,
    }
    assert current_find_validation_ready(old_validation, "find_demo", 20) is False

    ready_validation = {
        **old_validation,
        "policy_version": "full_text_required_v5_detailed_deep_read",
        "full_text_reading_count": 20,
        "pending_full_text_reading_count": 0,
        "blockers": [],
    }
    assert current_find_validation_ready(ready_validation, "find_demo", 20) is True

def test_frontend_start_read_requests_all_recommendations_by_default():
    from pathlib import Path

    api = Path("web/frontend/client/src/api.ts").read_text(encoding="utf-8")
    start = api.index("export async function startRead")
    end = api.index("export async function startIdea")
    block = api[start:end]

    assert "max_papers: 8" not in block
    assert "const selected = Array.isArray(paperIds) ? paperIds.filter(Boolean) : []" in block
    assert "paper_ids: selected" in block
    assert "max_papers: selected.length ? selected.length : 0" in block


def test_frontend_find_completed_run_uses_artifact_counts_over_stale_fresh_state():
    from pathlib import Path

    app_path = Path(__file__).resolve().parents[1] / "web" / "frontend" / "client" / "src" / "App.tsx"
    app = app_path.read_text(encoding="utf-8")
    start = app.index("function renderFindLiteratureSurveyPanel")
    end = app.index("const sourceRows = findSourceStatusRows()", start)
    block = app[start:end]

    assert "const hasCompletedFindResultsForPanel = hasCurrentFindResults && !viewingActiveIncompleteFindRun;" in block
    assert "const literatureFreshFindRunning = String(literature.status || \"\").toLowerCase() === \"fresh_find_running\";" in block
    assert "const freshFindActive = !hasCompletedFindResultsForPanel &&" in block
    assert "const currentFindCounts: any = freshFindActive ? {} : literatureCounts || {};" in block
    assert block.index("hasCompletedFindResultsForPanel") < block.index("const freshFindActive") < block.index("const currentFindCounts")
    assert "(currentFindCounts as any).tfidfScreened" in block
    assert "(currentFindCounts as any).llmTitleScored" in block

def test_frontend_markdown_renderer_supports_latex_links_and_math_markup():
    from pathlib import Path

    app_path = Path(__file__).resolve().parents[1] / "web" / "frontend" / "client" / "src" / "App.tsx"
    css_path = Path(__file__).resolve().parents[1] / "web" / "frontend" / "client" / "src" / "styles.css"
    app = app_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8")

    assert "function normalizePublicLatexLinks" in app
    assert "normalizePublicLatexLinks(stripLegacyArtifactPointerLines(markdown))" in app
    assert "PLAIN_MATH_FRAGMENT_RE" not in app
    assert "MATH_LATEX_COMMAND_RE" in app
    assert "function looksLikeBareMathExpression" in app
    assert "function renderBareMathInText" in app
    assert "function looksLikeCodeIdentifierOrLabel" in app
    assert "function looksLikePlainMetadataAssignment" in app
    assert "looksLikeCodeIdentifierOrLabel(core)" in app
    assert "looksLikePlainMetadataAssignment(value)" in app
    assert "MATH_INDEX_OFFSET_RE" in app
    assert "[一-鿿]+" in app
    assert "Rank|rank" in app
    assert "function normalizeMalformedLatexCommands" in app
    assert "String.fromCharCode(92)" in app
    assert "function renderBareLatexCommandsInText" in app
    assert "BARE_LATEX_TEXT_COMMAND_START_RE" in app
    assert "textit|emph|textbf|texttt|textsc|underline" in app
    assert "\\%" in app
    assert "uparrow|downarrow" in app
    assert "cdots|ldots|dots" in app
    assert "×÷·−" in app
    assert "hasBracketFormula" in app
    assert "hasMathKeyword" in app
    assert "RECTOKEN" in app
    assert "run|job|project|source|stage|task|current|management" in app
    assert "function isNumericDelimitedMathInContext" in app
    assert "isNumericDelimitedMathInContext(expression, before, after)" in app
    assert "function isDelimitedMathAffixInContext" in app
    assert "isDelimitedMathAffixInContext(expression, before, after)" in app
    assert "{}^{$1}" in app
    assert "{}_{$1}" in app
    assert "function displayMathExpressionFromLine" in app
    assert "function renderMathSource" in app
    assert "import katex from \"katex\"" in app
    assert "katex.renderToString" in app
    assert "katex/dist/katex.min.css" in app
    assert "normalizeInformalMathForKatex" in app
    assert "$1^{$2}" in app
    assert "UNICODE_MATH_TO_LATEX" in app
    assert "function decodeBasicHtmlEntities" in app
    assert "decodeBasicHtmlEntities(stripMathDelimiters(raw))" in app
    assert "function mathInlineHtml" in app
    assert 'class="math-inline"' in app
    assert "\\theta" in app
    assert "frac|sqrt|sum" in app
    assert "\\url" in app
    assert ".markdownBody .math-inline" in css
    assert ".markdownBody .math-display" in css


def test_frontend_paper_self_review_evidence_blockers_are_submission_gate():
    from pathlib import Path

    app = Path("web/frontend/client/src/App.tsx").read_text(encoding="utf-8")

    assert "function paperSubmissionEvidenceBlocked" in app
    assert "paper_self_review_evidence_blocker_count" in app
    assert "paper_self_review_preview_only_ready" in app
    assert "paper_self_review_submission_evidence_ready === false" in app
    assert "论文预览可看，投稿证据阻塞" in app
    assert "预览 PDF 已生成；投稿证据仍阻塞" in app
    assert "投稿证据阻塞的论文预览" in app
    assert "PDF 仅作预览；底层 LaTeX/BibTeX/自审诊断已保留给项目代理处理，不在这里展开。" in app
    assert "paperSelfReviewEvidenceText" in app
    assert "完整自审原文保留在审计 artifact" in app
    assert "详细待处理项见上方列表" not in app
    assert "const selfReviewIssue = paperSelfReviewSummary(paper);" not in app
    assert "Claude Code 自审阻塞" not in app
    assert "投稿证据门控" in app
    assert "paperSelfReviewDisplayStatus(researchStages?.paper)" in app
    assert "{t.paperSelfReviewStatus}={displayMaybe(researchStages?.paper?.paper_self_review_status)}" not in app


def test_frontend_paper_page_surfaces_global_experiment_evidence_gate():
    from pathlib import Path

    app = Path("web/frontend/client/src/App.tsx").read_text(encoding="utf-8")

    assert "paperGlobalEvidenceGateBlocked" in app
    assert "paperGlobalEvidenceGateText" in app
    assert "humanGateSummary?.scientific_progress?.status" in app
    assert "researchSummary?.current_blocker?.summary" in app
    assert 'category.includes("experiment_evidence")' in app
    assert "freshBaseMainBlocked || literatureGateBlocked || paperGlobalEvidenceGateBlocked" in app
    assert "paperGlobalEvidenceGateText" in app.split("freshBaseMainBlocked || literatureGateBlocked || paperGlobalEvidenceGateBlocked", 1)[1]


def test_stale_paper_placeholder_receipt_has_no_full_response_button():
    from auto_research.web import project_bridge

    receipt = {
        "status": "blocked",
        "stage": "paper",
        "response_markdown": "当前投稿目标为 ICLR；旧论文写作回执属于其他 venue，已隐藏。",
        "response_source": "venue_filtered_placeholder",
        "fallback_reason": "stage_receipt_stale_for_current_venue",
    }

    public = project_bridge._public_claude_receipt(receipt)

    assert public["full_response_available"] is False
    assert public["content_compacted"] is False
    assert public["raw_response_hidden"] is False
    assert public["response_chcount"] == 0


def test_paper_public_projection_hides_agent_repair_blocker_rows():
    from auto_research.web import project_bridge

    blocker = project_bridge._paper_public_blocker_text(
        "missing bib entries for cited keys=achiam2023gpt, baheti2024field"
    )
    row = {
        "status": "preview_available",
        "pdf_path": "/tmp/paper.pdf",
        "conference_preview_blocker_summary": blocker,
        "paper_citation_render_blockers": [{"id": "latex_undefined_citations", "detail": "achiam2023gpt"}],
        "paper_self_review_status": "block",
        "paper_self_review_blockers": [{"id": "self_review_hash_mismatch_pdf", "detail": "sha256 mismatch"}],
        "paper_self_review_evidence_blockers": [{"category": "missing_empirical_validation", "detail": "raw internal detail"}],
        "paper_self_review_evidence_blocker_count": 1,
    }

    out = project_bridge._paper_stage_public_fields(row)

    assert out["paper_citation_render_blockers"] == []
    assert out["paper_self_review_blockers"] == []
    assert out["paper_self_review_evidence_blockers"] == []
    assert out["conference_preview_blockers"] == []
    assert out["paper_self_review_evidence_blocker_count"] == 1
    assert "achiam2023gpt" not in out["paper_summary"]
    assert "sha256 mismatch" not in out["paper_summary"]
    assert "具体修复清单已交由项目代理处理" in out["paper_summary"]
    assert "论文自审未通过，具体修复项已交由项目代理处理" in out["paper_summary"]


def test_paper_self_review_evidence_rows_are_public_summaries():
    from auto_research.web import project_bridge

    raw = (
        "Paper proposes 'Example Candidate Method' but Results section "
        "contains zero empirical results validating the proposed method. The only numerical results are "
        "reference backbone calibration."
    )

    rows = project_bridge._paper_public_self_review_evidence_rows([
        {"id": "self_review_evidence_missing_empirical_validation", "category": "missing_empirical_validation", "detail": raw}
    ])

    assert len(rows) == 1
    row = rows[0]
    assert row["public_title"] == "缺少新方法实验验证"
    assert "Paper proposes" not in row["public_detail"]
    assert "zero empirical" not in row["detail"]
    assert "同协议本地指标" in row["public_detail"]
    assert row["raw_detail_chcount"] == len(raw)
    assert row["artifact_hint"] == "state/paper_evidence_audit.json / state/submission_readiness.json"


def test_compact_project_summary_prioritizes_selected_plan_gate_over_stale_experiment_snapshot(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    run_id = "find_demo_selected_plan_gate"

    write_json(
        taste_dir / "find_results.json",
        {
            "run_id": run_id,
            "strong_recommendations": [
                recommended_paper(f"paper-{idx}", f"Paper {idx}")
                for idx in range(20)
            ],
        },
    )
    write_json(
        taste_dir / "find_progress.json",
        {"run_id": run_id, "phase": "complete", "strong_recommendation_count": 20, "recommendation_target_count": 20, "recommendation_shortfall": 0, "counts": {}},
    )
    write_json(taste_dir / "read_results.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "readings": [{"paper_id": f"paper-{idx}", "title": f"Paper {idx}", "full_text_available": True, "full_text_status": "pdf_text_read", "pdf_text_chars": 2400, "source_evidence": {"text_chars": 2400, "text_path": f"texts/paper-{idx}.txt"}, "abstract_zh": "这是一段足够长的中文摘要，说明论文动机、方法和实验背景。", "motivation_zh": "这是一段足够长的中文动机分析，来自全文精读而不是摘要占位。", "method_details_zh": "这是一段足够长的中文方法细节，覆盖模型结构、训练目标和评测协议。", "experiments_zh": "这是一段足够长的中文实验分析，覆盖数据集、指标、对比和消融。", "limitations_zh": "这是一段足够长的中文局限性分析，说明适用边界和未解决问题。"} for idx in range(20)]})
    write_json(taste_dir / "ideas.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "ideas": [{"id": f"idea-{idx}"} for idx in range(5)]})
    write_json(taste_dir / "plans.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "plans": [{"plan_id": f"plan-{idx}", "idea_id": f"idea-{idx}"} for idx in range(5)]})
    write_json(state_dir / "current_find_research_plan.json", {"run_id": run_id, "status": "blocked_missing_selected_plan"})
    write_json(
        state_dir / "full_research_cycle.json",
        {
            "status": "stale_full_research_cycle_snapshot",
            "summary": "完整科研自循环已停在实验门控；参考复现已通过，但当前主线还缺少候选实验证据。",
            "summary_zh": "完整科研自循环已停在实验门控；参考复现已通过，但当前主线还缺少候选实验证据。",
            "current_goal": "继续真实候选实验。",
            "full_cycle_job": {"status": "stale", "process_alive": False},
        },
    )
    write_json(state_dir / "reference_reproduction_gate.json", {"status": "pass", "decision": "continue_base"})
    write_json(state_dir / "selected_base_viability_gate.json", {"status": "blocked", "decision": "continue_experiment_evidence_repair"})
    write_json(state_dir / "experiment_registry.json", [{"status": "completed", "repo": "legacy", "metric_value": 1.0}])
    write_json(state_dir / "experiment_record_table.json", {"rows": [{"审计状态": "通过", "repo": "legacy"}], "row_count": 1})

    pipeline_contract = {
        "run_id": run_id,
        "status": "blocked_missing_selected_plan",
        "failure_type": "missing_selected_plan",
        "next_required_action": "rerun_current_find_claude_takeover_select_single_best_plan",
        "content_ready": True,
        "read_idea_plan_ready": True,
        "execution_ready": False,
        "takeover_ready": False,
        "selected_execution": {
            "required": True,
            "selected_plan_id": "",
            "selected_idea_id": "",
            "status": "blocked_missing_selected_plan",
            "selection_issue": "missing_selected_plan",
            "candidate_counts": {"ideas": 5, "plans": 5},
        },
        "selected_execution_issue": "missing_selected_plan",
        "selected_plan_id": "",
        "selected_idea_id": "",
        "candidate_counts": {"ideas": 5, "plans": 5},
        "readings": 20,
        "reading_count": 20,
        "read_artifact_count": 20,
        "full_text_reading_count": 20,
        "pending_full_text_reading_count": 0,
        "ideas": 5,
        "plans": 5,
    }
    monkeypatch.setattr(project_bridge, "_current_find_pipeline_summary", lambda _root, **_kwargs: pipeline_contract)
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda _project, _root: {"venue_ids": ["openreview_iclr_2026"], "years": [2026]})
    monkeypatch.setattr(project_bridge, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])

    summary = project_bridge._fast_project_summary("demo_project", project_root, {"name": "demo_project", "topic": "Demo", "target_venue": "ICLR"})

    assert summary["status"] == "blocked_missing_selected_plan"
    assert "主控 Claude Code" in summary["summary"]
    assert summary["full_research_cycle"]["status"] == "blocked_missing_selected_plan"
    assert summary["human_supervision"]["blocker"]["category"] == "current_find_selected_plan_gate"
    assert "主控 Claude Code" in summary["human_supervision"]["blocker"]["next_action"]
    assert summary["stages"]["environment"]["status"] == "blocked_missing_selected_plan"
    assert summary["stages"]["experiment"]["status"] == "blocked_missing_selected_plan"
    assert summary["stages"]["plan"]["status"] == "blocked_missing_selected_plan"
    assert summary["stages"]["experiment"]["experiment_count"] == 0
    assert summary["state"]["experiment_count"] == 0


def test_compact_project_summary_prioritizes_current_find_idea_plan_block_over_stale_experiment_snapshot(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    run_id = "find_demo_idea_plan_block"
    write_json(taste_dir / "find_results.json", {"run_id": run_id, "strong_recommendations": [recommended_paper("paper-1", "Paper 1")]})
    write_json(taste_dir / "find_progress.json", {"run_id": run_id, "phase": "complete", "strong_recommendation_count": 1, "recommendation_target_count": 1, "recommendation_shortfall": 0, "counts": {}})
    write_json(taste_dir / "read_results.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "readings": []})
    write_json(taste_dir / "ideas.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "ideas": []})
    write_json(taste_dir / "plans.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "plans": []})
    write_json(state_dir / "current_find_research_plan.json", {"run_id": run_id, "status": "blocked_current_find_idea_plan_incomplete", "next_required_action": "run_or_approve_current_find_idea_plan"})
    write_json(state_dir / "full_research_cycle.json", {"status": "stale_full_research_cycle_snapshot", "summary_zh": "旧实验门控摘要", "full_cycle_job": {"status": "stale", "process_alive": False}})
    write_json(state_dir / "reference_reproduction_gate.json", {"status": "pass", "decision": "continue_base"})
    write_json(state_dir / "selected_base_viability_gate.json", {"status": "blocked", "decision": "continue_experiment_evidence_repair"})

    pipeline_contract = {
        "run_id": run_id,
        "status": "blocked_current_find_idea_plan_incomplete",
        "failure_type": "idea_plan_artifacts_incomplete",
        "next_required_action": "run_or_approve_current_find_idea_plan",
        "content_ready": False,
        "read_idea_plan_ready": False,
        "execution_ready": False,
        "takeover_ready": False,
        "readings": 1,
        "reading_count": 1,
        "read_artifact_count": 1,
        "full_text_reading_count": 1,
        "pending_full_text_reading_count": 0,
        "ideas": 0,
        "plans": 0,
        "summary_zh": "当前 Find 后处理未通过 Claude 接管 gate：全文精读 1 篇、idea 0 个、plan 0 个。",
    }
    monkeypatch.setattr(project_bridge, "_current_find_pipeline_summary", lambda _root, **_kwargs: pipeline_contract)
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda _project, _root: {"venue_ids": ["openreview_iclr_2026"], "years": [2026]})
    monkeypatch.setattr(project_bridge, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])

    summary = project_bridge._fast_project_summary("demo_project", project_root, {"name": "demo_project", "topic": "Demo", "target_venue": "ICLR"})

    assert summary["status"] == "blocked_current_find_idea_plan_incomplete"
    assert "当前 Find 后处理未通过" in summary["summary"]
    assert summary["main_route"]["base_selection_status"] == "blocked_current_find_idea_plan_incomplete"





def test_verified_venue_metadata_overrides_stale_kdd_but_keeps_category_screen_input(monkeypatch):
    from auto_research.web import project_bridge

    current_rows = [
        {
            "source": "ICLR",
            "source_kind": "venue",
            "venue_id": "openreview_iclr_2026",
            "venue": "ICLR",
            "ok": True,
            "count": 2063,
            "candidate_count": 2063,
            "raw_title_index_count": 5351,
            "detail_fetched_count": 80,
        },
        {
            "source": "KDD",
            "source_kind": "venue",
            "venue_id": "dblp_kdd",
            "venue": "KDD",
            "ok": True,
            "count": 100,
            "candidate_count": 100,
            "raw_title_index_count": 100,
            "detail_fetched_count": 59,
        },
    ]
    verified_rows = [
        {
            "source": "ICLR",
            "source_kind": "venue",
            "venue_id": "openreview_iclr_2026",
            "venue": "ICLR",
            "ok": True,
            "count": 5351,
            "candidate_count": 5351,
            "raw_title_index_count": 5351,
            "corpus_count": 5351,
            "metadata_completeness_status": "complete",
            "metadata_completeness_ok": True,
            "category_status": "official_or_cached_categories",
            "has_official_categories": True,
            "source_scope": "official_openreview_metadata",
            "official_title_index_verified": True,
            "official_accepted_list_verified": True,
            "has_abstracts_in_title_index": True,
        },
        {
            "source": "KDD",
            "source_kind": "venue",
            "venue_id": "dblp_kdd",
            "venue": "KDD",
            "ok": True,
            "count": 256,
            "candidate_count": 256,
            "raw_title_index_count": 256,
            "corpus_count": 256,
            "metadata_completeness_status": "complete",
            "metadata_completeness_ok": True,
            "category_status": "no_official_categories",
            "has_official_categories": False,
            "source_scope": "dblp_current_index_not_official_accepted_list",
            "official_title_index_verified": False,
            "official_accepted_list_verified": False,
            "has_abstracts_in_title_index": False,
            "missing_abstract_count": 256,
        },
    ]

    merged = project_bridge._merge_verified_venue_metadata_rows(current_rows, verified_rows)
    by_source = {row["source"]: row for row in merged}

    assert by_source["ICLR"]["raw_title_index_count"] == 5351
    assert by_source["ICLR"]["candidate_count"] == 2063
    assert by_source["ICLR"]["detail_fetched_count"] == 80
    assert by_source["ICLR"]["metadata_completeness_status"] == "complete"
    assert by_source["ICLR"]["source_scope"] == "official_openreview_metadata"
    assert by_source["ICLR"]["official_title_index_verified"] is True
    assert by_source["KDD"]["raw_title_index_count"] == 256
    assert by_source["KDD"]["candidate_count"] == 256
    assert by_source["KDD"]["detail_fetched_count"] == 59
    assert by_source["KDD"]["ok"] is True
    assert by_source["KDD"]["limited"] is True
    assert by_source["KDD"]["metadata_completeness_status"] == "title_index_only"
    assert by_source["KDD"]["metadata_completeness_ok"] is False
    assert by_source["KDD"]["source_scope"] == "dblp_current_index_not_official_accepted_list"
    assert by_source["KDD"]["official_title_index_verified"] is False
    assert by_source["KDD"]["official_accepted_list_verified"] is False
    assert by_source["KDD"]["missing_abstract_count"] == 256
    counts = project_bridge._venue_metadata_counts(merged)
    assert counts["raw_title_index_papers"] == 5607
    assert counts["venue_title_filter_input_papers"] == 2319


def test_missing_verified_venue_cache_does_not_override_current_run_status():
    from auto_research.web import project_bridge

    current_rows = [
        {
            "source": "ICLR",
            "source_kind": "venue",
            "venue_id": "openreview_iclr_2026",
            "venue": "ICLR",
            "ok": True,
            "limited": True,
            "count": 200,
            "candidate_count": 200,
            "raw_title_index_count": 200,
            "requested_years": [2026],
            "effective_years": [2026],
            "detail_fetched_count": 40,
            "message": "adapter=openreview; years=2026; corpus=200; screen_input=200; fetched=200",
        }
    ]
    verified_rows = [
        {
            "source": "openreview_iclr_2026",
            "source_kind": "venue",
            "venue_id": "openreview_iclr_2026",
            "venue": "openreview_iclr_2026",
            "ok": False,
            "limited": True,
            "count": 0,
            "message": "verified local venue metadata cache missing",
            "requested_years": [2026],
            "effective_years": [],
            "raw_title_index_count": 0,
            "candidate_count": 0,
            "metadata_completeness_status": "missing",
            "metadata_completeness_ok": False,
        }
    ]

    merged = project_bridge._merge_verified_venue_metadata_rows(current_rows, verified_rows)

    assert len(merged) == 1
    assert merged[0]["source"] == "ICLR"
    assert merged[0]["ok"] is True
    assert merged[0]["count"] == 200
    assert merged[0]["candidate_count"] == 200
    assert merged[0]["effective_years"] == [2026]
    assert merged[0]["detail_fetched_count"] == 40
    assert "metadata cache missing" not in merged[0].get("message", "")


def test_venue_metadata_normalization_trusts_adapter_over_stale_scope():
    from auto_research.web import project_bridge

    row = project_bridge._normalize_venue_metadata_status_row({
        "source": "ICML",
        "source_kind": "venue",
        "venue_id": "dblp_icml",
        "venue": "ICML",
        "adapter": "icml_downloads",
        "source_scope": "dblp_current_index_not_official_accepted_list",
        "ok": True,
        "raw_title_index_count": 6439,
        "candidate_count": 6439,
        "metadata_completeness_status": "title_index_only",
        "category_status": "no_official_categories",
        "has_official_categories": False,
        "has_abstracts_in_title_index": False,
    })

    assert row["source_scope"] == "official_icml_downloads_title_index"
    assert row["official_title_index_verified"] is True
    assert row["official_accepted_list_verified"] is True
    assert row["metadata_completeness_status"] == "title_index_only"
    assert row["limited"] is True


def test_venue_metadata_normalization_treats_usable_openreview_partial_as_public_ok():
    from auto_research.web import project_bridge

    row = project_bridge._normalize_venue_metadata_status_row({
        "source": "ICLR",
        "source_kind": "venue",
        "venue_id": "openreview_iclr",
        "venue": "ICLR",
        "adapter": "openreview",
        "ok": True,
        "limited": True,
        "raw_title_index_count": 5350,
        "candidate_count": 1850,
        "metadata_completeness_status": "partial",
        "metadata_completeness_limited": True,
        "category_status": "official_or_cached_categories",
        "has_official_categories": True,
        "has_abstracts": True,
        "has_abstracts_in_title_index": True,
        "source_verified": True,
    })

    assert row["source_scope"] == "official_openreview_metadata"
    assert row["limited"] is False
    assert row["metadata_completeness_limited"] is True


def test_frontend_source_status_distinguishes_public_limited_from_openreview_audit():
    app = (Path(__file__).resolve().parents[1] / "web" / "frontend" / "client" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "function sourceStatusIsLimited" in app
    assert "sourceStatusHasUsableOpenReviewMetadata(item)" in app
    assert 'adapter.includes("openreview")' in app
    assert "has_official_categories" in app
    assert "has_abstracts_in_title_index" in app
    assert "source remains partial until" in app
    assert 'return "适配器尚未完成总量审计' not in app


def test_base_switch_candidate_does_not_override_selected_base_main_route(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    state_dir = project_root / "state"
    taste_dir = project_root / "planning" / "finding"
    state_dir.mkdir(parents=True)
    taste_dir.mkdir(parents=True)

    run_id = "find_current"
    reference_repo = "/tmp/demo_project/repos/selected/example_org_reference_rec"
    candidate_repo = "/tmp/demo_project/repos/candidates/candidaterec"
    reference_title = "Reference Recommender Benchmark Paper"

    write_json(taste_dir / "find_progress.json", {"run_id": run_id, "phase": "complete"})
    write_json(state_dir / "current_find_research_plan.json", {"run_id": run_id, "status": "ready"})
    write_json(
        state_dir / "selected_base_viability_gate.json",
        {
            "status": "blocked",
            "decision": "base_switch_gate_required",
            "current_selected_repo": "example-org/ReferenceRec",
            "current_selected_repo_path": reference_repo,
            "selected_base_title": reference_title,
        },
    )
    write_json(
        state_dir / "fresh_base_implementation_plan.json",
        {
            "status": "implementation_ready_for_reference_probe",
            "repo": {"name": "example-org/ReferenceRec", "repo_path": reference_repo},
            "ready_datasets": ["ks"],
        },
    )
    write_json(
        state_dir / "selected_base_route_guard.json",
        {
            "selected_base_find_run_id": "find_previous_selected_base",
            "trusted_audit": {"repo_name": "example-org/ReferenceRec", "repo_path": reference_repo},
        },
    )
    write_json(
        state_dir / "fresh_base_reference_reproduction_audit.json",
        {
            "status": "completed_reference_reproduction",
            "repo_path": reference_repo,
            "selected_base": {"name": "example-org/ReferenceRec", "repo_path": reference_repo, "literature_base_title": reference_title},
        },
    )
    write_json(
        state_dir / "evidence_ready_repo_selection.json",
        {
            "selection_stage": "environment_claude_code",
            "selection_gate": "accepted_by_deterministic_base_switch_gate",
            "accepted_by_claude": True,
            "selected": {
                "name": "example-org/CandidateRec",
                "repo_path": candidate_repo,
                "local_path": candidate_repo,
                "fresh_find_run_id": run_id,
                "literature_base_title": "example-org/CandidateRec",
                "selection_stage": "environment_claude_code",
                "selection_gate": "accepted_by_deterministic_base_switch_gate",
                "decision": "selected_by_authorized_base_switch_gate",
            },
        },
    )
    write_json(
        state_dir / "base_switch_execution.json",
        {
            "status": "authorized_by_deterministic_base_switch_gate",
            "decision": "route_switch_executed",
            "new_route": {"name": "example-org/CandidateRec", "repo_path": candidate_repo, "local_path": candidate_repo},
        },
    )
    write_json(state_dir / "base_switch_gate.json", {"status": "pass", "decision": "authorize_base_switch", "switch_authorized": True})
    write_json(state_dir / "reference_reproduction_gate.json", {"status": "pass", "decision": "continue_base"})
    write_json(
        state_dir / "full_research_cycle.json",
        {
            "status": "stale_full_research_cycle_snapshot",
            "summary": "完整科研自循环已停在实验门控。",
            "summary_zh": "完整科研自循环已停在实验门控。",
            "full_cycle_job": {"status": "stale", "process_alive": False},
        },
    )
    monkeypatch.setattr(project_bridge, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda _project, _root: {"venue_ids": ["openreview_iclr_2026"], "years": [2026]})

    env = project_bridge._current_environment_selection(project_root)
    selected = env["selected"]
    assert env["reason"] == "selected_base_viability_current_route"
    assert selected["name"] == "example-org/ReferenceRec"
    assert selected["repo_path"] == reference_repo
    assert project_bridge._artifact_match_repo_path(project_root) == reference_repo

    summary = project_bridge._fast_project_summary("demo_project", project_root, {"name": "demo_project", "topic": "Demo", "target_venue": "ICLR"})
    main_route = summary["human_supervision"]["main_route"]
    assert main_route["base_title"] == reference_title
    assert main_route["repo_name"] == "example-org/ReferenceRec"
    assert main_route["repo_path"] == reference_repo
    assert "CandidateRec" not in main_route["repo_name"]


def test_paper_job_compact_uses_current_project_venue_over_stale_job_payload(tmp_path, monkeypatch):
    project = "demo_project"
    root = tmp_path / "projects" / project
    (root / "paper" / "output" / "iclr").mkdir(parents=True)
    (root / "paper" / "output" / "nature").mkdir(parents=True)
    (root / "paper" / "output" / "iclr" / "paper.pdf").write_bytes(b"iclr pdf")
    (root / "paper" / "output" / "nature" / "paper.pdf").write_bytes(b"nature pdf")
    write_json(root / "project.json", {"name": project, "target_venue": "ICLR", "venue": "ICLR"})
    monkeypatch.setattr(server, "PROJECT_IDS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(
        server,
        "get_active_paper_state",
        lambda _project, venue="": {
            "venue": "ICLR",
            "target_venue": "ICLR",
            "venue_slug": "iclr",
            "paper_venue_format_status": "pass",
            "venue_requirements_status": "ok",
            "conference_preview_ready": True,
        },
    )

    compact = server._compact_job_for_list(
        {
            "job_id": "paper_old",
            "stage": "paper",
            "status": "blocked",
            "created_at": "2026-06-07T00:00:00Z",
            "logs": [],
            "run_id": "",
            "result": {
                "project": project,
                "paper_stage": {
                    "venue": "Nature",
                    "target_venue": "Nature",
                    "venue_slug": "nature",
                    "raw_pdf_path": str(root / "paper" / "output" / "nature" / "paper.pdf"),
                },
            },
            "internal": False,
            "display": "",
            "error": "",
            "cancel_requested": False,
            "cancelled_at": "",
            "progress": {},
        }
    )

    paper = compact["result"]["paper_stage"]
    payload = json.dumps(compact, ensure_ascii=False)
    assert paper["venue"] == "ICLR"
    assert paper["target_venue"] == "ICLR"
    assert paper["venue_slug"] == "iclr"
    assert "paper/output/nature" not in payload


def test_jobs_api_restores_find_run_history_when_persisted_jobs_are_empty(tmp_path, monkeypatch):
    run_id = "find_20260607_203212_523158"
    run_root = tmp_path / "runs" / run_id
    run_root.mkdir(parents=True)
    write_json(run_root / "find_progress.json", {"phase": "complete", "strong_recommendation_count": 20, "recommendation_target_count": 20, "recommendation_shortfall": 0, "counts": {"raw_title_index": 17176, "evaluated_candidates": 2500}})
    write_json(run_root / "manifest.json", {"created_at": "2026-06-07T20:32:12Z"})
    write_json(run_root / "find_results.json", {"run_id": run_id, "articles": []})
    write_text(run_root / "article.md", "# article")
    server.JOBS.clear()
    monkeypatch.setattr(server, "_reconcile_detached_launcher_jobs", lambda: None)
    monkeypatch.setattr(server, "_live_jobs_from_projects", lambda compact=True: [])
    monkeypatch.setattr(server, "_cached_list_runs", lambda: [{"run_id": run_id, "created_at": "2026-06-07T20:32:12Z"}])
    monkeypatch.setattr(server, "run_dir", lambda _run_id: run_root)

    rows = server.api_jobs(compact=True, limit=10, include_history=True)

    history = next(item for item in rows if item["job_id"] == f"find-run-{run_id}")
    assert history["stage"] == "find"
    assert history["status"] == "done"
    assert history["run_id"] == run_id
    assert history["result"]["artifact_counts"]["raw_title_index"] == 17176


def test_synthesized_find_run_history_detail_is_clickable(tmp_path, monkeypatch):
    run_id = "find_20260607_203212_523158"
    run_root = tmp_path / "runs" / run_id
    run_root.mkdir(parents=True)
    write_json(run_root / "find_progress.json", {"phase": "complete", "strong_recommendation_count": 20, "recommendation_target_count": 20})
    write_json(run_root / "manifest.json", {"created_at": "2026-06-07T20:32:12Z"})
    write_json(run_root / "find_results.json", {"run_id": run_id, "articles": []})
    write_text(run_root / "article.md", "# article")
    server.JOBS.clear()
    monkeypatch.setattr(server, "_cached_list_runs", lambda: [{"run_id": run_id, "created_at": "2026-06-07T20:32:12Z"}])
    monkeypatch.setattr(server, "run_dir", lambda _run_id: run_root)

    detail = server.api_job(f"find-run-{run_id}", compact=True)

    assert detail["job_id"] == f"find-run-{run_id}"
    assert detail["stage"] == "find"
    assert detail["status"] == "done"
    assert detail["run_id"] == run_id




def test_live_full_cycle_guard_ignores_current_find_claude_child(monkeypatch, tmp_path):
    from auto_research.web import project_bridge

    project = "demo_project"
    root = tmp_path / "projects" / project
    state_dir = root / "state"
    state_dir.mkdir(parents=True)
    write_json(
        state_dir / "full_cycle_job.json",
        {
            "project": project,
            "status": "running",
            "pid": 333,
            "kind": "claude_cli",
            "stage": "full-cycle-ideation",
            "process_alive": True,
            "command": "/node/bin/claude -p --add-dir " + str(root),
        },
    )
    write_json(
        state_dir / "full_research_cycle.json",
        {
            "status": "running",
            "full_cycle_job": {
                "project": project,
                "status": "running",
                "pid": 333,
                "kind": "claude_cli",
                "stage": "full-cycle-ideation",
                "process_alive": True,
                "command": "/node/bin/claude -p --add-dir " + str(root),
            },
        },
    )
    monkeypatch.setattr(project_bridge, "PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(project_bridge, "_pid_alive", lambda pid: str(pid) in {"111", "222", "333"})
    monkeypatch.setattr(
        project_bridge,
        "_remote_process_rows",
        lambda: [
            {
                "pid": "111",
                "ppid": "1",
                "kind": "claude_session",
                "cmd": "python modules/reading/scripts/ensure_current_find_research_plan.py --project demo_project",
                "cwd": str(root),
            },
            {
                "pid": "222",
                "ppid": "111",
                "kind": "claude_session",
                "cmd": "python framework/scripts/claude_project_session.py --project demo_project --stage current-find-claude-read-idea-plan",
                "cwd": str(root),
            },
            {
                "pid": "333",
                "ppid": "222",
                "kind": "claude_cli",
                "cmd": "/node/bin/claude -p --add-dir " + str(root),
                "cwd": str(root),
            },
        ],
    )

    blocker = project_bridge.action_gate_blocker({"action": "full-cycle", "project": project, "use_existing_literature_packet": True})

    assert blocker is None


def test_jobs_api_hides_current_find_worker_when_top_level_read_job_is_running(monkeypatch):
    from auto_research.web import server

    server.JOBS.clear()
    server._LIVE_JOBS_CACHE.clear()
    project = "demo_project"
    read_job = server.JobState("read_demo", "read")
    read_job.status = "running"
    read_job.result = {"project": project, "run_id": "find_demo"}
    server.JOBS[read_job.job_id] = read_job
    dynamic_rows = [
        {
            "job_id": "current-find-worker_demo_111",
            "stage": "read",
            "status": "running",
            "created_at": "2026-06-08T00:00:00Z",
            "logs": [],
            "run_id": "find_demo",
            "result": {"project": project, "kind": "current_find_read_idea_plan_wrapper", "pid": "111"},
            "progress": {},
        },
        {
            "job_id": "find-run-find_demo",
            "stage": "find",
            "status": "done",
            "created_at": "2026-06-08T00:00:00Z",
            "logs": [],
            "run_id": "find_demo",
            "result": {"run_id": "find_demo", "artifact_counts": {"strong_recommendation": 20}},
            "progress": {},
        },
    ]
    monkeypatch.setattr(server, "_reconcile_detached_launcher_jobs", lambda: None)
    monkeypatch.setattr(server, "_live_jobs_from_projects", lambda compact=True: dynamic_rows)
    monkeypatch.setattr(server, "_cached_list_runs", lambda: [])

    rows = server.api_jobs(compact=True, limit=10, include_history=True)

    ids = [row["job_id"] for row in rows]
    assert "read_demo" in ids
    assert "current-find-worker_demo_111" not in ids
    assert "find-run-find_demo" in ids


def test_abstract_translation_status_for_recommendations_uses_actual_rows():
    translated = recommended_paper("paper-ok", "Translated Paper")
    missing = {**recommended_paper("paper-missing", "Missing Translation Paper"), "abstract_zh": ""}

    assert server._abstract_translation_status_for_recommendations([translated], "completed") == "completed"
    assert server._abstract_translation_status_for_recommendations([missing], "completed") == "partial"
    assert server._abstract_translation_status_for_recommendations([{"id": "paper-no-abstract", "title": "No Abstract"}], "completed") == "completed"


def test_sync_current_find_projection_recomputes_partial_translation_status(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    run_root = tmp_path / "run"
    (project_root / "state").mkdir(parents=True)
    run_root.mkdir(parents=True)
    write_json(
        run_root / "find_progress.json",
        {"abstract_translation_status": "completed", "recommendation_target_count": 2},
    )
    translated = recommended_paper("paper-ok", "Translated Paper")
    missing = {**recommended_paper("paper-missing", "Missing Translation Paper"), "abstract_zh": ""}
    find_results = {
        "run_id": "find_translation",
        "strong_recommendations": [translated, missing],
        "scoring_runtime": {"abstract_translation_status": "completed", "recommendation_target_count": 2},
        "recommendation_quality": {"status": "ok", "english_abstract_fallback_count": 0},
        "diagnostics": {"recommendation_quality": {"status": "ok", "english_abstract_fallback_count": 0}},
    }
    monkeypatch.setattr(server, "_find_artifact_run_dir_for_project", lambda _root, _run_id: run_root)

    projection = server._sync_current_find_projection(project_root, "find_translation", find_results, "test")

    assert projection["abstract_translation_status"] == "partial"
    assert find_results["abstract_translation_status"] == "partial"
    assert find_results["scoring_runtime"]["abstract_translation_status"] == "partial"
    assert projection["missing_recommendation_abstract_zh"] == [{"rank": "2", "id": "paper-missing", "title": "Missing Translation Paper"}]
    quality = find_results["recommendation_quality"]
    assert quality["missing_chinese_abstract_count"] == 1
    assert quality["english_abstract_fallback_count"] == 1
    assert quality["missing_chinese_abstract_ids"] == ["paper-missing"]
    assert quality["status"] == "needs_translation"
    assert find_results["scoring_runtime"]["recommendation_quality"] == quality
    assert find_results["diagnostics"]["recommendation_quality"] == quality


def test_find_adoption_gate_reads_progress_when_result_has_only_artifact_counts(tmp_path, monkeypatch):
    run_root = tmp_path / "find_progress_contract"
    run_root.mkdir(parents=True)
    write_json(
        run_root / "find_progress.json",
        {
            "phase": "complete",
            "strong_recommendation_count": 20,
            "strict_strong_anchor_count": 20,
            "recommendation_target_count": 20,
            "recommendation_shortfall": 0,
            "abstract_translation_status": "completed",
        },
    )
    monkeypatch.setattr(server, "run_dir", lambda _run_id: run_root)

    metrics = server._find_adoption_gate_metrics(
        "find_progress_contract",
        {
            "run_id": "find_progress_contract",
            "artifact_counts": {"strong_recommendations": 20},
        },
    )

    assert metrics["strong_count"] == 20
    assert metrics["target_count"] == 20
    assert metrics["shortfall"] == 0
    assert metrics["status"] == "complete"


def test_find_adoption_preserves_completed_same_run_downstream(tmp_path, monkeypatch):
    project = "demo_project"
    root = tmp_path / "projects" / project
    taste_dir = root / "planning" / "finding"
    state_dir = root / "state"
    run_root = tmp_path / "runs" / "find_done"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    run_root.mkdir(parents=True)
    recommendations = [recommended_paper("paper-a", "Completed Recommended Paper A"), recommended_paper("paper-b", "Completed Recommended Paper B")]
    write_json(
        run_root / "find_results.json",
        {
            "run_id": "find_done",
            "strong_recommendations": recommendations,
            "articles": recommendations,
            "recommendation_target_count": 2,
            "recommendation_shortfall": 0,
        },
    )
    write_json(run_root / "find_progress.json", {"run_id": "find_done", "recommendation_target_count": 2, "recommendation_shortfall": 0})
    validation = {
        "run_id": "find_done",
        "valid": True,
        "status": "current_find_reading_validation_pass",
        "expected_recommendation_count": 2,
        "actual_reading_count": 2,
        "full_text_reading_count": 2,
        "full_text_evidence_count": 2,
        "pending_full_text_reading_count": 0,
        "pending_without_evidence_count": 0,
        "blockers": [],
    }
    write_json(
        run_root / "read_results.json",
        {
            "run_id": "find_done",
            "source": "claude_code_current_find_takeover",
            "reading_validation": validation,
            "selected_idea_id": "idea-001",
            "selected_plan_id": "plan-001",
            "readings": [
                {"paper_id": "paper-a", "title": "Completed Recommended Paper A"},
                {"paper_id": "paper-b", "title": "Completed Recommended Paper B"},
            ],
        },
    )
    write_json(
        run_root / "ideas.json",
        {
            "run_id": "find_done",
            "source": "claude_code_current_find_takeover",
            "selected_idea_id": "idea-001",
            "selected_plan_id": "plan-001",
            "ideas": [{"id": "idea-001", "title": "Idea"}],
        },
    )
    write_json(
        run_root / "plans.json",
        {
            "run_id": "find_done",
            "source": "claude_code_current_find_takeover",
            "selected_idea_id": "idea-001",
            "selected_plan_id": "plan-001",
            "plans": [{"plan_id": "plan-001", "idea_id": "idea-001", "selected_for_execution": True}],
        },
    )
    write_text(run_root / "read.md", "# completed read")
    write_text(run_root / "idea.md", "# completed idea")
    write_text(run_root / "plan.md", "# completed plan")
    write_json(
        taste_dir / "full_text_reading" / "full_text_packet.json",
        {"run_id": "find_done", "papers": [{"paper_id": "paper-a", "title": "Completed Recommended Paper A", "text_path": "texts/a.txt", "text_chars": 24000}]},
    )
    write_text(taste_dir / "full_text_reading" / "texts" / "a.txt", "paper body" * 4000)
    write_json(state_dir / "current_find_claude_reading_validation.json", {"run_id": "find_done", "valid": False, "pending_full_text_reading_count": 2})
    monkeypatch.setattr(server, "run_dir", lambda _run_id: run_root)
    monkeypatch.setattr(server, "load_config", lambda: AppConfig(provider="openai", api_key="key", model="model", max_recommended_papers=2))

    receipt = server._adopt_find_run_for_project(root, project, "find_done", source="test_adoption")

    read_payload = read_json(taste_dir / "read_results.json", {})
    ideas_payload = read_json(taste_dir / "ideas.json", {})
    plans_payload = read_json(taste_dir / "plans.json", {})
    validation_payload = read_json(state_dir / "current_find_claude_reading_validation.json", {})
    plan_state = read_json(state_dir / "current_find_research_plan.json", {})
    packet = read_json(taste_dir / "full_text_reading" / "full_text_packet.json", {})

    assert receipt["status"] == "adopted"
    assert receipt["downstream_status"] == "completed_downstream_adopted"
    assert "read_results.json" in receipt["completed_downstream_copied"]
    assert receipt["stale_downstream_reset"] == []
    assert read_payload["source"] == "claude_code_current_find_takeover"
    assert len(read_payload["readings"]) == 2
    assert len(ideas_payload["ideas"]) == 1
    assert len(plans_payload["plans"]) == 1
    assert (taste_dir / "read.md").read_text(encoding="utf-8") == "# completed read"
    assert validation_payload["valid"] is True
    assert validation_payload["full_text_reading_count"] == 2
    assert plan_state["status"] == "claude_takeover_ready"
    assert plan_state["current_find_reading_count"] == 2
    assert plan_state["selected_plan_id"] == "plan-001"
    assert packet["run_id"] == "find_done"
    assert packet["papers"]
    assert (taste_dir / "full_text_reading" / "texts" / "a.txt").exists()


def test_find_adoption_preserves_existing_downstream_for_new_find(tmp_path, monkeypatch):
    project = 'demo_project'
    root = tmp_path / 'projects' / project
    taste_dir = root / 'planning' / 'finding'
    state_dir = root / 'state'
    run_root = tmp_path / 'runs' / 'find_new'
    stale_text_dir = taste_dir / 'full_text_reading' / 'texts'
    stale_fragment_dir = taste_dir / 'current_find_deep_read_fragments'
    stale_text_dir.mkdir(parents=True)
    stale_fragment_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    run_root.mkdir(parents=True)
    write_text(stale_text_dir / 'old.txt', 'old full text')
    write_json(stale_fragment_dir / 'old_fragment.json', {'run_id': 'find_old', 'reading': {'title': 'Old Paper'}})
    write_json(
        taste_dir / 'full_text_reading' / 'full_text_packet.json',
        {'run_id': 'find_old', 'papers': [{'paper_id': 'old', 'title': 'Old Paper', 'text_path': 'texts/old.txt', 'text_chars': 50000}]},
    )
    write_json(taste_dir / 'read_results.json', {'run_id': 'find_old', 'source': 'claude_code_current_find_takeover', 'readings': [{'paper_id': 'old'}]})
    write_json(taste_dir / 'ideas.json', {'run_id': 'find_old', 'source': 'claude_code_current_find_takeover', 'ideas': [{'id': 'old-idea'}]})
    write_json(taste_dir / 'plans.json', {'run_id': 'find_old', 'source': 'claude_code_current_find_takeover', 'plans': [{'plan_id': 'old-plan'}]})
    recommendations = [recommended_paper('paper-new', 'New Recommended Paper')]
    write_json(
        run_root / 'find_results.json',
        {
            'run_id': 'find_new',
            'strong_recommendations': recommendations,
            'articles': recommendations,
            'recommendation_target_count': 1,
            'recommendation_shortfall': 0,
        },
    )
    write_json(run_root / 'find_progress.json', {'recommendation_target_count': 1, 'recommendation_shortfall': 0})
    monkeypatch.setattr(server, 'run_dir', lambda _run_id: run_root)
    monkeypatch.setattr(server, 'load_config', lambda: AppConfig(provider='openai', api_key='key', model='model', max_recommended_papers=1))

    receipt = server._adopt_find_run_for_project(root, project, 'find_new', source='test_adoption')

    packet = read_json(taste_dir / 'full_text_reading' / 'full_text_packet.json', {})
    read_payload = read_json(taste_dir / 'read_results.json', {})
    ideas_payload = read_json(taste_dir / 'ideas.json', {})
    plans_payload = read_json(taste_dir / 'plans.json', {})
    plan_state = read_json(root / 'state' / 'current_find_research_plan.json', {})
    assert receipt['status'] == 'adopted'
    assert receipt['downstream_status'] == 'existing_downstream_preserved_pending_current_find_read'
    assert packet['run_id'] == 'find_old'
    assert packet['papers']
    assert read_payload['run_id'] == 'find_old'
    assert ideas_payload['run_id'] == 'find_old'
    assert plans_payload['run_id'] == 'find_old'
    assert plan_state['run_id'] == 'find_new'
    assert plan_state['status'] == 'pending_current_find_read'
    assert plan_state['downstream_status'] == 'existing_downstream_preserved_pending_current_find_read'
    assert plan_state['preserved_downstream'] is True
    assert receipt['stale_downstream_reset'] == []
    assert (taste_dir / 'full_text_reading' / 'texts' / 'old.txt').exists()
    assert (stale_fragment_dir / 'old_fragment.json').exists()
    assert (root / 'state' / 'current_find_research_plan.json').exists()


def test_find_adoption_copies_find_level_full_text_packet_for_new_find(tmp_path, monkeypatch):
    project = "demo_project"
    root = tmp_path / "projects" / project
    taste_dir = root / "planning" / "finding"
    state_dir = root / "state"
    run_root = tmp_path / "runs" / "find_new"
    (taste_dir / "full_text_reading" / "texts").mkdir(parents=True)
    (run_root / "full_text_reading" / "texts").mkdir(parents=True)
    state_dir.mkdir(parents=True)
    write_text(taste_dir / "full_text_reading" / "texts" / "old.txt", "old full text")
    write_json(taste_dir / "full_text_reading" / "full_text_packet.json", {"run_id": "find_old", "papers": [{"paper_id": "old", "text_path": "texts/old.txt", "text_chars": 9000}]})
    recommendations = [recommended_paper("paper-new", "New Recommended Paper")]
    write_json(
        run_root / "find_results.json",
        {
            "run_id": "find_new",
            "strong_recommendations": recommendations,
            "articles": recommendations,
            "recommendation_target_count": 1,
            "recommendation_shortfall": 0,
        },
    )
    write_json(run_root / "find_progress.json", {"recommendation_target_count": 1, "recommendation_shortfall": 0})
    write_json(
        run_root / "full_text_reading" / "full_text_packet.json",
        {"run_id": "find_new", "papers": [{"paper_id": "paper-new", "title": "New Recommended Paper", "text_path": "texts/new.txt", "text_chars": 9000}]},
    )
    write_text(run_root / "full_text_reading" / "texts" / "new.txt", "new full text" * 1000)
    monkeypatch.setattr(server, "run_dir", lambda _run_id: run_root)
    monkeypatch.setattr(server, "load_config", lambda: AppConfig(provider="openai", api_key="key", model="model", max_recommended_papers=1))

    receipt = server._adopt_find_run_for_project(root, project, "find_new", source="test_adoption")

    packet = read_json(taste_dir / "full_text_reading" / "full_text_packet.json", {})
    validation = read_json(state_dir / "current_find_claude_reading_validation.json", {})
    plan_state = read_json(state_dir / "current_find_research_plan.json", {})
    assert receipt["status"] == "adopted"
    assert receipt["downstream_status"] == "find_full_text_evidence_ready_pending_current_find_read"
    assert "full_text_reading/" in receipt["copied"]
    assert packet["run_id"] == "find_new"
    assert packet["papers"][0]["paper_id"] == "paper-new"
    assert (taste_dir / "full_text_reading" / "texts" / "new.txt").exists()
    assert not (taste_dir / "full_text_reading" / "texts" / "old.txt").exists()
    assert validation["run_id"] == "find_new"
    assert validation["pending_full_text_reading_count"] == 0
    assert validation["full_text_evidence_count"] == 1
    assert plan_state["reading_validation"]["pending_full_text_reading_count"] == 0
    assert plan_state["reading_validation"]["full_text_evidence_count"] == 1



def test_full_text_unavailable_repair_does_not_rewrite_find_topn(tmp_path):
    import sys
    from types import SimpleNamespace

    ensure_script_paths()
    repair = importlib.import_module("repair_current_find_full_text_evidence")

    project = "demo_project"
    root = tmp_path / "projects" / project
    taste_dir = root / "planning" / "finding"
    state_dir = root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    paths = SimpleNamespace(name=project, root=root, planning=root / "planning", state=state_dir)

    readable = {**recommended_paper("paper-readable", "Readable Current Paper"), "pdf_url": "https://openreview.net/pdf?id=readable"}
    unreadable = {**recommended_paper("paper-unreadable", "Unreadable Current Paper"), "url": "https://doi.org/10.1145/unreadable", "pdf_url": ""}
    extra_candidate = {
        **recommended_paper("paper-extra", "Readable Non Recommended Candidate"),
        "fit_score": 7.5,
        "llm_fit_score": 7.5,
        "score": 7.5,
        "url": "https://openreview.net/forum?id=extra",
        "pdf_url": "https://openreview.net/pdf?id=extra",
    }
    for key in ["find_recommendation", "recommended_by_llm_ranking", "_user_visible_recommendation"]:
        extra_candidate.pop(key, None)

    find_results = {
        "run_id": "find_demo",
        "strong_recommendations": [readable, unreadable],
        "articles": [readable, unreadable],
        "read_candidates": [readable, unreadable],
        "screened_ranking": [readable, unreadable, extra_candidate],
        "evaluated_candidates": [readable, unreadable, extra_candidate],
        "recommendation_target_count": 2,
        "recommendation_shortfall": 0,
        "scoring_runtime": {"recommendation_target_count": 2},
    }
    packet = {
        "run_id": "find_demo",
        "papers": [
            {"paper_id": "paper-readable", "title": "Readable Current Paper", "text_path": "texts/readable.txt", "text_chars": 24000},
            {"paper_id": "paper-unreadable", "title": "Unreadable Current Paper", "text_path": "", "text_chars": 0},
        ],
    }
    write_json(taste_dir / "find_results.json", find_results)
    write_json(taste_dir / "full_text_reading" / "full_text_packet.json", packet)
    write_json(taste_dir / "read_results.json", {"run_id": "find_demo", "source": "claude_code_current_find_takeover", "readings": [{"title": "stale"}]})

    receipt = repair.record_unavailable_full_text_evidence_blocker(paths, find_results, packet, [{"title": "Unreadable Current Paper"}])

    current_find = read_json(taste_dir / "find_results.json", {})
    current_titles = [row["title"] for row in current_find["strong_recommendations"]]
    packet_after = read_json(taste_dir / "full_text_reading" / "full_text_packet.json", {})
    read_after = read_json(taste_dir / "read_results.json", {})
    blocker_receipt = read_json(state_dir / "current_find_full_text_unavailable_read_stage_blocker.json", {})

    assert receipt["status"] == "read_stage_full_text_unavailable_recorded"
    assert receipt["unavailable_count"] == 1
    assert "must not rewrite" in receipt["policy"]
    assert current_titles == ["Readable Current Paper", "Unreadable Current Paper"]
    assert current_find["articles"][1]["title"] == "Unreadable Current Paper"
    assert current_find["read_candidates"][1]["title"] == "Unreadable Current Paper"
    assert current_find["screened_ranking"][2]["title"] == "Readable Non Recommended Candidate"
    assert "find_recommendation" not in current_find["screened_ranking"][2]
    assert current_find["recommendation_shortfall"] == 0
    assert read_after["source"] == "claude_code_current_find_takeover"
    packet_titles = [row["title"] for row in packet_after["papers"]]
    assert packet_titles == ["Readable Current Paper", "Unreadable Current Paper"]
    assert not (state_dir / "current_find_recommendation_projection.json").exists()
    assert blocker_receipt["unavailable_count"] == 1
    assert blocker_receipt["unavailable_titles"] == ["Unreadable Current Paper"]
    assert "must not rewrite" in blocker_receipt["policy"]



def test_full_text_repair_uses_same_run_replacement_without_rewriting_find(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    ensure_script_paths()
    repair = importlib.import_module("repair_current_find_full_text_evidence")

    project = "demo_project"
    root = tmp_path / "projects" / project
    taste_dir = root / "planning" / "finding"
    state_dir = root / "state"
    text_dir = taste_dir / "full_text_reading" / "texts"
    text_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    paths = SimpleNamespace(name=project, root=root, planning=root / "planning", state=state_dir)

    readable = {**recommended_paper("paper-readable", "Readable Current Paper"), "pdf_url": "https://openreview.net/pdf?id=readable", "abstract": "Readable abstract"}
    unreadable = {**recommended_paper("paper-unreadable", "Unreadable Current Paper"), "url": "https://example.test/unreadable", "pdf_url": "", "abstract": "Missing abstract"}
    replacement = {
        **recommended_paper("paper-extra", "Readable Same Run Candidate"),
        "url": "https://openreview.net/forum?id=extra",
        "pdf_url": "https://openreview.net/pdf?id=extra",
        "abstract": "Replacement abstract with enough topic evidence.",
        "topic_evidence_supported": True,
        "evidence_tier": "final_llm_scored_candidate",
        "score": 7.7,
    }
    find_results = {
        "run_id": "find_demo",
        "strong_recommendations": [readable, unreadable],
        "articles": [readable, unreadable],
        "read_candidates": [readable, unreadable],
        "screened_ranking": [readable, unreadable, replacement],
        "recommendation_target_count": 2,
        "recommendation_shortfall": 0,
    }
    repair.save_json(taste_dir / "find_results.json", find_results)
    repair.save_json(state_dir / "current_find_claude_reading_validation.json", {"run_id": "find_demo", "generated_at": "2026-06-10T00:00:00+00:00", "pending_without_evidence_titles": ["Unreadable Current Paper"]})

    def fake_paths(_project):
        return paths

    def fake_acquire(_paths, paper, rank):
        title = paper.get("title")
        if title == "Readable Same Run Candidate":
            text_path = text_dir / "replacement.txt"
            text_path.write_text("Readable Same Run Candidate abstract introduction method experiments evaluation results conclusion references " * 500, encoding="utf-8")
            return {
                "source": "test",
                "kind": "indexed_pdf_text_read",
                "pdf_url": paper.get("pdf_url"),
                "text_path": str(text_path.relative_to(root)),
                "text_chars": 50000,
                "page_count": 12,
                "full_text_status": "indexed_pdf_text_read",
            }, [{"kind": "mock", "accepted": True}]
        return None, [{"kind": "mock", "accepted": False}]

    monkeypatch.setattr(repair, "build_paths", fake_paths)
    monkeypatch.setattr(repair, "try_acquire_for_paper", fake_acquire)

    rc, receipt = repair.repair_current_find_full_text_evidence(project, force=True)

    current_find = repair.load_json(taste_dir / "find_results.json", {})
    packet = repair.load_json(taste_dir / "full_text_reading" / "full_text_packet.json", {})
    replacement_entries = [row for row in packet.get("papers", []) if row.get("read_replacement")]

    assert rc == 0
    assert receipt["status"] == "repaired_full_text_evidence_with_read_stage_replacements"
    assert receipt["original_unavailable_count"] == 1
    assert receipt["unavailable_count"] == 0
    assert receipt["replacement_acquired_count"] == 1
    assert receipt["replaced_unavailable_recommendation_titles"] == ["Unreadable Current Paper"]
    assert [row["title"] for row in current_find["strong_recommendations"]] == ["Readable Current Paper", "Unreadable Current Paper"]
    assert [row["title"] for row in current_find["read_candidates"]] == ["Readable Current Paper", "Unreadable Current Paper"]
    assert replacement_entries and replacement_entries[0]["title"] == "Readable Same Run Candidate"
    assert replacement_entries[0]["replacement_for_unavailable_recommendation"] == "Unreadable Current Paper"

def test_full_text_repair_accepts_high_author_overlap_preprint_title_variant():
    import sys

    ensure_script_paths()
    repair = importlib.import_module("repair_current_find_full_text_evidence")

    paper = {
        "title": "Efficient, Property-Aligned Fan-Out Retrieval via RL-Amortized Diffusion",
        "authors": "Pengcheng Jiang, Judith Li, Moonkyung Ryu, Lily Hu, Kun Su, Zhong Yi Wan, Liam Hebert, Hao Peng, Jiawei Han, Dima Kuzmin, Craig Boutilier",
    }

    accepted = repair.title_author_match_details(
        paper,
        "Efficient, Property-Aligned Fan-Out Retrieval via RL-Compiled Diffusion",
        ["Pengcheng Jiang", "Judith Li", "Moonkyung Ryu", "Lily Hu", "Kun Su", "Zhong Yi Wan", "Liam Hebert", "Hao Peng", "Jiawei Han", "Dima Kuzmin", "Craig Boutilier"],
    )
    rejected = repair.title_author_match_details(
        paper,
        "Efficient Human-in-the-Loop Optimization via Priors Learned from User Models",
        [],
    )

    assert accepted["accepted"] is True
    assert accepted["title_similarity"] >= 0.8
    assert len(accepted["author_overlap"]) >= 3
    assert rejected["accepted"] is False



def test_claude_project_session_stage_keys_are_panel_specific():
    import importlib
    import sys
    from pathlib import Path

    ensure_script_paths()
    session = importlib.import_module("claude_project_session")

    assert session.session_key_for("main", "environment") == "environment"
    assert session.session_key_for("main", "experiment") == "experiment"
    assert session.session_key_for("main", "paper") == "paper"
    assert session.session_key_for("main", "current-find-claude-read-idea-plan") == "main"
    assert session.session_key_for("main", "writing:revision") == "writing_revision"




def test_claude_project_session_runs_inside_project_root():
    source = (Path(__file__).resolve().parents[1] / "framework" / "scripts" / "claude_project_session.py").read_text(encoding="utf-8")

    assert "cwd=paths.root" in source
    assert "cwd=ROOT" not in source

def test_claude_status_payload_exposes_stage_specific_receipts(tmp_path):
    from auto_research.web import project_bridge

    root = tmp_path / "project"
    state = root / "state"
    reports = root / "reports"
    state.mkdir(parents=True)
    reports.mkdir(parents=True)

    write_json(state / "claude_project_session_last_result.json", {
        "status": "completed",
        "stage": "experiment",
        "return_code": 0,
        "finished_at": "2026-06-09T01:00:00+00:00",
        "session_id": "main-session",
        "stdout": "Claude: main experiment reply",
    })
    write_json(state / "claude_project_session_last_result_environment.json", {
        "status": "completed",
        "stage": "environment",
        "return_code": 0,
        "finished_at": "2026-06-09T01:01:00+00:00",
        "session_id": "env-session",
        "stdout": "Claude: environment reply",
    })
    write_json(state / "claude_project_session_last_result_paper.json", {
        "status": "completed",
        "stage": "paper",
        "return_code": 0,
        "finished_at": "2026-06-09T01:02:00+00:00",
        "session_id": "paper-session",
        "stdout": "Claude: paper reply",
    })

    payload = project_bridge._claude_status_payload(root)
    by_stage = payload["latest_receipt_by_stage"]

    assert by_stage["environment"]["stage_session_key"] == "environment"
    assert by_stage["environment"]["stage_local"] is True
    assert by_stage["paper"]["stage_session_key"] == "paper"
    assert by_stage["paper"]["stage_local"] is True
    assert by_stage["experiment"]["stage_session_key"] == "main"
    assert by_stage["experiment"]["stage_local"] is False
    assert by_stage["experiment"]["fallback_from_session_key"] == "main"
    assert payload["latest_receipt"]["stage"] == "experiment"


def test_claude_latest_response_payload_uses_stage_specific_result(tmp_path):
    root = tmp_path / "project"
    state = root / "state"
    reports = root / "reports"
    state.mkdir(parents=True)
    reports.mkdir(parents=True)

    write_json(state / "claude_project_session_last_result.json", {
        "status": "completed",
        "stage": "experiment",
        "finished_at": "2026-06-09T01:00:00+00:00",
        "session_id": "main-session",
        "response_markdown": "main experiment full reply",
    })
    write_json(state / "claude_project_session_last_result_paper.json", {
        "status": "completed",
        "stage": "paper",
        "finished_at": "2026-06-09T01:02:00+00:00",
        "session_id": "paper-session",
        "response_markdown": "paper full reply",
    })

    paper = server._latest_claude_response_payload(root, stage="paper", max_chars=1000)
    experiment = server._latest_claude_response_payload(root, stage="experiment", max_chars=1000)

    assert paper["response_markdown"] == "paper full reply"
    assert paper["stage_session_key"] == "paper"
    assert paper["stage_local"] is True
    assert experiment["response_markdown"] == "main experiment full reply"
    assert experiment["fallback_from_session_key"] == "main"
    assert experiment["fallback_reason"] == "historical_global_receipt_for_same_stage"


def test_experiment_latest_response_prefers_current_route_global_receipt(tmp_path):
    root = tmp_path / "project"
    state = root / "state"
    state.mkdir(parents=True)

    write_json(state / "claude_project_session_last_result.json", {
        "status": "completed",
        "stage": "current-find-claude-read-idea-plan",
        "session_key": "main",
        "finished_at": "2026-06-09T03:00:00+00:00",
        "session_id": "current-route-session",
        "claude_json": {"result": "current Find TA-Rec plan and experiment gate reply"},
    })
    write_json(state / "claude_project_session_last_result_trajectory_explore-9.json", {
        "status": "completed",
        "stage": "trajectory",
        "finished_at": "2026-06-04T03:00:00+00:00",
        "session_id": "old-trajectory-session",
        "claude_json": {"result": "stale CHIANGEL AlphaFuse trajectory reply"},
    })

    experiment = server._latest_claude_response_payload(root, stage="experiment", max_chars=1000)

    assert experiment["stage_session_key"] == "main"
    assert experiment["fallback_from_session_key"] == "main"
    assert experiment["fallback_reason"] == "current_route_global_receipt_for_experiment"
    assert "current Find TA-Rec" in experiment["response_markdown"]
    assert "stale CHIANGEL" not in experiment["response_markdown"]


def test_claude_status_payload_prefers_current_route_experiment_receipt(tmp_path):
    from auto_research.web import project_bridge

    root = tmp_path / "project"
    state = root / "state"
    state.mkdir(parents=True)

    current_reply = "current Find TA-Rec plan and experiment gate reply"
    write_json(state / "claude_project_session_last_result.json", {
        "status": "completed",
        "stage": "current-find-claude-read-idea-plan",
        "session_key": "main",
        "return_code": 0,
        "finished_at": "2026-06-09T03:00:00+00:00",
        "session_id": "current-route-session",
        "claude_json": {"result": current_reply},
    })
    write_json(state / "claude_project_session_last_result_trajectory_explore-9.json", {
        "status": "completed",
        "stage": "trajectory",
        "return_code": 0,
        "finished_at": "2026-06-04T03:00:00+00:00",
        "session_id": "old-trajectory-session",
        "claude_json": {"result": "stale CHIANGEL AlphaFuse trajectory reply"},
    })

    payload = project_bridge._claude_status_payload(root)
    experiment = payload["latest_receipt_by_stage"]["experiment"]

    assert experiment["stage_session_key"] == "main"
    assert experiment["stage_local"] is False
    assert experiment["fallback_from_session_key"] == "main"
    assert experiment["fallback_reason"] == "current_route_global_receipt_for_experiment"
    assert experiment["response_chcount"] == len(current_reply)
    assert experiment["stage"] == "current-find-claude-read-idea-plan"


def test_paper_latest_response_filters_receipts_for_previous_venue(tmp_path):
    root = tmp_path / "project"
    state = root / "state"
    state.mkdir(parents=True)

    write_json(root / "project.json", {"target_venue": "ICLR", "venue": "ICLR", "paper": {"venue_slug": "iclr"}})
    write_json(state / "claude_project_session_last_result_writing_revision.json", {
        "status": "completed",
        "stage": "paper",
        "finished_at": "2026-06-09T02:00:00+00:00",
        "session_id": "nature-session",
        "claude_json": {"result": "Updated paper/output/nature/paper_text.txt after Nature self review."},
    })
    write_json(state / "claude_project_session_last_result_paper_preview_repair.json", {
        "status": "completed",
        "stage": "paper-preview-repair",
        "finished_at": "2026-06-09T01:00:00+00:00",
        "session_id": "iclr-session",
        "claude_json": {"result": "Updated paper/output/iclr/paper.pdf after ICLR preview repair."},
    })

    paper = server._latest_claude_response_payload(root, stage="paper", max_chars=1000)

    assert paper["stage_session_key"] == "paper_preview_repair"
    assert "paper/output/iclr/paper.pdf" in paper["response_markdown"]
    assert "paper/output/nature" not in paper["response_markdown"]


def test_claude_status_payload_filters_paper_receipts_for_previous_venue(tmp_path):
    from auto_research.web import project_bridge

    root = tmp_path / "project"
    state = root / "state"
    state.mkdir(parents=True)

    write_json(root / "project.json", {"target_venue": "ICLR", "venue": "ICLR", "paper": {"venue_slug": "iclr"}})
    write_json(state / "claude_project_session_last_result_writing_revision.json", {
        "status": "completed",
        "stage": "paper",
        "return_code": 0,
        "finished_at": "2026-06-09T02:00:00+00:00",
        "session_id": "nature-session",
        "claude_json": {"result": "Updated paper/output/nature/paper_text.txt after Nature self review."},
    })
    write_json(state / "claude_project_session_last_result_writing_refinement.json", {
        "status": "completed",
        "stage": "writing:refinement",
        "return_code": 0,
        "finished_at": "2026-06-09T01:00:00+00:00",
        "session_id": "iclr-session",
        "claude_json": {"result": "Updated paper/output/iclr/paper.pdf after ICLR refinement."},
    })

    payload = project_bridge._claude_status_payload(root)
    paper = payload["latest_receipt_by_stage"]["paper"]

    assert paper["stage_session_key"] == "writing_refinement"
    assert paper["stage_local"] is True
    assert paper["fallback_reason"] in (None, "")
    assert "nature" not in paper["response_markdown"].lower()


def test_frontend_taskbar_help_does_not_advertise_commands_or_paths():
    app = Path("web/frontend/client/src/App.tsx").read_text(encoding="utf-8")

    assert "命令和产物路径" not in app
    assert "logs, commands, and artifact paths" not in app
    assert "阶段、进度、日志和产物状态" in app


def test_frontend_claude_panels_use_stage_specific_latest_responses():
    from pathlib import Path

    app = Path("web/frontend/client/src/App.tsx").read_text(encoding="utf-8")
    api = Path("web/frontend/client/src/api.ts").read_text(encoding="utf-8")

    assert "latest_receipt_by_stage" in app
    assert "latestClaudeReceiptForStage(stage)" in app
    assert "latestClaudeReceiptForPanel" not in app
    assert "latestClaudeFullResponseKey" not in app
    assert "getClaudeLatestResponse(projectId, stage)" in app
    assert "loadClaudeFullResponse(item.fullResponseKey, item.fullResponseStage || stage)" in app
    assert '<details className="transcriptBox" open>' in app
    assert '<p>{t.noClaudeTranscript}</p><p className="help">{logRedirectHelp}</p>' in app
    assert 'noClaudeTranscript: "还没有项目代理处理摘要' in app
    assert 'noClaudeTranscript: "No project-agent processing summary yet' in app
    assert "stage = \"\"" in api
    assert "params.set(\"stage\", stage)" in api
    assert 'const stages: ("environment" | "experiment" | "paper")[] = ["environment", "experiment", "paper"];' in app
    assert "latestClaudeFullResponseRequests" in app
    assert "jobMatchesClaudePanelStage(item, stage)" in app
    assert "function jobPanelStage(job: any)" in app
    assert "result.panel_stage" in app
    assert "result.requested_stage" in app
    assert 'const explicitPanelStage = isClaudeGuidanceJob(job) ? jobPanelStage(job) : "";' in app
    assert 'attachJob(nextJob, stage === "paper" ? "paperWrite" : stage === "experiment" ? "experiment" : "environment");' in app
    assert 'if (action === "claude-message")' not in app
    assert "const guidanceJob = jobs.find((item) => {" not in app


def test_public_config_response_redacts_saved_secrets(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    project_path = tmp_path / "project.json"
    write_json(project_path, {"llm": {"api_base": "https://llm.example/v1", "model": "gpt-demo", "api_key": "project-secret"}})
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "project_config_path", lambda: project_path)

    config = AppConfig(
        provider="openai_compatible",
        base_url="https://llm.example/v1",
        api_key="test-main-secret-1234",
        model="gpt-demo",
        llm_roles={"find": {"provider": "openai", "api_key": "role-secret-5678", "model": "role-model"}},
        email={
            "smtp_server": "smtp.example.test",
            "sender": "sender@example.test",
            "receivers": ["receiver@example.test"],
            "smtp_password": "smtp-secret",
        },
    )

    payload = server._public_config_response(config)
    rendered = json.dumps(payload, ensure_ascii=False)

    assert payload["api_key"] == ""
    assert payload["api_key_saved"] is True
    assert payload["api_key_suffix"] == "1234"
    assert payload["config_path"] == ""
    assert payload["project_llm_synced"] is True
    assert payload["llm_roles"]["find"]["api_key"] == ""
    assert payload["llm_roles"]["find"]["api_key_saved"] is True
    assert payload["llm_roles"]["find"]["api_key_suffix"] == "5678"
    assert payload["email"]["smtp_password"] == ""
    assert payload["email"]["smtp_password_saved"] is True
    assert "test-main-secret" not in rendered
    assert "role-secret" not in rendered
    assert "smtp-secret" not in rendered
    assert "project-secret" not in rendered


def test_public_text_normalizes_stale_sibling_workspace_paths():
    old_name = "".join(["A", "R"])
    old_root = server.WORKSPACE_ROOT.parent / old_name
    legacy_find_dir = "ar" + "_finding"
    stale = f"artifact path {old_root}/projects/demo_project/planning/{legacy_find_dir}/plan.md\n当前 {old_name} 项目\n{old_name} 运行环境"

    rendered = server._public_text(stale)

    assert str(old_root) not in rendered
    assert str(server.WORKSPACE_ROOT / "projects" / "demo_project" / "planning" / "finding" / "plan.md") in rendered
    assert f"当前 {old_name} 项目" not in rendered
    assert f"{old_name} 运行环境" not in rendered


def test_project_bridge_public_names_normalizes_stale_sibling_workspace_paths():
    from auto_research.web import project_bridge

    old_name = "".join(["A", "R"])
    old_root = project_bridge.ROOT.parent / old_name
    stale = f"artifact={old_root}/runtime/runs/find_demo/read.md"

    rendered = project_bridge._public_internal_names(stale)

    assert str(old_root) not in rendered
    assert str(project_bridge.ROOT / "runtime" / "runs" / "find_demo" / "read.md") in rendered


def test_api_save_config_preserves_saved_secrets_and_never_writes_project_api_key(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    project_path = tmp_path / "project.json"
    write_json(project_path, {"name": "demo", "llm": {"api_key": "old-project-secret", "api_key_env": "OPENAI_API_KEY"}})
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "project_config_path", lambda: project_path)

    initial = AppConfig(
        provider="openai_compatible",
        base_url="https://old.example/v1",
        api_key="test-old-main-9999",
        model="old-model",
        llm_roles={"find": {"provider": "openai", "api_key": "test-old-role-8888", "model": "old-role"}},
        email={
            "smtp_server": "smtp.old.example",
            "sender": "old@example.test",
            "receivers": ["old@example.test"],
            "smtp_password": "old-smtp-secret",
        },
    )
    server.save_config(initial)

    response = server.api_save_config(AppConfig(
        provider="openai_compatible",
        base_url="https://new.example/v1",
        api_key="",
        model="new-model",
        llm_roles={"find": {"provider": "openai", "api_key": "", "model": "new-role"}},
        email={
            "smtp_server": "smtp.new.example",
            "sender": "new@example.test",
            "receivers": ["new@example.test"],
            "smtp_password": "",
        },
    ))

    stored = read_json(cfg_path, {})
    project = read_json(project_path, {})
    rendered_response = json.dumps(response, ensure_ascii=False)

    assert stored["api_key"] == "test-old-main-9999"
    assert stored["llm_roles"]["find"]["api_key"] == "test-old-role-8888"
    assert stored["email"]["smtp_password"] == "old-smtp-secret"
    assert stored["base_url"] == "https://new.example/v1"
    assert stored["model"] == "new-model"
    assert project["llm"]["api_base"] == "https://new.example/v1"
    assert project["llm"]["model"] == "new-model"
    assert project["llm"]["api_key_env"] == "OPENAI_API_KEY"
    assert "api_key" not in project["llm"]
    assert response["api_key"] == ""
    assert response["api_key_saved"] is True
    assert response["llm_roles"]["find"]["api_key"] == ""
    assert response["email"]["smtp_password"] == ""
    assert "sk-old" not in rendered_response
    assert "old-smtp-secret" not in rendered_response
    assert "old-project-secret" not in json.dumps(project, ensure_ascii=False)


def test_claude_latest_response_payload_uses_stage_local_environment_and_experiment_results(tmp_path):
    root = tmp_path / "project"
    state = root / "state"
    state.mkdir(parents=True)
    write_json(state / "claude_project_session_last_result.json", {"status": "completed", "stage": "paper", "response_markdown": "global paper reply"})
    write_json(state / "claude_project_session_last_result_environment.json", {"status": "completed", "stage": "environment", "response_markdown": "environment full reply"})
    write_json(state / "claude_project_session_last_result_experiment.json", {"status": "completed", "stage": "experiment", "response_markdown": "experiment full reply"})

    environment = server._latest_claude_response_payload(root, stage="environment", max_chars=1000)
    experiment = server._latest_claude_response_payload(root, stage="experiment", max_chars=1000)

    assert environment["response_markdown"] == "environment full reply"
    assert environment["stage_session_key"] == "environment"
    assert environment["stage_local"] is True
    assert environment["fallback_reason"] == ""
    assert experiment["response_markdown"] == "experiment full reply"
    assert experiment["stage_session_key"] == "experiment"
    assert experiment["stage_local"] is True


def test_claude_experiment_latest_response_scans_trajectory_receipts(tmp_path):
    from auto_research.web import project_bridge

    root = tmp_path / "project"
    state = root / "state"
    state.mkdir(parents=True)
    write_json(state / "claude_project_session_last_result.json", {
        "status": "completed",
        "stage": "experiment",
        "finished_at": "2026-06-09T01:00:00+00:00",
        "claude_json": {"result": "global experiment reply"},
    })
    write_json(state / "claude_project_session_last_result_paper.json", {
        "status": "completed",
        "stage": "paper",
        "finished_at": "2026-06-09T04:00:00+00:00",
        "claude_json": {"result": "paper reply must not appear in experiment"},
    })
    write_json(state / "claude_project_session_last_result_trajectory_explore-9.json", {
        "status": "completed",
        "stage": "trajectory-explore",
        "return_code": 0,
        "finished_at": "2026-06-09T03:00:00+00:00",
        "session_id": "trajectory-session",
        "claude_json": {"result": "trajectory experiment full reply"},
    })

    response = server._latest_claude_response_payload(root, stage="experiment", max_chars=1000)
    status = project_bridge._claude_status_payload(root)["latest_receipt_by_stage"]["experiment"]

    assert response["response_markdown"] == "trajectory experiment full reply"
    assert response["stage_session_key"] == "trajectory_explore-9"
    assert response["stage_local"] is True
    assert response["fallback_reason"] == ""
    assert "paper reply" not in response["response_markdown"]
    assert status["stage_session_key"] == "trajectory_explore-9"
    assert status["stage_local"] is True
    assert status["full_response_available"] is True
    assert status["response_chcount"] == len("trajectory experiment full reply")


def test_claude_experiment_latest_response_keeps_main_as_fallback_only(tmp_path):
    from auto_research.web import project_bridge

    root = tmp_path / "project"
    state = root / "state"
    state.mkdir(parents=True)
    write_json(state / "claude_project_session_last_result.json", {
        "status": "completed",
        "stage": "experiment",
        "return_code": 0,
        "finished_at": "2026-06-09T01:00:00+00:00",
        "session_id": "main-session",
        "claude_json": {"result": "main experiment fallback reply"},
    })

    response = server._latest_claude_response_payload(root, stage="experiment", max_chars=1000)
    status = project_bridge._claude_status_payload(root)["latest_receipt_by_stage"]["experiment"]

    assert response["response_markdown"] == "main experiment fallback reply"
    assert response["stage_session_key"] == "main"
    assert response["stage_local"] is False
    assert response["fallback_from_session_key"] == "main"
    assert response["fallback_reason"] == "historical_global_receipt_for_same_stage"
    assert status["stage_session_key"] == "main"
    assert status["stage_local"] is False
    assert status["fallback_from_session_key"] == "main"


def test_project_search_queries_ignore_project_id_and_use_find_context(tmp_path, monkeypatch):
    import importlib.util
    from types import SimpleNamespace

    module_path = Path(__file__).resolve().parents[1] / 'modules' / 'environment' / 'scripts' / 'run_environment_stage.py'
    spec = importlib.util.spec_from_file_location('run_environment_stage_test', module_path)
    env_stage = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(env_stage)

    planning = tmp_path / 'planning' / 'finding'
    reports = tmp_path / 'reports'
    state = tmp_path / 'state'
    planning.mkdir(parents=True)
    reports.mkdir(parents=True)
    state.mkdir(parents=True)
    write_json(planning / 'find_results.json', {
        'stage0_profile': {'profile': {'explicit_profile': {'research_interest_summary': 'evaluate autonomous scientific workflow agents with real benchmark traces'}}},
        'recommended_papers': [{'title': 'Scientific Agent Evaluation Benchmark'}],
    })
    write_json(planning / 'plans.json', {'plans': [{'title': 'Trace-based workflow evaluation', 'selected_for_execution': True}]})
    write_json(reports / 'repo_topic_fit_decision.json', {
        'stewardship_memory': "Priority search terms: 'scientific agent evaluation', 'LLM science benchmark'.",
        'rationale': "If no repo is good enough, say 'needs-more-search'. Current search found 8 candidates, none data-ready.",
    })

    monkeypatch.setattr(env_stage, 'load_project_config', lambda _project: {'topic': 'demo_project'})
    monkeypatch.setattr(env_stage, 'build_paths', lambda _project: SimpleNamespace(planning=tmp_path / 'planning', reports=reports, state=state))

    queries = env_stage.project_search_queries('demo_project')

    assert 'demo_project' not in queries
    assert queries[0] == 'scientific agent evaluation'
    assert any('autonomous scientific workflow agents' in query for query in queries)
    assert 'LLM science benchmark' in queries
    assert all('needs-more-search' not in query.lower() for query in queries)
    assert all('current search found' not in query.lower() for query in queries)
    assert all('none data-ready' not in query.lower() for query in queries)


def test_project_search_queries_use_current_find_artifact_shape(tmp_path, monkeypatch):
    import importlib.util
    from types import SimpleNamespace

    module_path = Path(__file__).resolve().parents[1] / 'modules' / 'environment' / 'scripts' / 'run_environment_stage.py'
    spec = importlib.util.spec_from_file_location('run_environment_stage_test_current_shape', module_path)
    env_stage = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(env_stage)

    planning = tmp_path / 'planning' / 'finding'
    reports = tmp_path / 'reports'
    state = tmp_path / 'state'
    planning.mkdir(parents=True)
    reports.mkdir(parents=True)
    state.mkdir(parents=True)
    write_json(planning / 'find_results.json', {
        'stage0_profile': {
            'profile': {'explicit_profile': {'research_interest_summary': 'demo_project demo_project'}},
            'retrieval_text': 'demo_project demo_project Research goal: demo_project demo_project The workflow should prioritize papers and ideas that directly help the current research loop.',
        },
        'articles': [
            {'title': 'FlowRL: Matching Reward Distributions for LLM Reasoning'},
            {'title': 'COMAL: A Convergent Meta-Algorithm for Aligning LLMs with General Preferences'},
        ],
    })
    write_json(planning / 'plans.json', {
        'selected_plan_id': 'plan-iteration-001',
        'plans': [
            {'plan_id': 'plan-iteration-001', 'title': 'FlowBalance-Align execution plan', 'selected_for_execution': True},
        ],
    })
    write_json(planning / 'ideas.json', {
        'selected_idea_id': 'idea-001',
        'ideas': [
            {'id': 'idea-001', 'title': 'FlowBalance-Align: reward distribution matching with game-theoretic preference alignment', 'selected_for_execution': True},
        ],
    })

    monkeypatch.setattr(env_stage, 'load_project_config', lambda _project: {'topic': 'demo_project', 'queries': ['demo_project']})
    monkeypatch.setattr(env_stage, 'build_paths', lambda _project: SimpleNamespace(planning=tmp_path / 'planning', reports=reports, state=state))

    queries = env_stage.project_search_queries('demo_project')

    assert queries[:4] == [
        'FlowRL: Matching Reward Distributions for LLM Reasoning code dataset',
        'COMAL: A Convergent Meta-Algorithm for Aligning LLMs with General Preferences code dataset',
        'FlowBalance-Align execution plan',
        'FlowBalance-Align: reward distribution matching with game-theoretic preference alignment',
    ]
    assert all(query != 'demo_project demo_project' for query in queries)
    assert all('research goal:' not in query.lower() for query in queries)
    assert all('current research loop' not in query.lower() for query in queries)


def test_same_phase_descendant_workers_are_folded_into_parent_row():
    rows = [
        {'pid': '100', 'ppid': '1', 'phase': 'environment', 'kind': 'environment_stage'},
        {'pid': '101', 'ppid': '100', 'phase': 'environment', 'kind': 'environment_stage'},
        {'pid': '102', 'ppid': '100', 'phase': 'paper', 'kind': 'paper_pipeline'},
    ]

    kept = server._suppress_same_phase_descendant_workers(rows)

    assert [row['pid'] for row in kept] == ['100', '102']


def test_environment_page_utility_jobs_map_to_environment_stage():
    assert server._public_taste_stage("healthcheck") == "environment"
    assert server._public_taste_stage("status") == "environment"
    assert server._public_taste_stage("init") == "environment"


def test_cancel_running_project_job_without_live_child_remains_cancelling(monkeypatch, tmp_path):
    JOBS.clear()
    monkeypatch.setattr(server, "JOBS_PATH", tmp_path / "web_jobs.json")
    monkeypatch.setattr(server, "_live_jobs_from_projects", lambda compact=False: [])
    monkeypatch.setattr(server, "_pid_from_project_worker_job_id", lambda _job_id: "")
    monkeypatch.setattr(server, "_project_from_job_payload", lambda _job_id, _live_job, _known_job: "")

    job = server.JobState("environment_demo", "environment")
    job.status = "running"
    JOBS[job.job_id] = job

    result = server.api_cancel_job(job.job_id)

    assert result["status"] == "cancelling"
    assert job.status == "cancelling"
    assert job.progress["phase"] == "cancelling"
    assert job.cancel_requested is True


def test_running_stage_job_hides_synthetic_project_worker(monkeypatch, tmp_path):
    JOBS.clear()
    monkeypatch.setattr(server, '_reconcile_detached_launcher_jobs', lambda: None)
    monkeypatch.setattr(server, '_current_project_for_find_guard', lambda: ('demo_project', tmp_path))
    monkeypatch.setattr(server, '_live_jobs_from_projects', lambda compact=True: [{
        'job_id': 'project-worker_demo_project_123',
        'stage': 'environment',
        'status': 'running',
        'created_at': '2026-06-10T00:00:01Z',
        'logs': [],
        'result': {'project': 'demo_project', 'pid': '123', 'kind': 'environment_stage', 'phase': 'environment', 'process_alive': True},
        'progress': {'phase': 'environment', 'current': 0, 'total': 0, 'percent': 0, 'message': 'environment worker running'},
    }])

    job = server.JobState('environment_real', 'environment')
    job.status = 'running'
    job.created_at = '2026-06-10T00:00:02Z'
    job.result = {}
    job.progress = {'phase': 'environment', 'current': 1, 'total': 0, 'percent': 0, 'message': 'real environment job'}
    JOBS[job.job_id] = job

    rows = server.api_jobs(compact=True, limit=10, include_history=False)
    ids = [row['job_id'] for row in rows]

    assert 'environment_real' in ids
    assert 'project-worker_demo_project_123' not in ids


def test_project_search_queries_extract_unquoted_stewardship_phrases(tmp_path, monkeypatch):
    import importlib.util
    from types import SimpleNamespace

    module_path = Path(__file__).resolve().parents[1] / 'modules' / 'environment' / 'scripts' / 'run_environment_stage.py'
    spec = importlib.util.spec_from_file_location('run_environment_stage_test_unquoted', module_path)
    env_stage = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(env_stage)

    planning = tmp_path / 'planning' / 'finding'
    reports = tmp_path / 'reports'
    state = tmp_path / 'state'
    planning.mkdir(parents=True)
    reports.mkdir(parents=True)
    state.mkdir(parents=True)
    write_json(planning / 'find_results.json', {'stage0_profile': {'profile': {'explicit_profile': {'research_interest_summary': 'broad project sentence'}}}})
    write_json(reports / 'repo_topic_fit_decision.json', {
        'stewardship_memory': 'Search for repositories explicitly related to evaluating autonomous scientific workflow agents, literature discovery, experiment planning, or evidence-grounded paper drafting.'
    })
    monkeypatch.setattr(env_stage, 'load_project_config', lambda _project: {'topic': 'demo_project'})
    monkeypatch.setattr(env_stage, 'build_paths', lambda _project: SimpleNamespace(planning=tmp_path / 'planning', reports=reports, state=state))

    queries = env_stage.project_search_queries('demo_project')

    assert queries[:4] == [
        'autonomous scientific workflow agents',
        'literature discovery',
        'experiment planning',
        'evidence-grounded paper drafting',
    ]


def test_selected_base_reference_audit_supports_generic_analysis_data_repo(tmp_path):
    import importlib.util

    script_path = Path(__file__).resolve().parents[1] / "modules" / "environment" / "scripts" / "run_selected_base_reference_reproduction_audit.py"
    spec = importlib.util.spec_from_file_location("selected_base_reference_audit_under_test", script_path)
    audit = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(audit)

    repo = tmp_path / "analysis_repo"
    (repo / "analysis").mkdir(parents=True)
    (repo / "data").mkdir()
    (repo / "analysis" / "evaluate.py").write_text("", encoding="utf-8")
    (repo / "data" / "records.jsonl").write_text("{}\n", encoding="utf-8")

    assert audit.repo_adapter(repo) == "analysis_data_quickstart"
    cmd = audit.official_command(repo, "demo_benchmark", "bounded", 1)
    assert cmd[:4] == ["analysis/evaluate.py", "--input", "data/records.jsonl", "--outdir"]
    assert "--n_boot" in cmd
    metrics = audit.parse_metrics("Loaded 40 traces\nTrajectory rows: 40\nAll figures saved to outputs/reference_bounded/\n")
    assert metrics["trace_count"] == 40
    assert metrics["trajectory_rows"] == 40
    assert metrics["analysis_outputs_saved"] == 1


def test_bootstrap_repo_env_uses_target_python_module_pip(tmp_path, monkeypatch):
    import importlib.util

    script_path = Path(__file__).resolve().parents[1] / "modules" / "environment" / "scripts" / "bootstrap_repo_env.py"
    spec = importlib.util.spec_from_file_location("bootstrap_repo_env_under_test", script_path)
    bootstrap = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(bootstrap)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    (repo / "setup.py").write_text("from setuptools import setup\nsetup(name='demo')\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "conda_env_exists", lambda *_args: True)

    steps = bootstrap.infer_install_steps(repo, "demo_env", "3.10", {}, "/conda")

    assert ["run", "-n", "demo_env", "python", "-m", "pip", "install", "-r", str(repo / "requirements.txt")] in steps
    assert ["run", "-n", "demo_env", "python", "-m", "pip", "install", "-e", str(repo)] in steps
    assert not any(step[:4] == ["run", "-n", "demo_env", "pip"] for step in steps)

def test_bootstrap_repo_env_skips_editable_for_setup_helper_script(tmp_path, monkeypatch):
    import importlib.util

    script_path = Path(__file__).resolve().parents[1] / "modules" / "environment" / "scripts" / "bootstrap_repo_env.py"
    spec = importlib.util.spec_from_file_location("bootstrap_repo_env_setup_helper_under_test", script_path)
    bootstrap = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(bootstrap)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    (repo / "setup.py").write_text(
        "import subprocess\n\n"
        "def main():\n"
        "    subprocess.check_call(['python', '-m', 'pip', '--version'])\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "conda_env_exists", lambda *_args: True)

    steps = bootstrap.infer_install_steps(repo, "demo_env", "3.10", {}, "/conda")

    assert ["run", "-n", "demo_env", "python", "-m", "pip", "install", "-r", str(repo / "requirements.txt")] in steps
    assert not any("-e" in step for step in steps)
    assert bootstrap.repo_has_editable_package(repo) is False


def test_environment_stage_bootstrap_verifies_without_auto_install():
    script_path = Path(__file__).resolve().parents[1] / "modules" / "environment" / "scripts" / "run_environment_stage.py"
    text = script_path.read_text(encoding="utf-8")

    bootstrap_line = next(line for line in text.splitlines() if "modules/environment/scripts/bootstrap_repo_env.py" in line and "bootstrap =" in line)
    assert "--verify-only" in bootstrap_line
    assert "--auto-install-missing" not in bootstrap_line


def test_bootstrap_repo_env_verify_only_never_auto_installs_missing_import(tmp_path, monkeypatch):
    import importlib.util
    import json
    import sys
    from types import SimpleNamespace

    script_path = Path(__file__).resolve().parents[1] / "modules" / "environment" / "scripts" / "bootstrap_repo_env.py"
    spec = importlib.util.spec_from_file_location("bootstrap_repo_env_verify_only_under_test", script_path)
    bootstrap = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(bootstrap)

    repo = tmp_path / "repo"
    state = tmp_path / "state"
    reports = tmp_path / "reports"
    config = tmp_path / "config.json"
    repo.mkdir()
    state.mkdir()
    reports.mkdir()
    config.write_text("{}\n", encoding="utf-8")

    class Proc:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    calls = []

    def fake_run(cmd, cwd=None):
        calls.append(cmd)
        if cmd[:4] == ["/conda", "run", "-n", "demo_env"]:
            return Proc(1, "", "No module named 'yaml'")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(bootstrap, "build_paths", lambda _project: SimpleNamespace(state=state, reports=reports, config=config))
    monkeypatch.setattr(bootstrap, "load_project_config", lambda _project: {})
    monkeypatch.setattr(bootstrap, "ensure_machine_profile", lambda _project: {"accelerator": {}, "dependencies": {"cli": {}}})
    monkeypatch.setattr(bootstrap, "discover_conda_executable", lambda _machine: "/conda")
    monkeypatch.setattr(bootstrap, "conda_env_exists", lambda *_args: True)
    monkeypatch.setattr(bootstrap, "run", fake_run)
    monkeypatch.setattr(bootstrap, "install_missing_import", lambda *_args: (_ for _ in ()).throw(AssertionError("must not install")))
    monkeypatch.setattr(sys, "argv", ["bootstrap_repo_env.py", "--project", "demo", "--repo-path", str(repo), "--env-name", "demo_env", "--verify-only"])

    bootstrap.main()

    payload = json.loads((state / "repo_env_bootstrap.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["verify_only"] is True
    assert payload["auto_install_missing"] is False
    assert payload["missing_import"] == "yaml"
    assert not any("install" in cmd for call in calls for cmd in call)



def test_bootstrap_repo_env_repairs_missing_python_pip(monkeypatch):
    import importlib.util

    script_path = Path(__file__).resolve().parents[1] / "modules" / "environment" / "scripts" / "bootstrap_repo_env.py"
    spec = importlib.util.spec_from_file_location("bootstrap_repo_env_repair_under_test", script_path)
    bootstrap = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(bootstrap)

    class Proc:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    checks = {"count": 0}
    calls = []

    def fake_run(cmd, cwd=None):
        calls.append(cmd)
        if cmd == ["/conda", "run", "-n", "demo_env", "python", "-m", "pip", "--version"]:
            checks["count"] += 1
            if checks["count"] == 1:
                return Proc(1, "", "No module named pip")
            return Proc(0, "pip 25.0")
        if cmd == ["/conda", "install", "-y", "-n", "demo_env", "pip"]:
            return Proc(0, "installed pip")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(bootstrap, "run", fake_run)

    ready, records = bootstrap.ensure_env_python_pip("/conda", "demo_env")

    assert ready is True
    assert [record["reason"] for record in records] == ["check-python-pip", "install-conda-pip", "verify-after-install-conda-pip"]
    assert calls[1] == ["/conda", "install", "-y", "-n", "demo_env", "pip"]

def test_repo_archive_directory_is_reused_without_redownload(tmp_path, monkeypatch):
    import importlib.util

    script_path = Path(__file__).resolve().parents[1] / "modules" / "environment" / "scripts" / "select_evidence_ready_repo.py"
    spec = importlib.util.spec_from_file_location("select_evidence_ready_repo_under_test", script_path)
    selector = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(selector)

    class Paths:
        repos_selected = tmp_path

    target = tmp_path / "owner_repo"
    target.mkdir()
    (target / "README.md").write_text("cached archive", encoding="utf-8")

    def fail_archive(*_args, **_kwargs):
        raise AssertionError("archive fallback should not run for an existing extracted repo")

    monkeypatch.setattr(selector, "github_archive_fallback", fail_archive)

    repo, info = selector.clone_or_reuse(Paths, {"name": "owner/repo", "url": "https://github.com/owner/repo"})

    assert repo == target.resolve()
    assert info["status"] == "reused_existing_archive"



def test_environment_selector_blocks_active_repo_as_only_current_candidate(monkeypatch, tmp_path):
    import importlib.util
    import sys

    script_path = Path(__file__).resolve().parents[1] / "modules" / "environment" / "scripts" / "select_evidence_ready_repo.py"
    spec = importlib.util.spec_from_file_location("select_evidence_ready_repo_active_only_under_test", script_path)
    selector = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(selector)

    repo = tmp_path / "active_repo"
    repo.mkdir()
    paths = type(
        "Paths",
        (),
        {
            "state": tmp_path / "state",
            "planning": tmp_path / "planning",
            "reports": tmp_path / "reports",
            "repos_selected": tmp_path / "repos" / "selected",
        },
    )()
    paths.state.mkdir(parents=True)
    paths.reports.mkdir(parents=True)
    paths.repos_selected.mkdir(parents=True)
    write_json(
        paths.state / "active_repo.json",
        {
            "name": "owner/active",
            "url": "https://github.com/owner/active",
            "repo_path": str(repo),
            "claim_ready_dataset": "amazon-beauty",
            "claim_ready_datasets": ["amazon-beauty"],
        },
    )
    write_json(paths.state / "repo_candidates.json", [])

    monkeypatch.setattr(selector, "build_paths", lambda _project: paths)
    monkeypatch.setattr(selector, "load_project_config", lambda _project: {"conda_env": "demo_env"})
    monkeypatch.setattr(selector, "clone_or_reuse", lambda _paths, _row: (repo, {"status": "reused_existing_clone", "path": str(repo)}))
    monkeypatch.setattr(selector, "quick_signals", lambda _repo: {"has_entrypoint": True, "has_data_dir": True, "has_readme": True, "readme_data_mentions": 1})
    monkeypatch.setattr(
        selector,
        "probe_repo",
        lambda _project, _repo, _env_name, _timeout: {
            "probe_return_code": 0,
            "probes": [{"dataset": "amazon-beauty", "claim_ready": True, "loader_probe": {"success": True}}],
        },
    )
    monkeypatch.setattr(selector, "write_repo_env_strategy", lambda *args, **kwargs: {})
    monkeypatch.setattr(sys, "argv", [
        "select_evidence_ready_repo.py",
        "--project",
        "demo",
        "--env-name",
        "demo_env",
        "--selection-stage",
        "environment_claude_code",
        "--fresh-find-run-id",
        "find_current",
        "--write-active",
    ])

    rc = selector.main()

    payload = read_json(paths.state / "evidence_ready_repo_selection.json")
    assert rc == 2
    assert payload["selection_gate"] == "continued_search_required_active_repo_only"
    assert payload["selected"] == {}
    assert payload["active_repo_reaudit_blocked"] is True
    assert read_json(paths.state / "active_repo.json")["name"] == "owner/active"


def test_environment_current_find_reaudits_evidence_ready_active_repo(tmp_path):
    import importlib.util

    script_path = Path(__file__).resolve().parents[1] / "modules" / "environment" / "scripts" / "select_evidence_ready_repo.py"
    spec = importlib.util.spec_from_file_location("select_evidence_ready_repo_under_test", script_path)
    selector = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(selector)

    repo = tmp_path / "active_repo"
    repo.mkdir()
    active = {
        "name": "owner/active",
        "url": "https://github.com/owner/active",
        "repo_path": str(repo),
        "repo_reuse_score": 12,
        "claim_ready_dataset": "amazon-beauty",
        "claim_ready_datasets": ["amazon-beauty"],
        "selected_base_title": "Prior Evidence-Ready Base",
    }

    row = selector.active_repo_candidate(active, "find_current")

    assert row is not None
    assert row["_source"] == "active_repo"
    assert row["source"] == "current_active_repo_reaudit"
    assert row["fresh_find_run_id"] == "find_current"
    assert row["claim_ready_dataset"] == "amazon-beauty"
    assert "automatically selected" in row["reaudit_policy"]


def test_configured_max_ideas_reads_runtime_config(monkeypatch, tmp_path):
    project_paths = importlib.import_module("project_paths")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    write_json(runtime / ".config.json", {"max_ideas": 2})
    monkeypatch.setenv("WORKFLOW_RUNTIME_DIR", str(runtime))

    assert project_paths.configured_max_ideas("", {}, default=5) == 2
    assert project_paths.configured_max_ideas("", {}, explicit=3, default=5) == 3


def test_ensure_current_find_loads_web_generated_idea_plan_sources(tmp_path):
    import importlib.util

    module_path = Path(__file__).resolve().parents[1] / "modules" / "reading" / "scripts" / "ensure_current_find_research_plan.py"
    spec = importlib.util.spec_from_file_location("ensure_current_find_research_plan_test_web_sources", module_path)
    ensure_plan = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(ensure_plan)

    finding = tmp_path / "finding"
    finding.mkdir()
    run_id = "find_web_sources"
    find_results = {"run_id": run_id, "strong_recommendations": [recommended_paper("p1", "Paper One")]}
    write_json(
        finding / "read_results.json",
        {
            "run_id": run_id,
            "source": "auto_read_recommended_articles",
            "readings": [{"paper_id": "p1", "title": "Paper One", "abstract_zh": "中文摘要", "method_details_zh": "具体方法细节"}],
        },
    )
    write_json(
        finding / "ideas.json",
        {
            "run_id": run_id,
            "source": "taste_auto_idea",
            "ideas": [{"id": "idea-001", "title": "Web idea", "new_method": "具体新方法", "mechanism": "机制说明"}],
        },
    )
    write_json(
        finding / "plans.json",
        {
            "run_id": run_id,
            "source": "taste_auto_plan",
            "plans": [{"plan_id": "plan-001", "idea_id": "idea-001", "title": "Web plan", "steps": ["step"]}],
        },
    )

    readings, ideas, plans = ensure_plan.load_claude_outputs(finding, run_id, find_results, read_limit=1, write_pending_validation=False)

    assert len(readings) == 1
    assert [idea["id"] for idea in ideas] == ["idea-001"]
    assert [plan["plan_id"] for plan in plans] == ["plan-001"]


def test_ensure_claude_plan_state_accepts_selected_web_generated_plan(tmp_path, monkeypatch):
    import importlib.util
    from types import SimpleNamespace

    module_path = Path(__file__).resolve().parents[1] / "modules" / "reading" / "scripts" / "ensure_current_find_research_plan.py"
    spec = importlib.util.spec_from_file_location("ensure_current_find_research_plan_test_web_selected", module_path)
    ensure_plan = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(ensure_plan)
    monkeypatch.setattr(ensure_plan, "load_project_config", lambda _project: {"target_venue": "ICLR"})

    paths = SimpleNamespace(root=tmp_path / "demo_project", name="demo_project", state=tmp_path / "demo_project" / "state", planning=tmp_path / "demo_project" / "planning")
    finding = paths.planning / "finding"
    finding.mkdir(parents=True)
    paths.state.mkdir(parents=True)
    run_id = "find_web_selected"
    readings = [
        {"paper_id": "p1", "title": "Paper One", "abstract_zh": "中文摘要", "method_details_zh": "具体方法细节", "full_text_available": True},
        {"paper_id": "p2", "title": "Paper Two", "abstract_zh": "中文摘要", "method_details_zh": "具体方法细节", "full_text_available": True},
    ]
    ideas = [
        {"id": "idea-001", "title": "Selected idea", "status": "approved", "new_method": "一个足够具体的新方法", "mechanism": "机制说明"},
        {"id": "idea-002", "title": "Backlog idea", "status": "approved", "new_method": "另一个足够具体的新方法", "mechanism": "机制说明"},
    ]
    plans = [
        {"plan_id": "plan-001", "idea_id": "idea-001", "title": "Selected plan", "steps": ["step"], "selected_for_execution": True, "execute_next": True, "execution_selection": {"selected": True, "selected_by": "human_supervision", "reason": "best"}},
        {"plan_id": "plan-002", "idea_id": "idea-002", "title": "Backlog plan", "steps": ["step"], "selected_for_execution": False, "execute_next": False},
    ]
    write_json(finding / "find_results.json", {"run_id": run_id, "strong_recommendations": [recommended_paper("p1", "Paper One"), recommended_paper("p2", "Paper Two")]})
    write_json(finding / "read_results.json", {"run_id": run_id, "source": "auto_read_recommended_articles", "readings": readings})
    write_json(finding / "ideas.json", {"run_id": run_id, "source": "taste_auto_idea", "ideas": ideas})
    write_json(finding / "plans.json", {"run_id": run_id, "source": "taste_auto_plan", "plans": plans})
    write_json(
        paths.state / "current_find_claude_reading_validation.json",
        {
            "run_id": run_id,
            "valid": True,
            "policy_version": ensure_plan.FULL_TEXT_READ_POLICY_VERSION,
            "expected_recommendation_count": 2,
            "actual_reading_count": 2,
            "full_text_reading_count": 2,
            "pending_full_text_reading_count": 0,
            "blockers": [],
        },
    )

    payload = ensure_plan.ensure_claude_plan_state("demo_project", paths, run_id, readings, ideas, plans, {"status": "web_artifacts", "return_code": 0}, idea_count=2)

    assert payload["status"] == "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection"
    assert payload["source"] == "current_find_execution_contract"
    assert payload["execution_ready"] is True
    assert payload["selected_plan_id"] == "plan-001"
    assert payload["selected_idea_id"] == "idea-001"
    assert payload["selected_execution_issue"] == ""
    assert payload["idea_schema_ready"] is True
    assert payload["targeted_search_query_count"] == 0




def test_compact_project_summary_environment_waiting_message_overrides_stale_experiment_gate(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    run_id = "find_demo_env_wait"
    write_json(taste_dir / "find_results.json", {"run_id": run_id, "strong_recommendations": [recommended_paper("paper-1", "Paper 1")]})
    write_json(taste_dir / "find_progress.json", {"run_id": run_id, "phase": "complete", "strong_recommendation_count": 1, "recommendation_target_count": 1, "recommendation_shortfall": 0, "counts": {}})
    write_json(taste_dir / "read_results.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "readings": [{"paper_id": "paper-1", "title": "Paper 1", "full_text_available": True, "full_text_status": "pdf_text_read", "pdf_text_chars": 9000, "source_evidence": {"text_chars": 9000}, "abstract_zh": "足够长的摘要内容", "motivation_zh": "足够长的动机内容", "method_details_zh": "足够长的方法内容", "experiments_zh": "足够长的实验内容", "limitations_zh": "足够长的局限内容", "subagent_deep_read": True, "deep_read_audit": {"status": "completed"}}]})
    write_json(taste_dir / "ideas.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "status": "approved", "selected_plan_id": "plan-1", "selected_idea_id": "idea-1", "ideas": [{"id": "idea-1", "title": "Idea 1", "approved_for_planning": True}]})
    write_json(taste_dir / "plans.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "status": "plan_selected", "selected_plan_id": "plan-1", "selected_idea_id": "idea-1", "plans": [{"plan_id": "plan-1", "idea_id": "idea-1", "selected_for_execution": True, "execute_next": True, "execution_selection": {"selected": True, "selected_by": "main_claude_code_after_deep_read", "reason": "best"}}]})
    write_json(state_dir / "current_find_research_plan.json", {"run_id": run_id, "status": "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection", "selected_plan_id": "plan-1", "selected_idea_id": "idea-1", "content_ready": True, "read_idea_plan_ready": True, "execution_ready": True, "takeover_ready": True, "claude_current_find_ready": True, "next_required_action": "environment_base_selection_and_repo_data_protocol_audit"})
    write_json(state_dir / "current_find_claude_reading_validation.json", {"run_id": run_id, "valid": True, "expected_recommendation_count": 1, "actual_reading_count": 1, "full_text_reading_count": 1, "full_text_evidence_count": 1, "pending_full_text_reading_count": 0, "pending_without_evidence_count": 0, "blockers": []})
    write_json(state_dir / "full_research_cycle.json", {"status": "stale_full_research_cycle_snapshot", "summary_zh": "旧实验门控摘要", "full_cycle_job": {"status": "stale", "process_alive": False}})
    write_json(state_dir / "selected_base_viability_gate.json", {"status": "blocked", "decision": "continue_experiment_evidence_repair"})
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda _project, _root: {"venue_ids": ["openreview_iclr_2026"], "years": [2026]})
    monkeypatch.setattr(project_bridge, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])

    summary = project_bridge._fast_project_summary("demo_project", project_root, {"name": "demo_project", "topic": "Demo", "target_venue": "ICLR"})

    assert summary["status"] == "blocked_environment_base_selection_required"
    assert "等待环境阶段" in summary["summary"]
    assert "旧实验门控摘要" not in summary["summary"]

def test_compact_project_summary_current_find_ready_overrides_stale_summary_without_viability_gate(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project_root = tmp_path / "demo_project"
    state_dir = project_root / "state"
    finding_dir = project_root / "planning" / "finding"
    state_dir.mkdir(parents=True)
    finding_dir.mkdir(parents=True)
    run_id = "find_demo_env_wait_no_gate"
    write_json(finding_dir / "ideas.json", {"run_id": run_id, "status": "approved", "ideas": [{"id": "idea-1"}]})
    write_json(finding_dir / "plans.json", {"run_id": run_id, "status": "plan_selected", "selected_plan_id": "plan-1", "plans": [{"plan_id": "plan-1", "selected_for_execution": True}]})
    write_json(state_dir / "current_find_research_plan.json", {"run_id": run_id, "status": "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection", "selected_plan_id": "plan-1", "selected_idea_id": "idea-1", "read_idea_plan_ready": True, "execution_ready": True, "claude_current_find_ready": True})
    write_json(state_dir / "full_research_cycle.json", {"status": "stale_full_research_cycle_snapshot", "summary_zh": "旧实验门控摘要", "full_cycle_job": {"status": "stale", "process_alive": False}})
    monkeypatch.setattr(project_bridge, "_current_project_source_selection", lambda _project, _root: {"venue_ids": ["openreview_iclr_2026"], "years": [2026]})
    monkeypatch.setattr(project_bridge, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])

    summary = project_bridge._fast_project_summary("demo_project", project_root, {"name": "demo_project", "topic": "Demo", "target_venue": "ICLR"})

    assert summary["status"] == "blocked_environment_base_selection_required"
    assert "等待环境阶段" in summary["summary"]
    assert "旧实验门控摘要" not in summary["summary"]
    assert summary["current_blocker"]["category"] == "environment_anchor_selection_required"


def test_start_job_done_result_not_overridden_by_stale_blocked_summary(monkeypatch):
    from auto_research.web import server

    job = server.start_job(
        "current-find-selection",
        lambda _log, _cancel, _progress: {
            "status": "done",
            "summary": {"full_research_cycle": {"status": "blocked_old_experiment_snapshot"}},
        },
        job_id="unit-current-find-done-status",
    )
    deadline = time.time() + 5
    while (job.status in {"queued", "running"} or job.progress.get("phase") != "complete") and time.time() < deadline:
        time.sleep(0.05)

    try:
        assert job.status == "done"
        assert job.progress["phase"] == "complete"
    finally:
        server.JOBS.pop(job.job_id, None)

def test_current_find_selection_result_exposes_current_find_blocker(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project = "demo_project"
    project_root = tmp_path / project
    state_dir = project_root / "state"
    state_dir.mkdir(parents=True)
    write_json(state_dir / "current_find_research_plan.json", {"run_id": "find_demo", "status": "blocked_current_find_idea_plan_incomplete", "next_required_action": "run_or_approve_current_find_idea_plan"})
    pipeline = {
        "run_id": "find_demo",
        "status": "blocked_current_find_idea_plan_incomplete",
        "next_required_action": "run_or_approve_current_find_idea_plan",
        "summary_zh": "当前 Find 后处理未通过 Claude 接管 gate：idea 0 个、plan 0 个。",
        "selected_plan_id": "",
    }
    monkeypatch.setattr(project_bridge, "PROJECTS", tmp_path)
    monkeypatch.setattr(project_bridge, "build_command", lambda payload: (project, ["/bin/sh", "-c", "exit 2"]))
    monkeypatch.setattr(project_bridge, "interactive_env", lambda project: {})
    monkeypatch.setattr(project_bridge, "upsert_agent", lambda *args, **kwargs: None)
    monkeypatch.setattr(project_bridge, "append_agent_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(project_bridge, "project_summary", lambda project: {"status": "stale_full_research_cycle_snapshot", "summary": "旧实验门控摘要"})
    monkeypatch.setattr(project_bridge, "_current_find_pipeline_summary", lambda root, **_kwargs: pipeline)
    progress_events = []

    result = project_bridge.run_action(
        {"project": project, "action": "current-find-selection"},
        lambda _line: None,
        lambda: False,
        lambda phase, current, total, message: progress_events.append((phase, message)),
    )

    assert result["status"] == "blocked"
    assert result["current_find_pipeline"]["status"] == "blocked_current_find_idea_plan_incomplete"
    assert result["blocker"]["next_action"] == "run_or_approve_current_find_idea_plan"
    assert "当前 Find 后处理未通过" in result["blocker"]["summary"]
    assert progress_events[-1][0] == "blocked"
    assert "当前 Find 后处理未通过" in progress_events[-1][1]
