import json
import re
import time

import pytest

from auto_research.auto_find.category_select import select_relevant_categories
from auto_research.auto_find.local_rank import rank_papers_tfidf
from auto_research.auto_find import sources
from auto_research.auto_find import pipeline as find_pipeline
from auto_research.auto_find.pipeline import _abstract_enrichment_limits, _attach_abstract_language_fields, _enrich_missing_abstracts_for_adaptive_recall, _evaluate_items, _has_strong_topic_evidence, _is_transient_llm_service_error, _prefilter_titles, _read_candidates, _recommended, _repair_llm_alternative_route_false_negative, _run_diagnostics, _screened_ranking, _strict_strong_anchor_count, _triage_candidates, _venue_metadata_status_fields
from auto_research.models import AppConfig
from auto_research.jobs import JobCancelled


class BatchLLM:
    enabled = True

    def __init__(self):
        self.prompts = []
        self.temperatures = []

    def json_or_error(self, prompt: str, temperature=None):
        self.prompts.append(prompt)
        self.temperatures.append(temperature)
        ids = re.findall(r"paper_\d+", prompt)
        if "evaluations" in prompt:
            weak_prompt = any(
                marker in prompt.lower()
                for marker in [
                    "generic materials discovery",
                    "personalized text-to-image diffusion",
                    "distributionally robust generative recommendation",
                ]
            )
            return {
                "ok": True,
                "error": "",
                "data": {
                    "evaluations": [
                        {
                            "id": item_id,
                            "category": "Materials AI",
                            "fit_score": 4 if (item_id in {"paper_999", "paper_002"} or weak_prompt) else 8,
                            "diversity_score": 7,
                            "hit_directions": ["生成式AI", "材料物理"],
                            "topic_evidence": "weak: missing adaptive topic evidence" if (item_id in {"paper_999", "paper_002"} or weak_prompt) else "passed:adaptive_llm_topic_route",
                            "topic_evidence_supported": not (item_id in {"paper_999", "paper_002"} or weak_prompt),
                            "matched_topic_route": "adaptive route from current research profile",
                            "topic_evidence_basis": "abstract",
                            "missing_topic_evidence": ["missing direct topic evidence"] if (item_id in {"paper_999", "paper_002"} or weak_prompt) else [],
                            "fit_explanation": "缺少当前主题的直接摘要证据。" if (item_id in {"paper_999", "paper_002"} or weak_prompt) else "命中生成式AI和材料物理。",
                            "fit_explanation_zh": "缺少当前主题的直接摘要证据。" if (item_id in {"paper_999", "paper_002"} or weak_prompt) else "命中生成式AI和材料物理。",
                            "fit_explanation_en": "Missing direct abstract evidence for the current topic." if (item_id in {"paper_999", "paper_002"} or weak_prompt) else "Hits generative AI and materials physics.",
                            "reason": "该候选没有足够摘要证据支撑当前主题，只能作为边界候选。" if (item_id in {"paper_999", "paper_002"} or weak_prompt) else "这篇文章研究生成模型如何服务材料发现。方法与用户画像中的生成式AI方向直接相关。它还连接材料物理问题，因此不是泛泛AI相关。推荐用于构建科学发现方向的候选研究线。",
                            "reason_zh": "该候选没有足够摘要证据支撑当前主题，只能作为边界候选。" if (item_id in {"paper_999", "paper_002"} or weak_prompt) else "这篇文章研究生成模型如何服务材料发现。方法与用户画像中的生成式AI方向直接相关。它还连接材料物理问题，因此不是泛泛AI相关。推荐用于构建科学发现方向的候选研究线。",
                            "reason_en": "This candidate lacks direct abstract evidence for the current topic." if (item_id in {"paper_999", "paper_002"} or weak_prompt) else "This paper studies how generative models support materials discovery. It directly matches generative AI and materials physics.",
                        }
                        for item_id in ids
                    ]
                },
            }
        return {
            "ok": True,
            "error": "",
            "data": {
                "scored": [
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

    selected = _prefilter_titles(items, cfg, llm, "TestVenue", log=lambda _msg: None, should_cancel=lambda: False)

    assert len(selected) == 23
    assert len(llm.prompts) == 3
    assert selected[0]["fit_score"] == 8
    assert selected[0]["score"] == 7.75


def test_title_prefilter_ignores_local_topic_pseudo_categories_for_dynamic_groups(monkeypatch):
    monkeypatch.setenv("TITLE_DETAIL_CANDIDATE_TARGET", "20")
    items = [
        {
            "id": f"paper_{index}",
            "title": f"LLM Retrieval Benchmark Candidate {index}",
            "venue": "ICML",
            "year": 2026,
            "category": f"Local topic: candidate / {index}",
            "primary_area": f"Local topic: candidate / {index}",
            "classification_source": "uncategorized_title_index",
            "metadata": {"category_status": "no_official_categories"},
        }
        for index in range(30)
    ]
    reports = []
    llm = BatchLLM()
    selected = _prefilter_titles(
        items,
        AppConfig(provider="mock", research_interest="LLM-assisted retrieval benchmark systems", max_recommended_papers=20),
        llm,
        "ICML",
        log=lambda _msg: None,
        should_cancel=lambda: False,
        dynamic_title_filter=True,
        scan_all=True,
        title_filter_reports=reports,
    )

    assert len(selected) == 20
    assert len(llm.prompts) == 3
    assert reports[0]["groups"] == []
    assert reports[0]["title_filter_batches"] == 3


def test_abstract_evaluation_ranks_top_fit_before_low_fit():
    llm = BatchLLM()
    cfg = AppConfig(provider="mock", research_interest="生成式AI 科学发现 材料物理", max_recommended_papers=1, llm_concurrency=2)
    items = [
        {"id": "paper_001", "title": "Generative materials discovery", "abstract": "Materials and generative model.", "classification_source": "llm_inferred"},
        {"id": "paper_999", "title": "Generic privacy policy", "abstract": "Privacy only.", "classification_source": "llm_inferred"},
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    recommended = _recommended(evaluated, cfg)

    assert [item["id"] for item in recommended] == ["paper_001"]
    assert recommended[0]["evidence_tier"] == "strong_recommendation"
    assert evaluated[0]["hit_directions"] == ["生成式AI", "材料物理"]
    assert evaluated[0]["hit_directions_zh"] == ["生成式AI", "材料物理"]
    assert evaluated[0]["fit_explanation_en"]
    assert evaluated[0]["reason_en"]


def test_final_scoring_enriches_title_filtered_missing_abstract_before_llm(monkeypatch):
    def fill_with_openalex(rows, limit=80):
        for row in rows[:limit]:
            if row.get("id") == "paper_001":
                row["abstract"] = "This paper studies generative models for materials discovery with benchmark evaluation."
                row.setdefault("metadata", {})["abstract_source"] = "openalex"
        return rows

    monkeypatch.setattr(find_pipeline, "enrich_with_openalex", fill_with_openalex)
    monkeypatch.setattr(find_pipeline, "enrich_with_semantic_scholar", lambda rows, limit=20: rows)
    monkeypatch.setattr(find_pipeline, "enrich_with_arxiv_title_match", lambda rows, limit=40: rows)
    llm = BatchLLM()
    cfg = AppConfig(provider="mock", research_interest="生成式AI 科学发现 材料物理", max_recommended_papers=5, llm_concurrency=1)
    items = [{"id": "paper_001", "title": "Generative materials discovery", "abstract": "", "classification_source": "llm_inferred"}]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)

    assert evaluated[0]["abstract"].startswith("This paper studies")
    assert evaluated[0]["reason_source"] == "llm abstract evaluation"
    assert not evaluated[0].get("abstract_fetch_failed")
    assert llm.prompts


def test_final_scoring_missing_abstract_records_lookup_reason(monkeypatch):
    monkeypatch.setattr(find_pipeline, "enrich_with_openalex", lambda rows, limit=80: rows)
    monkeypatch.setattr(find_pipeline, "enrich_with_semantic_scholar", lambda rows, limit=20: rows)
    monkeypatch.setattr(find_pipeline, "enrich_with_arxiv_title_match", lambda rows, limit=40: rows)
    llm = BatchLLM()
    cfg = AppConfig(provider="mock", research_interest="生成式AI 科学发现 材料物理", max_recommended_papers=5, llm_concurrency=1)
    items = [{"id": "paper_001", "title": "Generative materials discovery", "abstract": "", "classification_source": "llm_inferred", "metadata": {"doi": "10.1145/example"}}]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)

    assert evaluated[0]["abstract_fetch_failed"] is True
    assert evaluated[0]["llm_final_scoring_skipped"] is True
    assert "doi_metadata_lookup_no_real_abstract" in evaluated[0]["abstract_fetch_failed_reason"]
    assert llm.prompts == []


def test_screened_ranking_uses_same_find_recommendation_gate_and_sorts_by_score():
    items = [
        {"id": "low", "fit_score": 6, "score": 9.9, "reason_source": "llm abstract evaluation", "topic_evidence": "passed:adaptive_llm_topic_route", "abstract": "real abstract"},
        {"id": "middle", "fit_score": 7, "score": 7.2, "reason_source": "llm abstract evaluation", "topic_evidence": "passed:adaptive_llm_topic_route", "abstract": "real abstract"},
        {"id": "top", "fit_score": 8, "score": 8.4, "reason_source": "llm abstract evaluation", "topic_evidence": "passed:adaptive_llm_topic_route", "abstract": "real abstract"},
        {"id": "fallback", "fit_score": 9, "score": 10.0, "reason_source": "adaptive profile fallback"},
    ]

    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model")
    ranked = _screened_ranking(items, cfg)
    recommended = _recommended(items, cfg)

    assert [item["id"] for item in ranked] == ["top", "middle", "low"]
    assert [item["id"] for item in recommended] == ["top", "middle", "low"]
    assert ranked[-1]["fit_score"] == 6
    assert all(item["evidence_tier"] == "final_llm_scored_candidate" for item in ranked)
    assert all(item["evidence_tier"] == "strong_recommendation" for item in recommended)
    assert "find_recommendation_reject_reason" not in items[0]


def test_final_scoring_forces_temperature_zero():
    llm = BatchLLM()
    cfg = AppConfig(
        provider="mock",
        research_interest="生成式AI 科学发现 材料物理",
        max_recommended_papers=5,
        llm_concurrency=1,
    )
    items = [
        {"id": "paper_001", "title": "Generative materials discovery", "abstract": "Materials and generative model.", "classification_source": "llm_inferred"},
    ]

    _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)

    assert llm.temperatures[-1] == 0.0


class OmittedRetryLLM(BatchLLM):
    def __init__(self, recover_on_attempt: int | None):
        super().__init__()
        self.recover_on_attempt = recover_on_attempt
        self.single_calls = 0

    def json_or_error(self, prompt: str, temperature=None):
        self.prompts.append(prompt)
        self.temperatures.append(temperature)
        ids = re.findall(r"paper_\d+", prompt)
        if "Candidate items" in prompt and "paper_002" in ids:
            ids = [item_id for item_id in ids if item_id != "paper_002"]
        elif "Candidate item" in prompt and "paper_002" in ids:
            self.single_calls += 1
            if self.recover_on_attempt is None or self.single_calls < self.recover_on_attempt:
                return {"ok": True, "error": "", "data": {"evaluations": []}}
        return {
            "ok": True,
            "error": "",
            "data": {
                "evaluations": [
                    {
                        "id": item_id,
                        "category": "Materials AI",
                        "fit_score": 8,
                        "diversity_score": 7,
                        "hit_directions": ["生成式AI", "材料物理"],
                        "topic_evidence": "passed:adaptive_llm_topic_route",
                        "topic_evidence_supported": True,
                        "matched_topic_route": "adaptive route from current research profile",
                        "topic_evidence_basis": "abstract",
                        "missing_topic_evidence": [],
                        "fit_explanation": "命中生成式AI和材料物理。",
                        "fit_explanation_zh": "命中生成式AI和材料物理。",
                        "fit_explanation_en": "Hits generative AI and materials physics.",
                        "reason": "这篇文章研究生成模型如何服务材料发现。方法与用户画像中的生成式AI方向直接相关。它还连接材料物理问题，因此不是泛泛AI相关。推荐用于构建科学发现方向的候选研究线。",
                        "reason_zh": "这篇文章研究生成模型如何服务材料发现。方法与用户画像中的生成式AI方向直接相关。它还连接材料物理问题，因此不是泛泛AI相关。推荐用于构建科学发现方向的候选研究线。",
                        "reason_en": "This paper studies how generative models support materials discovery.",
                    }
                    for item_id in ids
                ]
            },
        }


def test_omitted_final_scoring_retries_multiple_times_and_recovers(monkeypatch):
    monkeypatch.setenv("OMITTED_ITEM_RETRY_ATTEMPTS", "3")
    llm = OmittedRetryLLM(recover_on_attempt=2)
    cfg = AppConfig(provider="mock", research_interest="生成式AI 科学发现 材料物理", max_recommended_papers=5, llm_concurrency=1)
    items = [
        {"id": "paper_001", "title": "Generative materials discovery", "abstract": "Materials and generative model.", "classification_source": "llm_inferred"},
        {"id": "paper_002", "title": "Generative materials physics", "abstract": "Materials and generative model.", "classification_source": "llm_inferred"},
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    by_id = {item["id"]: item for item in evaluated}

    assert by_id["paper_002"]["reason_source"] == "llm abstract evaluation"
    assert by_id["paper_002"]["llm_retry_attempts"] == 2
    assert not by_id["paper_002"].get("llm_retry_exhausted")


def test_omitted_final_scoring_exhausted_items_are_marked_and_not_strong(monkeypatch):
    monkeypatch.setenv("OMITTED_ITEM_RETRY_ATTEMPTS", "2")
    llm = OmittedRetryLLM(recover_on_attempt=None)
    cfg = AppConfig(provider="mock", research_interest="生成式AI 科学发现 材料物理", max_recommended_papers=5, llm_concurrency=1)
    items = [
        {"id": "paper_001", "title": "Generative materials discovery", "abstract": "Materials and generative model.", "classification_source": "llm_inferred"},
        {"id": "paper_002", "title": "Generative materials physics", "abstract": "Materials and generative model.", "classification_source": "llm_inferred"},
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    by_id = {item["id"]: item for item in evaluated}
    diagnostics = _run_diagnostics({"evaluated_candidates": evaluated, "articles": _recommended(evaluated, cfg), "strong_recommendations": _recommended(evaluated, cfg), "read_candidates": [], "critique_candidates": [], "source_status": []})

    assert by_id["paper_002"]["reason_source"] == "adaptive profile fallback"
    assert by_id["paper_002"]["llm_retry_exhausted"] is True
    assert diagnostics["llm_retry_exhausted_count"] == 1
    assert "paper_002" not in [item["id"] for item in _recommended(evaluated, cfg)]


def test_final_scoring_prompt_preserves_valid_subdirections():
    llm = BatchLLM()
    cfg = AppConfig(
        provider="openai",
        api_key="test-key",
        model="test-model",
        research_interest="Generic semantic-conditioning retrieval benchmark; discrete retrieval smoke test",
        max_recommended_papers=5,
        llm_concurrency=1,
    )
    items = [
        {
            "id": "paper_001",
            "title": "Collaborative Retrieval Models for Benchmarking",
            "abstract": "A retrieval system uses diffusion and denoising for collaborative retrieval.",
            "classification_source": "llm_inferred",
        },
    ]

    _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)

    assert "Final Find recommendation contract" in llm.prompts[-1]
    assert "Do not apply a fixed global keyword table" in llm.prompts[-1]
    assert "route/foundation/claim labels" in llm.prompts[-1]

def test_recommendation_tie_break_uses_final_llm_combined_score_for_boundary_ties():
    cfg = AppConfig(provider="mock", max_recommended_papers=2)
    items = [
        {
            "id": "alpha",
            "title": "Alpha Transferable Preference Background",
            "abstract": "A real abstract with transferable preference modeling background.",
            "fit_score": 6.0,
            "llm_fit_score": 6.0,
            "combined_score": 6.0,
            "llm_combined_score": 6.0,
            "diversity_score": 6.0,
            "llm_diversity_score": 6.0,
            "title_llm_fit_score": 6.0,
        },
        {
            "id": "zeta",
            "title": "Zeta Direct LLM Recommendation Reranking",
            "abstract": "A real abstract with direct LLM reranking signals for recommendation.",
            "fit_score": 6.0,
            "llm_fit_score": 6.0,
            "combined_score": 6.5,
            "llm_combined_score": 6.5,
            "diversity_score": 7.0,
            "llm_diversity_score": 7.0,
            "title_llm_fit_score": 6.0,
        },
    ]

    ranked = find_pipeline._recommendable_ranked(items, cfg)

    assert [item["id"] for item in ranked[:2]] == ["zeta", "alpha"]


def test_recommendation_tie_break_preserves_stable_order_for_high_fit_ties():
    cfg = AppConfig(provider="mock", max_recommended_papers=2)
    items = [
        {
            "id": "alpha",
            "title": "Alpha Direct High Fit Paper",
            "abstract": "A real abstract with high-confidence relevance.",
            "fit_score": 8.0,
            "llm_fit_score": 8.0,
            "combined_score": 6.0,
            "llm_combined_score": 6.0,
            "diversity_score": 5.0,
            "llm_diversity_score": 5.0,
            "title_llm_fit_score": 5.0,
        },
        {
            "id": "zeta",
            "title": "Zeta Higher Combined High Fit Paper",
            "abstract": "A real abstract with high-confidence relevance.",
            "fit_score": 8.0,
            "llm_fit_score": 8.0,
            "combined_score": 9.0,
            "llm_combined_score": 9.0,
            "diversity_score": 9.0,
            "llm_diversity_score": 9.0,
            "title_llm_fit_score": 9.0,
        },
    ]

    ranked = find_pipeline._recommendable_ranked(items, cfg)

    assert [item["id"] for item in ranked[:2]] == ["alpha", "zeta"]


def test_topic_evidence_uses_source_text_not_llm_explanation():
    llm = BatchLLM()
    cfg = AppConfig(provider="mock", research_interest="LLM-assisted retrieval benchmark", max_recommended_papers=5, llm_concurrency=1)
    items = [
        {
            "id": "paper_001",
            "title": "Generic materials discovery",
            "abstract": "Materials discovery with a generic generative model for crystal design.",
            "classification_source": "llm_inferred",
        }
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    recommended = _recommended(evaluated, cfg)

    assert evaluated[0]["topic_evidence"].startswith("weak:")
    assert [item["id"] for item in recommended] == ["paper_001"]
    assert "find_recommendation_reject_reason" not in evaluated[0]


def test_topic_audit_fields_do_not_create_hidden_find_gate():
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", max_recommended_papers=5)
    items = [
        {
            "id": "weak_topic_high_llm",
            "title": "High LLM ranked paper",
            "abstract": "This real abstract gives enough method and evaluation detail for final title and abstract LLM scoring.",
            "fit_score": 8.0,
            "diversity_score": 6.0,
            "score": 8.0,
            "reason_source": "llm abstract evaluation",
            "recommend_for_deep_reading": True,
            "topic_evidence": "weak: audit note only",
            "topic_evidence_supported": False,
            "evidence_role": "foundation_borrowing",
        },
        {
            "id": "low_llm_not_recommended",
            "title": "Low LLM ranked paper",
            "abstract": "This real abstract exists; its lower final LLM score should affect only ranking order.",
            "fit_score": 6.0,
            "diversity_score": 6.0,
            "score": 6.0,
            "reason_source": "llm abstract evaluation",
            "recommend_for_deep_reading": True,
            "topic_evidence": "passed: audit route",
            "topic_evidence_supported": True,
        },
    ]

    recommended = _recommended(items, cfg)

    assert [item["id"] for item in recommended] == ["weak_topic_high_llm", "low_llm_not_recommended"]
    assert items[0]["find_recommendation"] is True
    assert items[1]["find_recommendation"] is True
    assert "find_recommendation_reject_reason" not in items[0]
    assert "find_recommendation_reject_reason" not in items[1]


def test_diagnostics_reports_strong_and_read_candidate_semantics():
    artifacts = {
        "evaluated_candidates": [{"reason_source": "llm abstract evaluation"}],
        "articles": [],
        "strong_recommendations": [],
        "read_candidates": [{"id": "readable"}],
        "critique_candidates": [{"id": "weak"}],
        "source_status": [],
    }

    diagnostics = _run_diagnostics(artifacts)

    assert diagnostics["strong_recommendation_count"] == 0
    assert diagnostics["read_candidate_count"] == 1
    assert diagnostics["critique_candidate_count"] == 1
    assert {warning["code"] for warning in diagnostics["warnings"]} >= {"no_strong_recommendations", "read_candidates_include_non_recommended_items"}


def test_category_selection_defaults_to_adaptive_deterministic_fallback(monkeypatch):
    monkeypatch.delenv("USE_LLM_CATEGORY_SELECT", raising=False)
    llm = BatchLLM()
    cfg = AppConfig(provider="mock", research_interest="LLM-assisted retrieval benchmark")
    category_summary = {
        "venue_id": "test",
        "venue": "TestVenue",
        "year": 2026,
        "paper_count": 300,
        "category_summary": [
            {"name": "foundation or frontier models, including LLMs", "count": 120, "sample_titles": ["Large language model reasoning"], "sample_keywords": ["LLM"]},
            {"name": "generative models", "count": 100, "sample_titles": ["Diffusion models"], "sample_keywords": ["diffusion"]},
            {"name": "computer vision", "count": 80, "sample_titles": ["Image segmentation"], "sample_keywords": ["vision"]},
        ],
    }

    selection = select_relevant_categories(category_summary, cfg, llm)

    assert selection["fallback_used"] is True
    assert llm.prompts == []
    selected = [row["name"] for row in selection["selected_categories"]]
    assert "foundation or frontier models, including LLMs" in selected
    assert "generative models" in selected
    assert all("adaptive" in row["reason"].lower() for row in selection["selected_categories"])


def test_category_selection_uses_llm_by_default_for_live_provider(monkeypatch):
    monkeypatch.delenv("USE_LLM_CATEGORY_SELECT", raising=False)

    class CategoryLLM(BatchLLM):
        def json_or_none(self, prompt: str, temperature=None):
            self.prompts.append(prompt)
            self.temperatures.append(temperature)
            return {
                "selected_categories": [
                    {"name": "foundation or frontier models, including LLMs", "reason": "matches the current profile"},
                    {"name": "generative models", "reason": "adjacent route from the current profile"},
                ],
                "rejected_categories": [
                    {"name": "computer vision", "reason": "not supported by the current profile"},
                ],
            }

    llm = CategoryLLM()
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", research_interest="LLM-assisted retrieval benchmark")
    category_summary = {
        "venue_id": "test",
        "venue": "TestVenue",
        "year": 2026,
        "paper_count": 300,
        "category_summary": [
            {"name": "foundation or frontier models, including LLMs", "count": 120, "sample_titles": ["Large language model reasoning"], "sample_keywords": ["LLM"]},
            {"name": "generative models", "count": 100, "sample_titles": ["Diffusion models"], "sample_keywords": ["diffusion"]},
            {"name": "computer vision", "count": 80, "sample_titles": ["Image segmentation"], "sample_keywords": ["vision"]},
        ],
    }

    selection = select_relevant_categories(category_summary, cfg, llm)

    assert selection["fallback_used"] is False
    assert selection["selection_mode"] == "llm_adaptive_category_select"
    assert len(llm.prompts) == 1
    assert [row["name"] for row in selection["selected_categories"]] == [
        "foundation or frontier models, including LLMs",
        "generative models",
    ]


def test_category_selection_uses_llm_only_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("USE_LLM_CATEGORY_SELECT", "1")

    class CategoryLLM(BatchLLM):
        def json_or_none(self, prompt: str, temperature=None):
            self.prompts.append(prompt)
            self.temperatures.append(temperature)
            return {"selected_categories": [{"name": "generative models", "reason": "matches the current profile"}]}

    llm = CategoryLLM()
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", research_interest="LLM-assisted retrieval benchmark")
    category_summary = {
        "venue_id": "test",
        "venue": "TestVenue",
        "year": 2026,
        "paper_count": 300,
        "category_summary": [
            {"name": "foundation or frontier models, including LLMs", "count": 120, "sample_titles": ["Large language model reasoning"], "sample_keywords": ["LLM"]},
            {"name": "generative models", "count": 100, "sample_titles": ["Diffusion models"], "sample_keywords": ["diffusion"]},
        ],
    }

    selection = select_relevant_categories(category_summary, cfg, llm)

    assert selection["fallback_used"] is False
    assert selection["selection_mode"] == "llm_adaptive_category_select"
    assert llm.temperatures == [0.0]
    assert "fixed global topic list" in llm.prompts[0]
    assert [row["name"] for row in selection["selected_categories"]] == ["generative models"]

def test_final_display_score_is_recommendation_score_with_stable_audit_score():
    llm = BatchLLM()
    cfg = AppConfig(provider="mock", research_interest="LLM-assisted retrieval benchmark", max_recommended_papers=5, llm_concurrency=1)
    items = [
        {
            "id": "paper_001",
            "title": "Diffusion Recommender with Language Model Signals",
            "abstract": "A retrieval system uses discrete retrieval and large language model semantic signals for preference ranking.",
            "classification_source": "llm_inferred",
            "local_score": 0.2,
            "local_rank": 3,
        }
    ]

    first = _evaluate_items([dict(items[0])], cfg, llm, "articles", log=lambda _msg: None)[0]
    second = _evaluate_items([dict(items[0])], cfg, llm, "articles", log=lambda _msg: None)[0]

    assert first["score_source"] == "llm_title_abstract_score_only"
    assert first["score"] == second["score"]
    assert first["llm_fit_score"] == 8.0
    assert first["score"] == first["recommendation_score"]
    assert first["score"] == 8.0
    assert first["combined_score"] <= first["recommendation_score"]
    assert first["stable_source_base_score"] <= first["stable_source_score"]


def test_topic_routes_allow_diffusion_recommendation_without_llm():
    llm = BatchLLM()
    cfg = AppConfig(provider="mock", research_interest="LLM semantic condition retrieval benchmark; discrete retrieval benchmark", max_recommended_papers=5, llm_concurrency=1)
    items = [
        {
            "id": "paper_001",
            "title": "Discrete Diffusion for Sequential Recommendation",
            "abstract": "A retrieval system uses discrete retrieval and denoising to model user preferences for sequential retrieval.",
            "classification_source": "llm_inferred",
        }
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    recommended = _recommended(evaluated, cfg)

    assert evaluated[0]["topic_evidence"].startswith("passed:")
    assert evaluated[0]["topic_evidence_source"] == "llm_adaptive"
    assert recommended and recommended[0]["evidence_tier"] == "strong_recommendation"


def test_passed_single_alternative_route_is_not_weakened_by_unmatched_routes():
    class RouteLLM(BatchLLM):
        def json_or_error(self, prompt: str, temperature=None):
            self.prompts.append(prompt)
            self.temperatures.append(temperature)
            return {
                "ok": True,
                "error": "",
                "data": {
                    "evaluations": [
                        {
                            "id": "paper_001",
                            "category": "Discrete diffusion",
                            "fit_score": 7,
                            "diversity_score": 4,
                            "hit_directions": ["discrete retrieval"],
                            "topic_evidence": "passed: discrete retrieval language model route",
                            "topic_evidence_supported": True,
                            "matched_topic_route": "discrete retrieval language model",
                            "topic_evidence_basis": "abstract",
                            "missing_topic_evidence": ["recommendation application", "route-specific deployment details"],
                            "fit_explanation": "摘要直接支持离散扩散语言模型路线。",
                            "fit_explanation_zh": "摘要直接支持离散扩散语言模型路线。",
                            "fit_explanation_en": "The abstract directly supports the discrete retrieval language-model route.",
                            "reason": "该论文命中一个由当前研究主题自动生成的替代路线；未覆盖其他路线不应削弱该证据。",
                            "reason_zh": "该论文命中一个由当前研究主题自动生成的替代路线；未覆盖其他路线不应削弱该证据。",
                            "reason_en": "It hits one generated alternative route; missing other routes should not weaken it.",
                        }
                    ]
                },
            }

    llm = RouteLLM()
    cfg = AppConfig(
        provider="mock",
        research_interest="LLM semantic condition retrieval benchmark; discrete retrieval language model",
        max_recommended_papers=5,
        llm_concurrency=1,
    )
    items = [
        {
            "id": "paper_001",
            "title": "Scaling Behavior of Discrete Diffusion Language Models",
            "abstract": "This paper studies scaling laws for discrete retrieval language models and generation behavior.",
            "classification_source": "llm_inferred",
        }
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    recommended = _recommended(evaluated, cfg)

    assert evaluated[0]["topic_evidence"].startswith("passed:")
    assert evaluated[0]["missing_topic_evidence"] == []
    assert evaluated[0]["unmatched_topic_routes"] == ["recommendation application", "route-specific deployment details"]
    assert [item["id"] for item in recommended] == ["paper_001"]
    assert not evaluated[0].get("not_positive_support")
    assert "Do not decide downstream experimental support here" in llm.prompts[-1]
    assert "Find recommends papers for Read" in llm.prompts[-1]


def test_local_rank_reports_adaptive_recall_not_keyword_table():
    papers = [
        {"id": "hit", "title": "Discrete retrieval benchmark", "abstract": "A recommender uses discrete retrieval."},
        {"id": "miss", "title": "Vision benchmark", "abstract": "A visual recognition benchmark."},
    ]

    ranked, report = rank_papers_tfidf(papers, "discrete retrieval benchmark", global_limit=2)

    assert ranked[0]["id"] == "hit"
    assert report["adaptive_profile_signal_count"] > 0
    assert report["adaptive_profile_phrase_count"] > 0
    assert report["profile_signal_source"] == "current research_interest/profile"
    assert "query_terms" not in report
    assert "query_phrases" not in report
    assert "local_query_phrase_matches" not in ranked[0]
    assert "local_profile_phrase_match_count" in ranked[0]
    assert "candidate retrieval, not as strong evidence" in ranked[0]["local_filter_reason"]


def test_local_rank_fills_global_recall_when_single_category_cap_is_smaller():
    papers = [
        {
            "id": f"paper_{index}",
            "title": f"Diffusion recommendation candidate {index}",
            "abstract": "A retrieval system uses diffusion modeling for user preference prediction.",
            "category": "unknown",
        }
        for index in range(350)
    ]

    ranked, report = rank_papers_tfidf(
        papers,
        "retrieval benchmark",
        per_category_limit=200,
        global_limit=300,
    )

    assert len(ranked) == 300
    assert report["balanced_selected_count"] == 200
    assert report["selected_count"] == 300


def test_recommended_caps_strong_items_to_five_per_enabled_channel():
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", max_recommended_papers=100)
    items = [
        {
            "id": f"strong_{index}",
            "title": f"Strong paper {index}",
            "fit_score": 8,
            "score": 8 - index * 0.01,
            "recommendation_score": 8 - index * 0.01,
            "stable_rank_score": 8 - index * 0.01,
            "topic_evidence": "passed:adaptive_llm_topic_route",
            "topic_evidence_supported": True,
            "reason_source": "llm abstract evaluation",
            "abstract": "This abstract provides direct adaptive topic evidence for the current research profile.",
        }
        for index in range(80)
    ]

    recommended = _recommended(items, cfg, source_count=5)
    screened = _screened_ranking(items, cfg)

    assert len(recommended) == 25
    assert len(screened) == 80
    assert recommended[0]["id"] == "strong_0"


def test_contradictory_passed_topic_evidence_is_not_strong():
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", max_recommended_papers=5)
    items = [
        {
            "id": "contradictory",
            "title": "Diffusion Language Model",
            "fit_score": 8,
            "score": 8,
            "topic_evidence": "passed:discrete retrieval method; weak due to missing recommendation application",
            "topic_evidence_supported": True,
            "reason_source": "llm abstract evaluation",
            "abstract": "This paper studies discrete retrieval language models for text generation.",
        }
    ]

    recommended = _recommended(items, cfg)

    assert [item["id"] for item in recommended] == ["contradictory"]
    assert recommended[0]["find_recommendation"] is True


def test_foundation_borrowing_route_is_not_user_visible_recommendation():
    class FoundationLLM(BatchLLM):
        def json_or_error(self, prompt: str, temperature=None):
            self.prompts.append(prompt)
            self.temperatures.append(temperature)
            return {
                "ok": True,
                "error": "",
                "data": {
                    "evaluations": [
                        {
                            "id": "paper_001",
                            "category": "Recommendation foundation",
                            "fit_score": 7.5,
                            "diversity_score": 5.5,
                            "hit_directions_zh": ["基础借鉴路线"],
                            "hit_directions_en": ["foundation route"],
                            "topic_evidence": "passed:foundation:retrieval benchmark backbone for the current compound topic",
                            "topic_evidence_supported": True,
                            "matched_topic_route": "foundation retrieval benchmark backbone",
                            "topic_evidence_basis": "abstract",
                            "missing_topic_evidence": ["LLM component not covered by this foundation paper"],
                            "fit_explanation": "摘要支持可借鉴的检索基准基础路线，但不声称覆盖完整复合目标。",
                            "fit_explanation_zh": "摘要支持可借鉴的检索基准基础路线，但不声称覆盖完整复合目标。",
                            "fit_explanation_en": "The abstract supports a useful retrieval-benchmark foundation route without claiming the full compound target.",
                            "reason": "该论文可作为当前复合研究目标的基础借鉴论文；缺失的完整目标组件只作为未覆盖路线记录。",
                            "reason_zh": "该论文可作为当前复合研究目标的基础借鉴论文；缺失的完整目标组件只作为未覆盖路线记录。",
                            "reason_en": "This is a foundation/borrowing paper for the current compound topic; missing full-target components are recorded as unmatched routes.",
                        }
                    ]
                },
            }

    llm = FoundationLLM()
    cfg = AppConfig(
        provider="mock",
        research_interest="大模型和扩散融合的推荐",
        max_recommended_papers=5,
        llm_concurrency=1,
    )
    items = [
        {
            "id": "paper_001",
            "title": "Collaborative Retrieval Models for Benchmarking",
            "abstract": "This paper develops a diffusion backbone for collaborative retrieval with denoising-based preference modeling.",
            "classification_source": "llm_inferred",
        }
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    recommended = _recommended(evaluated, cfg)

    assert [item["id"] for item in recommended] == ["paper_001"]
    assert evaluated[0]["evidence_role"] == "foundation_borrowing"
    assert evaluated[0]["topic_evidence_audit_only"] is True
    assert "find_recommendation_reject_reason" not in evaluated[0]
    assert not evaluated[0].get("not_positive_support")


def test_source_guard_allows_missing_route_specific_phrase_without_treating_it_as_no_recommendation():
    class WeakRouteSpecificLLM(BatchLLM):
        def json_or_error(self, prompt: str, temperature=None):
            self.prompts.append(prompt)
            self.temperatures.append(temperature)
            return {
                "ok": True,
                "error": "",
                "data": {
                    "evaluations": [
                        {
                            "id": "paper_001",
                            "category": "Diffusion recommendation",
                            "fit_score": 5.0,
                            "diversity_score": 4.0,
                            "hit_directions": ["retrieval benchmark"],
                            "topic_evidence": "weak: missing LLM and discrete retrieval route-specific deployment recommendation",
                            "topic_evidence_supported": False,
                            "matched_topic_route": "",
                            "topic_evidence_basis": "abstract",
                            "missing_topic_evidence": ["No large models or LLMs; no semantic condition; no discrete retrieval or route-specific deployment recommendation"],
                            "fit_explanation": "缺少其他替代路线组件。",
                            "fit_explanation_zh": "缺少其他替代路线组件。",
                            "fit_explanation_en": "Missing other alternative-route components.",
                            "reason": "摘要明确是检索基准，但缺少特定部署路线。",
                            "reason_zh": "摘要明确是检索基准，但缺少特定部署路线。",
                            "reason_en": "The abstract clearly supports retrieval benchmark but lacks the route-specific deployment route.",
                        }
                    ]
                },
            }

    llm = WeakRouteSpecificLLM()
    cfg = AppConfig(
        provider="openai",
        api_key="test-key",
        model="test-model",
        research_interest="Generic semantic-conditioning retrieval benchmark; discrete retrieval smoke test",
        max_recommended_papers=5,
        llm_concurrency=1,
    )
    items = [
        {
            "id": "paper_001",
            "title": "Unleashing Retrieval Benchmarks for Diversified Sequential Evidence Selection",
            "abstract": "This paper designs a retrieval benchmark for diversified sequential evidence selection. It uses retrieval inference and query-evidence modeling to improve retrieval systems.",
            "classification_source": "llm_inferred",
        }
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    recommended = _recommended(evaluated, cfg)

    assert not evaluated[0].get("llm_alternative_route_false_negative_audited")
    assert not evaluated[0].get("llm_alternative_route_false_negative_repaired")
    assert evaluated[0]["topic_evidence"].startswith("weak:")
    assert [item["id"] for item in recommended] == ["paper_001"]
    assert "find_recommendation_reject_reason" not in evaluated[0]
    assert not evaluated[0].get("not_positive_support")


def test_passed_topic_evidence_fit_score_is_consistent_with_strong_gate():
    class PassedLowFitLLM(BatchLLM):
        def json_or_error(self, prompt: str, temperature=None):
            self.prompts.append(prompt)
            self.temperatures.append(temperature)
            return {
                "ok": True,
                "error": "",
                "data": {
                    "evaluations": [
                        {
                            "id": "paper_001",
                            "category": "Foundation",
                            "fit_score": 1.5,
                            "diversity_score": 1.0,
                            "hit_directions": ["retrieval benchmark"],
                            "topic_evidence": "passed:foundation:collaborative diffusion models for recommendation",
                            "topic_evidence_supported": True,
                            "matched_topic_route": "foundation retrieval benchmark",
                            "topic_evidence_basis": "abstract",
                            "missing_topic_evidence": [],
                            "fit_explanation": "摘要支持检索基准基础路线。",
                            "fit_explanation_zh": "摘要支持检索基准基础路线。",
                            "fit_explanation_en": "The abstract supports a diffusion-recommendation foundation route.",
                            "reason": "LLM给出了passed证据但fit分数过低。",
                            "reason_zh": "LLM给出了passed证据但fit分数过低。",
                            "reason_en": "The LLM returned passed evidence with a sub-threshold fit score.",
                        }
                    ]
                },
            }

    llm = PassedLowFitLLM()
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", research_interest="retrieval benchmark", max_recommended_papers=5, llm_concurrency=1)
    items = [{"id": "paper_001", "title": "Collaborative Retrieval Models for Benchmarking", "abstract": "This paper develops retrieval models for benchmark systems with collaborative evidence selection over query-document signals.", "classification_source": "llm_inferred"}]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    recommended = _recommended(evaluated, cfg)

    assert not evaluated[0].get("llm_passed_topic_fit_consistency_repaired")
    assert evaluated[0]["fit_score"] == 1.5
    assert [item["id"] for item in recommended] == ["paper_001"]
    assert "find_recommendation_reject_reason" not in evaluated[0]
    assert evaluated[0]["find_recommendation"] is True


def test_source_guard_repairs_llm_or_route_false_negative_for_diffusion_recommendation():
    class WeakAlternativeRouteLLM(BatchLLM):
        def json_or_error(self, prompt: str, temperature=None):
            self.prompts.append(prompt)
            self.temperatures.append(temperature)
            return {
                "ok": True,
                "error": "",
                "data": {
                    "evaluations": [
                        {
                            "id": "paper_001",
                            "category": "Diffusion recommendation",
                            "fit_score": 2.0,
                            "diversity_score": 3.0,
                            "hit_directions": ["retrieval benchmark"],
                            "topic_evidence": "weak:no direct match because the paper lacks LLM, semantic condition, and discrete route-specific deployment components",
                            "topic_evidence_supported": False,
                            "matched_topic_route": "",
                            "topic_evidence_basis": "abstract",
                            "missing_topic_evidence": [
                                "missing large language model",
                                "missing semantic condition",
                                "missing route-specific deployment details",
                            ],
                            "fit_explanation": "LLM误把其他替代路线当成累计要求。",
                            "fit_explanation_zh": "LLM误把其他替代路线当成累计要求。",
                            "fit_explanation_en": "The LLM incorrectly treats other alternative routes as cumulative requirements.",
                            "reason": "摘要已经支持推荐系统中的扩散模型，但缺少其他路线组件。",
                            "reason_zh": "摘要已经支持推荐系统中的扩散模型，但缺少其他路线组件。",
                            "reason_en": "The abstract supports diffusion models for recommendation but misses other route components.",
                        }
                    ]
                },
            }

    llm = WeakAlternativeRouteLLM()
    cfg = AppConfig(
        provider="openai",
        api_key="test-key",
        model="test-model",
        research_interest="Generic semantic-conditioning retrieval benchmark; discrete retrieval smoke test",
        max_recommended_papers=5,
        llm_concurrency=1,
    )
    items = [
        {
            "id": "paper_001",
            "title": "Collaborative Retrieval Models for Benchmarking",
            "abstract": "This paper develops retrieval models for benchmark systems. It denoises query-document evidence signals and improves collaborative retrieval with a diffusion-based backbone.",
            "classification_source": "llm_inferred",
        }
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    recommended = _recommended(evaluated, cfg)

    assert not evaluated[0].get("llm_alternative_route_false_negative_audited")
    assert not evaluated[0].get("llm_alternative_route_false_negative_repaired")
    assert evaluated[0]["topic_evidence"].startswith("weak:")
    assert [item["id"] for item in recommended] == ["paper_001"]
    assert "find_recommendation_reject_reason" not in evaluated[0]
    assert evaluated[0]["find_recommendation"] is True


def test_self_contradictory_foundation_explanation_is_demoted_from_strong():
    cfg = AppConfig(
        provider="openai",
        api_key="test-key",
        model="test-model",
        research_interest="Generic semantic-conditioning retrieval benchmark; discrete retrieval smoke test",
        max_recommended_papers=5,
    )
    items = [
        {
            "id": "paper_bad_foundation",
            "title": "Language Ranker: A Lightweight Ranking framework for LLM Decoding",
            "abstract": "This paper revisits LLM generation through the lens of ranking and proposes a lightweight module to rerank candidate responses during decoding.",
            "fit_score": 6.0,
            "diversity_score": 6.0,
            "score": 6.0,
            "reason_source": "llm abstract evaluation",
            "topic_evidence": "passed:foundation:source-supported adaptive route",
            "topic_evidence_supported": True,
            "evidence_role": "foundation_borrowing",
            "matched_topic_route": "",
            "topic_evidence_basis": "abstract",
            "missing_topic_evidence": [],
            "hit_directions_zh": ["大语言模型解码"],
            "hit_directions_en": ["LLM decoding"],
            "fit_explanation_zh": "论文将LLM解码类比为排序，但未涉及扩散模型、离散扩散、特定部署场景或具体的生成式推荐方法，与研究兴趣核心不符。",
            "reason_zh": "摘要仅提出解码过程的排序类比，缺乏与扩散、离散扩散、生成式推荐或可复现实验协议相关的具体技术或系统组件。",
        }
    ]

    recommended = _recommended(items, cfg)

    assert [item["id"] for item in recommended] == ["paper_bad_foundation"]
    assert "find_recommendation_reject_reason" not in items[0]
    assert not items[0].get("not_positive_support")


def test_framework_does_not_demote_foundation_by_hard_coded_project_topic_words():
    class GenericFoundationLLM(BatchLLM):
        def json_or_error(self, prompt: str, temperature=None):
            self.prompts.append(prompt)
            self.temperatures.append(temperature)
            return {
                "ok": True,
                "error": "",
                "data": {
                    "evaluations": [
                        {
                            "id": "paper_001",
                            "category": "Generic diffusion",
                            "fit_score": 7.0,
                            "diversity_score": 6.0,
                            "hit_directions_zh": ["基础借鉴路线"],
                            "hit_directions_en": ["foundation route"],
                            "topic_evidence": "passed:foundation:generic discrete retrieval method",
                            "topic_evidence_supported": True,
                            "matched_topic_route": "foundation discrete retrieval method",
                            "topic_evidence_basis": "abstract",
                            "missing_topic_evidence": [],
                            "fit_explanation": "摘要支持通用离散扩散方法；是否足以服务当前项目由本轮 LLM 路由和后续精读决定。",
                            "fit_explanation_zh": "摘要支持通用离散扩散方法；是否足以服务当前项目由本轮 LLM 路由和后续精读决定。",
                            "fit_explanation_en": "The abstract supports a generic discrete retrieval method; whether it serves the current project is determined by the run-specific LLM route and later reading.",
                            "reason": "该论文提供可借鉴的扩散方法基础，但不能证明推荐、LLM语义条件或特定部署组件成立。",
                            "reason_zh": "该论文提供可借鉴的扩散方法基础，但不能证明推荐、LLM语义条件或特定部署组件成立。",
                            "reason_en": "The paper provides a reusable diffusion-method foundation, but it does not prove recommendation, LLM semantic conditioning, or route-specific deployment components.",
                        }
                    ]
                },
            }

    llm = GenericFoundationLLM()
    cfg = AppConfig(
        provider="openai",
        api_key="test-key",
        model="test-model",
        research_interest="Generic semantic-conditioning retrieval benchmark; discrete retrieval smoke test",
        max_recommended_papers=5,
        llm_concurrency=1,
    )
    items = [
        {
            "id": "paper_001",
            "title": "Generalized Discrete Diffusion with Self-Correction",
            "abstract": "This paper improves discrete retrieval sampling for language generation and categorical generative modeling.",
            "classification_source": "llm_inferred",
        }
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    recommended = _recommended(evaluated, cfg)

    assert not evaluated[0].get("foundation_demoted_from_strong")
    assert [item["id"] for item in recommended] == ["paper_001"]
    assert "find_recommendation_reject_reason" not in evaluated[0]


def test_chinese_llm_diffusion_fusion_interest_allows_diffrec_route():
    llm = BatchLLM()
    cfg = AppConfig(provider="mock", research_interest="大模型和扩散融合的推荐", max_recommended_papers=5, llm_concurrency=1)
    items = [
        {
            "id": "paper_001",
            "title": "Collaborative Retrieval Models for Benchmarking",
            "abstract": "A retrieval system uses diffusion and denoising for collaborative retrieval.",
            "classification_source": "llm_inferred",
        }
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)

    assert evaluated[0]["topic_evidence"].startswith("passed:")
    assert evaluated[0]["topic_evidence_source"] == "llm_adaptive"


def test_topic_routes_block_diffusion_personalization_false_positive():
    llm = BatchLLM()
    cfg = AppConfig(provider="mock", research_interest="LLM semantic condition retrieval benchmark; discrete retrieval benchmark", max_recommended_papers=5, llm_concurrency=1)
    items = [
        {
            "id": "paper_001",
            "title": "Personalized Text-to-Image Diffusion",
            "abstract": "A prompt based diffusion model personalizes image generation for a visual concept without catalog or user-behavior modeling.",
            "classification_source": "llm_inferred",
        }
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    recommended = _recommended(evaluated, cfg)

    assert evaluated[0]["topic_evidence"].startswith("weak:")
    assert [item["id"] for item in recommended] == ["paper_001"]


def test_generative_recommendation_is_not_diffusion_evidence():
    llm = BatchLLM()
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", research_interest="discrete retrieval benchmark", max_recommended_papers=5, llm_concurrency=1)
    items = [
        {
            "id": "paper_001",
            "title": "Distributionally Robust Generative Recommendation",
            "abstract": "A retrieval system optimizes generated candidates with robust preference learning and candidate reranking.",
            "classification_source": "llm_inferred",
        }
    ]

    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    recommended = _recommended(evaluated, cfg)

    assert evaluated[0]["topic_evidence"].startswith("weak:")
    assert "adaptive topic evidence" in evaluated[0]["topic_evidence"]
    assert [item["id"] for item in recommended] == ["paper_001"]


def test_abstract_enrichment_prioritizes_adaptive_recall_candidates(monkeypatch):
    calls = []

    def fake_openalex(papers, limit=80):
        return papers

    def fake_enrich(papers, limit=20, api_key=""):
        calls.append([paper["id"] for paper in papers[:limit]])
        for paper in papers[:limit]:
            paper["abstract"] = "filled abstract"
        return papers

    monkeypatch.setattr("auto_research.auto_find.pipeline.enrich_with_openalex", fake_openalex)
    monkeypatch.setattr("auto_research.auto_find.pipeline.enrich_with_semantic_scholar", fake_enrich)
    monkeypatch.setattr("auto_research.auto_find.pipeline.enrich_with_arxiv_title_match", lambda papers, limit=40: papers)
    cfg = AppConfig(research_interest="LLM semantic condition retrieval benchmark; discrete retrieval benchmark")
    generic = [
        {"id": f"generic_{index}", "title": f"Generic Machine Learning Paper {index}", "abstract": ""}
        for index in range(25)
    ]
    adaptive = {"id": "adaptive_topic", "title": "Discrete Retrieval for Sequential Benchmarking", "abstract": ""}

    enriched = _enrich_missing_abstracts_for_adaptive_recall(generic + [adaptive], cfg, "TestVenue", log=lambda _msg: None, progress=lambda *_args: None)

    assert calls
    assert "adaptive_topic" in calls[0]
    assert enriched[-1]["abstract"] == "filled abstract"



def test_semantic_scholar_doi_enrichment_fills_kdd_acm_abstract(monkeypatch, tmp_path):
    cache_path = tmp_path / "semantic_cache.json"
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "paperId": "s2-paper",
                "externalIds": {"DOI": "10.1145/3770854.3780288", "ArXiv": "2512.16576"},
                "url": "https://www.semanticscholar.org/paper/s2-paper",
                "title": "InfoDCL: Informative Noise Enhanced Diffusion Based Contrastive Learning",
                "abstract": "This paper proposes a retrieval-based contrastive learning method for benchmark systems with benchmark evaluation.",
                "openAccessPdf": {"url": ""},
            }

    def fake_get(url, headers=None, timeout=12):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(sources, "_semantic_scholar_cache_path", lambda: str(cache_path))
    monkeypatch.setattr(sources.requests, "get", fake_get)
    papers = [
        {
            "title": "InfoDCL: Informative Noise Enhanced Diffusion Based Contrastive Learning",
            "abstract": "",
            "doi": "10.1145/3770854.3780288",
            "url": "https://doi.org/10.1145/3770854.3780288",
            "metadata": {"doi": "10.1145/3770854.3780288"},
        }
    ]

    sources.enrich_with_semantic_scholar(papers, limit=1)

    assert calls and "/paper/DOI:" in calls[0]
    assert papers[0]["abstract"].startswith("This paper proposes")
    assert papers[0]["pdf_url"] == "https://arxiv.org/pdf/2512.16576"
    assert papers[0]["metadata"]["abstract_source"] == "semantic_scholar_doi"


def test_arxiv_title_match_enrichment_fills_same_title_abstract(monkeypatch, tmp_path):
    cache_path = tmp_path / "arxiv_title_match_cache.json"
    xml = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
    <feed xmlns=\"http://www.w3.org/2005/Atom\">
      <entry>
        <id>http://arxiv.org/abs/2601.12345v1</id>
        <title>Scaling Recommender Transformers to One Billion Parameters</title>
        <summary>This paper studies scaling recommender transformers with model and system details.</summary>
        <published>2026-01-01T00:00:00Z</published>
        <updated>2026-01-01T00:00:00Z</updated>
        <author><name>Example Author</name></author>
      </entry>
    </feed>"""

    class Response:
        text = xml

    monkeypatch.setattr(sources, "_arxiv_title_match_cache_path", lambda: cache_path)
    monkeypatch.setattr(sources, "_request_arxiv_page", lambda _url, _timeout: Response())
    papers = [{"id": "kdd", "title": "Scaling Recommender Transformers to One Billion Parameters", "abstract": "", "url": "https://doi.org/10.1145/example", "metadata": {}}]

    sources.enrich_with_arxiv_title_match(papers, limit=1)

    assert papers[0]["abstract"].startswith("This paper studies scaling recommender")
    assert papers[0]["metadata"]["abstract_source"] == "arxiv_title_match"
    assert papers[0]["metadata"]["arxiv_title_similarity"] >= 0.92

def test_openalex_enrichment_uses_inverted_abstract_cache(monkeypatch, tmp_path):
    cache_path = tmp_path / "openalex_cache.json"
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "doi": "https://doi.org/10.1145/example",
                "abstract_inverted_index": {
                    "OpenAlex": [0],
                    "filled": [1],
                    "abstract": [2],
                },
            }

    def fake_get(url, headers=None, timeout=12):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(sources, "_openalex_cache_path", lambda: str(cache_path))
    monkeypatch.setattr(sources.requests, "get", fake_get)

    first = [{"title": "Cached Retrieval Benchmark", "url": "https://doi.org/10.1145/example", "abstract": ""}]
    second = [{"title": "Cached Retrieval Benchmark", "url": "https://doi.org/10.1145/example", "abstract": ""}]
    sources.enrich_with_openalex(first, limit=1)
    sources.enrich_with_openalex(second, limit=1)

    assert len(calls) == 1
    assert first[0]["abstract"] == "OpenAlex filled abstract"
    assert second[0]["abstract"] == "OpenAlex filled abstract"


def test_openalex_enrichment_uses_title_fallback_to_fill_repository_pdf(monkeypatch, tmp_path):
    cache_path = tmp_path / "openalex_cache.json"
    calls = []

    class FakeDoiMiss:
        status_code = 404

        def json(self):
            return {}

    class FakeTitleHit:
        status_code = 200

        def json(self):
            return {
                "results": [
                    {
                        "id": "https://openalex.org/W4415191119",
                        "doi": "https://doi.org/10.48550/arxiv.2508.05667",
                        "display_name": "ITDR: An Instruction Tuning Dataset for Enhancing Large Language Models in Recommendations",
                        "primary_location": {
                            "landing_page_url": "http://arxiv.org/abs/2508.05667",
                            "pdf_url": "https://arxiv.org/pdf/2508.05667",
                        },
                        "authorships": [
                            {"author": {"display_name": "Zekun Liu"}},
                            {"author": {"display_name": "Xiaowen Huang"}},
                        ],
                    }
                ]
            }

    def fake_get(url, headers=None, timeout=12):
        calls.append(url)
        if "works/doi:" in url:
            return FakeDoiMiss()
        return FakeTitleHit()

    monkeypatch.setattr(sources, "_openalex_cache_path", lambda: str(cache_path))
    monkeypatch.setattr(sources.requests, "get", fake_get)

    papers = [
        {
            "title": "ITDR: An Instruction Tuning Dataset for Enhancing Large Language Models in Recommendations",
            "authors": "Zekun Liu, Xiaowen Huang 0001, Jitao Sang 0001",
            "url": "https://doi.org/10.1145/3770854.3785694",
            "abstract": "Already enriched abstract should not prevent repository PDF discovery.",
            "pdf_url": "",
            "metadata": {},
        }
    ]

    sources.enrich_with_openalex(papers, limit=1)

    assert len(calls) == 2
    assert papers[0]["url"] == "https://doi.org/10.1145/3770854.3785694"
    assert papers[0]["pdf_url"] == "https://arxiv.org/pdf/2508.05667"
    assert papers[0]["metadata"]["publisher_doi_openalex_status"] == 404
    assert papers[0]["metadata"]["openalex_title_fallback_used"] is True


def test_openalex_title_fallback_rejects_same_title_with_wrong_authors(monkeypatch, tmp_path):
    cache_path = tmp_path / "openalex_cache.json"
    calls = []

    class FakeDoiMiss:
        status_code = 404

        def json(self):
            return {}

    class FakeTitleHitWrongAuthor:
        status_code = 200

        def json(self):
            return {
                "results": [
                    {
                        "id": "https://openalex.org/W_wrong_author",
                        "doi": "https://doi.org/10.48550/arxiv.2508.05667",
                        "display_name": "ITDR: An Instruction Tuning Dataset for Enhancing Large Language Models in Recommendations",
                        "primary_location": {
                            "landing_page_url": "http://arxiv.org/abs/2508.05667",
                            "pdf_url": "https://arxiv.org/pdf/2508.05667",
                        },
                        "authorships": [
                            {"author": {"display_name": "Unrelated Author"}},
                            {"author": {"display_name": "Different Person"}},
                        ],
                    }
                ]
            }

    def fake_get(url, headers=None, timeout=12):
        calls.append(url)
        if "works/doi:" in url:
            return FakeDoiMiss()
        return FakeTitleHitWrongAuthor()

    monkeypatch.setattr(sources, "_openalex_cache_path", lambda: str(cache_path))
    monkeypatch.setattr(sources.requests, "get", fake_get)

    papers = [
        {
            "title": "ITDR: An Instruction Tuning Dataset for Enhancing Large Language Models in Recommendations",
            "authors": "Zekun Liu, Xiaowen Huang 0001, Jitao Sang 0001",
            "url": "https://doi.org/10.1145/3770854.3785694",
            "abstract": "Already enriched abstract should not bypass author verification for repository PDFs.",
            "pdf_url": "",
            "metadata": {},
        }
    ]

    sources.enrich_with_openalex(papers, limit=1)

    assert len(calls) == 2
    assert papers[0]["pdf_url"] == ""
    assert papers[0]["metadata"] == {}


def test_semantic_scholar_tldr_is_not_promoted_to_abstract(monkeypatch, tmp_path):
    cache_path = tmp_path / "semantic_cache.json"
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "title": "TLDR Only Paper",
                "abstract": None,
                "url": "https://semanticscholar.org/paper/tldr-only",
                "openAccessPdf": {"url": "https://example.test/tldr-only.pdf"},
                "tldr": {"text": "This is only a Semantic Scholar TLDR summary, not the publisher abstract."},
            }

    def fake_get(url, headers=None, timeout=12):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(sources, "_semantic_scholar_cache_path", lambda: str(cache_path))
    monkeypatch.setattr(sources.requests, "get", fake_get)
    papers = [{"title": "TLDR Only Paper", "abstract": "", "doi": "10.1145/example", "metadata": {"doi": "10.1145/example"}}]

    sources.enrich_with_semantic_scholar(papers, limit=1)

    assert calls
    assert papers[0].get("abstract", "") == ""
    assert papers[0]["metadata"]["tldr"].startswith("This is only")
    assert papers[0]["metadata"]["semantic_scholar_tldr_available"] is True
    assert "abstract_source" not in papers[0]["metadata"]
    assert papers[0]["pdf_url"] == "https://example.test/tldr-only.pdf"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert any(item.get("miss") is True and item.get("tldr") for item in cached.values() if isinstance(item, dict))


def test_pipeline_real_abstract_guard_rejects_semantic_tldr_artifact():
    tldr = "This long Semantic Scholar TLDR summary should not be treated as the real paper abstract."

    assert find_pipeline._has_real_abstract({"abstract": tldr, "metadata": {"abstract_source": "semantic_scholar_doi_tldr", "tldr": tldr}}) is False
    assert find_pipeline._has_real_abstract({"abstract": tldr, "metadata": {"abstract_source": "semantic_scholar_doi", "tldr": tldr}}) is False
    assert find_pipeline._has_real_abstract({"abstract": "This is a real venue abstract with enough concrete method and experiment details.", "metadata": {"abstract_source": "openalex"}}) is True


def test_semantic_scholar_enrichment_uses_title_cache(monkeypatch, tmp_path):
    cache_path = tmp_path / "semantic_cache.json"
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {
                        "abstract": "cached abstract",
                        "url": "https://example.test/paper",
                        "openAccessPdf": {"url": "https://example.test/paper.pdf"},
                        "tldr": {"text": "cached tldr"},
                    }
                ]
            }

    def fake_get(url, headers=None, timeout=12):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(sources, "_semantic_scholar_cache_path", lambda: str(cache_path))
    monkeypatch.setattr(sources.requests, "get", fake_get)

    first = [{"title": "Cached Retrieval Benchmark", "abstract": ""}]
    second = [{"title": "Cached Retrieval Benchmark", "abstract": ""}]
    sources.enrich_with_semantic_scholar(first, limit=1)
    sources.enrich_with_semantic_scholar(second, limit=1)

    assert len(calls) == 1
    assert first[0]["abstract"] == "cached abstract"
    assert second[0]["abstract"] == "cached abstract"


def test_semantic_scholar_retryable_429_is_not_cached_as_permanent_miss(monkeypatch, tmp_path):
    cache_path = tmp_path / "semantic_cache.json"
    calls = []

    class RateLimitedResponse:
        status_code = 429

        def json(self):
            return {}

    def fake_get(url, headers=None, timeout=12):
        calls.append(url)
        return RateLimitedResponse()

    monkeypatch.setattr(sources, "_semantic_scholar_cache_path", lambda: str(cache_path))
    monkeypatch.setattr(sources.requests, "get", fake_get)

    first = [{"title": "KDD DOI Retrieval Benchmark", "abstract": "", "doi": "10.1145/3770854.3780206"}]
    second = [{"title": "KDD DOI Retrieval Benchmark", "abstract": "", "doi": "10.1145/3770854.3780206"}]

    sources.enrich_with_semantic_scholar(first, limit=1)
    first_call_count = len(calls)
    sources.enrich_with_semantic_scholar(second, limit=1)

    assert first_call_count == 2
    assert len(calls) == 4
    assert not cache_path.exists()
    assert first[0]["metadata"]["semantic_scholar_lookup_retryable"] is True
    assert "http_429" in first[0]["metadata"]["semantic_scholar_lookup_error"]


def test_semantic_scholar_old_retryable_miss_cache_is_retried(monkeypatch, tmp_path):
    cache_path = tmp_path / "semantic_cache.json"
    title = "KDD Cached Retryable Recommendation"
    doi = "10.1145/3770854.3780206"
    title_key = sources._semantic_scholar_cache_key(title)
    cache_path.write_text(json.dumps({
        f"doi:{doi}": {
            "title": title,
            "miss": True,
            "lookup_errors": ["semantic_scholar_doi:http_429", "semantic_scholar_title:http_429"],
        },
        f"title:{title_key}": {
            "title": title,
            "miss": True,
            "lookup_errors": ["semantic_scholar_doi:http_429", "semantic_scholar_title:http_429"],
        },
        title_key: {
            "title": title,
            "miss": True,
            "lookup_errors": ["semantic_scholar_doi:http_429", "semantic_scholar_title:http_429"],
        },
    }), encoding="utf-8")
    calls = []

    class SuccessResponse:
        status_code = 200

        def json(self):
            return {
                "paperId": "s2-recovered",
                "title": title,
                "abstract": "Recovered abstract after a previous retryable Semantic Scholarar rate limit.",
                "openAccessPdf": {"url": ""},
            }

    def fake_get(url, headers=None, timeout=12):
        calls.append(url)
        return SuccessResponse()

    monkeypatch.setattr(sources, "_semantic_scholar_cache_path", lambda: str(cache_path))
    monkeypatch.setattr(sources.requests, "get", fake_get)

    papers = [{"title": title, "abstract": "", "doi": doi}]
    sources.enrich_with_semantic_scholar(papers, limit=1)

    assert calls and "/paper/DOI:" in calls[0]
    assert papers[0]["abstract"].startswith("Recovered abstract")
    refreshed = json.loads(cache_path.read_text(encoding="utf-8"))
    assert refreshed[f"doi:{doi}"]["miss"] is False
    assert refreshed[f"doi:{doi}"]["semantic_scholar_paper_id"] == "s2-recovered"


def test_attach_abstract_language_fields_strips_abstract_placeholders():
    class DisabledLLM:
        enabled = False

    items = [
        {
            "id": "paper_001",
            "title": "Paper",
            "abstract": "No abstract available.",
            "reason_en": "No abstract available; title only covers retrieval.",
            "missing_topic_evidence": ["No abstract available to confirm recommendation or diffusion"],
        },
        {"id": "paper_002", "title": "Paper 2", "abstract": ""},
    ]

    _attach_abstract_language_fields(items, DisabledLLM(), log=lambda _msg: None, should_cancel=lambda: False)

    assert items[0]["abstract"] == ""
    assert items[0]["abstract_missing"] is True
    assert "abstract_en" not in items[0]
    assert "abstract_zh" not in items[0]
    assert "No abstract available" not in items[0]["reason_en"]
    assert "No abstract available" not in items[0]["missing_topic_evidence"][0]
    assert items[1]["abstract_missing"] is True


def test_attach_abstract_language_fields_translates_english_abstracts():
    class TranslationLLM:
        enabled = True
        timeout_sec = 10

        def __init__(self):
            self.temperatures = []

        def json_or_error(self, prompt: str, temperature=None, max_tokens=None):
            self.temperatures.append(temperature)
            return {
                "ok": True,
                "data": {"translations": [{"id": "paper_001", "abstract_zh": "这是一段中文摘要。"}]},
                "error": "",
            }

    items = [{"id": "paper_001", "title": "Paper", "abstract": "This paper studies retrieval benchmark systems with language models.", "find_recommendation": True}]
    llm = TranslationLLM()

    _attach_abstract_language_fields(items, llm, log=lambda _msg: None, should_cancel=lambda: False)

    assert items[0]["abstract_en"].startswith("This paper")
    assert items[0]["abstract_zh"] == "这是一段中文摘要。"
    assert llm.temperatures == [0.0]



def test_attach_abstract_language_fields_skips_non_recommended_candidates():
    class TranslationLLM:
        enabled = True
        timeout_sec = 10

        def __init__(self):
            self.calls = 0

        def json_or_error(self, prompt: str, temperature=None, max_tokens=None):
            self.calls += 1
            return {"ok": True, "data": {"translations": [{"id": "audit_1", "abstract_zh": "不应写入。"}]}, "error": ""}

    items = [{"id": "audit_1", "title": "Audit-only paper", "abstract": "This audit-only paper studies an adjacent topic."}]
    llm = TranslationLLM()

    result = _attach_abstract_language_fields(items, llm, log=lambda _msg: None, should_cancel=lambda: False)

    assert result["status"] == "skipped_no_user_visible_recommendations"
    assert llm.calls == 0
    assert "abstract_zh" not in items[0]


def test_attach_abstract_language_fields_translates_recommended_articles_before_audit_pool(monkeypatch):
    class TranslationLLM:
        enabled = True
        timeout_sec = 10

        def json_or_error(self, prompt: str, temperature=None, max_tokens=None):
            ids = re.findall(r"(visible_\d+|audit_\d+)", prompt)
            return {
                "ok": True,
                "data": {"translations": [{"id": item_id, "abstract_zh": f"{item_id} 的中文摘要。"} for item_id in ids]},
                "error": "",
            }

    items = [
        {"id": "audit_1", "title": "Audit-only paper", "abstract": "This audit-only paper studies an adjacent diffusion topic."},
        {"id": "visible_1", "title": "Recommended paper one", "abstract": "This recommended paper studies discrete retrieval system systems.", "_user_visible_recommendation": True},
        {"id": "visible_2", "title": "Recommended paper two", "abstract": "This recommended paper studies language model signals for recommendation.", "_user_visible_recommendation": True},
    ]

    result = _attach_abstract_language_fields(items, TranslationLLM(), log=lambda _msg: None, should_cancel=lambda: False)

    by_id = {item["id"]: item for item in items}
    assert by_id["visible_1"]["abstract_zh"].endswith("中文摘要。")
    assert by_id["visible_2"]["abstract_zh"].endswith("中文摘要。")
    assert "abstract_zh" not in by_id["audit_1"]
    assert result["total"] == 2
    assert result["translated"] == 2
    assert result["missing_visible"] == 0


def test_attach_abstract_language_fields_retries_untranslated_abstracts_singly():
    class RetryTranslationLLM:
        enabled = True
        timeout_sec = 10

        def __init__(self):
            self.temperatures = []
            self.calls = 0

        def json_or_error(self, prompt: str, temperature=None, max_tokens=None):
            self.temperatures.append(temperature)
            self.calls += 1
            if self.calls == 1:
                return {"ok": True, "data": {"translations": []}, "error": ""}
            return {"ok": True, "data": {"abstract_zh": "重试后得到的中文摘要。"}, "error": ""}

    items = [{"id": "paper_001", "title": "Paper", "abstract": "This paper studies discrete retrieval for retrieval systems.", "find_recommendation": True}]
    llm = RetryTranslationLLM()

    _attach_abstract_language_fields(items, llm, log=lambda _msg: None, should_cancel=lambda: False)

    assert items[0]["abstract_zh"] == "重试后得到的中文摘要。"
    assert llm.calls == 2
    assert llm.temperatures == [0.0, 0.0]


def test_attach_abstract_language_fields_single_retry_accepts_id_wrapped_translation():
    class RetryTranslationLLM:
        enabled = True
        timeout_sec = 10

        def __init__(self):
            self.calls = 0

        def json_or_error(self, prompt: str, temperature=None, max_tokens=None):
            self.calls += 1
            if self.calls == 1:
                return {"ok": True, "data": {"translations": []}, "error": ""}
            return {"ok": True, "data": {"id": "paper_001", "abstract_zh": "这是一段由单条重试返回的完整中文摘要。"}, "error": ""}

    items = [{"id": "paper_001", "title": "Paper", "abstract": "This paper studies discrete retrieval for retrieval systems with benchmark evidence.", "find_recommendation": True}]

    result = _attach_abstract_language_fields(items, RetryTranslationLLM(), log=lambda _msg: None, should_cancel=lambda: False)

    assert result["missing"] == 0
    assert items[0]["abstract_zh"] == "这是一段由单条重试返回的完整中文摘要。"


def test_attach_abstract_language_fields_single_retry_rejects_mismatched_id():
    class RetryTranslationLLM:
        enabled = True
        timeout_sec = 10

        def __init__(self):
            self.calls = 0

        def json_or_error(self, prompt: str, temperature=None, max_tokens=None):
            self.calls += 1
            if self.calls == 1:
                return {"ok": True, "data": {"translations": []}, "error": ""}
            return {"ok": True, "data": {"id": "other_paper", "abstract_zh": "这是一段不应被写入的中文摘要。"}, "error": ""}

    items = [{"id": "paper_001", "title": "Paper", "abstract": "This paper studies discrete retrieval for retrieval systems with benchmark evidence.", "find_recommendation": True}]

    result = _attach_abstract_language_fields(items, RetryTranslationLLM(), log=lambda _msg: None, should_cancel=lambda: False)

    assert result["missing"] == 1
    assert "abstract_zh" not in items[0]



def test_attach_abstract_language_fields_cleans_escape_residue_tail():
    class TranslationLLM:
        enabled = True
        timeout_sec = 10

        def json_or_error(self, prompt: str, temperature=None, max_tokens=None):
            return {"ok": True, "data": {"translations": [{"id": "paper_001", "abstract_zh": "这是一段完整中文摘要包含足够长度并且末尾带转义残留n"}]}, "error": ""}

    items = [{"id": "paper_001", "title": "Paper", "abstract": "This paper studies a benchmarked diffusion retrieval system.", "find_recommendation": True}]

    result = _attach_abstract_language_fields(items, TranslationLLM(), log=lambda _msg: None, should_cancel=lambda: False)

    assert result["missing"] == 0
    assert items[0]["abstract_zh"] == "这是一段完整中文摘要包含足够长度并且末尾带转义残留。"


def test_abstract_enrichment_defaults_scale_to_read_candidate_target():
    cfg = AppConfig(max_recommended_papers=20, detail_fetch_count=800)

    adaptive_limit, general_limit = _abstract_enrichment_limits(cfg, 1000)

    assert adaptive_limit >= 400
    assert general_limit >= 200


def test_read_candidates_mirror_recommendations_and_triage_stays_separate():
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", max_recommended_papers=20)
    items = []
    for index in range(70):
        items.append({
            "id": f"paper_{index}",
            "title": f"Boundary candidate {index}",
            "abstract": "A real abstract supports inspection of this boundary candidate for the current profile.",
            "reason_source": "llm abstract evaluation",
            "fit_score": 5.2,
            "diversity_score": 4.0,
            "score": 5.0,
            "stable_source_score": 5.0,
            "stable_rank_score": 5.0,
            "topic_evidence": "weak: boundary candidate for reading",
            "topic_evidence_supported": False,
        })

    read_candidates = _read_candidates(items, cfg)
    triage = _triage_candidates(items, cfg)

    assert len(read_candidates) == 20
    assert [item["id"] for item in read_candidates[:3]] == ["paper_0", "paper_1", "paper_10"]
    assert len(triage) == 50
    assert all(item["weak_candidate_for_critique"] for item in triage)



def test_title_filter_scored_rows_cannot_be_find_recommendations():
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", max_recommended_papers=5)
    items = [
        {
            "id": "title_only_llm",
            "title": "Strong looking title",
            "abstract": "This abstract is real but has not been judged by the final title and abstract scorer.",
            "reason_source": "llm title filter",
            "fit_score": 9.0,
            "diversity_score": 8.0,
            "score": 9.0,
            "recommendation_score": 9.0,
            "topic_evidence": "passed:adaptive_llm_topic_route",
            "topic_evidence_supported": True,
        }
    ]

    assert _recommended(items, cfg) == []
    assert _read_candidates(items, cfg) == []
    assert items[0]["find_recommendation_reject_reason"] == "missing_final_title_abstract_llm_scoring"


def test_triage_excludes_user_visible_recommendation_pool():
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", max_recommended_papers=1)
    recommended_row = {
        "id": "recommended_1",
        "title": "Recommended paper",
        "abstract": "This real abstract provides final title and abstract evidence for a concrete adaptive route.",
        "reason_source": "llm abstract evaluation",
        "fit_score": 8.5,
        "diversity_score": 7.0,
        "score": 8.5,
        "recommendation_score": 8.5,
        "stable_source_score": 8.5,
        "stable_rank_score": 8.5,
        "topic_evidence": "passed:adaptive_llm_topic_route",
        "topic_evidence_supported": True,
    }
    boundary_row = {
        "id": "boundary_1",
        "title": "Boundary paper",
        "abstract": "This real abstract has some inspectable evidence but is not a final recommendation.",
        "reason_source": "llm abstract evaluation",
        "fit_score": 5.5,
        "diversity_score": 4.0,
        "score": 5.5,
        "stable_source_score": 5.5,
        "stable_rank_score": 5.5,
        "topic_evidence": "weak: boundary evidence only",
        "topic_evidence_supported": False,
    }

    recommended = _recommended([recommended_row, boundary_row], cfg)
    triage = _triage_candidates([recommended_row, boundary_row], cfg)

    assert [item["id"] for item in recommended] == ["recommended_1"]
    assert [item["id"] for item in triage] == ["boundary_1"]
    assert triage[0]["not_positive_support"] is True


def test_source_guard_is_adaptive_to_current_topic_not_fixed_recsys_diffusion():
    item = {
        "id": "paper_adaptive",
        "title": "Causal Graph Retrieval for Tool-Augmented Scientific Agents",
        "abstract": "We study causal graph retrieval for tool-augmented scientific agents. The method builds adaptive graph memory and evaluates planning reliability for automated research workflows.",
        "fit_score": 5.9,
        "diversity_score": 4.0,
        "score": 5.5,
        "topic_evidence": "weak: missing another route component",
        "topic_evidence_supported": False,
        "missing_topic_evidence": ["other route component"],
        "reason": "Near-threshold foundation candidate; missing another component but directly supports this route.",
    }

    repaired = _repair_llm_alternative_route_false_negative(item, "causal graph retrieval for tool-augmented scientific agents")

    assert repaired is True
    assert item["source_guard_audit_only"] is True
    assert item["llm_alternative_route_false_negative_repaired"] is False
    assert item["topic_evidence"].startswith("weak:")
    assert item["source_supported_adaptive_route"]
    assert set(item["source_supported_adaptive_terms"]) >= {"causal", "graph", "retrieval"}
    assert item["not_positive_support"] is True
    assert _has_strong_topic_evidence(item) is False


def test_source_guard_refuses_title_only_adaptive_match():
    item = {
        "id": "paper_title_only",
        "title": "Causal Graph Retrieval for Tool-Augmented Scientific Agents",
        "abstract": "",
        "fit_score": 6.0,
        "diversity_score": 4.0,
        "topic_evidence": "weak: missing another route component",
        "topic_evidence_supported": False,
        "reason": "Near-threshold title-only item.",
    }

    repaired = _repair_llm_alternative_route_false_negative(item, "causal graph retrieval for tool-augmented scientific agents")

    assert repaired is False
    assert _has_strong_topic_evidence(item) is False


def test_source_guard_refuses_partial_llm_summary_without_retrieval_core():
    item = {
        "id": "paper_partial_llm_summary",
        "title": "Efficient LLM-based Research Summarization",
        "abstract": "Large language models help summarize long research logs and user notes, but this method does not use retrieval, indexing, or benchmark-grounded evidence selection.",
        "fit_score": 5.8,
        "diversity_score": 4.0,
        "score": 5.4,
        "topic_evidence": "weak: missing retrieval benchmark component",
        "topic_evidence_supported": False,
        "missing_topic_evidence": ["missing semantic condition and retrieval benchmark"],
        "reason": "Boundary foundation candidate; it covers LLM summarization but lacks the retrieval benchmark core.",
    }

    repaired = _repair_llm_alternative_route_false_negative(item, "LLM semantic condition retrieval benchmark")

    assert repaired is False
    assert item["source_supported_adaptive_route"] == ""
    assert set(item["source_supported_adaptive_terms"]) >= {"llm"}
    assert "retrieval" in item["source_missing_adaptive_terms"]
    assert _has_strong_topic_evidence(item) is False


def test_source_guard_allows_llm_evaluation_when_current_route_matches():
    item = {
        "id": "paper_llm_eval",
        "title": "Efficient LLM-based Evaluation",
        "abstract": "Large language models support evaluation workflows for long research traces and improve audit quality with structured rubrics.",
        "fit_score": 5.8,
        "diversity_score": 4.0,
        "score": 5.4,
        "topic_evidence": "weak: missing another route component",
        "topic_evidence_supported": False,
        "missing_topic_evidence": ["missing another component"],
        "reason": "Boundary foundation candidate; it directly supports the current LLM evaluation route.",
    }

    repaired = _repair_llm_alternative_route_false_negative(item, "LLM evaluation")

    assert repaired is True
    assert item["source_guard_audit_only"] is True
    assert item["source_supported_adaptive_route"] == "LLM evaluation"
    assert set(item["source_supported_adaptive_terms"]) >= {"llm", "evaluation"}
    assert item["not_positive_support"] is True
    assert _has_strong_topic_evidence(item) is False


def test_source_guard_backfills_hit_directions_for_repaired_foundation_route():
    item = {
        "id": "paper_repaired_retrieval_benchmark",
        "title": "Retrieval Benchmark with Query Planning",
        "abstract": "This paper designs a retrieval benchmark for query planning agents. It evaluates evidence selection, indexing quality, and multi-step retrieval reliability with ablation studies.",
        "fit_score": 5.8,
        "diversity_score": 4.0,
        "score": 5.4,
        "hit_directions": [],
        "hit_directions_zh": [],
        "hit_directions_en": [],
        "topic_evidence": "weak: missing another route component",
        "topic_evidence_supported": False,
        "missing_topic_evidence": ["missing LLM semantic condition"],
        "reason": "Near-threshold foundation candidate; it directly supports the retrieval benchmark route.",
    }

    repaired = _repair_llm_alternative_route_false_negative(item, "LLM semantic condition retrieval benchmark")

    assert repaired is True
    assert item["source_guard_audit_only"] is True
    assert item["topic_evidence"].startswith("weak:")
    assert item["source_supported_adaptive_route"] == "LLM semantic condition retrieval benchmark"
    assert set(item["source_supported_adaptive_terms"]) >= {"retrieval", "benchmark"}
    assert set(item["source_missing_adaptive_terms"]) >= {"llm", "semantic", "condition"}
    assert item["not_positive_support"] is True
    assert _has_strong_topic_evidence(item) is False


def test_transient_llm_service_errors_do_not_expand_retry_work():
    transient_errors = [
        "LLM HTTP 503 via chat_completions: service_unavailable_error: Service is too busy",
        "LLM HTTP 429 via chat_completions: rate limit exceeded",
        "LLM request failed via chat_completions after 3 attempts: timed out",
    ]
    for message in transient_errors:
        assert _is_transient_llm_service_error(message) is True

    assert _is_transient_llm_service_error("JSON parse failed: missing evaluations field") is False


class SlowSingleRetryLLM(BatchLLM):
    def json_or_error(self, prompt: str, temperature=None, max_tokens=None):
        self.prompts.append(prompt)
        self.temperatures.append(temperature)
        ids = re.findall(r"paper_\d+", prompt)
        if "Candidate items" in prompt and "paper_002" in ids:
            ids = [item_id for item_id in ids if item_id != "paper_002"]
        elif "Candidate item" in prompt and "paper_002" in ids:
            time.sleep(5)
            return {"ok": True, "error": "", "data": {"evaluations": []}}
        return {
            "ok": True,
            "error": "",
            "data": {
                "evaluations": [
                    {
                        "id": item_id,
                        "category": "Materials AI",
                        "fit_score": 8,
                        "diversity_score": 7,
                        "hit_directions": ["生成式AI", "材料物理"],
                        "topic_evidence": "passed:adaptive_llm_topic_route",
                        "topic_evidence_supported": True,
                        "matched_topic_route": "adaptive route from current research profile",
                        "topic_evidence_basis": "abstract",
                        "missing_topic_evidence": [],
                        "fit_explanation": "命中生成式AI和材料物理。",
                        "reason": "这篇文章研究生成模型如何服务材料发现。",
                    }
                    for item_id in ids
                ]
            },
        }


def test_single_item_retry_uses_wall_clock_timeout(monkeypatch):
    monkeypatch.setenv("ABSTRACT_SCORING_BATCH_SIZE", "2")
    monkeypatch.setenv("OMITTED_ITEM_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("ABSTRACT_SCORING_WALL_TIMEOUT_SEC", "1")
    llm = SlowSingleRetryLLM()
    cfg = AppConfig(provider="mock", research_interest="生成式AI 科学发现 材料物理", max_recommended_papers=5, llm_concurrency=1)
    items = [
        {"id": "paper_001", "title": "Generative materials discovery", "abstract": "Materials and generative model.", "classification_source": "llm_inferred"},
        {"id": "paper_002", "title": "Generative materials physics", "abstract": "Materials and generative model.", "classification_source": "llm_inferred"},
    ]
    start = time.time()
    evaluated = _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)
    by_id = {item["id"]: item for item in evaluated}

    assert time.time() - start < 3
    assert by_id["paper_002"]["llm_retry_exhausted"] is True
    assert "LLM wall-clock timeout" in by_id["paper_002"]["llm_retry_last_error"]
    assert "paper_002" not in [item["id"] for item in _recommended(evaluated, cfg)]


def test_find_rejects_llm_passed_route_when_source_text_does_not_support_route_terms():
    cfg = AppConfig(
        provider="openai",
        api_key="test-key",
        model="test-model",
        research_interest="Generic semantic-conditioning retrieval benchmark; discrete retrieval smoke test",
        max_recommended_papers=5,
    )
    items = [
        {
            "id": "llm_agent_only",
            "title": "Think before Recommendation: Autonomous Reasoning-enhanced Recommender",
            "abstract": "This paper proposes an autonomous reasoning-enhanced recommender agent that uses reinforcement learning and planning to improve recommendation decisions.",
            "fit_score": 8.0,
            "diversity_score": 6.0,
            "score": 8.0,
            "reason_source": "llm abstract evaluation",
            "topic_evidence": "passed:LLM semantic condition retrieval benchmark",
            "topic_evidence_supported": True,
            "evidence_role": "direct_target",
            "topic_evidence_basis": "abstract",
            "missing_topic_evidence": [],
            "hit_directions": ["利用大模型推理能力增强推荐系统", "强化学习推荐范式"],
        }
    ]

    recommended = _recommended(items, cfg)

    assert [item["id"] for item in recommended] == ["llm_agent_only"]
    assert not items[0].get("not_positive_support")
    assert "strong_gate_reject_reason" not in items[0]


def test_framework_does_not_hard_reject_agent_memory_terms_when_current_topic_matches():
    cfg = AppConfig(
        provider="openai",
        api_key="test-key",
        model="test-model",
        research_interest="causal graph retrieval for tool-augmented scientific agents",
        max_recommended_papers=5,
    )
    items = [
        {
            "id": "agent_memory_topic",
            "title": "Causal Graph Retrieval for Tool-Augmented Scientific Agents",
            "abstract": "We study causal graph retrieval for tool-augmented scientific agents. The method builds adaptive graph memory and evaluates planning reliability for automated research workflows.",
            "fit_score": 8.0,
            "diversity_score": 6.0,
            "score": 8.0,
            "reason_source": "llm abstract evaluation",
            "topic_evidence": "passed:causal graph retrieval for tool-augmented scientific agents",
            "topic_evidence_supported": True,
            "evidence_role": "direct_target",
            "topic_evidence_basis": "abstract",
            "missing_topic_evidence": [],
        }
    ]

    recommended = _recommended(items, cfg)

    assert [item["id"] for item in recommended] == ["agent_memory_topic"]
    assert recommended[0]["evidence_tier"] == "strong_recommendation"


def test_find_recommendation_does_not_require_deep_read_entrypoint():
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", research_interest="dynamic route retrieval systems", max_recommended_papers=2)
    items = [
        {
            "id": "ordinary_abstract_only",
            "title": "High Score Ordinary Abstract Only",
            "abstract": "This paper studies dynamic route retrieval systems with benchmark evaluation.",
            "url": "https://example.test/conference/abstract-only",
            "venue": "TestConf",
            "fit_score": 9.5,
            "llm_fit_score": 9.5,
            "reason_source": "llm abstract evaluation",
        },
        {
            "id": "icml_abstract_only",
            "title": "High Score ICML Abstract Only",
            "abstract": "This paper studies dynamic route retrieval systems with benchmark evaluation.",
            "url": "https://icml.cc/virtual/2026/poster/66818",
            "venue": "ICML",
            "fit_score": 9.0,
            "llm_fit_score": 9.0,
            "reason_source": "llm abstract evaluation",
        },
        {
            "id": "openreview_ready",
            "title": "OpenReview Ready Paper",
            "abstract": "This paper studies dynamic route retrieval systems with benchmark evaluation.",
            "url": "https://openreview.net/forum?id=ready",
            "pdf_url": "https://openreview.net/pdf?id=ready",
            "fit_score": 8.0,
            "llm_fit_score": 8.0,
            "reason_source": "llm abstract evaluation",
        },
    ]

    recommended = _recommended(items, cfg)

    assert [item["id"] for item in recommended] == ["ordinary_abstract_only", "icml_abstract_only"]
    assert "find_recommendation_reject_reason" not in items[0]
    assert "find_recommendation_reject_reason" not in items[1]


def test_find_recommendation_topn_includes_six_when_it_ranks_next():
    cfg = AppConfig(
        provider="openai",
        api_key="test-key",
        model="test-model",
        research_interest="discrete retrieval benchmark",
        max_recommended_papers=5,
    )
    items = [
        {
            "id": "audit_only_six",
            "title": "Diffusion Session Recommendation",
            "abstract": "A retrieval system uses diffusion for session-based recommendation.",
            "fit_score": 6.0,
            "diversity_score": 6.0,
            "score": 6.0,
            "reason_source": "llm abstract evaluation",
            "topic_evidence": "passed:retrieval benchmark",
            "topic_evidence_supported": True,
            "evidence_role": "direct_target",
            "evidence_tier": "strong_recommendation",
            "topic_evidence_basis": "abstract",
            "missing_topic_evidence": [],
        },
        {
            "id": "recommended_seven",
            "title": "Discrete Retrieval Benchmark with Benchmarks",
            "abstract": "This paper studies discrete retrieval benchmark with reusable benchmark evaluation and ablation evidence.",
            "fit_score": 7.0,
            "diversity_score": 6.0,
            "score": 7.0,
            "reason_source": "llm abstract evaluation",
            "topic_evidence": "passed:retrieval benchmark",
            "topic_evidence_supported": True,
            "evidence_role": "direct_target",
            "topic_evidence_basis": "abstract",
            "missing_topic_evidence": [],
        },
    ]

    recommended = _recommended(items, cfg)
    screened = _screened_ranking(items, cfg)

    assert [item["id"] for item in recommended] == ["recommended_seven", "audit_only_six"]
    assert [item["id"] for item in screened] == ["recommended_seven", "audit_only_six"]
    assert items[0]["find_recommendation"] is True
    assert not items[0].get("not_positive_support")


def test_read_and_critique_candidates_do_not_mutate_recommended_items():
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", research_interest="semantic conditioned recommendation", max_recommended_papers=5)
    items = [
        {
            "id": "recommended_reader",
            "title": "Useful semantic recommendation paper",
            "abstract": "This paper studies semantic retrieval models with benchmark evaluation and reusable training protocols.",
            "fit_score": 7.0,
            "diversity_score": 6.0,
            "score": 7.0,
            "stable_source_score": 6.0,
            "reason_source": "llm abstract evaluation",
            "topic_evidence": "passed:semantic recommendation benchmark",
            "topic_evidence_supported": True,
            "evidence_role": "direct_target",
            "topic_evidence_basis": "abstract",
            "missing_topic_evidence": [],
        }
    ]

    recommended = _recommended(items, cfg)
    assert recommended[0]["evidence_tier"] == "strong_recommendation"
    before_note = recommended[0]["recommendation_note_zh"]

    _read_candidates(items, cfg)
    _screened_ranking(items, cfg)

    assert recommended[0]["evidence_tier"] == "strong_recommendation"
    assert recommended[0]["recommendation_note_zh"] == before_note
    assert "内部审计候选" not in recommended[0]["recommendation_note_zh"]


def test_legacy_claim_reject_reason_does_not_create_second_find_ranking():
    cfg = AppConfig(
        provider="openai",
        api_key="test-key",
        model="test-model",
        research_interest="semantic conditioned retrieval benchmark",
        max_recommended_papers=5,
    )
    items = [
        {
            "id": "reading_anchor",
            "title": "Efficiency Effectiveness Trade-off of Retrieval Benchmarks",
            "abstract": "This paper studies retrieval-based benchmark systems, efficiency, effectiveness, and benchmark evaluation for retrieval models.",
            "fit_score": 9.0,
            "diversity_score": 6.0,
            "score": 9.0,
            "reason_source": "llm abstract evaluation",
            "topic_evidence": "passed:retrieval benchmark foundation",
            "topic_evidence_supported": True,
            "evidence_role": "direct_target",
            "topic_evidence_basis": "abstract",
            "strong_gate_reject_reason": "historical downstream paper-conclusion reject reason from an older audit",
        }
    ]

    recommended = _recommended(items, cfg)

    assert [item["id"] for item in recommended] == ["reading_anchor"]
    assert not items[0].get("find_recommendation_reject_reason")
    assert not items[0].get("not_positive_support")

def test_strict_anchor_count_is_capped_to_user_visible_recommendations():
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", research_interest="dynamic route retrieval systems", max_recommended_papers=2)
    items = []
    for index in range(5):
        items.append({
            "id": f"paper_{index}",
            "title": f"Dynamic Route Recommender Systems {index}",
            "abstract": "This paper studies dynamic route retrieval systems with benchmark evaluation and reusable algorithms.",
            "fit_score": 8.0 - index * 0.1,
            "diversity_score": 6.0,
            "score": 8.0 - index * 0.1,
            "reason_source": "llm abstract evaluation",
            "topic_evidence": "passed:dynamic route retrieval systems",
            "topic_evidence_supported": True,
            "evidence_role": "direct_target",
            "topic_evidence_basis": "abstract",
            "missing_topic_evidence": [],
        })

    assert len(_screened_ranking(items, cfg)) == 5
    assert len(_recommended(items, cfg)) == 2
    assert _strict_strong_anchor_count(items, cfg) == 2


def test_llm_audit_booleans_do_not_create_hidden_find_gate():
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", research_interest="dynamic route retrieval systems", max_recommended_papers=5)
    items = [
        {
            "id": "llm_high_score_audit_false",
            "title": "Dynamic Route Recommender Systems",
            "abstract": "This paper studies dynamic route retrieval systems with benchmark evaluation.",
            "fit_score": 8.0,
            "diversity_score": 7.0,
            "score": 8.0,
            "reason_source": "llm abstract evaluation",
            "recommend_for_deep_reading": False,
            "supports_complete_requested_route": False,
            "topic_evidence": "weak:audit route says incomplete",
            "topic_evidence_supported": False,
            "evidence_role": "foundation_borrowing",
            "topic_evidence_basis": "abstract",
            "missing_topic_evidence": ["audit-only field"],
        },
        {
            "id": "llm_low_score_audit_true",
            "title": "Dynamic Route Recommender Systems low score",
            "abstract": "This paper studies a weakly related system with benchmark evaluation.",
            "fit_score": 6.0,
            "diversity_score": 7.0,
            "score": 6.0,
            "reason_source": "llm abstract evaluation",
            "recommend_for_deep_reading": True,
            "supports_complete_requested_route": True,
            "topic_evidence": "passed:debug route",
            "topic_evidence_supported": True,
            "evidence_role": "direct_target",
            "topic_evidence_basis": "abstract",
            "missing_topic_evidence": [],
        },
    ]

    recommended = _recommended(items, cfg)

    assert [item["id"] for item in recommended] == ["llm_high_score_audit_false", "llm_low_score_audit_true"]
    assert "find_recommendation_reject_reason" not in items[0]
    assert "find_recommendation_reject_reason" not in items[1]

def test_title_filtered_candidate_without_real_abstract_cannot_be_recommended():
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", research_interest="dynamic route retrieval systems", max_recommended_papers=5)
    items = [{
        "id": "title_only",
        "title": "Dynamic Route Recommender Systems",
        "abstract": "",
        "fit_score": 9.0,
        "diversity_score": 7.0,
        "score": 9.0,
        "reason_source": "llm abstract evaluation",
        "topic_evidence": "passed:dynamic route retrieval systems",
        "topic_evidence_supported": True,
        "evidence_role": "direct_target",
        "topic_evidence_basis": "title_only",
        "missing_topic_evidence": [],
    }]

    assert _recommended(items, cfg) == []
    assert items[0]["find_recommendation_reject_reason"] == "missing_real_abstract"


def test_find_recommendation_ranking_ignores_stable_source_score():
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", research_interest="dynamic route retrieval systems", max_recommended_papers=2)
    items = [
        {
            "id": "lower_llm_high_stable",
            "title": "Lower LLM score with high source score",
            "abstract": "This paper studies dynamic route retrieval systems with benchmark evaluation.",
            "fit_score": 7.0,
            "diversity_score": 5.0,
            "recommendation_score": 7.0,
            "stable_rank_score": 10.0,
            "reason_source": "llm abstract evaluation",
            "topic_evidence": "passed:dynamic route retrieval systems",
            "topic_evidence_supported": True,
            "evidence_role": "direct_target",
            "topic_evidence_basis": "abstract",
            "missing_topic_evidence": [],
        },
        {
            "id": "higher_llm_low_stable",
            "title": "Higher LLM score with low source score",
            "abstract": "This paper studies dynamic route retrieval systems with reusable algorithms and evaluation.",
            "fit_score": 8.0,
            "diversity_score": 5.0,
            "recommendation_score": 8.0,
            "stable_rank_score": 1.0,
            "reason_source": "llm abstract evaluation",
            "topic_evidence": "passed:dynamic route retrieval systems",
            "topic_evidence_supported": True,
            "evidence_role": "direct_target",
            "topic_evidence_basis": "abstract",
            "missing_topic_evidence": [],
        },
    ]

    assert [item["id"] for item in _recommended(items, cfg)] == ["higher_llm_low_stable", "lower_llm_high_stable"]



def test_final_llm_score_not_hard_filtered_by_project_debug_fields():
    cfg = AppConfig(
        provider="openai",
        api_key="test-key",
        model="test-model",
        research_interest="a future unrelated project topic that should not become a framework hard filter",
        max_recommended_papers=3,
    )
    items = [
        {
            "id": "final_llm_high_score_old_debug_weak",
            "title": "Reusable Benchmark Protocols for Recommendation Evaluation",
            "abstract": "This paper presents reusable benchmark protocols for recommendation evaluation, including datasets, metrics, baselines, and ablation settings that make it valuable for deep literature reading.",
            "fit_score": 8.2,
            "diversity_score": 4.0,
            "score": 8.2,
            "reason_source": "llm abstract evaluation",
            "score_source": "llm_title_abstract_score_only",
            "topic_evidence": "weak: legacy project-topic audit field from an older run",
            "topic_evidence_supported": False,
            "evidence_role": "foundation_borrowing",
            "missing_topic_evidence": ["old project-specific keyword"],
            "recommend_for_deep_reading": False,
            "supports_complete_requested_route": False,
            "not_positive_support": True,
            "strong_gate_reject_reason": "legacy downstream claim gate should not affect Find recommendation ranking",
        }
    ]

    recommended = _recommended(items, cfg)

    assert [item["id"] for item in recommended] == ["final_llm_high_score_old_debug_weak"]
    assert recommended[0]["find_recommendation"] is True
    assert recommended[0]["recommended_by_llm_ranking"] is True
    assert "find_recommendation_reject_reason" not in recommended[0]
    assert "not_positive_support" not in recommended[0]


def test_recommendation_readability_repair_does_not_leak_claim_or_audit_jargon():
    cfg = AppConfig(
        provider="openai",
        api_key="test-key",
        model="test-model",
        research_interest="recommendation evaluation protocols",
        max_recommended_papers=1,
    )
    items = [
        {
            "id": "needs_reason_repair",
            "title": "Benchmark Protocols for Recommendation Evaluation",
            "abstract": "This paper presents reusable benchmark protocols for recommendation evaluation, including datasets, metrics, baselines, and ablation settings that make it valuable for deep literature reading.",
            "fit_score": 8.0,
            "diversity_score": 5.0,
            "score": 8.0,
            "reason_source": "llm abstract evaluation",
            "hit_directions_zh": ["推荐评测协议"],
            "hit_directions_en": ["recommendation evaluation protocols"],
            "fit_explanation_zh": "摘要明确讨论推荐评测协议、数据集、指标、基线和消融设置，能够为精读阶段提供可复用的实验设计信息。",
            "fit_explanation_en": "The abstract discusses recommendation evaluation protocols, datasets, metrics, baselines, and ablations, so it provides reusable experiment-design information for deep reading.",
            "reason_zh": "太短",
            "reason_en": "Too short.",
            "topic_evidence": "weak: stale debug field",
            "topic_evidence_supported": False,
            "missing_topic_evidence": ["old debug axis"],
        }
    ]

    recommended = _recommended(items, cfg)
    text = " ".join(
        str(recommended[0].get(key) or "")
        for key in ["reason_zh", "reason_en", "fit_explanation_zh", "fit_explanation_en", "recommendation_note_zh", "recommendation_note_en"]
    )

    forbidden = [
        "证据边界", "论文结论", "claim", "paper-conclusion", "foundation", "内部候选", "实现",
        "值得推荐和精读", "帮助读者", "阅读提示", "全文精读", "摘要仍不足以替代全文精读",
        "Reading note", "full-text reading", "deep reading",
    ]
    assert all(term not in text for term in forbidden)
    assert find_pipeline._readable_text_len(recommended[0]["reason_zh"]) >= 120
    assert "当前研究画像" in recommended[0]["reason_zh"]
    assert "可直接借鉴" in recommended[0]["reason_zh"]
    assert "Find" not in recommended[0]["reason_zh"]
    assert "证据门控" not in recommended[0]["reason_zh"]
    assert recommended[0]["reader_instruction_zh"].startswith("内部给 Read 阶段")
    assert "核查" in recommended[0]["reader_instruction_zh"]

def test_recommendation_readability_rewrites_stale_internal_find_notes():
    cfg = AppConfig(
        provider="openai",
        api_key="test-key",
        model="test-model",
        research_interest="LLM semantic condition retrieval benchmark",
        max_recommended_papers=1,
    )
    stale_zh = (
        "《Semantic Retrieval Benchmark》值得推荐和精读。"
        "阅读提示：题名筛选线索只进入详情评分；"
        "只有取得真实摘要并完成最终相关性评分后才会展示为推荐论文。"
    )
    stale_en = "Reading note: final relevance score placed this row in the recommendation pool."
    items = [
        {
            "id": "stale_internal_note",
            "title": "Semantic Retrieval Benchmark",
            "abstract": "This paper studies semantic conditioning for retrieval benchmark with datasets, metrics, baselines, ablations, and limitations described in the abstract.",
            "abstract_zh": "本文研究面向检索基准的语义条件机制，并在摘要中说明数据集、指标、基线、消融和局限。",
            "fit_score": 8.0,
            "llm_fit_score": 8.0,
            "diversity_score": 5.0,
            "score": 8.0,
            "reason_source": "llm abstract evaluation",
            "hit_directions_zh": ["语义条件检索基准"],
            "hit_directions_en": ["semantic conditioning for retrieval benchmark"],
            "fit_explanation_zh": stale_zh,
            "fit_explanation_en": stale_en,
            "fit_explanation_zh_original": "摘要明确描述语义条件、检索基准、数据集、指标、基线和消融，可为精读阶段提供具体方法与实验协议线索。",
            "fit_explanation_en_original": "The abstract describes semantic conditioning, retrieval benchmark, datasets, metrics, baselines, and ablations, giving concrete method and protocol clues for deep reading.",
            "reason_zh": stale_zh,
            "reason_en": stale_en,
            "recommendation_note_zh": "题名筛选线索：尚未通过最终相关性评分，不展示为推荐论文。",
            "recommendation_note_en": "Title-screened signal before final relevance scoring.",
        }
    ]

    recommended = _recommended(items, cfg)
    text = " ".join(
        str(recommended[0].get(key) or "")
        for key in ["reason_zh", "reason_en", "recommendation_note_zh", "recommendation_note_en"]
    )

    forbidden = [
        "高召回", "最终 LLM", "LLM 题名", "LLM 评分", "Find", "Top-N", "证据门控", "内部候选",
        "值得推荐和精读", "帮助读者", "阅读提示", "全文精读", "摘要仍不足以替代全文精读",
        "Reading note", "full-text reading", "deep reading",
    ]
    assert all(term not in text for term in forbidden)
    assert recommended[0]["reason_quality_repaired"] is True
    assert "当前研究画像" in recommended[0]["reason_zh"]
    assert "可直接借鉴" in recommended[0]["reason_zh"]
    assert "全文精读" not in recommended[0]["fit_explanation_zh"]
    assert find_pipeline._has_internal_find_public_text(stale_zh, zh=True)
    assert find_pipeline._has_internal_find_public_text(stale_en, zh=False)
    assert not find_pipeline._has_internal_find_public_text(recommended[0]["fit_explanation_zh"], zh=True)
    assert not find_pipeline._has_internal_find_public_text(recommended[0]["fit_explanation_en"], zh=False)
    assert recommended[0]["recommendation_note_zh"] == find_pipeline._PUBLIC_FIND_RECOMMENDATION_NOTE_ZH
    assert recommended[0]["reader_instruction_zh"].startswith("内部给 Read 阶段")


def test_recommendation_quality_flags_generic_short_reasons():
    rows = [
        {
            "id": "short_reason",
            "title": "Retrieval Benchmark",
            "abstract": "This paper proposes a retrieval benchmark with datasets, metrics, baselines, and ablations for evaluation.",
            "abstract_zh": "本文提出检索基准方法，并包含数据集、指标、基线和消融实验。",
            "reason_zh": "推荐精读：方法相关，需全文确认实验协议。",
        }
    ]

    quality = find_pipeline._recommendation_quality_audit(rows)

    assert quality["status"] == "needs_repair"
    assert quality["short_or_negative_reason_count"] == 1


def test_final_scoring_prompt_forbids_foundation_passed_route():
    llm = BatchLLM()
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", research_interest="LLM-assisted retrieval benchmark", max_recommended_papers=5, llm_concurrency=1)
    items = [{"id": "paper_001", "title": "Retrieval Benchmark", "abstract": "A retrieval system uses diffusion for preference modeling.", "classification_source": "llm_inferred"}]

    _evaluate_items(items, cfg, llm, "articles", log=lambda _msg: None)

    final_prompt = llm.prompts[-1]
    assert "route/foundation/claim labels" in final_prompt
    assert "passed:foundation:<route>" not in final_prompt



def test_icml_virtual_detail_enrichment_fetches_abstracts_concurrently(monkeypatch):
    import threading

    lock = threading.Lock()
    active = 0
    max_active = 0
    requested_urls = []

    class FakeResponse:
        def __init__(self, text: str):
            self.text = text
            self.status_code = 200

    def fake_request(url, timeout=12):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            requested_urls.append(url)
        try:
            time.sleep(0.05)
            paper_id = str(url).rstrip('/').rsplit('/', 1)[-1]
            return FakeResponse(
                f"""
                <html><head>
                  <script type="application/ld+json">
                    {{"@context":"https://schema.org/","@type":"CreativeWork","name":"ICML Generative Recommendation Paper {paper_id}","author":[{{"@type":"Person","name":"Author {paper_id}"}},{{"@type":"Person","name":"Second Writer"}}]}}
                  </script>
                </head><body>
                  <div class="abstract-section">
                    <h3 class="abstract-header">Abstract</h3>
                    <div class="abstract-content">
                      <div id="abstractText" class="abstract-text collapsed">
                        <div class="abstract-text-inner">
                          This ICML official page abstract for {paper_id} explains a generative recommendation method,
                          its modeling mechanism, and benchmark evaluation protocol in enough detail for title plus abstract scoring.
                        </div>
                      </div>
                    </div>
                  </div>
                </body></html>
                """
            )
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(sources, "_request", fake_request)
    monkeypatch.setenv("ICML_DETAIL_WORKERS", "4")
    papers = [
        {
            "id": f"paper_{idx}",
            "source": "icml_downloads",
            "title": f"ICML Generative Recommendation Paper {idx}",
            "abstract": "",
            "url": f"https://icml.cc/virtual/2026/poster/{65000 + idx}",
            "pdf_url": "",
            "venue": "ICML",
            "year": 2026,
            "metadata": {"title_index_only": True},
        }
        for idx in range(8)
    ]

    enriched = sources.fetch_selected_venue_details(papers, wall_timeout_sec=20)

    assert len(requested_urls) == 8
    assert max_active > 1
    assert all("official page abstract" in item["abstract"] for item in enriched)
    assert all("Author" in item["authors"] for item in enriched)
    assert all(item["metadata"].get("abstract_source") == "icml_virtual" for item in enriched)
    assert all(item["metadata"].get("authors_source") == "icml_virtual_jsonld" for item in enriched)
    stats = enriched[0]["metadata"].get("detail_fetch_stats")
    assert stats["attempted"] == 8
    assert stats["abstracts_filled"] == 8
    assert stats["authors_filled"] == 8
    assert stats["deferred"] == 0



def test_large_venue_title_screen_uses_configured_recall_budget(monkeypatch):
    monkeypatch.setenv("DISABLE_LLM_TITLE_FILTER", "1")
    cfg = AppConfig(
        provider="openai",
        research_interest="LLM-assisted retrieval benchmark systems",
        find_recall_count=3000,
        detail_fetch_count=800,
        max_recommended_papers=100,
        default_find_selection={"venue_ids": ["openreview_iclr_2026", "openreview_neurips", "dblp_icml", "dblp_kdd"]},
    )
    items = [
        {
            "id": f"paper_{idx}",
            "title": f"Generic Machine Learning Paper {idx}",
            "abstract": "",
            "venue": "ICML",
            "year": 2026,
        }
        for idx in range(1200)
    ]
    for idx, title in enumerate([
        "Discrete Diffusion for Recommendation",
        "LLM Semantic Diffusion Recommender Systems",
        "Retrieval Benchmark with Item Semantics",
    ]):
        items[idx]["title"] = title

    logs: list[str] = []
    selected = _prefilter_titles(items, cfg, BatchLLM(), "ICML", log=logs.append, should_cancel=lambda: False, scan_all=True)

    assert len(selected) == cfg.detail_fetch_count
    assert len(selected) >= 800
    assert len(selected) < cfg.find_recall_count
    assert any("bounded local title ranking" in message for message in logs)


def test_large_uncategorized_venue_scores_full_title_pool_with_llm(monkeypatch):
    monkeypatch.setenv("LARGE_TITLE_POOL_THRESHOLD", "50")
    monkeypatch.setenv("LLM_TITLE_FILTER_MAX_TITLES", "50")
    monkeypatch.setenv("TITLE_DETAIL_CANDIDATE_TARGET", "60")
    cfg = AppConfig(provider="openai", api_key="test", model="test", research_interest="LLM-assisted retrieval benchmark systems", llm_concurrency=1, max_recommended_papers=20)
    items = [
        {
            "id": f"paper_{idx}",
            "title": f"LLM Retrieval Benchmark Candidate {idx}",
            "abstract": "",
            "venue": "ICML",
            "year": 2026,
            "category": f"Local topic: singleton / {idx}",
            "classification_source": "uncategorized_title_index",
            "metadata": {"category_status": "no_official_categories"},
        }
        for idx in range(90)
    ]
    llm = BatchLLM()
    logs: list[str] = []

    selected = _prefilter_titles(items, cfg, llm, "ICML", log=logs.append, should_cancel=lambda: False, scan_all=True)

    assert len(llm.prompts) == 9
    assert len(selected) == 60
    assert any("full title pool" in message for message in logs)
    assert not any("locally shortlisted" in message for message in logs)


class SparseTitleLLM(BatchLLM):
    def json_or_error(self, prompt: str, temperature=None):
        self.prompts.append(prompt)
        self.temperatures.append(temperature)
        ids = re.findall(r"paper_\d+", prompt)
        return {
            "ok": True,
            "error": "",
            "data": {
                "scored": [
                    {
                        "id": item_id,
                        "fit_score": 6.5,
                        "diversity_score": 5.0,
                        "hit_directions": ["推荐系统"],
                        "category": "Recommendation",
                        "reason": "标题级相关性评分。",
                    }
                    for item_id in ids[:4]
                ]
            },
        }


def test_llm_title_scoring_retains_ranked_budget_not_sparse_selected(monkeypatch):
    monkeypatch.setenv("TITLE_DETAIL_CANDIDATE_TARGET", "20")
    cfg = AppConfig(
        provider="openai",
        research_interest="LLM-assisted retrieval benchmark systems",
        max_recommended_papers=20,
        default_find_selection={"venue_ids": ["openreview_iclr_2026", "openreview_neurips", "dblp_icml", "dblp_kdd"]},
    )
    items = [
        {
            "id": f"paper_{idx}",
            "title": f"LLM Retrieval Benchmark Candidate {idx}",
            "abstract": "",
            "venue": "ICML",
            "year": 2026,
        }
        for idx in range(30)
    ]

    selected = _prefilter_titles(items, cfg, SparseTitleLLM(), "ICML", log=lambda _msg: None, should_cancel=lambda: False, scan_all=True)

    assert len(selected) == 20
    assert sum(item.get("reason_source") == "llm title filter" for item in selected) == 12
    assert sum(bool(item.get("title_llm_missing")) for item in selected) == 8
    llm_ranked = [item for item in selected if item.get("reason_source") == "llm title filter"]
    assert llm_ranked[0]["title_llm_fit_score"] >= llm_ranked[-1]["title_llm_fit_score"]
    assert all("title_llm_fit_score" not in item for item in selected if item.get("title_llm_missing"))


def test_find_recommendations_fill_topn_without_absolute_fit_cutoff():
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", max_recommended_papers=20)
    high = [
        {
            "id": f"high-{idx}",
            "title": f"High priority recommendation {idx}",
            "abstract": "This real abstract describes a concrete reusable recommendation method with evaluation protocol and results.",
            "fit_score": 7.0 + idx * 0.01,
            "score": 7.0 + idx * 0.01,
            "reason_source": "llm abstract evaluation",
            "recommend_for_deep_reading": True,
        }
        for idx in range(3)
    ]
    low = [
        {
            "id": f"low-{idx}",
            "title": f"Partial background item {idx}",
            "abstract": "This real abstract has lower final relevance but still has a real abstract and final LLM score.",
            "fit_score": 6.0,
            "score": 6.0,
            "reason_source": "llm abstract evaluation",
            "recommend_for_deep_reading": True,
        }
        for idx in range(10)
    ]

    recommended = _recommended(high + low, cfg)

    assert [item["id"] for item in recommended] == ["high-2", "high-1", "high-0"] + [f"low-{idx}" for idx in range(10)]
    assert any(item["fit_score"] < 7 for item in recommended)
    assert all(item["find_recommendation"] is True for item in recommended)
    assert all("find_recommendation_reject_reason" not in item for item in low)


def test_find_recommendations_still_reject_missing_abstract_or_final_llm_score():
    cfg = AppConfig(provider="openai", api_key="test-key", model="test-model", max_recommended_papers=5)
    items = [
        {
            "id": "valid_low_score",
            "title": "Valid Low Score",
            "abstract": "This real abstract was scored by the final LLM judge.",
            "fit_score": 4.0,
            "score": 4.0,
            "reason_source": "llm abstract evaluation",
        },
        {
            "id": "missing_abstract",
            "title": "Missing Abstract",
            "abstract": "",
            "fit_score": 9.0,
            "score": 9.0,
            "reason_source": "llm abstract evaluation",
        },
        {
            "id": "local_only",
            "title": "Local Only",
            "abstract": "This real abstract exists but was not final LLM-scored.",
            "fit_score": 9.0,
            "score": 9.0,
            "reason_source": "local title screen",
        },
    ]

    recommended = _recommended(items, cfg)

    assert [item["id"] for item in recommended] == ["valid_low_score"]
    assert items[1]["find_recommendation_reject_reason"] == "missing_real_abstract"
    assert items[2]["find_recommendation_reject_reason"] == "missing_final_title_abstract_llm_scoring"

class BlockingLLM(BatchLLM):
    def __init__(self, wait_sec=5.0):
        super().__init__()
        self.wait_sec = wait_sec

    def json_or_error(self, prompt: str, temperature=None, max_tokens=None):
        self.prompts.append(prompt)
        self.temperatures.append(temperature)
        time.sleep(self.wait_sec)
        return {"ok": True, "error": "", "data": {"evaluations": []}}


def test_parallel_title_filter_cancel_exits_without_waiting_for_blocked_workers(monkeypatch):
    monkeypatch.setenv("TITLE_FILTER_WALL_TIMEOUT_SEC", "5")
    cfg = AppConfig(
        provider="openai",
        api_key="test-key",
        model="test-model",
        research_interest="LLM-assisted retrieval benchmark systems",
        max_recommended_papers=20,
        llm_concurrency=4,
    )
    items = [
        {"id": f"paper_{idx:03d}", "title": f"LLM-assisted retrieval benchmark candidate {idx}", "venue": "ICML", "year": 2026}
        for idx in range(40)
    ]
    checks = 0

    def should_cancel():
        nonlocal checks
        checks += 1
        return checks >= 2

    started = time.monotonic()
    with pytest.raises(JobCancelled):
        _prefilter_titles(items, cfg, BlockingLLM(), "ICML", log=lambda _msg: None, should_cancel=should_cancel, scan_all=True)

    assert time.monotonic() - started < 3.0


def test_parallel_final_scoring_cancel_exits_without_waiting_for_blocked_workers(monkeypatch):
    monkeypatch.setenv("ABSTRACT_SCORING_WALL_TIMEOUT_SEC", "5")
    monkeypatch.setenv("ABSTRACT_SCORING_MAX_WORKERS", "4")
    cfg = AppConfig(
        provider="openai",
        api_key="test-key",
        model="test-model",
        research_interest="LLM-assisted retrieval benchmark systems",
        max_recommended_papers=20,
    )
    items = [
        {
            "id": f"paper_{idx:03d}",
            "title": f"LLM-assisted retrieval benchmark candidate {idx}",
            "abstract": "This paper studies retrieval models and large language model semantic signals for benchmark systems.",
            "classification_source": "llm_inferred",
        }
        for idx in range(24)
    ]
    checks = 0

    def should_cancel():
        nonlocal checks
        checks += 1
        return checks >= 2

    started = time.monotonic()
    with pytest.raises(JobCancelled):
        _evaluate_items(items, cfg, BlockingLLM(), "articles", log=lambda _msg: None, should_cancel=should_cancel)

    assert time.monotonic() - started < 3.0


def test_title_complete_without_abstracts_is_source_limited_for_find_ui():
    fields = _venue_metadata_status_fields({
        "status": "complete",
        "complete": True,
        "has_abstracts": False,
        "has_official_categories": False,
        "category_status": "no_official_categories",
        "completeness_basis": "DBLP paginated stream search completed.",
    })

    assert fields["title_index_completeness_ok"] is True
    assert fields["metadata_completeness_status"] == "title_index_only"
    assert fields["metadata_completeness_ok"] is False
    assert fields["metadata_completeness_limited"] is True
    assert "does not expose abstracts" in fields["metadata_completeness_basis"]
    assert "No trusted official venue categories" in fields["metadata_completeness_basis"]


def test_official_category_title_index_counts_as_complete_metadata_for_screening():
    fields = _venue_metadata_status_fields({
        "status": "complete",
        "complete": True,
        "has_abstracts": False,
        "has_official_categories": True,
        "category_status": "official_or_cached_categories",
        "completeness_basis": "OpenReview title corpus with official area metadata.",
    })

    assert fields["metadata_completeness_status"] == "complete"
    assert fields["metadata_completeness_ok"] is True
    assert fields["metadata_completeness_limited"] is False



def test_combined_title_only_local_audit_is_not_complete_metadata():
    combined = find_pipeline._combined_metadata_audit(
        [
            {
                "status": "complete",
                "complete": True,
                "source_verified": True,
                "paper_count": 100,
                "missing_abstract_count": 100,
                "has_abstracts": False,
                "any_abstracts": False,
                "has_official_categories": False,
                "category_status": "no_official_categories",
                "source_scope": "dblp_current_index_not_official_accepted_list",
                "official_accepted_list_verified": False,
                "completeness_basis": "DBLP title index only.",
            },
            {
                "status": "complete",
                "complete": True,
                "source_verified": True,
                "paper_count": 10,
                "missing_abstract_count": 0,
                "has_abstracts": True,
                "any_abstracts": True,
                "has_official_categories": False,
                "category_status": "no_official_categories",
                "source_scope": "dblp_current_index_not_official_accepted_list",
                "official_accepted_list_verified": False,
                "completeness_basis": "Partial enriched abstracts.",
            },
        ],
        "local_database",
    )
    fields = _venue_metadata_status_fields(combined)

    assert combined["complete"] is True
    assert combined["has_abstracts"] is False
    assert combined["any_abstracts"] is True
    assert combined["has_official_categories"] is False
    assert combined["official_accepted_list_verified"] is False
    assert fields["metadata_completeness_status"] == "title_index_only"
    assert fields["metadata_completeness_ok"] is False
    assert fields["source_scope"] == "dblp_current_index_not_official_accepted_list"
    assert fields["official_title_index_verified"] is False
    assert fields["official_accepted_list_verified"] is False
    assert fields["official_metadata_complete"] is False


def test_local_title_only_database_skips_category_selection_even_with_pseudo_categories(monkeypatch):
    papers = [
        {
            "id": f"paper_{idx}",
            "title": f"LLM Retrieval Benchmark Candidate {idx}",
            "abstract": "",
            "category": f"Local topic: singleton / {idx}",
            "primary_area": f"Local topic: singleton / {idx}",
            "classification_source": "uncategorized_title_index",
        }
        for idx in range(12)
    ]
    local_payload = {
        "venue_id": "dblp_icml",
        "year": 2026,
        "papers_path": "/tmp/papers.json",
        "category_summary_path": "/tmp/category_summary.json",
        "manifest_path": "/tmp/manifest.json",
        "source_adapter": "icml_downloads",
        "paper_count": len(papers),
        "papers": papers,
        "category_summary": {"category_summary": [{"name": "Local topic", "count": len(papers)}]},
        "metadata_completeness_audit": {
            "complete": True,
            "has_abstracts": False,
            "has_official_categories": False,
            "category_status": "no_or_partial_categories",
        },
    }
    monkeypatch.setattr(find_pipeline, "load_local_venue_year", lambda venue, year: local_payload)
    monkeypatch.setattr(
        find_pipeline,
        "select_relevant_categories",
        lambda *_args, **_kwargs: pytest.fail("no official categories should skip category selection"),
    )

    result = find_pipeline._load_local_category_guided_index(
        {"id": "dblp_icml", "name": "ICML"},
        [2026],
        AppConfig(),
        BatchLLM(),
        12000,
        log=lambda _msg: None,
    )

    assert result is not None
    title_index, reports, corpus = result
    assert len(title_index) == len(papers)
    assert len(corpus) == len(papers)
    assert reports[0]["selection"]["selected_paper_count"] == len(papers)
    assert "selection_mode" not in reports[0]["selection"]
    assert "fallback_used" not in reports[0]["selection"]
    assert reports[0]["metadata_completeness_status"] == "title_index_only"


def test_local_dblp_cache_complete_means_title_index_not_official_accepted_list():
    papers = [
        {"id": "kdd_1", "title": "Retrieval Benchmark with LLM Semantics", "abstract": ""},
        {"id": "kdd_2", "title": "Sequential Recommendation with Denoising", "abstract": ""},
    ]
    local_payload = {
        "venue_id": "dblp_kdd",
        "year": 2026,
        "papers_path": "/tmp/papers.json",
        "category_summary_path": "/tmp/category_summary.json",
        "manifest_path": "/tmp/manifest.json",
        "source_adapter": "dblp",
        "paper_count": len(papers),
        "papers": papers,
        "category_summary": {"category_summary": []},
        "metadata_completeness_audit": {
            "status": "complete",
            "complete": True,
            "source_verified": True,
            "adapter": "dblp_search_api",
            "source_scope": "dblp_current_index_not_official_accepted_list",
            "has_abstracts": False,
            "has_official_categories": False,
            "category_status": "no_official_categories",
            "official_title_index_verified": True,
            "official_accepted_list_verified": True,
        },
    }

    audit = find_pipeline._local_database_metadata_audit(local_payload)
    fields = _venue_metadata_status_fields(audit)

    assert audit["complete"] is True
    assert audit["title_index_complete"] is True
    assert audit["source_scope"] == "dblp_current_index_not_official_accepted_list"
    assert audit["official_title_index_verified"] is False
    assert audit["official_accepted_list_verified"] is False
    assert audit["official_metadata_complete"] is False
    assert fields["metadata_completeness_status"] == "title_index_only"
    assert fields["metadata_completeness_limited"] is True
    assert fields["source_scope"] == "dblp_current_index_not_official_accepted_list"
    assert fields["missing_abstract_count"] == len(papers)


def test_dblp_stream_api_marks_index_scope_not_official_accepted_list(monkeypatch):
    def fake_hits(stream_id, *, year, max_items):
        return [
            {
                "info": {
                    "year": str(year),
                    "title": "Retrieval Benchmark via User Preference Denoising",
                    "ee": "https://doi.org/10.1145/example",
                    "doi": "10.1145/example",
                    "key": "conf/kdd/example",
                    "authors": {"author": ["A. Researcher"]},
                }
            }
        ], {"query_year": year, "total": 1, "sent": 1, "pages_fetched": 1, "complete": True, "truncated": False}

    monkeypatch.setattr(sources, "_dblp_search_hits", fake_hits)

    rows = sources.fetch_dblp_stream_api(
        {"id": "dblp_kdd", "name": "KDD", "address": "https://dblp.org/db/conf/kdd/index.html"},
        [2026],
        max_items=100,
    )
    audit = rows[0]["metadata"][sources.VENUE_METADATA_AUDIT_KEY]

    assert len(rows) == 1
    assert audit["source_scope"] == "dblp_current_index_not_official_accepted_list"
    assert audit["title_index_complete"] is True
    assert audit["dblp_stream_index_complete"] is True
    assert audit["official_metadata_complete"] is False
    assert audit["official_title_index_verified"] is False
    assert audit["official_accepted_list_verified"] is False
    assert audit["has_abstracts"] is False
    assert audit["has_official_categories"] is False

def test_abstract_enrichment_tries_semantic_scholar_before_openalex_and_arxiv(monkeypatch):
    calls = []

    def fake_semantic(papers, limit=20, api_key=""):
        calls.append("semantic_scholar")
        for paper in papers[:limit]:
            paper["abstract"] = "Semantic Scholarar DOI abstract for this KDD paper."
            paper.setdefault("metadata", {})["abstract_source"] = "semantic_scholar_doi"
        return papers

    def fake_openalex(papers, limit=80):
        calls.append("openalex")
        return papers

    def fake_arxiv(papers, limit=40):
        calls.append("arxiv")
        return papers

    monkeypatch.setattr("auto_research.auto_find.pipeline.enrich_with_semantic_scholar", fake_semantic)
    monkeypatch.setattr("auto_research.auto_find.pipeline.enrich_with_openalex", fake_openalex)
    monkeypatch.setattr("auto_research.auto_find.pipeline.enrich_with_arxiv_title_match", fake_arxiv)
    cfg = AppConfig(research_interest="LLM-assisted retrieval benchmark", default_find_selection={"include_arxiv": True})
    items = [{"id": "kdd_doi", "title": "Retrieval Benchmark", "abstract": "", "doi": "10.1145/example", "metadata": {"doi": "10.1145/example"}}]

    _enrich_missing_abstracts_for_adaptive_recall(items, cfg, "KDD", log=lambda _msg: None, progress=lambda *_args: None)

    assert calls == ["semantic_scholar"]
    assert items[0]["abstract"].startswith("Semantic Scholarar DOI abstract")
    assert items[0]["metadata"]["abstract_enrichment_sources"] == ["semantic_scholar", "openalex", "arxiv_title_match"]


def test_abstract_enrichment_falls_through_when_semantic_scholar_has_no_abstract(monkeypatch):
    calls = []

    def fake_semantic(papers, limit=20, api_key=""):
        calls.append("semantic_scholar")
        return papers

    def fake_openalex(papers, limit=80):
        calls.append("openalex")
        for paper in papers[:limit]:
            paper["abstract"] = "OpenAlex abstract after Semantic Scholarar miss."
        return papers

    def fake_arxiv(papers, limit=40):
        calls.append("arxiv")
        return papers

    monkeypatch.setattr("auto_research.auto_find.pipeline.enrich_with_semantic_scholar", fake_semantic)
    monkeypatch.setattr("auto_research.auto_find.pipeline.enrich_with_openalex", fake_openalex)
    monkeypatch.setattr("auto_research.auto_find.pipeline.enrich_with_arxiv_title_match", fake_arxiv)
    cfg = AppConfig(research_interest="LLM-assisted retrieval benchmark", default_find_selection={"include_arxiv": True})
    items = [{"id": "kdd_doi", "title": "Retrieval Benchmark", "abstract": "", "doi": "10.1145/example", "metadata": {"doi": "10.1145/example"}}]

    _enrich_missing_abstracts_for_adaptive_recall(items, cfg, "KDD", log=lambda _msg: None, progress=lambda *_args: None)

    assert calls == ["semantic_scholar", "openalex"]
    assert items[0]["abstract"].startswith("OpenAlex abstract")



def test_venue_metadata_cache_manifest_keeps_title_only_source_limited(tmp_path):
    from auto_research.auto_update.json_builder import build_venue_metadata_cache as builder

    cache_dir = tmp_path / "dblp_kdd" / "2026"
    cache_dir.mkdir(parents=True)
    papers = [
        {"id": "kdd_1", "title": "Retrieval Benchmark with Semantic Signals", "abstract": ""},
        {"id": "kdd_2", "title": "Sequential Recommender Systems with Denoising", "abstract": ""},
    ]
    (cache_dir / "papers.json").write_text(
        json.dumps({
            "venue_id": "dblp_kdd",
            "venue": "KDD",
            "year": 2026,
            "source_adapter": "dblp",
            "paper_count": len(papers),
            "metadata_completeness_audit": {
                "complete": True,
                "source_verified": True,
                "has_abstracts": False,
                "has_official_categories": False,
                "category_status": "no_official_categories",
            },
            "papers": papers,
        }),
        encoding="utf-8",
    )
    (cache_dir / "category_summary.json").write_text(
        json.dumps({
            "venue_id": "dblp_kdd",
            "venue": "KDD",
            "year": 2026,
            "source_adapter": "dblp",
            "paper_count": len(papers),
            "category_summary": [],
        }),
        encoding="utf-8",
    )

    builder.write_manifest_for_existing_cache("dblp_kdd", 2026, tmp_path)
    manifest = json.loads((cache_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["title_index_completeness_ok"] is True
    assert manifest["metadata_completeness_status"] == "title_index_only"
    assert manifest["metadata_completeness_ok"] is False
    assert manifest["metadata_completeness_limited"] is True
    assert manifest["has_abstracts"] is False
    assert manifest["any_abstracts"] is False
    assert manifest["missing_abstract_count"] == len(papers)
    assert manifest["source_scope"] == "dblp_current_index_not_official_accepted_list"
    assert manifest["official_title_index_verified"] is False
    assert manifest["official_accepted_list_verified"] is False


def test_venue_metadata_cache_manifest_uses_adapter_not_dblp_prefix_for_icml(tmp_path):
    from auto_research.auto_update.json_builder import build_venue_metadata_cache as builder

    cache_dir = tmp_path / "dblp_icml" / "2026"
    cache_dir.mkdir(parents=True)
    papers = [
        {"id": "icml_1", "title": "Retrieval Benchmark with Semantic Signals", "abstract": ""},
        {"id": "icml_2", "title": "LLM Conditioned Sequential Recommendation", "abstract": ""},
    ]
    (cache_dir / "papers.json").write_text(
        json.dumps({
            "venue_id": "dblp_icml",
            "venue": "ICML",
            "year": 2026,
            "source_adapter": "icml_downloads",
            "paper_count": len(papers),
            "metadata_completeness_audit": {
                "complete": True,
                "source_verified": True,
                "source_scope": "dblp_current_index_not_official_accepted_list",
                "has_abstracts": False,
                "has_official_categories": False,
                "category_status": "no_official_categories",
            },
            "papers": papers,
        }),
        encoding="utf-8",
    )
    (cache_dir / "category_summary.json").write_text(
        json.dumps({
            "venue_id": "dblp_icml",
            "venue": "ICML",
            "year": 2026,
            "source_adapter": "icml_downloads",
            "paper_count": len(papers),
            "category_summary": [],
        }),
        encoding="utf-8",
    )

    builder.write_manifest_for_existing_cache("dblp_icml", 2026, tmp_path)
    manifest = json.loads((cache_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["adapter"] == "icml_downloads"
    assert manifest["source_scope"] == "official_icml_downloads_title_index"
    assert manifest["official_title_index_verified"] is True
    assert manifest["official_accepted_list_verified"] is True
    assert manifest["metadata_completeness_status"] == "title_index_only"
    assert manifest["metadata_completeness_ok"] is False


def test_venue_metadata_cache_manifest_treats_official_categories_as_find_ready(tmp_path):
    from auto_research.auto_update.json_builder import build_venue_metadata_cache as builder

    cache_dir = tmp_path / "openreview_iclr_2026" / "2026"
    cache_dir.mkdir(parents=True)
    papers = [
        {"id": "iclr_1", "title": "Retrieval Benchmark with Semantic Signals", "abstract": "", "primary_area": "retrieval systems"},
        {"id": "iclr_2", "title": "Sequential Evidence Selection for Retrieval", "abstract": "", "primary_area": "retrieval systems"},
    ]
    categories = [{"name": "retrieval systems", "count": len(papers)}]
    (cache_dir / "papers.json").write_text(
        json.dumps({
            "venue_id": "openreview_iclr_2026",
            "venue": "ICLR",
            "year": 2026,
            "source_adapter": "openreview_api2",
            "paper_count": len(papers),
            "metadata_completeness_audit": {
                "complete": True,
                "source_verified": True,
                "has_abstracts": False,
                "has_official_categories": True,
                "category_status": "official_or_cached_categories",
            },
            "papers": papers,
        }),
        encoding="utf-8",
    )
    (cache_dir / "category_summary.json").write_text(
        json.dumps({
            "venue_id": "openreview_iclr_2026",
            "venue": "ICLR",
            "year": 2026,
            "source_adapter": "openreview_api2",
            "paper_count": len(papers),
            "category_summary": categories,
        }),
        encoding="utf-8",
    )

    builder.write_manifest_for_existing_cache("openreview_iclr_2026", 2026, tmp_path)
    manifest = json.loads((cache_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["title_index_completeness_ok"] is True
    assert manifest["metadata_completeness_status"] == "complete"
    assert manifest["metadata_completeness_ok"] is True
    assert manifest["metadata_completeness_limited"] is False
    assert manifest["has_official_categories"] is True
    assert manifest["has_abstracts"] is False
    assert manifest["source_scope"] == "official_openreview_metadata"
    assert manifest["official_title_index_verified"] is True
    assert manifest["official_accepted_list_verified"] is True
