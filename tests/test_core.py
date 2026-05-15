from auto_research.auto_find.catalog import load_catalog
from auto_research.auto_find.sources import _dblp_page_url, _openreview_venue_ids, _parse_neurips_detail, _parse_neurips_list, fetch_arxiv, fetch_dblp_stream_api, fetch_openreview_venue, fetch_venue_sample, fetch_venue_title_index, normalize_date
from auto_research.llm import LLMClient, clamp_workers, extract_json, fallback_score, keyword_category
from auto_research.markdown import paper_markdown
from auto_research.models import AppConfig, LLMRoleConfig
from auto_research.storage import redacted_config


def test_extract_json_from_fenced_response():
    assert extract_json("```json\n{\"a\": 1}\n```") == {"a": 1}


def test_keyword_category_detects_llm():
    assert "LLM" in keyword_category("A Large Language Model Method", "")


def test_fallback_score_increases_with_interest_match():
    low = fallback_score("graph retrieval", "Unrelated title", "")
    high = fallback_score("graph retrieval", "Graph retrieval for agents", "")
    assert high > low


def test_fallback_score_handles_chinese_research_profile_synonyms():
    low = fallback_score("生成式AI 科学发现 材料物理", "More effort is needed to protect pedestrian privacy", "")
    high = fallback_score("生成式AI 科学发现 材料物理", "Generative diffusion models for materials discovery and physics simulation", "")
    assert high > low


def test_markdown_contains_classification_source():
    content = paper_markdown([
        {
            "id": "p1",
            "title": "Paper",
            "source": "arxiv",
            "venue": "arXiv",
            "year": 2026,
            "category": "LLM",
            "classification_source": "llm_inferred",
            "score": 8,
            "url": "https://example.com",
            "pdf_url": "",
            "abstract": "Abstract",
            "reason": "Reason",
        }
    ])
    assert "`llm_inferred`" in content


def test_catalog_has_reference_entries():
    catalog = load_catalog()
    assert isinstance(catalog, list)
    assert len(catalog) >= 600
    assert any(item["source"] == "ccf" for item in catalog)
    assert any(item["id"] == "openreview_iclr" for item in catalog)


def test_neurips_detail_parser_extracts_openreview_and_pdf():
    item = _parse_neurips_detail(
        """
        <html><body>
          <h1>Useful NeurIPS Paper</h1>
          <p>Alice Example · Bob Example</p>
          <h3>Abstract</h3>
          <p>This paper studies a useful method.</p>
          <a href="https://openreview.net/forum?id=abc123">OpenReview</a>
        </body></html>
        """,
        "https://neurips.cc/virtual/2025/poster/1",
        "Useful NeurIPS Paper",
        2025,
    )
    assert item["source"] == "neurips_virtual"
    assert item["title"] == "Useful NeurIPS Paper"
    assert item["url"] == "https://openreview.net/forum?id=abc123"
    assert item["pdf_url"] == "https://openreview.net/pdf?id=abc123"


def test_neurips_detail_parser_ignores_navigation_title():
    item = _parse_neurips_detail(
        """
        <html><body>
          <h1>Main Navigation</h1>
          <h2>NeurIPS 2025</h2>
          <h3>Abstract</h3>
          <p>Actual abstract text.</p>
        </body></html>
        """,
        "https://neurips.cc/virtual/2025/poster/1",
        "A Real Paper Title",
        2025,
    )
    assert item["title"] == "A Real Paper Title"


def test_neurips_list_parser_accepts_virtual_poster_links():
    candidates = _parse_neurips_list(
        '<a href="/virtual/2025/poster/121923">More effort is needed to protect pedestrian privacy in the era of AI</a>',
        "https://neurips.cc/virtual/2025/papers.html",
        3,
    )
    assert candidates == [
        (
            "https://neurips.cc/virtual/2025/poster/121923",
            "More effort is needed to protect pedestrian privacy in the era of AI",
        )
    ]


def test_redacted_config_hides_api_key():
    api_secret = "demo-" + "api-secret"
    mail_secret = "demo-" + "mail-secret"
    config = redacted_config({"api_key": api_secret, "model": "demo", "email": {"smtp_password": mail_secret}})
    assert config["api_key"] == "********"
    assert config["email"]["smtp_password"] == "********"
    assert api_secret not in str(config)
    assert mail_secret not in str(config)


def test_normalize_date_accepts_slash_and_dash_formats():
    assert normalize_date("2026/4/30") == "2026-04-30"
    assert normalize_date("2026-04-30") == "2026-04-30"
    assert normalize_date("") == ""


