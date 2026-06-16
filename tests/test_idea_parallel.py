import re

from idea_pipeline import run_idea
from auto_research.models import AppConfig, IdeaRequest
from auto_research.storage import create_run_dir, delete_run, write_json


class FakeIdeaLLM:
    def __init__(self, _config, role=None):
        self.role = role
        self.enabled = True

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
    monkeypatch.setattr("idea_pipeline.LLMClient", FakeIdeaLLM)
    run_id, directory = create_run_dir("idea_parallel_test")
    try:
        write_json(
            directory / "find_results.json",
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
    finally:
        delete_run(run_id)
