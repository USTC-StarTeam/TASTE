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
        def __init__(self, _config, role=None, conversation_key="", persist_session=True, resume_session=False):
            self.role = role
            self.conversation_key = conversation_key
            self.persist_session = persist_session
            self.resume_session = resume_session
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
    monkeypatch.setattr("auto_research.auto_read.pipeline._download_pdf", lambda _url, _target: (False, 1, "test"))
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
                        "pdf_url": "https://example.com/paper.pdf",
                        "abstract": "Abstract",
                        "venue": "ExampleConf",
                        "year": 2026,
                        "reason": "Relevant",
                    }
                ],
            },
        )
        result = run_read(ReadRequest(run_id=run_id, max_papers=1), AppConfig(provider="mock"), log=lambda _msg: None)
        assert [instance.conversation_key for instance in instances] == [
            f"run:{run_id}:worker:auto_read:paper-1",
            f"run:{run_id}:main",
        ]
        assert [instance.persist_session for instance in instances] == [False, True]
        assert [instance.resume_session for instance in instances] == [False, False]
        reading = result["readings"][0]
        assert sorted(reading.keys()) == ["content", "metadata"]
        assert reading["content"]["title"] == "Paper One"
        assert reading["content"]["abstract"] == "Abstract"
        assert {
            "title",
            "venue",
            "year",
            "abstract",
            "motivation",
            "method_summary",
            "limitations",
        }.issubset(reading["content"])
        assert reading["metadata"]["paper_id"] == "paper-1"
        assert reading["metadata"]["url"] == "https://example.com/paper"
        assert reading["metadata"]["pdf_url"] == "https://example.com/paper.pdf"
        assert reading["metadata"]["pdf_path"] == str(directory / "pdf" / "paper-1.pdf")
        assert reading["metadata"]["pdf_cache_hit"] is False
        assert (read_dir / "read_paper_001.json").exists()
        assert (read_dir / "read_paper_001.md").exists()
        read_md = (read_dir / "read.md").read_text(encoding="utf-8")
        assert "Main Agent Synthesis" in read_md
        assert "Paper One" in read_md
        assert "ExampleConf (2026)" in read_md
        assert "https://example.com/paper" in read_md
        assert "https://example.com/paper.pdf" in read_md
        assert "Abstract" in read_md
        assert "### Motivation" in read_md
        assert "### Method Summary" in read_md
        assert "### Limitations" in read_md
        assert "Method Cross-Comparison" in read_md
        assert "paper-1" not in read_md
        assert result["main_agent"]["conversation_key"] == f"run:{run_id}:main"
        assert result["main_agent"]["persist_session"] is True
        assert result["main_agent"]["status"] == "disabled"
        assert result["main_agent_summary"] == result["cross_summary"]
        assert {"summary", "method_differences", "pros_cons"} == set(result["method_analysis"])
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


def test_read_pdf_cache_reuses_existing_pdf(monkeypatch, tmp_path):
    target = tmp_path / "pdf" / "paper-1.pdf"
    target.parent.mkdir()
    target.write_bytes(b"%PDF-1.7 cached")

    def fail_download(_url, _target):
        raise AssertionError("cached PDF should not be downloaded again")

    monkeypatch.setattr("auto_research.auto_read.pipeline._download_pdf", fail_download)

    available, cache_hit, attempts, error = read_pipeline._get_cached_or_download_pdf("https://example.com/paper.pdf", target)

    assert available is True
    assert cache_hit is True
    assert attempts == 0
    assert error == ""


def test_read_worker_repairs_response_missing_mandatory_fields():
    class FakeLLM:
        def __init__(self):
            self.responses = [
                {"ok": True, "data": {"summary": "Incomplete"}, "error": ""},
                {
                    "ok": True,
                    "data": {
                        "motivation": "Why",
                        "method_summary": "How",
                        "limitations": "Limits",
                    },
                    "error": "",
                },
            ]

        def json_or_error(self, _prompt):
            return self.responses.pop(0)

    data, status, error, attempts = read_pipeline._run_agent_read(
        FakeLLM(),
        {"title": "Paper", "abstract": "Abstract"},
        "Source text",
    )

    assert status == "repaired"
    assert attempts == 2
    assert "mandatory reading fields" in error
    assert data == {"motivation": "Why", "method_summary": "How", "limitations": "Limits"}


