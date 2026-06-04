import re

from auto_research.auto_idea.pipeline import _source_items, confirm_idea, run_idea
from auto_research.models import AppConfig, IdeaRequest
from auto_research.storage import create_run_dir, delete_run, read_json, stage_dir, write_json


class FakeIdeaLLM:
    instances = []

    def __init__(self, _config, role=None, conversation_key="", persist_session=True, resume_session=False):
        self.role = role
        self.conversation_key = conversation_key
        self.persist_session = persist_session
        self.resume_session = resume_session
        self.enabled = True
        self.prompts = []
        self.instances.append(self)

    def summary(self):
        return {"role": self.role, "enabled": True, "conversation_key": self.conversation_key, "persist_session": self.persist_session}

    def json_or_error(self, prompt: str):
        self.prompts.append(prompt)
        if "Evaluate the methods in the structured reading results" in prompt:
            return {
                "ok": True,
                "error": "",
                "data": {
                    "summary": "Evaluation summary",
                    "methods": [{"title": "Readable Title", "profile_alignment": "Strong"}],
                    "cross_method_assessment": "Cross-method assessment",
                    "recommended_focus": ["Focus"],
                },
            }
        if "You are an idea-exploration worker" in prompt:
            return {
                "ok": True,
                "error": "",
                "data": [
                    {
                        "title": f"Worker idea {self.conversation_key}",
                        "hypothesis": "H",
                        "motivation": "M",
                        "novelty": "N",
                        "feasibility": "F",
                        "inspired_by": ["Paper"],
                    }
                ],
            }
        if "Using your existing auto_read context, method evaluation, and worker candidate ideas" in prompt:
            return {
                "ok": True,
                "error": "",
                "data": {
                    "candidate_ideas": [
                        {
                            "title": f"Synthesized idea {index}",
                            "hypothesis": "H",
                            "motivation": "M",
                            "novelty": "N",
                            "feasibility": "F",
                            "inspired_by": ["Paper"],
                        }
                        for index in range(1, 7)
                    ],
                    "rejected_candidates": [{"title": "Rejected", "reason": "Duplicate"}],
                },
            }
        return {"ok": False, "error": "unexpected prompt", "data": None}

    def json_or_none(self, prompt: str):
        ids = re.findall(r"- (idea-candidate-\d+):", prompt)
        return {"selected": [{"id": item_id, "judge_score": 9.5 - index, "judge_reason": "strong"} for index, item_id in enumerate(ids[:3])]}


def test_parallel_idea_generation_uses_candidate_pool_and_judge(monkeypatch):
    FakeIdeaLLM.instances = []
    monkeypatch.setattr("auto_research.auto_idea.pipeline.LLMClient", FakeIdeaLLM)
    run_id, directory = create_run_dir("idea_parallel_test")
    try:
        find_dir = stage_dir(directory, "find")
        write_json(
            find_dir / "find_results.json",
            {
                "run_id": run_id,
                "articles": [{"title": f"Paper {index}", "url": f"https://example.com/{index}", "reason": "summary"} for index in range(8)],
            },
        )
        cfg = AppConfig(provider="openai", api_key="test", model="fake", max_ideas=3, idea_parallel_workers=4)
        result = run_idea(IdeaRequest(run_id=run_id, max_ideas=3, parallel_workers=4), cfg, log=lambda _msg: None)
        assert len(result["ideas"]) == 3
        assert result["ideas"][0]["judge_score"] == 9.5
        assert result["ideas"][0]["id"] == "idea-001"
        main_agents = [client for client in FakeIdeaLLM.instances if client.conversation_key == f"run:{run_id}:main"]
        worker_agents = [client for client in FakeIdeaLLM.instances if ":worker:auto_idea:" in client.conversation_key]
        assert len(main_agents) == 2
        assert all(client.resume_session and client.persist_session for client in main_agents)
        assert len(worker_agents) == 4
        assert all(not client.resume_session and not client.persist_session for client in worker_agents)
        saved = read_json(stage_dir(directory, "idea") / "ideas.json", {})
        assert len(saved["worker_candidates"]) == 4
        assert len(saved["candidate_pool"]) == 6
        assert saved["rejected_candidates"] == [{"title": "Rejected", "reason": "Duplicate"}]
    finally:
        delete_run(run_id)


