import re

from auto_research.auto_find.pipeline import _evaluate_items, _prefilter_titles, _recommended, _screened_ranking
from auto_research.models import AppConfig


class BatchLLM:
    enabled = True

    def __init__(self):
        self.prompts = []

    def json_or_error(self, prompt: str):
        self.prompts.append(prompt)
        ids = re.findall(r"paper_\d+", prompt)
        if "evaluations" in prompt:
            return {
                "ok": True,
                "error": "",
                "data": {
                    "evaluations": [
                        {
                            "id": item_id,
                            "category": "Materials AI",
                            "fit_score": 8 if item_id != "paper_999" else 4,
                            "diversity_score": 7,
                            "hit_directions": ["生成式AI", "材料物理"],
                            "fit_explanation": "命中生成式AI和材料物理。",
                            "reason": "这篇文章研究生成模型如何服务材料发现。方法与用户画像中的生成式AI方向直接相关。它还连接材料物理问题，因此不是泛泛AI相关。推荐用于构建科学发现方向的候选研究线。",
                        }
                        for item_id in ids
                    ]
                },
            }
        return {
            "ok": True,
            "error": "",
            "data": {
                "selected": [
                    {
                        "id": item_id,
                        "fit_score": 8,
                        "diversity_score": 7,
                        "hit_directions": ["生成式AI", "材料物理"],
                        "category": "Materials AI",
                        "reason": "标题明确命中生成式AI和材料物理。",
                    }
                    for item_id in ids
                ]
            },
        }


def test_title_prefilter_batches_by_ten_and_appends_candidates():
    items = [{"id": f"paper_{index}", "title": f"Generative model for materials {index}"} for index in range(23)]
    llm = BatchLLM()
    cfg = AppConfig(provider="mock", research_interest="生成式AI 科学发现 材料物理", max_fetch_papers=40, llm_concurrency=4)
    logs: list[str] = []
    progress_updates: list[tuple[str, int, int, str]] = []

    selected = _prefilter_titles(
        items,
        cfg,
        llm,
        "TestVenue",
        log=logs.append,
        should_cancel=lambda: False,
        progress=lambda phase, current, total, message: progress_updates.append((phase, current, total, message)),
    )

    assert len(selected) == 23
    assert len(llm.prompts) == 3
    assert selected[0]["fit_score"] == 8
    assert selected[0]["score"] == 7.75
    assert not any("title filtering" in line for line in logs)
    assert progress_updates[-1] == ("llm_title_filter", 3, 3, "TestVenue: title filtering 100% (3/3 batches), candidates=23")


class Filter2LLM:
    enabled = True

    def json_or_error(self, prompt: str):
        ids = re.findall(r"paper_\d+", prompt)
        rows = []
        for item_id in ids:
            if item_id == "paper_0":
                rows.append({
                    "id": item_id,
                    "decision": "keep",
                    "fit_score": 8,
                    "diversity_score": 6,
                    "hit_directions": ["agents"],
                    "category": "Research Agents",
                    "reason": "明确相关。",
                })
            elif item_id == "paper_1":
                rows.append({
                    "id": item_id,
                    "decision": "uncertain",
                    "fit_score": 5,
                    "diversity_score": 4,
                    "hit_directions": [],
                    "category": "Research Agents",
                    "reason": "标题信息不足但可能相关。",
                })
            else:
                rows.append({
                    "id": item_id,
                    "decision": "reject",
                    "fit_score": 2,
                    "diversity_score": 1,
                    "hit_directions": [],
                    "category": "Unrelated",
                    "reason": "主题不相关。",
                })
        return {"ok": True, "error": "", "data": {"decisions": rows}}


def test_filter2_keeps_uncertain_and_rejects_only_clear_mismatches():
    items = [{"id": f"paper_{index}", "title": f"Paper title {index}"} for index in range(3)]
    cfg = AppConfig(provider="mock", research_interest="LLM agents for research automation", max_fetch_papers=10)

    selected = _prefilter_titles(
        items,
        cfg,
        Filter2LLM(),
        "TestVenue",
        log=lambda _msg: None,
        should_cancel=lambda: False,
    )

    assert [item["id"] for item in selected] == ["paper_0", "paper_1"]
    assert selected[0]["filter2_decision"] == "keep"
    assert selected[1]["filter2_decision"] == "uncertain"


def test_abstract_evaluation_filters_low_fit_recommendations():
    llm = BatchLLM()
    cfg = AppConfig(provider="mock", research_interest="生成式AI 科学发现 材料物理", max_recommended_papers=5, llm_concurrency=2)
    items = [
        {"id": "paper_001", "title": "Generative materials discovery", "abstract": "Materials and generative model.", "classification_source": "llm_inferred"},
        {"id": "paper_999", "title": "Generic privacy policy", "abstract": "Privacy only.", "classification_source": "llm_inferred"},
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    recommended = _recommended(evaluated, cfg)

    assert [item["id"] for item in recommended] == ["paper_001"]
    assert evaluated[0]["hit_directions"] == ["生成式AI", "材料物理"]


def test_abstract_evaluation_caps_diversity_when_fit_is_weak():
    llm = BatchLLM()
    cfg = AppConfig(provider="mock", research_interest="生成式AI 科学发现 材料物理", max_recommended_papers=5, llm_concurrency=2)
    items = [
        {"id": "paper_999", "title": "Generic privacy policy", "abstract": "Privacy only.", "classification_source": "llm_inferred"},
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)

    assert evaluated[0]["fit_score"] == 4
    assert evaluated[0]["diversity_score"] == 4
    assert evaluated[0]["score"] == 4.0
    assert "diversity_score cannot compensate for weak fit" in llm.prompts[0]


def test_screened_ranking_keeps_only_strong_fit_and_sorts_by_score():
    items = [
        {"id": "low", "fit_score": 6, "score": 9.9},
        {"id": "middle", "fit_score": 7, "score": 7.2},
        {"id": "top", "fit_score": 8, "score": 8.4},
    ]

    ranked = _screened_ranking(items)

    assert [item["id"] for item in ranked] == ["top", "middle"]
