from auto_research.auto_find.pipeline import run_find
from auto_research.auto_idea.pipeline import patch_idea, run_idea
from auto_research.auto_plan.pipeline import run_plan
from auto_research.auto_read import pipeline as read_pipeline
from auto_research.auto_read.pipeline import run_read
from auto_research.models import AppConfig, FindRequest, IdeaPatch, IdeaRequest, PlanRequest, ReadRequest, VenueSelection
from auto_research.storage import create_run_dir, delete_run, read_json, run_dir, stage_dir, write_json


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
    directory = run_dir(run_id)
    stage0_path = stage_dir(directory, "find") / "stage0_profile.json"
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
    assert directory.exists()
    assert delete_run(run_id) is True


def test_read_stage_uses_worker_agent_conversation(monkeypatch):
    instances = []

    class FakeReadLLM:
        def __init__(self, _config, role=None, conversation_key="", persist_session=True):
            self.role = role
            self.conversation_key = conversation_key
            self.persist_session = persist_session
            self.enabled = False
            instances.append(self)

        def summary(self):
            return {
                "provider": "mock",
                "backend": "chat-completions",
                "enabled": self.enabled,
                "session_id": "",
            }

    monkeypatch.setattr("auto_research.auto_read.pipeline.LLMClient", FakeReadLLM)
    run_id, directory = create_run_dir("read_agent_session_test")
    find_dir = stage_dir(directory, "find")
    read_dir = stage_dir(directory, "read")
    try:
        write_json(
            find_dir / "find_results.json",
            {
                "run_id": run_id,
                "articles": [
                    {
                        "id": "paper-1",
                        "title": "Paper One",
                        "url": "https://example.com/paper",
                        "pdf_url": "",
                        "abstract": "Abstract",
                        "reason": "Relevant",
                    }
                ],
            },
        )
        result = run_read(ReadRequest(run_id=run_id, max_papers=1), AppConfig(provider="mock"), log=lambda _msg: None)
        assert [instance.conversation_key for instance in instances] == [
            f"run:{run_id}:worker:auto_read:paper-1",
            f"run:{run_id}:worker:auto_read:synthesis",
        ]
        assert [instance.persist_session for instance in instances] == [False, True]
        reading = result["readings"][0]
        assert sorted(reading.keys()) == ["content", "metadata"]
        assert reading["content"]["title"] == "Paper One"
        assert reading["content"]["abstract"] == "Abstract"
        assert reading["metadata"]["paper_id"] == "paper-1"
        assert (read_dir / "read_paper_001.json").exists()
        assert (read_dir / "read_paper_001.md").exists()
        read_md = (read_dir / "read.md").read_text(encoding="utf-8")
        assert "Cross-Paper Synthesis" in read_md
        assert "https://example.com/paper" not in read_md
        assert "paper-1" not in read_md
    finally:
        delete_run(run_id)


def test_pdf_download_retries_after_failed_attempt(monkeypatch, tmp_path):
    calls = []

    class Response:
        def __init__(self, status_code, content_type, content):
            self.status_code = status_code
            self.headers = {"content-type": content_type}
            self.content = content

    def fake_get(url, timeout, headers):
        calls.append((url, timeout, headers))
        if len(calls) == 1:
            return Response(503, "text/html", b"retry later")
        return Response(200, "application/octet-stream", b"%PDF-1.7 full pdf")

    monkeypatch.setattr("auto_research.auto_read.pipeline.requests.get", fake_get)
    target = tmp_path / "paper.pdf"

    ok, attempts, error = read_pipeline._download_pdf("https://example.com/paper", target, retries=2)

    assert ok is True
    assert attempts == 2
    assert error == ""
    assert target.read_bytes().startswith(b"%PDF")