def test_idea_stage_uses_read_content_without_metadata():
    run_id, directory = create_run_dir("idea_content_only_test")
    try:
        write_json(stage_dir(directory, "find") / "find_results.json", {"run_id": run_id, "articles": []})
        write_json(
            stage_dir(directory, "read") / "read_results.json",
            {
                "run_id": run_id,
                "readings": [
                    {
                        "content": {"title": "Readable Title", "summary": "Content summary"},
                        "metadata": {"url": "https://metadata.example/paper", "paper_id": "secret-id"},
                    }
                ],
                "cross_summary": {"overview": "Content-only synthesis", "common_themes": "", "method_comparison": "", "limitations_comparison": "", "next_stage_notes": ""},
                "method_analysis": {
                    "summary": "Method summary",
                    "method_differences": "Method differences",
                    "pros_cons": [{"title": "Readable Title", "pros": "Strong", "cons": "Costly"}],
                },
            },
        )

        items = _source_items(directory)

        read_items = [item for item in items if item["source"] == "read"]
        assert read_items == [{"source": "read", "title": "Readable Title", "url": "", "summary": "Content summary"}]
        method_items = [item for item in items if item["source"] == "method_analysis"]
        assert len(method_items) == 1
        assert "Method differences" in method_items[0]["summary"]
        assert all("metadata.example" not in str(item) and "secret-id" not in str(item) for item in items)
    finally:
        delete_run(run_id)


def test_idea_stage_main_agent_evaluates_methods_against_normalized_profile(monkeypatch):
    FakeIdeaLLM.instances = []
    monkeypatch.setattr("auto_research.auto_idea.pipeline.LLMClient", FakeIdeaLLM)
    run_id, directory = create_run_dir("idea_method_evaluation_test")
    try:
        write_json(
            stage_dir(directory, "find") / "find_results.json",
            {"run_id": run_id, "articles": [{"title": "Broad Signal", "reason": "Relevant"}]},
        )
        write_json(
            stage_dir(directory, "find") / "stage0_profile.json",
            {
                "profile": {
                    "explicit_profile": {"research_interest_summary": "NORMALIZED PROFILE"},
                    "filtering_hints": {"hard_exclusions": ["privacy"]},
                }
            },
        )
        write_json(
            stage_dir(directory, "read") / "read_results.json",
            {
                "run_id": run_id,
                "readings": [
                    {
                        "content": {
                            "title": "Readable Title",
                            "summary": "Summary",
                            "method_summary": "Method",
                            "experiments": "Evidence",
                            "limitations": "Risk",
                        },
                        "metadata": {"paper_id": "private-metadata"},
                    }
                ],
                "main_agent_summary": {"overview": "Read synthesis"},
                "method_analysis": {"summary": "Read method analysis"},
            },
        )

        result = run_idea(
            IdeaRequest(run_id=run_id, max_ideas=1),
            AppConfig(provider="openai", api_key="test", model="fake", research_interest="RAW PROFILE"),
            log=lambda _msg: None,
        )

        assert result["method_evaluation"]["summary"] == "Evaluation summary"
        saved = read_json(stage_dir(directory, "idea") / "ideas.json", {})
        assert saved["method_evaluation"] == result["method_evaluation"]
        evaluation_prompt = next(
            prompt
            for prompt in FakeIdeaLLM.instances[0].prompts
            if "Evaluate the methods in the structured reading results" in prompt
        )
        assert "NORMALIZED PROFILE" in evaluation_prompt
        assert "RAW PROFILE" not in evaluation_prompt
        assert "method_summary" in evaluation_prompt
        assert "private-metadata" not in evaluation_prompt
        assert FakeIdeaLLM.instances[0].conversation_key == f"run:{run_id}:main"
        assert FakeIdeaLLM.instances[0].resume_session is True
        worker_prompt = next(
            client.prompts[0]
            for client in FakeIdeaLLM.instances
            if ":worker:auto_idea:" in client.conversation_key
        )
        assert "NORMALIZED PROFILE" in worker_prompt
        assert "Evaluation summary" in worker_prompt
        assert "private-metadata" not in worker_prompt
        synthesis_prompt = next(
            prompt
            for prompt in FakeIdeaLLM.instances[0].prompts
            if "Using your existing auto_read context, method evaluation, and worker candidate ideas" in prompt
        )
        assert "Worker idea" in synthesis_prompt
        assert "Broad Signal" in synthesis_prompt
        assert "Evaluation summary" in synthesis_prompt
    finally:
        delete_run(run_id)


