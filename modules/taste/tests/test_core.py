import json

from auto_research.auto_find.catalog import catalog_by_id, load_catalog
from auto_research.auto_find.sources import _acm_metadata_from_doi, _dblp_page_url, _parse_neurips_detail, _parse_neurips_list, fetch_arxiv, fetch_dblp_stream_api, fetch_openreview_venue, fetch_venue_sample, fetch_venue_title_index, normalize_date
from auto_research.llm import LLMClient, clamp_workers, extract_json, fallback_score, keyword_category
from auto_research.markdown import paper_markdown
from auto_research.models import AppConfig, LLMRoleConfig
from auto_research.storage import redacted_config


def test_sync_latest_does_not_overwrite_project_when_run_id_mismatches(monkeypatch, tmp_path):
    from auto_research import storage

    runtime_latest = tmp_path / "runtime_latest"
    root = tmp_path / "root"
    project_dir = root / "projects" / "demo_project"
    project_taste = project_dir / "planning" / "finding"
    state_dir = project_dir / "state"
    project_taste.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    storage.write_json(state_dir / "current_find_research_plan.json", {"run_id": "find_current"})
    storage.write_json(project_taste / "read_results.json", {"run_id": "find_current", "marker": "keep"})
    stale_source = tmp_path / "stale" / "read_results.json"
    current_source = tmp_path / "current" / "read_results.json"
    storage.write_json(stale_source, {"run_id": "find_stale", "marker": "stale"})
    storage.write_json(current_source, {"run_id": "find_current", "marker": "current"})

    monkeypatch.setenv("WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("PROJECT_ID", "demo_project")
    monkeypatch.setattr(storage, "stage_latest_path", lambda stage, filename: runtime_latest / stage / filename)

    storage.sync_latest("auto_read", "read_results.json", stale_source)
    assert storage.read_json(project_taste / "read_results.json", {})["marker"] == "keep"
    assert storage.read_json(runtime_latest / "auto_read" / "read_results.json", {})["marker"] == "stale"

    storage.sync_latest("auto_read", "read_results.json", current_source)
    assert storage.read_json(project_taste / "read_results.json", {})["marker"] == "current"


def test_sync_latest_copies_find_markdown_derivatives_for_current_run(monkeypatch, tmp_path):
    from auto_research import storage

    runtime_latest = tmp_path / "runtime_latest"
    root = tmp_path / "root"
    project_dir = root / "projects" / "demo_project"
    project_taste = project_dir / "planning" / "finding"
    state_dir = project_dir / "state"
    project_taste.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    storage.write_json(state_dir / "current_find_research_plan.json", {"run_id": "find_current"})

    stale_dir = tmp_path / "stale"
    current_dir = tmp_path / "current"
    stale_dir.mkdir()
    current_dir.mkdir()
    storage.write_json(stale_dir / "find_results.json", {"run_id": "find_stale"})
    storage.write_json(current_dir / "find_results.json", {"run_id": "find_current"})
    (stale_dir / "read_candidates.md").write_text("stale", encoding="utf-8")
    (current_dir / "read_candidates.md").write_text("current", encoding="utf-8")

    monkeypatch.setenv("WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("PROJECT_ID", "demo_project")
    monkeypatch.setattr(storage, "stage_latest_path", lambda stage, filename: runtime_latest / stage / filename)

    storage.sync_latest("auto_find", "read_candidates.md", stale_dir / "read_candidates.md")
    assert not (project_taste / "read_candidates.md").exists()
    assert (runtime_latest / "auto_find" / "read_candidates.md").read_text(encoding="utf-8") == "stale"

    storage.sync_latest("auto_find", "read_candidates.md", current_dir / "read_candidates.md")
    assert (project_taste / "read_candidates.md").read_text(encoding="utf-8") == "current"

def test_extract_json_from_fenced_response():
    assert extract_json("```json\n{\"a\": 1}\n```") == {"a": 1}



def test_source_selection_expands_multiple_years_into_venue_year_pairs():
    from auto_research.source_selection import normalize_source_selection

    selection = normalize_source_selection({
        "venue_ids": ["openreview_iclr_2026", "openreview_iclr_2026"],
        "years": [2026, 2025, 2026],
    })

    assert selection["venue_ids"] == ["openreview_iclr_2026"]
    assert selection["years"] == [2026, 2025]
    assert selection["venue_years"] == [
        {"venue_id": "openreview_iclr_2026", "year": 2026},
        {"venue_id": "openreview_iclr_2026", "year": 2025},
    ]

def test_keyword_category_detects_llm():
    assert keyword_category("A Large Language Model Method", "").startswith("Local topic")


def test_fallback_score_increases_with_interest_match():
    low = fallback_score("graph retrieval", "Unrelated title", "")
    high = fallback_score("graph retrieval", "Graph retrieval for agents", "")
    assert high > low


def test_fallback_score_uses_dynamic_terms_from_research_profile():
    low = fallback_score("generative diffusion materials physics", "More effort is needed to protect pedestrian privacy", "")
    high = fallback_score("generative diffusion materials physics", "Generative diffusion models for materials discovery and physics simulation", "")
    assert high > low


def test_markdown_hides_internal_recommendation_debug_fields():
    content = paper_markdown([
        {
            "id": "p1",
            "title": "Paper",
            "source": "arxiv",
            "venue": "arXiv",
            "year": 2026,
            "category": "LLM",
            "classification_source": "llm_inferred",
            "fit_score": 8,
            "score": 8,
            "url": "https://example.com",
            "pdf_url": "",
            "abstract": "Abstract",
            "reason": "Reason",
        }
    ])
    assert "- **方法/主题类别**: LLM" in content
    assert "论文页面" not in content
    assert "https://example.com" not in content
    assert "llm_inferred" not in content
    assert "- **ID**" not in content
    assert "Fit 分数" not in content
    assert "最终分数" not in content
    assert "- **URL**" not in content
    assert "- **PDF**:" not in content

def test_markdown_contains_quality_labels_and_score_bonus_details():
    content = paper_markdown([
        {
            "id": "p1",
            "title": "Paper",
            "source": "openreview",
            "venue": "NeurIPS",
            "year": 2026,
            "track": "NeurIPS 2026 oral",
            "quality_labels": ["oral"],
            "category": "Recommendation",
            "classification_source": "llm_inferred",
            "fit_score": 8,
            "diversity_score": 7,
            "score": 8.5,
            "stable_source_score": 8.5,
            "quality_bonus": 0.45,
            "quality_bonus_reason": "发表类型: oral +0.45",
            "source_context_bonus": 0.18,
            "source_context_bonus_reason": "新近会议论文 2026 +0.18",
            "abstract": "Abstract",
            "reason": "Reason",
        }
    ])
    assert "oral" in content
    assert "Abstract" in content
    assert "Reason" in content
    assert "Freshness/Citation Bonus" not in content


def test_markdown_missing_abstract_uses_actionable_note():
    content = paper_markdown([
        {
            "id": "p1",
            "title": "Paper",
            "source": "dblp",
            "venue": "SIGIR",
            "year": 2025,
            "category": "Recommendation",
            "classification_source": "official",
            "score": 8,
            "url": "https://example.com",
            "abstract": "",
        }
    ])
    assert "No abstract available." not in content
    assert "Abstract not available in the indexed venue metadata" in content


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


def test_openreview_fetch_caps_large_page_size(monkeypatch):
    calls = []

    class Response:
        def __init__(self, notes):
            self._notes = notes

        def raise_for_status(self):
            return None

        def json(self):
            return {"notes": self._notes}

    def fake_get(_url, params=None, headers=None, timeout=None):
        params = params or {}
        calls.append(dict(params))
        assert int(params.get("limit") or 0) <= 1000
        offset = int(params.get("offset") or 0)
        if offset > 0:
            return Response([])
        notes = [
            {
                "id": "note1",
                "forum": "forum1",
                "content": {
                    "title": {"value": "A Strong OpenReview Paper"},
                    "abstract": {"value": "This paper has a reusable benchmark, datasets, baselines, metrics, ablations, and limitations."},
                    "authors": {"value": ["Alice"]},
                },
            }
        ]
        return Response(notes)

    monkeypatch.setattr("auto_research.auto_find.sources.requests.get", fake_get)

    papers = fetch_openreview_venue({"name": "ICLR", "full_name": "International Conference on Learning Representations"}, [2026], 100000)

    assert len(papers) == 1
    assert calls[0]["limit"] == 1000
    assert calls[0]["offset"] == 0


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


def test_catalog_dynamic_iclr_years():
    catalog = load_catalog()
    iclr = next(item for item in catalog if item["id"] == "openreview_iclr")
    assert {2023, 2024, 2025, 2026}.issubset(set(iclr["years"]))


def test_packaged_ccf_catalog_has_sigkdd_dblp_address():
    catalog = load_catalog()
    kdd = next(item for item in catalog if item["name"] == "SIGKDD")
    assert kdd["address"].endswith("/db/conf/kdd/")


def test_catalog_merges_sigkdd_kdd_aliases():
    full_name = "ACM SIGKDD Conference on Knowledge Discovery and Data Mining"
    catalog = load_catalog()
    matches = [item for item in catalog if item.get("full_name") == full_name]

    assert len(matches) == 1
    kdd = matches[0]
    assert kdd["name"] == "SIGKDD"
    assert kdd["rank"] == "A"
    assert {2026, 2025, 2024, 2023}.issubset(set(kdd["years"]))
    assert any(alias.get("id") == "dblp_kdd" for alias in kdd.get("aliases", []))

    by_id = catalog_by_id()
    assert by_id["dblp_kdd"]["canonical_id"] == kdd["id"]
    assert by_id["dblp_kdd"]["name"] == "SIGKDD"
    assert {2026, 2025, 2024, 2023}.issubset(set(by_id["dblp_kdd"]["years"]))


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
                                    "ee": "https://doi.org/10.1145/3770854.3785694",
                                    "url": "https://dblp.org/rec/conf/demo/AliceBob25",
                                    "key": "conf/demo/AliceBob25",
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
    assert papers[0]["doi"] == "10.1145/3770854.3785694"
    assert papers[0]["metadata"]["dblp_key"] == "conf/demo/AliceBob25"
    assert papers[0]["metadata"]["acm_article_id"] == "3785694"
    assert papers[0]["metadata"]["acm_pdf_url"] == "https://dl.acm.org/doi/pdf/10.1145/3770854.3785694"


def test_acm_metadata_from_doi_derives_official_acm_urls():
    metadata = _acm_metadata_from_doi("10.1145/3770854.3785694")
    assert metadata["acm_proceedings_id"] == "3770854"
    assert metadata["acm_article_id"] == "3785694"
    assert metadata["acm_full_html_url"].endswith("10.1145/3770854.3785694")


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
    assert status["date_window_source"] == "default_recent_180_days"
    assert status["default_recent_days"] == 180
    assert len(status["queries"]) == 1
    assert status["queries"][0].startswith("cat:cs.AI AND submittedDate:")
    assert status["start_date"].replace("-", "") in status["queries"][0]
    assert status["end_date"].replace("-", "") in status["queries"][0]


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


def test_llm_json_response_format_adds_user_prompt_json_hint(monkeypatch):
    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            payload = {"choices": [{"message": {"content": '{"ok":true}'}}]}
            return json.dumps(payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured.append(json.loads(request.data.decode("utf-8")))
        return Response()

    monkeypatch.setenv("LLM_RESPONSE_FORMAT", "json_object")
    monkeypatch.setattr("auto_research.llm.urllib.request.urlopen", fake_urlopen)
    cfg = AppConfig(provider="openai_compatible", base_url="https://llm.example/v1", api_key="key", model="model")

    text = LLMClient(cfg, "find").chat("Return an object with ok true")

    assert text == '{"ok":true}'
    payload = captured[0]
    assert payload["response_format"] == {"type": "json_object"}
    assert "json" in payload["messages"][-1]["content"].lower()


def test_clamp_workers_bounds_values():
    assert clamp_workers(0, default=16, maximum=32) == 1
    assert clamp_workers(16, default=16, maximum=32) == 16
    assert clamp_workers(32, default=16, maximum=32) == 32
    assert clamp_workers(100, default=16, maximum=32) == 32



def test_dblp_stream_api_paginates_yequery_and_filters_proceedings(monkeypatch):
    from auto_research.auto_find import sources

    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def hit(title, key):
        return {
            "info": {
                "title": title,
                "year": "2026",
                "authors": {"author": [{"text": "Alice"}]},
                "ee": "https://doi.org/10.1145/3770854.3785694",
                "url": f"https://dblp.org/rec/{key}",
                "key": key,
            }
        }

    pages = {
        0: [
            hit("Proceedings of the 32nd ACM SIGKDD Conference on Knowledge Discovery and Data Mining V.1, KDD 2026.", "conf/kdd/Proceedings26"),
            hit("LLM Grounded Recommendation with Diffusion Signals.", "conf/kdd/Alice26"),
        ],
        100: [hit("Causal Graph Recommendation with Semantic Feedback.", "conf/kdd/Bob26")],
    }

    def fake_get(_url, params=None, **_kwargs):
        params = params or {}
        calls.append(dict(params))
        offset = int(params.get("f") or 0)
        rows = pages.get(offset, [])
        return Response({
            "result": {
                "hits": {
                    "@total": "3",
                    "@sent": str(len(rows)),
                    "@first": str(offset),
                    "hit": rows,
                }
            }
        })

    monkeypatch.setattr(sources.requests, "get", fake_get)
    papers = sources.fetch_dblp_stream_api({"id": "dblp_kdd", "name": "KDD", "address": "https://dblp.org/db/conf/kdd/"}, [2026], None)

    assert [call["f"] for call in calls] == [0, 100]
    assert [paper["title"] for paper in papers] == [
        "LLM Grounded Recommendation with Diffusion Signals",
        "Causal Graph Recommendation with Semantic Feedback",
    ]
    audit = sources.venue_metadata_audit_from_papers(papers)
    assert audit["search_total_hits"] == 3
    assert audit["deduped_paper_count"] == 2
    assert audit["complete"] is True
    assert audit["category_status"] == "no_official_categories"


def test_icml_downloads_records_metadata_completeness_audit(monkeypatch):
    from auto_research.auto_find import sources

    class Response:
        text = """
        <html><body>
          <a href="/virtual/2026/poster/1">Diffusion Recommendation with Semantic Signals</a>
          <a href="/virtual/2026/poster/2">LLM Conditioned Sequential Recommendation</a>
          <a href="/virtual/2026/workshop/3">Accepted Workshops</a>
          <a href="/virtual/2026/poster/1">Diffusion Recommendation with Semantic Signals</a>
        </body></html>
        """

    monkeypatch.setattr(sources, "_request", lambda _url, timeout=30: Response())
    papers = sources.fetch_icml_downloads([2026], 100)

    assert [paper["title"] for paper in papers] == [
        "Diffusion Recommendation with Semantic Signals",
        "LLM Conditioned Sequential Recommendation",
    ]
    audit = sources.venue_metadata_audit_from_papers(papers)
    assert audit["status"] == "complete"
    assert audit["complete"] is True
    assert audit["source_verified"] is True
    assert audit["deduped_paper_count"] == 2
    assert audit["has_official_categories"] is False
    assert audit["category_status"] == "no_official_categories"
    assert audit["source_scope"] == "official_icml_downloads_title_index"
    assert audit["official_title_index_verified"] is True
    assert audit["official_accepted_list_verified"] is True
    assert audit["has_abstracts"] is False
