from auto_research.auto_find.pipeline import run_find
from auto_research.auto_idea.pipeline import patch_idea, run_idea
from auto_research.auto_plan.pipeline import run_plan
from auto_research.auto_read.pipeline import run_read
from auto_research.models import AppConfig, FindRequest, IdeaPatch, IdeaRequest, PlanRequest, ReadRequest, VenueSelection
from auto_research.storage import delete_run, read_json, run_dir


def test_mock_pipeline_arxiv_disabled_external_optional():
    cfg = AppConfig(
        provider="mock",
        research_interest="LLM agents retrieval",
        max_fetch_papers=2,
        max_recommended_papers=2,
        max_ideas=2,
    )
    result = run_find(
        FindRequest(
            config=cfg,
            selection=VenueSelection(
                venue_ids=["openreview_iclr_2026"],
                years=[2026],
                include_arxiv=False,
                include_huggingface=False,
                include_github=False,
            ),
        ),
        log=lambda _msg: None,
    )
    run_id = result["run_id"]
    assert result["articles"]
    stage0_path = run_dir(run_id) / "stage0_profile.json"
    assert stage0_path.exists()
    stage0 = read_json(stage0_path, {})
    assert stage0["fallback_used"] is True
    assert stage0["profile"]["explicit_profile"]["research_interest_summary"] == "LLM agents retrieval"
    assert result["stage0_profile"] == stage0

    read_result = run_read(ReadRequest(run_id=run_id, max_papers=1), cfg, log=lambda _msg: None)
    assert read_result["readings"]

    idea_result = run_idea(IdeaRequest(run_id=run_id, max_ideas=2), cfg, log=lambda _msg: None)
    assert idea_result["ideas"]
    idea_id = idea_result["ideas"][0]["id"]
    patch_idea(run_id, idea_id, IdeaPatch(status="approved"))

    plan_result = run_plan(PlanRequest(run_id=run_id, idea_ids=[idea_id]), cfg, log=lambda _msg: None)
    assert plan_result["plans"]
    assert run_dir(run_id).exists()
    assert delete_run(run_id) is True