def test_openreview_dynamic_iclr_years(monkeypatch):
    captured = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"notes": []}

    def fake_get(_url, params=None, **_kwargs):
        captured.append(params["content.venueid"])
        return Response()

    monkeypatch.setattr("auto_research.auto_find.sources.requests.get", fake_get)
    fetch_openreview_venue({"name": "ICLR", "full_name": "International Conference on Learning Representations"}, [2023, 2024, 2025], 2)
    assert captured == ["ICLR.cc/2023/Conference", "ICLR.cc/2024/Conference", "ICLR.cc/2025/Conference"]


def test_openreview_dynamic_icml_years(monkeypatch):
    captured = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"notes": []}

    def fake_get(_url, params=None, **_kwargs):
        if params and "content.venueid" in params:
            captured.append(params["content.venueid"])
        return Response()

    monkeypatch.setattr("auto_research.auto_find.sources.requests.get", fake_get)
    fetch_openreview_venue({"name": "ICML", "full_name": "International Conference on Machine Learning"}, [2023, 2024, 2025], 2)
    assert captured == ["ICML.cc/2023/Conference", "ICML.cc/2024/Conference", "ICML.cc/2025/Conference"]


def test_openreview_dynamic_supported_venue_ids():
    cases = [
        ({"name": "AISTATS", "full_name": "International Conference on Artificial Intelligence and Statistics"}, "aistats.org/AISTATS/2025/Conference"),
        ({"name": "UAI", "full_name": "Conference on Uncertainty in Artificial Intelligence"}, "auai.org/UAI/2025/Conference"),
        ({"name": "COLT", "full_name": "Conference on Learning Theory"}, "learningtheory.org/COLT/2025/Conference"),
        ({"name": "CoRL", "full_name": "Conference on Robot Learning"}, "robot-learning.org/CoRL/2025/Conference"),
        ({"name": "COLM", "full_name": "Conference on Language Modeling"}, "colmweb.org/COLM/2025/Conference"),
        ({"name": "RLC", "full_name": "Reinforcement Learning Conference"}, "rl-conference.cc/RLC/2025/Conference"),
        ({"name": "LoG", "full_name": "Learning on Graphs Conference"}, "logconference.io/LOG/2025/Conference"),
        ({"name": "MIDL", "full_name": "Medical Imaging with Deep Learning"}, "MIDL.io/2025/Conference"),
        ({"name": "TMLR", "full_name": "Transactions on Machine Learning Research"}, "TMLR"),
    ]

    for venue, expected_id in cases:
        assert _openreview_venue_ids(venue, 2025) == [expected_id]


def test_openreview_keyword_matching_avoids_substring_false_positives():
    assert _openreview_venue_ids({"name": "SIGLOG", "full_name": "ACM SIGLOG"}, 2025) == []
    assert _openreview_venue_ids({"name": "EvaluationConf", "full_name": "Evaluation Methods"}, 2025) == []


def test_catalog_dynamic_iclr_years():
    catalog = load_catalog()
    iclr = next(item for item in catalog if item["id"] == "openreview_iclr")
    assert {2023, 2024, 2025, 2026}.issubset(set(iclr["years"]))


def test_packaged_ccf_catalog_has_sigkdd_dblp_address():
    catalog = load_catalog()
    kdd = next(item for item in catalog if item["name"] == "SIGKDD")
    assert kdd["address"].endswith("/db/conf/kdd/")


def test_dblp_page_url_uses_stable_uni_trier_host():
    assert _dblp_page_url("https://dblp.org/db/conf/iccad/") == "https://dblp.uni-trier.de/db/conf/iccad/"
    assert _dblp_page_url("http://dblp.uni-trier.de/db/conf/iccad/") == "https://dblp.uni-trier.de/db/conf/iccad/"


def test_dblp_stream_api_parses_json_hits(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "result": {
                    "hits": {
                        "hit": [
                            {
                                "info": {
                                    "title": "Generative Systems Paper.",
                                    "year": "2025",
                                    "authors": {"author": [{"text": "Alice"}, {"text": "Bob"}]},
                                    "ee": "https://example.com/paper",
                                }
                            }
                        ]
                    }
                }
            }

    monkeypatch.setattr("auto_research.auto_find.sources.requests.get", lambda *_args, **_kwargs: Response())
    papers = fetch_dblp_stream_api({"id": "v1", "name": "Demo", "address": "https://dblp.org/db/conf/demo/"}, [2025], 1)
    assert papers[0]["title"] == "Generative Systems Paper"
    assert papers[0]["authors"] == "Alice, Bob"
    assert papers[0]["year"] == 2025