def test_idea_stage_preserves_fallbacks_and_logs_agent_failures(monkeypatch):
    logs = []

    class FailingIdeaLLM(FakeIdeaLLM):
        def json_or_error(self, prompt: str):
            self.prompts.append(prompt)
            return {"ok": False, "error": "No JSON object or array found", "data": None}

        def json_or_none(self, _prompt: str):
            return None

    FailingIdeaLLM.instances = []
    monkeypatch.setattr("auto_research.auto_idea.pipeline.LLMClient", FailingIdeaLLM)
    run_id, directory = create_run_dir("idea_fallback_test")
    try:
        write_json(
            stage_dir(directory, "find") / "find_results.json",
            {"run_id": run_id, "articles": [{"title": "Broad Signal", "url": "https://example.com", "reason": "Relevant"}]},
        )
        write_json(
            stage_dir(directory, "find") / "stage0_profile.json",
            {"profile": {"explicit_profile": {"research_interest_summary": "Profile"}}},
        )
        write_json(
            stage_dir(directory, "read") / "read_results.json",
            {"run_id": run_id, "readings": [{"content": {"title": "Paper", "summary": "Summary"}}]},
        )

        result = run_idea(
            IdeaRequest(run_id=run_id, max_ideas=2),
            AppConfig(provider="openai", api_key="test", model="fake"),
            log=logs.append,
        )

        assert len(result["ideas"]) == 2
        assert all(idea["status"] == "pending" for idea in result["ideas"])
        assert any("Main agent method evaluation unavailable" in message for message in logs)
        assert any("Idea worker 1 unavailable" in message for message in logs)
        assert any("preserving fallback ideas" in message for message in logs)
    finally:
        delete_run(run_id)


def test_idea_stage_preserves_ranked_candidates_when_judge_fails(monkeypatch):
    class FailingJudgeLLM(FakeIdeaLLM):
        def json_or_none(self, _prompt: str):
            return None

    FailingJudgeLLM.instances = []
    monkeypatch.setattr("auto_research.auto_idea.pipeline.LLMClient", FailingJudgeLLM)
    run_id, directory = create_run_dir("idea_judge_fallback_test")
    try:
        write_json(
            stage_dir(directory, "find") / "find_results.json",
            {"run_id": run_id, "articles": [{"title": "Broad Signal", "reason": "Relevant"}]},
        )

        result = run_idea(
            IdeaRequest(run_id=run_id, max_ideas=3),
            AppConfig(provider="openai", api_key="test", model="fake"),
            log=lambda _msg: None,
        )

        assert len(result["ideas"]) == 3
        assert all(idea["title"].startswith("Synthesized idea") for idea in result["ideas"])
    finally:
        delete_run(run_id)


def test_confirm_idea_persists_exactly_one_selection():
    run_id, directory = create_run_dir("idea_confirm_test")
    try:
        write_json(
            stage_dir(directory, "idea") / "ideas.json",
            {
                "run_id": run_id,
                "ideas": [
                    {"id": "idea-001", "title": "First", "status": "pending"},
                    {"id": "idea-002", "title": "Second", "status": "pending"},
                ],
            },
        )

        result = confirm_idea(run_id, "idea-002")

        assert result["selected_idea_id"] == "idea-002"
        assert [idea["status"] for idea in result["ideas"]] == ["deleted", "approved"]
        saved = read_json(stage_dir(directory, "idea") / "ideas.json", {})
        assert saved == result
    finally:
        delete_run(run_id)
