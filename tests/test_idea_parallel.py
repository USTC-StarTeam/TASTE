import re

from auto_research.auto_idea.pipeline import _source_items, run_idea
from auto_research.models import AppConfig, IdeaRequest
from auto_research.storage import create_run_dir, delete_run, stage_dir, write_json


class FakeIdeaLLM:
    instances = []

    def __init__(self, _config, role=None, conversation_key="", resume_session=False):
        self.role = role
        self.conversation_key = conversation_key
        self.resume_session = resume_session
        self.enabled = True
        self.instances.append(self)

    def summary(self):
        return {"role": self.role, "enabled": True}

    def json_or_error(self, prompt: str):
        batch_match = re.search(r"Batch index: (\d+)", prompt)
        batch = int(batch_match.group(1)) if batch_match else 0
        return {
            "ok": True,
            "error": "",
            "data": [
                {
                    "id": f"cand-b{batch}-{index}",
                    "title": f"Idea {batch}-{index}",
                    "hypothesis": "H",
                    "min_experiment": "E",
                    "novelty": "HIGH",
                    "feasibility": "HIGH",
                    "score": 10 - index,
                    "inspired_by": [],
                }
                for index in range(1, 7)
            ],
        }

    def json_or_none(self, prompt: str):
        ids = re.findall(r"- (cand-b\d+-\d+):", prompt)
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
        assert {client.conversation_key for client in FakeIdeaLLM.instances} == {f"run:{run_id}:main"}
        assert all(client.resume_session for client in FakeIdeaLLM.instances)
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