def test_openreview_known_venues_are_checked_before_dblp(monkeypatch):
    calls = []
    paper = {"id": "p1", "title": "OpenReview paper", "url": "https://openreview.net/forum?id=x"}

    def fake_openreview(_venue, _years, _max_items):
        calls.append("openreview")
        return [paper]

    def fail_dblp(_venue, _years, _max_items):
        calls.append("dblp")
        raise AssertionError("DBLP should not run before successful OpenReview for known OpenReview venues")

    monkeypatch.setattr("auto_research.auto_find.sources.fetch_openreview_venue", fake_openreview)
    monkeypatch.setattr("auto_research.auto_find.sources.fetch_dblp_venue", fail_dblp)

    venue = {
        "id": "ccf_ai_conference_a_neurips_conference_on_neural_information_processing_systems",
        "name": "NeurIPS",
        "full_name": "Conference on Neural Information Processing Systems",
        "address": "https://dblp.org/db/conf/nips/",
    }
    papers, adapter = fetch_venue_title_index(venue, [2025], 1)

    assert adapter == "openreview"
    assert papers == [paper]
    assert calls == ["openreview"]


def test_openreview_sample_falls_back_to_dblp_when_empty(monkeypatch):
    calls = []

    monkeypatch.setattr("auto_research.auto_find.sources.fetch_openreview_venue", lambda *_args: (calls.append("openreview") or []))
    monkeypatch.setattr("auto_research.auto_find.sources.fetch_neurips_virtual", lambda *_args: (calls.append("neurips_virtual") or []))
    monkeypatch.setattr(
        "auto_research.auto_find.sources.fetch_dblp_venue",
        lambda *_args: (calls.append("dblp") or [{"title": "DBLP fallback paper", "url": "https://example.com", "abstract": ""}]),
    )
    venue = {
        "id": "ccf_ai_conference_a_neurips_conference_on_neural_information_processing_systems",
        "name": "NeurIPS",
        "full_name": "Conference on Neural Information Processing Systems",
        "address": "https://dblp.org/db/conf/nips/",
    }

    result = fetch_venue_sample(venue, 2024, 1)

    assert result["ok"] is True
    assert result["source_adapter"] == "dblp"
    assert calls == ["openreview", "neurips_virtual", "dblp"]


def test_icml_title_index_uses_openreview_before_databases(monkeypatch):
    calls = []
    paper = {"id": "p1", "title": "ICML OpenReview paper", "url": "https://openreview.net/forum?id=x"}

    monkeypatch.setattr("auto_research.auto_find.sources.fetch_dblp_venue", lambda *_args: (calls.append("dblp") or []))
    monkeypatch.setattr("auto_research.auto_find.sources.fetch_pmlr_index", lambda *_args: (calls.append("pmlr") or []))
    monkeypatch.setattr("auto_research.auto_find.sources.fetch_openreview_venue", lambda *_args: (calls.append("openreview") or [paper]))

    papers, adapter = fetch_venue_title_index(
        {
            "id": "ccf_ai_conference_a_icml_international_conference_on_machine_learning",
            "name": "ICML",
            "full_name": "International Conference on Machine Learning",
            "address": "https://dblp.org/db/conf/icml/",
        },
        [2025],
        1,
    )

    assert adapter == "openreview"
    assert papers == [paper]
    assert calls == ["openreview"]


def test_new_openreview_supported_title_index_uses_openreview_before_databases(monkeypatch):
    calls = []
    paper = {"id": "p1", "title": "AISTATS OpenReview paper", "url": "https://openreview.net/forum?id=x"}

    monkeypatch.setattr("auto_research.auto_find.sources.fetch_dblp_venue", lambda *_args: (calls.append("dblp") or []))
    monkeypatch.setattr("auto_research.auto_find.sources.fetch_pmlr_index", lambda *_args: (calls.append("pmlr") or []))
    monkeypatch.setattr("auto_research.auto_find.sources.fetch_openreview_venue", lambda *_args: (calls.append("openreview") or [paper]))

    papers, adapter = fetch_venue_title_index(
        {
            "id": "ccf_ai_conference_a_aistats_artificial_intelligence_and_statistics",
            "name": "AISTATS",
            "full_name": "International Conference on Artificial Intelligence and Statistics",
            "address": "https://dblp.org/db/conf/aistats/",
        },
        [2025],
        1,
    )

    assert adapter == "openreview"
    assert papers == [paper]
    assert calls == ["openreview"]