def test_read_main_agent_returns_cross_summary_and_method_analysis(monkeypatch):
    class FakeMainLLM:
        enabled = True

        def __init__(self, _config, role=None, conversation_key="", persist_session=True, resume_session=False):
            self.role = role
            self.conversation_key = conversation_key
            self.persist_session = persist_session
            self.resume_session = resume_session

        def summary(self):
            return {"provider": "claude-code", "backend": "claude-code", "enabled": True, "session_id": "main-session"}

        def json_or_error(self, _prompt):
            return {
                "ok": True,
                "error": "",
                "data": {
                    "cross_summary": {
                        "overview": "Overview",
                        "common_themes": "Themes",
                        "method_comparison": "Comparison",
                        "limitations_comparison": "Limitations",
                        "next_stage_notes": "Next",
                    },
                    "method_analysis": {
                        "summary": "Method summary",
                        "method_differences": "Differences",
                        "pros_cons": [{"title": "Paper", "pros": "Pros", "cons": "Cons"}],
                    },
                },
            }

    monkeypatch.setattr("auto_research.auto_read.pipeline.LLMClient", FakeMainLLM)
    reading = {
        "content": {"title": "Paper", "method_summary": "Method", "limitations": "Limit", "relevance": "Relevant"},
        "metadata": {},
    }

    cross_summary, method_analysis, main_agent = read_pipeline._run_main_agent(
        "run-1",
        [reading],
        AppConfig(provider="mock"),
        log=lambda _msg: None,
    )

    assert cross_summary["overview"] == "Overview"
    assert method_analysis["method_differences"] == "Differences"
    assert method_analysis["pros_cons"][0]["pros"] == "Pros"
    assert main_agent["conversation_key"] == "run:run-1:main"
    assert main_agent["persist_session"] is True
    assert main_agent["resume_session"] is False
    assert main_agent["status"] == "accepted"


def test_read_rerun_resumes_existing_main_agent(monkeypatch):
    instances = []
    prompts = []

    class FakeLLM:
        def __init__(self, _config, role=None, conversation_key="", persist_session=True, resume_session=False):
            self.role = role
            self.conversation_key = conversation_key
            self.persist_session = persist_session
            self.resume_session = resume_session
            self.enabled = role == "read" and persist_session
            instances.append(self)

        def summary(self):
            return {
                "provider": "claude-code",
                "backend": "claude-code",
                "enabled": self.enabled,
                "session_id": "main-session" if self.persist_session else "worker-session",
            }

        def json_or_error(self, prompt):
            prompts.append(prompt)
            return {
                "ok": True,
                "error": "",
                "data": {
                    "cross_summary": {
                        "overview": "Overview",
                        "common_themes": "Themes",
                        "method_comparison": "Comparison",
                        "limitations_comparison": "Limitations",
                        "next_stage_notes": "Next",
                    },
                    "method_analysis": {
                        "summary": "Summary",
                        "method_differences": "Differences",
                        "pros_cons": [],
                    },
                },
            }

    monkeypatch.setattr("auto_research.auto_read.pipeline.LLMClient", FakeLLM)
    monkeypatch.setattr("auto_research.auto_read.pipeline._download_pdf", lambda _url, _target: (False, 0, ""))
    run_id, directory = create_run_dir("read_main_rerun_test")
    try:
        write_json(
            stage_dir(directory, "find") / "find_results.json",
            {"run_id": run_id, "articles": [{"id": "paper-1", "title": "Paper", "abstract": "Abstract"}]},
        )

        first = run_read(ReadRequest(run_id=run_id, max_papers=1), AppConfig(provider="mock"), log=lambda _msg: None)
        second = run_read(ReadRequest(run_id=run_id, max_papers=1), AppConfig(provider="mock"), log=lambda _msg: None)

        main_agents = [instance for instance in instances if instance.persist_session]
        assert [agent.resume_session for agent in main_agents] == [False, True]
        assert first["main_agent"]["invocation"] == "created"
        assert second["main_agent"]["invocation"] == "resumed"
        assert "This is an auto_read rerun" in prompts[-1]
    finally:
        delete_run(run_id)
