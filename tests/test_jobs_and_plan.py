import time

from auto_research.auto_plan.pipeline import run_plan
from auto_research.auto_plan.pipeline import finish_plan, polish_plan
from auto_research.emailer import build_run_email_html
from auto_research.jobs import JobCancelled
from auto_research.models import AppConfig, EmailJobRequest, PlanPolishRequest, PlanRequest
from auto_research.storage import create_run_dir, delete_run, read_json, write_json, write_text
from auto_research.web.server import JOBS, api_artifacts, start_job


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
        assert "<h1>TASTE Report:" in html
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

