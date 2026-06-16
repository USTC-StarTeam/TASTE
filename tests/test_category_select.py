from find_support import filter_papers_by_selected_categories, select_relevant_categories
from auto_research.models import AppConfig


class CategoryLLM:
    enabled = True

    def json_or_none(self, _prompt):
        return {
            "selected_categories": [
                {"name": "datasets and benchmarks", "reason": "Useful for evaluation."},
                {"name": "foundation or frontier models, including LLMs", "reason": "Relevant to LLM agents."},
                {"name": "not a real category", "reason": "Should be ignored."},
            ],
            "rejected_categories": [
                {"name": "optimization", "reason": "Too generic for this profile."},
            ],
        }


class DisabledLLM:
    enabled = False


def _summary():
    return {
        "venue_id": "openreview_iclr",
        "venue": "ICLR",
        "year": 2026,
        "paper_count": 10,
        "category_summary": [
            {
                "name": "datasets and benchmarks",
                "count": 4,
                "sample_titles": ["Benchmarking LLM agents for paper review"],
                "sample_keywords": ["agents", "evaluation"],
            },
            {
                "name": "foundation or frontier models, including LLMs",
                "count": 3,
                "sample_titles": ["Large language model agents"],
                "sample_keywords": ["LLM", "agent"],
            },
            {
                "name": "optimization",
                "count": 3,
                "sample_titles": ["Convex optimization"],
                "sample_keywords": ["optimization"],
            },
        ],
    }


def test_category_llm_selection_keeps_only_valid_exact_categories(monkeypatch):
    monkeypatch.setenv("USE_LLM_CATEGORY_SELECT", "1")
    cfg = AppConfig(research_interest="LLM agents for research automation", researcher_profile="I care about evaluation benchmarks.")
    result = select_relevant_categories(_summary(), cfg, CategoryLLM(), max_categories=4)

    assert result["fallback_used"] is False
    assert [item["name"] for item in result["selected_categories"]] == [
        "datasets and benchmarks",
        "foundation or frontier models, including LLMs",
    ]
    assert result["selected_paper_count"] == 7
    assert "not a real category" not in str(result)


def test_category_fallback_selects_matching_categories_without_llm():
    cfg = AppConfig(provider="mock", research_interest="LLM agents benchmark evaluation")
    result = select_relevant_categories(_summary(), cfg, DisabledLLM(), max_categories=2)

    assert result["fallback_used"] is True
    assert result["selected_categories"]
    assert result["selected_categories"][0]["name"] in {
        "datasets and benchmarks",
        "foundation or frontier models, including LLMs",
    }


def test_filter_papers_by_selected_categories():
    papers = [
        {"id": "p1", "category": "datasets and benchmarks"},
        {"id": "p2", "category": "optimization"},
        {"id": "p3", "primary_area": "foundation or frontier models, including LLMs"},
    ]
    selection = {
        "selected_categories": [
            {"name": "datasets and benchmarks"},
            {"name": "foundation or frontier models, including LLMs"},
        ]
    }

    assert [paper["id"] for paper in filter_papers_by_selected_categories(papers, selection)] == ["p1", "p3"]
