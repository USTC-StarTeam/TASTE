import auto_research.auto_read.pipeline as read_pipeline

from datetime import date

from auto_research.auto_find.pipeline import SCORING_POLICY_VERSION, _apply_quality_bonus, _apply_stable_ranking_score, _attach_latest_released_venue_context, _latest_released_venue_context, _min_title_candidates, _resolve_venue_years, _score_title_pool, _selection_source_count, _selection_venue_year_groups, run_find
from auto_research.paths import RUNS_DIR
from auto_research.storage import read_json
from auto_research.auto_idea.pipeline import patch_idea, run_idea
from auto_research.auto_plan.pipeline import run_plan
from auto_research.auto_read.pipeline import run_read
from auto_research.models import AppConfig, FindRequest, IdeaPatch, IdeaRequest, PlanRequest, ReadRequest, VenueSelection
from auto_research.storage import delete_run, run_dir



def test_pipeline_selection_venue_year_groups_preserve_multiple_years():
    selection = VenueSelection(venue_ids=["openreview_iclr_2026"], years=[2026, 2025])

    assert _selection_venue_year_groups(selection) == [
        ("openreview_iclr_2026", [2026]),
        ("openreview_iclr_2026", [2025]),
    ]
    assert _selection_source_count(selection) == 2

def test_mock_pipeline_arxiv_disabled_external_optional():
    cfg = AppConfig(
        provider="mock",
        research_interest="Discrete Diffusion Retrieval Construction",
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


def test_read_marks_metadata_only_recommendations_pending_full_text(monkeypatch):
    from auto_research.storage import create_run_dir, write_json

    run_id, directory = create_run_dir("read_pending_test")
    try:
        write_json(
            directory / "find_results.json",
            {
                "run_id": run_id,
                "strong_recommendations": [
                    {
                        "id": "paper-1",
                        "title": "Steering Diffusion Models Towards Credible Content Recommendation",
                        "venue": "ICLR",
                        "year": 2026,
                        "abstract": "This paper studies diffusion models for recommendation with credibility constraints.",
                        "reason_zh": "论文摘要显示其讨论检索基准和可信内容约束，值得读正文后判断方法细节。",
                        "pdf_url": "",
                        "url": "https://example.test/paper-1",
                    }
                ],
            },
        )
        monkeypatch.setenv("SKIP_PDF", "1")

        result = run_read(ReadRequest(run_id=run_id, max_papers=0), AppConfig(provider="mock"), log=lambda _msg: None)

        reading = result["readings"][0]
        assert reading["full_text_available"] is False
        assert reading["full_text_status"] == "pending_full_text_reading"
        assert reading["pdf_text_chars"] == 0
        assert reading["method_details_zh"] == ""
        assert reading["experiments_zh"] == ""
        assert reading["method_advantages_zh"] == []
        assert "diffusion models for recommendation" not in reading["method_details_zh"]
        markdown = (directory / "read.md").read_text(encoding="utf-8")
        assert "### 详细方法" in markdown
        assert "详细方法待补" not in markdown
        assert "全文未读取" not in markdown
        assert "（该字段未提供合格精读内容。）" in markdown
        assert "diffusion models for recommendation" not in markdown
        forbidden = ["对系统实现的直接含义", "Guardrail", "仍需阅读全文", "实验与证据限制"]
        assert not any(item in markdown for item in forbidden)
    finally:
        delete_run(run_id)


def test_read_syncs_results_json_to_project_finding(monkeypatch, tmp_path):
    from auto_research.storage import create_run_dir, write_json

    run_id, directory = create_run_dir("read_project_sync_test")
    project_root = tmp_path / "root"
    taste_dir = project_root / "projects" / "demo_project" / "planning" / "finding"
    taste_dir.mkdir(parents=True)
    try:
        write_json(
            directory / "find_results.json",
            {
                "run_id": run_id,
                "strong_recommendations": [
                    {
                        "id": "paper-1",
                        "title": "Metadata Only Paper",
                        "abstract": "A recommendation abstract.",
                        "reason_zh": "值得精读。",
                        "url": "https://example.test/paper-1",
                    }
                ],
            },
        )
        monkeypatch.setenv("WORKSPACE_ROOT", str(project_root))
        monkeypatch.setenv("PROJECT_ID", "demo_project")
        monkeypatch.setenv("SKIP_PDF", "1")

        run_read(ReadRequest(run_id=run_id, max_papers=0), AppConfig(provider="mock"), log=lambda _msg: None)

        project_read = read_json(taste_dir / "read_results.json", {})
        assert project_read["run_id"] == run_id
        assert project_read["readings"][0]["full_text_status"] == "pending_full_text_reading"
        assert (taste_dir / "read.md").exists()
    finally:
        delete_run(run_id)


def test_read_marks_pdf_text_as_full_text_evidence(monkeypatch):
    from auto_research.storage import create_run_dir, write_json

    run_id, directory = create_run_dir("read_full_text_test")
    try:
        write_json(
            directory / "find_results.json",
            {
                "run_id": run_id,
                "strong_recommendations": [
                    {
                        "id": "paper-1",
                        "title": "Full Text Diffusion Recommendation",
                        "venue": "ICLR",
                        "year": 2026,
                        "abstract": "A short abstract.",
                        "pdf_url": "https://example.test/paper.pdf",
                        "url": "https://example.test/paper",
                    }
                ],
            },
        )
        monkeypatch.setattr(read_pipeline, "_download_pdf", lambda _url, _target: True)
        monkeypatch.setattr(read_pipeline, "_extract_pdf_text", lambda _path: "方法和实验正文。" * 500)

        result = run_read(ReadRequest(run_id=run_id, max_papers=0), AppConfig(provider="mock"), log=lambda _msg: None)

        reading = result["readings"][0]
        assert reading["full_text_available"] is False
        assert reading["full_text_status"] == "full_text_packet_ready_pending_deep_read_synthesis"
        assert reading["pdf_text_read"] is False
        assert reading["source_evidence"]["full_text_available"] is True
        assert reading["source_evidence"]["pdf_text_chars"] >= read_pipeline.FULL_TEXT_MIN_CHARS
        markdown = (directory / "read.md").read_text(encoding="utf-8")
        assert "详细方法待补" not in markdown
        assert "全文未读取" not in markdown
    finally:
        delete_run(run_id)




def test_read_arxiv_title_variant_accepts_author_verified_pdf(monkeypatch):
    feed = b"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom'>
  <entry>
    <title>Exploring Recommender System Evaluation: A Multi-Modal User Agent Framework for A/B Testing</title>
    <id>http://arxiv.org/abs/2601.04554v1</id>
    <author><name>Unrelated Author</name></author>
    <link title='pdf' type='application/pdf' href='https://arxiv.org/pdf/2601.04554v1'/>
  </entry>
  <entry>
    <title>Exploring Recommender System Evaluation: A Multi-Modal User Agent Framework for A/B Testing</title>
    <id>http://arxiv.org/abs/2601.04554v2</id>
    <author><name>Wenlin Zhang</name></author>
    <author><name>Xiangyang Li</name></author>
    <author><name>Xiangyu Zhao</name></author>
    <link title='pdf' type='application/pdf' href='https://arxiv.org/pdf/2601.04554v2'/>
  </entry>
</feed>"""

    class Response:
        status_code = 200
        content = feed
        headers = {"content-type": "application/atom+xml"}

    monkeypatch.setattr(read_pipeline.requests, "get", lambda *_args, **_kwargs: Response())
    paper = {
        "title": "Exploring Recommender System Evaluation: A Multi-Modal LLM Agent Framework for A/B Testing",
        "authors": "Wenlin Zhang 0001, Xiangyang Li 0004, Xiangyu Zhao 0001",
        "url": "https://doi.org/10.1145/3770854.3785688",
    }

    arxiv_rows = read_pipeline._arxiv_pdf_candidates(paper)
    assert arxiv_rows[0]["accepted"] is False
    assert arxiv_rows[1]["accepted"] is True
    assert arxiv_rows[1]["author_overlap"] == ["li", "zhang", "zhao"]

    pdf_candidates = read_pipeline._pdf_candidates_for_reading(paper)
    assert pdf_candidates[0]["kind"] == "arxiv_title_verified_pdf"
    assert pdf_candidates[0]["pdf_url"] == "https://arxiv.org/pdf/2601.04554v2"


def test_read_sanitizes_llm_public_output_before_persisting(monkeypatch):
    from auto_research.storage import create_run_dir, write_json

    class DirtyReadLLM:
        def __init__(self, *_args, **_kwargs):
            self.enabled = True

        def json_or_none(self, _prompt):
            return {
                "summary": "原论文摘要：论文提出可信检索基准框架，通过在候选生成和排序阶段联合建模用户偏好、内容可信度和去噪轨迹，缓解低可信内容被高相关性信号误推的问题。对系统实现的直接含义：该条目是当前用户可见推荐文章。",
                "abstract_zh": "中文摘要：论文研究可信检索基准，提出一种将内容可信度约束并入扩散去噪排序的框架，在候选生成、轨迹校正和最终重排三个环节同时考虑偏好匹配与可信风险，从而降低低可信内容进入推荐结果的概率。论文还分析了不同可信度信号在去噪轨迹中的作用，并说明该框架可以在不完全牺牲排序相关性的前提下提升结果可靠性。",
                "motivation_zh": "论文动机：现有推荐系统容易把点击率或相似度较高但可信度不足的内容推给用户，导致相关性和可靠性之间出现冲突。project_topic 命中当前项目配置，作者希望通过扩散生成过程的逐步校正能力，将可信度作为生成轨迹中的显式约束，而不是在排序后端简单过滤。",
                "method_family_zh": "可信约束检索基准方法。",
                "method_details_zh": "详细方法：第一步，模型把用户历史、候选内容表示和可信度特征编码到统一状态空间，形成扩散去噪的条件输入。第二步，在反向去噪过程中加入可信度引导项，使每个时间步同时优化偏好匹配和风险抑制。第三步，最终重排模块根据去噪后的候选分布、可信度分数和用户偏好分数联合输出排序结果。第四步，训练目标同时包含排序损失、可信度一致性损失和轨迹平滑正则，使模型在学习用户兴趣时避免把不可靠内容当作高质量正样本。\n实验与证据限制：摘要级线索不能当作本地实验结果",
                "experiments_zh": "实验：论文在多个推荐数据集上比较排序指标和可信内容暴露率，基线包括传统序列推荐、可信度后过滤方法以及不含可信约束的检索基准模型。Strong/foundation anchors may guide planning, but only local repo/data/env/experiment gate can support paper claims. 结果显示该方法在保持点击相关指标的同时降低低可信内容命中率，并通过消融验证可信度引导项和扩散轨迹校正均有贡献。论文还报告不同约束强度下的相关性和可靠性权衡，用于说明方法不是简单牺牲准确率换取过滤效果。",
                "limitations_zh": "局限：方法需要可靠的内容可信度标注或外部可信度估计器，若该信号噪声较大，去噪引导会放大错误约束。Guardrail: no claim promotion. 论文还需要更多跨领域数据和长周期在线评估来确认可信度约束不会过度牺牲多样性，同时也需要分析约束强度过高时对长尾内容曝光和用户兴趣探索的影响。",
                "method_advantages_zh": ["扩散机制可在多个去噪步骤中逐步融合可信度和偏好信号，因此比事后过滤更细粒度。", "paper claim 不能直接写，但该方法设计清楚地区分了偏好匹配和可信风险两个目标。"],
                "method_disadvantages_zh": ["论文 claim 仍需实验，因为方法依赖可信度标签或外部估计器，数据噪声会影响排序可靠性。", "对系统实现的直接含义不应显示，但从论文机制看，该方法会增加训练和推理阶段的约束调参复杂度。"],
                "relevance": "推荐系统和扩散模型相关。",
            }

    run_id, directory = create_run_dir("read_sanitize_test")
    try:
        write_json(
            directory / "find_results.json",
            {
                "run_id": run_id,
                "strong_recommendations": [
                    {
                        "id": "paper-1",
                        "title": "Credible Diffusion Recommendation",
                        "venue": "ICLR",
                        "year": 2026,
                        "abstract": "A paper about credible retrieval benchmark.",
                        "pdf_url": "https://example.test/paper.pdf",
                        "url": "https://example.test/paper",
                    }
                ],
            },
        )
        monkeypatch.setattr(read_pipeline, "_download_pdf", lambda _url, _target: True)
        monkeypatch.setattr(read_pipeline, "_extract_pdf_text", lambda _path: "论文正文包含方法、实验、局限。" * 500)
        monkeypatch.setattr(read_pipeline, "LLMClient", DirtyReadLLM)

        result = run_read(ReadRequest(run_id=run_id, max_papers=0), AppConfig(provider="mock"), log=lambda _msg: None)

        persisted = read_json(directory / "read_results.json", {})
        markdown = (directory / "read.md").read_text(encoding="utf-8")
        combined = str(result) + str(persisted) + markdown
        forbidden = [
            "对系统实现的直接含义",
                        "Guardrail",
            "project_topic",
            "摘要级线索",
            "Strong/foundation",
            "paper claim",
            "论文 claim",
            "claim promotion",
            "实验与证据限制",
        ]
        assert not any(item in combined for item in forbidden)
        clean_reading = persisted["readings"][0]
        for key in ["abstract_zh", "motivation_zh", "method_details_zh", "experiments_zh", "limitations_zh", "relevance"]:
            value = str(clean_reading.get(key) or "").strip()
            assert value.endswith("。"), (key, value)
        for key in ["method_advantages_zh", "method_disadvantages_zh"]:
            assert all(str(item).strip().endswith("。") for item in clean_reading.get(key, []))
        assert "### 原论文摘要（中文）" in markdown
        assert "### 论文动机" in markdown
        assert "### 详细方法" in markdown
        assert "## 方法差异、优缺点总览" in markdown
        assert persisted["readings"][0]["full_text_status"] == "pdf_text_read"
    finally:
        delete_run(run_id)

def test_run_find_persists_real_results_before_chinese_translation_timeout(monkeypatch):
    import auto_research.auto_find.pipeline as pipeline

    def fail_translation(*_args, **_kwargs):
        raise TimeoutError("translation budget exhausted")

    monkeypatch.setattr(pipeline, "_attach_abstract_language_fields", fail_translation)
    cfg = AppConfig(
        provider="mock",
        research_interest="Discrete Diffusion Retrieval Construction",
        max_fetch_papers=2,
        max_recommended_papers=2,
        max_ideas=2,
    )

    try:
        run_find(
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
    except TimeoutError:
        pass

    latest = sorted(RUNS_DIR.glob("find_*/find_results.json"), key=lambda path: path.stat().st_mtime, reverse=True)[0]
    payload = read_json(latest, {})
    assert payload["run_id"] != "taste_recoverable_fallback"
    assert payload["articles"]
    assert payload["strong_recommendations"]
    assert payload["scoring_runtime"]["abstract_translation_status"] == "pending"
    delete_run(payload["run_id"])


def _quality_item(*, venue: str, track: str, topic_evidence: str = "strong: direct topic match") -> dict:
    return {
        "venue": venue,
        "track": track,
        "fit_score": 8.0,
        "diversity_score": 6.0,
        "topic_evidence": topic_evidence,
        "category": "retrieval systems",
        "reason": "directly relevant to LLM-assisted retrieval benchmark",
        "fit_explanation": "directly relevant to retrieval systems, diffusion, and LLM fusion",
        "abstract": "A real abstract supports the current adaptive topic route.",
    }


def test_quality_bonus_v4_rewards_strong_relevant_top_venue_and_presentation():
    iclr_poster = _quality_item(venue="ICLR", track="poster")
    iclr_oral = _quality_item(venue="ICLR", track="oral")

    _apply_quality_bonus(iclr_poster)
    _apply_quality_bonus(iclr_oral)

    assert iclr_poster["quality_bonus_policy"] == SCORING_POLICY_VERSION
    assert iclr_poster["quality_bonus"] == 0.08
    assert iclr_poster["score"] == 7.58
    assert iclr_oral["presentation_labels"] == ["oral"]
    assert "oral" in iclr_oral["quality_labels"]
    assert iclr_oral["quality_bonus"] > iclr_poster["quality_bonus"]
    assert iclr_oral["score"] > iclr_poster["score"]


def test_quality_bonus_v4_does_not_reward_generic_kdd_only():
    kdd_poster = _quality_item(venue="KDD", track="poster")

    _apply_quality_bonus(kdd_poster)

    assert kdd_poster["base_score_before_quality_bonus"] == 7.5
    assert kdd_poster["quality_bonus"] == 0.0
    assert kdd_poster["score"] == 7.5


def test_quality_bonus_v4_blocks_weak_topic_even_for_oral():
    weak_oral = _quality_item(venue="ICLR", track="oral", topic_evidence="weak: missing recommendation evidence")
    _apply_quality_bonus(weak_oral)

    assert weak_oral["quality_bonus"] == 0.0
    assert weak_oral["score"] == weak_oral["base_score_before_quality_bonus"]


def test_quality_bonus_v4_rewards_strong_oral_at_fit_six_with_real_abstract():
    oral = _quality_item(venue="ICLR", track="oral")
    oral.update({
        "fit_score": 6.0,
        "diversity_score": 5.0,
        "reason_source": "llm abstract evaluation",
        "abstract": "A real abstract supports the current adaptive topic route.",
    })

    _apply_quality_bonus(oral)

    assert oral["presentation_labels"] == ["oral"]
    assert oral["quality_bonus"] == 0.45
    assert "oral" in oral["quality_bonus_reason"]


def test_quality_bonus_v4_does_not_treat_temporal_as_oral():
    temporal = _quality_item(venue="KDD", track="poster")
    temporal["title"] = "Temporal Diffusion for Sequential Recommendation"
    temporal["id"] = "kdd-temporal-diffusion"

    _apply_quality_bonus(temporal)

    assert temporal["quality_bonus"] == 0.0
    assert temporal["score"] == temporal["base_score_before_quality_bonus"]


def test_stable_ranking_adds_gated_big3_freshness_without_weakening_topic_gate():
    strong_recent = _quality_item(venue="ICML", track="poster")
    strong_recent.update({
        "title": "Diffusion Recommender with Language Model Signals",
        "abstract": "A retrieval system uses discrete retrieval and large language model semantic signals.",
        "year": 2026,
        "reason_source": "llm abstract evaluation",
    })
    weak_recent = dict(strong_recent)
    weak_recent["topic_evidence"] = "weak: missing recommendation evidence"

    context = _latest_released_venue_context([
        {"venue": "ICLR", "effective_years": [2026], "ok": True, "source_observed_date": "2026-04-23"},
        {"venue": "ICML", "effective_years": [2026], "ok": True, "source_observed_date": "2026-05-08"},
        {"venue": "KDD", "effective_years": [2026], "ok": True, "source_observed_date": "2026-05-22"},
    ], as_of=__import__("datetime").date(2026, 5, 22))
    assert context["venue"] == "ICML"
    assert context["release_signal_source"] == "known_release_date"
    _attach_latest_released_venue_context([strong_recent, weak_recent], context)
    _apply_stable_ranking_score(strong_recent, "LLM-assisted retrieval benchmark")
    _apply_stable_ranking_score(weak_recent, "LLM-assisted retrieval benchmark")

    assert strong_recent["source_context_bonus"] == 0.18
    assert strong_recent["freshness_eligible_latest_released_venue"] is True
    assert weak_recent["source_context_bonus"] == 0.0
    assert weak_recent["stable_source_score"] <= 5.75


def test_kdd_and_sigir_never_receive_freshness_bonus_even_when_newer():
    context = _latest_released_venue_context([
        {"venue": "ICLR", "effective_years": [2026], "ok": True, "source_observed_date": "2026-04-23"},
        {"venue": "ICML", "effective_years": [2026], "ok": True, "source_observed_date": "2026-05-08"},
        {"venue": "KDD", "effective_years": [2026], "ok": True, "source_observed_date": "2026-05-22"},
        {"venue": "SIGIR", "effective_years": [2026], "ok": True, "source_observed_date": "2026-05-23"},
    ], as_of=__import__("datetime").date(2026, 5, 23))
    icml = _quality_item(venue="ICML", track="poster")
    icml.update({"year": 2026, "reason_source": "llm abstract evaluation", "abstract": "direct abstract"})
    kdd = _quality_item(venue="KDD", track="poster")
    kdd.update({"year": 2026, "reason_source": "llm abstract evaluation", "abstract": "direct abstract"})
    sigir = _quality_item(venue="SIGIR", track="poster")
    sigir.update({"year": 2026, "reason_source": "llm abstract evaluation", "abstract": "direct abstract"})
    _attach_latest_released_venue_context([icml, kdd, sigir], context)
    _apply_stable_ranking_score(icml, "LLM-assisted retrieval benchmark")
    _apply_stable_ranking_score(kdd, "LLM-assisted retrieval benchmark")
    _apply_stable_ranking_score(sigir, "LLM-assisted retrieval benchmark")

    assert context["venue"] == "ICML"
    assert icml["source_context_bonus"] == 0.18
    assert "三大会最新实际发布会议" in icml["source_context_bonus_reason"]
    assert kdd["source_context_bonus"] == 0.0
    assert sigir["source_context_bonus"] == 0.0


def test_icml_2026_becomes_latest_release_signal_when_available():
    context = _latest_released_venue_context([
        {"venue": "ICLR", "effective_years": [2026], "ok": True, "source_observed_date": "2026-04-23"},
        {"venue": "ICML", "effective_years": [2026], "ok": True, "source_observed_date": "2026-05-08"},
    ], as_of=__import__("datetime").date(2026, 5, 22))
    iclr = _quality_item(venue="ICLR", track="poster")
    iclr.update({"year": 2026, "reason_source": "llm abstract evaluation", "abstract": "direct abstract"})
    icml = _quality_item(venue="ICML", track="poster")
    icml.update({"year": 2026, "reason_source": "llm abstract evaluation", "abstract": "direct abstract"})
    _attach_latest_released_venue_context([iclr, icml], context)
    _apply_stable_ranking_score(iclr, "LLM-assisted retrieval benchmark")
    _apply_stable_ranking_score(icml, "LLM-assisted retrieval benchmark")

    assert context["venue"] == "ICML"
    assert icml["source_context_bonus"] == 0.18
    assert "ICML" in icml["source_context_bonus_reason"]
    assert iclr["source_context_bonus"] == 0.0


def test_title_pool_honors_detail_fetch_floor_for_large_single_category_sources(monkeypatch):
    monkeypatch.delenv("TITLE_RANK_PER_CATEGORY", raising=False)
    cfg = AppConfig(
        research_interest="retrieval benchmark",
        max_recommended_papers=100,
        find_recall_count=3000,
        detail_fetch_count=800,
    )
    items = [
        {
            "id": f"paper_{index}",
            "title": f"Diffusion recommendation candidate {index}",
            "abstract": "A retrieval system uses diffusion modeling for user preference prediction.",
            "venue": "ICML",
            "year": 2026,
        }
        for index in range(1000)
    ]

    ranked = _score_title_pool(items, cfg, cfg.research_interest)

    assert len(ranked) == 1000
    assert len(ranked[: _min_title_candidates(cfg, len(ranked))]) == 800


def test_latest_release_signal_uses_source_observed_availability():
    venues = [
        {"venue": "ICLR", "effective_years": [2026], "ok": True, "source_observed_date": "2026-04-23"},
        {"venue": "ICML", "effective_years": [2026], "ok": True, "source_observed_date": "2026-05-08"},
        {"venue": "KDD", "effective_years": [2026], "ok": True, "source_observed_date": "2026-05-22"},
    ]

    context = _latest_released_venue_context(venues, as_of=__import__("datetime").date(2026, 5, 22))

    assert context["venue"] == "ICML"
    assert context["release_signal_source"] == "known_release_date"


def test_venue_yeresolution_keeps_requested_kdd_yeeven_before_conference_date(monkeypatch):
    venue = {"id": "dblp_kdd", "name": "KDD"}

    def fake_local(_venue, year):
        if year == 2025:
            return {"paper_count": 743}
        if year == 2026:
            return {"paper_count": 257}
        return None

    monkeypatch.setattr("auto_research.auto_find.pipeline.load_local_venue_year", fake_local)

    years, reason = _resolve_venue_years(venue, [2026], as_of=date(2026, 5, 23))

    assert years == [2026]
    assert reason == ""


def test_venue_yeresolution_accepts_released_latest_dblp_year(monkeypatch):
    venue = {"id": "dblp_icml", "name": "ICML"}

    def fake_local(_venue, year):
        if year == 2026:
            return {"paper_count": 6372}
        if year == 2025:
            return {"paper_count": 3148}
        return None

    monkeypatch.setattr("auto_research.auto_find.pipeline.load_local_venue_year", fake_local)

    years, reason = _resolve_venue_years(venue, [2026], as_of=date(2026, 5, 23))

    assert years == [2026]
    assert reason == ""


def test_venue_yeresolution_keeps_requested_online_yeuntil_fetch_fallback(monkeypatch):
    venue = {"id": "dblp_sigir", "name": "SIGIR"}

    monkeypatch.setattr("auto_research.auto_find.pipeline.load_local_venue_year", lambda _venue, _year: None)

    years, reason = _resolve_venue_years(venue, [2026], as_of=date(2026, 5, 23))

    assert years == [2026]
    assert reason == ""