def test_openreview_supported_title_index_falls_back_to_existing_sources_when_empty(monkeypatch):
    calls = []
    paper = {"id": "p1", "title": "AISTATS PMLR paper", "url": "https://proceedings.mlr.press/example"}

    monkeypatch.setattr("auto_research.auto_find.sources.fetch_openreview_venue", lambda *_args: (calls.append("openreview") or []))
    monkeypatch.setattr("auto_research.auto_find.sources.fetch_dblp_venue", lambda *_args: (calls.append("dblp") or []))
    monkeypatch.setattr("auto_research.auto_find.sources.fetch_pmlr_index", lambda *_args: (calls.append("pmlr") or [paper]))

    papers, adapter = fetch_venue_title_index(
        {
            "id": "ccf_ai_conference_a_aistats_artificial_intelligence_and_statistics",
            "name": "AISTATS",
            "full_name": "International Conference on Artificial Intelligence and Statistics",
            "address": "https://dblp.org/db/conf/aistats/",
        },
        [2025],
        1,
    )

    assert adapter == "pmlr"
    assert papers == [paper]
    assert calls == ["openreview", "dblp", "pmlr"]


def test_icml_sample_uses_openreview_before_databases(monkeypatch):
    calls = []
    paper = {"id": "p1", "title": "ICML OpenReview paper", "url": "https://openreview.net/forum?id=x", "abstract": "Abstract."}

    monkeypatch.setattr("auto_research.auto_find.sources.fetch_dblp_venue", lambda *_args: (calls.append("dblp") or []))
    monkeypatch.setattr("auto_research.auto_find.sources.fetch_pmlr_index", lambda *_args: (calls.append("pmlr") or []))
    monkeypatch.setattr("auto_research.auto_find.sources.fetch_openreview_venue", lambda *_args: (calls.append("openreview") or [paper]))

    result = fetch_venue_sample(
        {
            "id": "ccf_ai_conference_a_icml_international_conference_on_machine_learning",
            "name": "ICML",
            "full_name": "International Conference on Machine Learning",
            "address": "https://dblp.org/db/conf/icml/",
        },
        2025,
        1,
    )

    assert result["ok"] is True
    assert result["source_adapter"] == "openreview"
    assert result["sample_count"] == 1
    assert calls == ["openreview"]


def test_arxiv_returns_status_for_success(monkeypatch):
    class Response:
        text = """<?xml version='1.0' encoding='UTF-8'?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>http://arxiv.org/abs/2604.00001v1</id>
            <title>Generative models for materials discovery</title>
            <summary>Abstract text.</summary>
            <published>2026-04-30T00:00:00Z</published>
            <author><name>Alice</name></author>
          </entry>
        </feed>"""

    monkeypatch.setattr("auto_research.auto_find.sources._request", lambda _url: Response())
    items, status = fetch_arxiv(["cs.AI"], 3, "", "")
    assert len(items) == 1
    assert status["ok"] is True
    assert status["count"] == 1
    assert status["queries"] == ["cat:cs.AI"]


def test_arxiv_returns_status_for_failure(monkeypatch):
    def fail(_url):
        raise RuntimeError("network down")

    monkeypatch.setattr("auto_research.auto_find.sources._request", fail)
    items, status = fetch_arxiv(["cs.AI"], 3, "", "")
    assert items == []
    assert status["ok"] is False
    assert "network down" in status["message"]


def test_role_llm_config_inherits_and_overrides_global():
    cfg = AppConfig(
        provider="mock",
        base_url="https://global.example/v1",
        api_key="global-key",
        model="global-model",
        temperature=0.4,
        llm_roles={"idea_judge": LLMRoleConfig(model="judge-model", temperature=0.1)},
    )
    client = LLMClient(cfg, "idea_judge")
    assert client.provider == "mock"
    assert client.base_url == "https://global.example/v1"
    assert client.model == "judge-model"
    assert client.temperature == 0.1


def test_clamp_workers_bounds_values():
    assert clamp_workers(0, default=16, maximum=32) == 1
    assert clamp_workers(16, default=16, maximum=32) == 16
    assert clamp_workers(32, default=16, maximum=32) == 32
    assert clamp_workers(100, default=16, maximum=32) == 32
