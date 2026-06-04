import time

import auto_research.auto_plan.pipeline as plan_pipeline
from auto_research.auto_plan.pipeline import run_plan
from auto_research.auto_plan.pipeline import finish_plan, polish_plan
from auto_research.emailer import build_run_email_html
from auto_research.jobs import JobCancelled
from auto_research.models import AppConfig, EmailJobRequest, LLMRoleConfig, PlanPolishRequest, PlanRequest
from auto_research.storage import create_run_dir, delete_run, read_json, stage_dir, write_json, write_text
from auto_research.web.server import JOBS, api_artifacts, api_confirm_idea, start_job


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


def test_job_progress_shape_is_serialized():
    JOBS.clear()
    job = start_job("find", lambda _log, _should_cancel, progress: (progress("phase", 2, 4, "halfway") or {"ok": True}))
    job.done.wait(2)
    data = job.as_dict()
    assert data["progress"]["phase"] == "complete"
    assert data["progress"]["percent"] == 100
    assert data["progress"]["message"] == "find complete"


def test_plan_uses_only_selected_approved_ideas():
    run_id, directory = create_run_dir("plan_test")
    try:
        write_json(
            stage_dir(directory, "idea") / "ideas.json",
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


def test_plan_uses_disposable_reference_worker_and_grounded_main_agent(monkeypatch):
    calls = []

    class FakeLLMClient:
        def __init__(self, _config, role, conversation_key="", persist_session=True, resume_session=False, tools=""):
            self.role = role
            self.conversation_key = conversation_key
            self.persist_session = persist_session
            self.resume_session = resume_session
            self.tools = tools
            self.enabled = True

        def summary(self):
            return {
                "role": self.role,
                "conversation_key": self.conversation_key,
                "persist_session": self.persist_session,
                "resume_session": self.resume_session,
                "tools": self.tools,
            }

        def json_or_error(self, prompt):
            calls.append(("json_or_error", self.conversation_key, self.persist_session, prompt))
            if ":worker:auto_plan:references:" in self.conversation_key:
                return {
                    "ok": True,
                    "data": {
                        "references": [
                            {
                                "source_type": "repository",
                                "paper_or_project": "Example",
                                "url": "https://github.com/example/project",
                                "version": "v1.0",
                                "location": "src/model.py::Model",
                                "reuse": "Reuse the baseline model.",
                                "modification": "Add the proposed module.",
                                "evidence": "The repository implements the cited baseline.",
                                "verification_status": "verified",
                                "verification_evidence": "Inspected src/model.py at v1.0.",
                            },
                            {
                                "source_type": "repository",
                                "paper_or_project": "Unsupported",
                                "url": "https://github.com/example/unsupported",
                                "location": "src/claimed.py::Claimed",
                                "verification_status": "verified",
                            },
                        ]
                    },
                    "error": "",
                }
            return {"ok": True, "data": {"evaluation": "Grounded", "weaknesses": []}, "error": ""}

        def json_or_none(self, prompt):
            calls.append(("json_or_none", self.conversation_key, self.persist_session, prompt))
            return {
                "experimental_design": "Reuse the verified baseline and add the proposed module.",
                "feasibility": "Feasible.",
                "steps": ["Run the minimum experiment."],
            }

    monkeypatch.setattr(plan_pipeline, "LLMClient", FakeLLMClient)
    run_id, directory = create_run_dir("grounded_plan_test")
    try:
        write_json(
            stage_dir(directory, "idea") / "ideas.json",
            {"run_id": run_id, "ideas": [{"id": "approved", "title": "Approved idea", "hypothesis": "H", "status": "approved"}]},
        )
        result = run_plan(PlanRequest(run_id=run_id, idea_ids=["approved"], repair_rounds=1), AppConfig(provider="mock"), log=lambda _msg: None)

        plan = result["plans"][0]
        references = plan["reference_foundation"]
        assert references[0]["verification_status"] == "verified"
        assert references[1]["verification_status"] == "unresolved"
        assert plan["reference_worker"]["persist_session"] is False
        assert plan["reference_worker"]["tools"] == "WebSearch,WebFetch,Read,Glob,Grep"
        assert any(
            key == f"run:{run_id}:main" and persist_session and "Reference foundation:" in prompt
            for kind, key, persist_session, prompt in calls
            if kind == "json_or_none"
        )
        markdown = (stage_dir(directory, "plan") / "plan.md").read_text(encoding="utf-8")
        assert "src/model.py::Model" in markdown
        assert "Unsupported" in markdown
    finally:
        delete_run(run_id)


def test_plan_inherits_configured_main_agent_when_plan_roles_are_unset(monkeypatch):
    clients = []

    class CapturingLLMClient:
        def __init__(self, config, role, conversation_key="", persist_session=True, resume_session=False, tools=""):
            clients.append((role, config.llm_roles.get(role), conversation_key, resume_session, tools))
            self.enabled = False

        def summary(self):
            return {}

    monkeypatch.setattr(plan_pipeline, "LLMClient", CapturingLLMClient)
    run_id, directory = create_run_dir("plan_main_agent_config_test")
    try:
        write_json(
            stage_dir(directory, "idea") / "ideas.json",
            {"run_id": run_id, "ideas": [{"id": "approved", "title": "Approved idea", "hypothesis": "H", "status": "approved"}]},
        )
        config = AppConfig(
            provider="deepseek",
            llm_roles={"idea_generator": LLMRoleConfig(provider="claude-code", model="sonnet")},
        )

        run_plan(PlanRequest(run_id=run_id, idea_ids=["approved"], repair_rounds=1), config, log=lambda _msg: None)

        main_clients = [item for item in clients if item[2] == f"run:{run_id}:main"]
        assert [item[0] for item in main_clients] == ["plan_generator", "plan_evaluator"]
        assert all(item[1].provider == "claude-code" and item[3] for item in main_clients)
    finally:
        delete_run(run_id)


def test_confirming_idea_starts_plan_and_persists_run_artifacts(monkeypatch):
    JOBS.clear()
    monkeypatch.setattr("auto_research.web.server.load_config", lambda: AppConfig(provider="mock"))
    run_id, directory = create_run_dir("confirm_plan_test")
    try:
        write_json(
            stage_dir(directory, "idea") / "ideas.json",
            {"run_id": run_id, "ideas": [{"id": "idea-001", "title": "Selected", "hypothesis": "H", "status": "pending"}]},
        )

        response = api_confirm_idea(run_id, "idea-001")
        job = JOBS[response["job_id"]]
        job.done.wait(3)

        assert job.status == "done"
        assert (stage_dir(directory, "plan") / "plans.json").exists()
        assert (stage_dir(directory, "plan") / "plan.md").exists()
        assert read_json(stage_dir(directory, "plan") / "plans.json", {})["plans"][0]["idea_id"] == "idea-001"
    finally:
        delete_run(run_id)


def test_plan_polish_appends_version_without_overwriting():
    run_id, directory = create_run_dir("polish_test")
    try:
        write_json(
            stage_dir(directory, "idea") / "ideas.json",
            {"run_id": run_id, "ideas": [{"id": "approved", "title": "Approved idea", "hypothesis": "H", "status": "approved"}]},
        )
        cfg = AppConfig(provider="mock")
        result = run_plan(PlanRequest(run_id=run_id, idea_ids=["approved"], repair_rounds=1), cfg, log=lambda _msg: None)
        plan_id = result["plans"][0]["plan_id"]
        polish_plan(PlanPolishRequest(run_id=run_id, plan_id=plan_id, version_id="v1", rounds=1), cfg, log=lambda _msg: None)
        data = read_json(stage_dir(directory, "plan") / "plans.json", {})
        versions = data["plans"][0]["versions"]
        assert [version["version_id"] for version in versions] == ["v1", "v2"]
    finally:
        delete_run(run_id)


def test_finish_plan_hides_rounds_in_markdown_but_keeps_json():
    run_id, directory = create_run_dir("finish_plan_test")
    try:
        write_json(
            stage_dir(directory, "idea") / "ideas.json",
            {"run_id": run_id, "ideas": [{"id": "approved", "title": "Approved idea", "hypothesis": "H", "status": "approved"}]},
        )
        cfg = AppConfig(provider="mock")
        result = run_plan(PlanRequest(run_id=run_id, idea_ids=["approved"], repair_rounds=1), cfg, log=lambda _msg: None)
        plan_id = result["plans"][0]["plan_id"]
        data = finish_plan(run_id, plan_id)
        assert data["plans"][0]["completed"] is True
        assert data["plans"][0]["completed_at"]
        markdown = (stage_dir(directory, "plan") / "plan.md").read_text(encoding="utf-8")
        assert "Evaluation / Repair Rounds" not in markdown
        stored = read_json(stage_dir(directory, "plan") / "plans.json", {})
        assert stored["plans"][0]["versions"][0]["evaluation_rounds"]
    finally:
        delete_run(run_id)


def test_email_report_renders_markdown_and_ranking_html():
    run_id, directory = create_run_dir("email_test")
    try:
        find_dir = stage_dir(directory, "find")
        write_text(find_dir / "article.md", "# Report\n\n- **Item**: value")
        write_json(
            find_dir / "find_results.json",
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
        assert "<h1>TASTE Report:" in html
        assert "<strong>Item</strong>" in html
        assert "Full Screened Ranking" in html
        assert str(find_dir / "article.md") in html
    finally:
        delete_run(run_id)


def test_artifact_api_returns_paths():
    run_id, directory = create_run_dir("artifact_path_test")
    try:
        article_path = stage_dir(directory, "find") / "article.md"
        write_text(article_path, "# Article")
        response = api_artifacts(run_id)
        article = next(item for item in response["artifacts"] if item["name"] == "article.md")
        assert article["path"] == str(article_path)
    finally:
        delete_run(run_id)
