from auto_research.auto_find.catalog import load_catalog
from auto_research.auto_find.local_rank import rank_papers_tfidf
from auto_research.auto_find.sources import _dblp_page_url, _openreview_venue_ids, _parse_dblp_year_links, _parse_neurips_detail, _parse_neurips_list, enrich_science_details, fetch_arxiv, fetch_biorxiv, fetch_dblp_stream_api, fetch_dblp_venue, fetch_nature_portfolio, fetch_openreview_venue, fetch_science_family, fetch_venue_sample, fetch_venue_title_index, normalize_date
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


def test_openreview_fetch_pages_api2_large_requests(monkeypatch):
    captured = []

    class Response:
        def __init__(self, notes):
            self._notes = notes

        def raise_for_status(self):
            return None

        def json(self):
            return {"notes": self._notes}

    def note(index):
        return {
            "id": f"note_{index}",
            "forum": f"forum_{index}",
            "content": {
                "title": {"value": f"Paged OpenReview Paper {index}"},
                "authors": {"value": ["A. Author"]},
                "abstract": {"value": "A paged abstract."},
                "venueid": {"value": "ICLR.cc/2026/Conference"},
            },
        }

    def fake_get(url, params=None, **_kwargs):
        if url == "https://api2.openreview.net/notes":
            captured.append(params)
            offset = int(params.get("offset", 0))
            limit = int(params.get("limit", 0))
            assert limit <= 1000
            if offset == 0:
                return Response([note(i) for i in range(limit)])
            if offset == 1000:
                return Response([note(i) for i in range(offset, offset + 2)])
            return Response([])
        return Response([])

    monkeypatch.setattr("auto_research.auto_find.sources.requests.get", fake_get)
    papers = fetch_openreview_venue({"name": "ICLR", "full_name": "International Conference on Learning Representations"}, [2026], 1002)

    assert len(papers) == 1002
    assert [item["offset"] for item in captured] == [0, 1000]
    assert papers[-1]["title"] == "Paged OpenReview Paper 1001"


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


def test_dblp_year_links_accept_volume_suffix_in_href(monkeypatch):
    class Response:
        text = """
        <html><body>
          <a href="kdd2026-1.html">table of contents in dblp</a>
          <a href="kdd2025-1.html">table of contents in dblp</a>
        </body></html>
        """

    monkeypatch.setattr("auto_research.auto_find.sources._request", lambda *_args, **_kwargs: Response())

    links = _parse_dblp_year_links("https://dblp.uni-trier.de/db/conf/kdd/", [2026])

    assert links == [(2026, "https://dblp.uni-trier.de/db/conf/kdd/kdd2026-1.html")]


def test_dblp_full_fetch_uses_stream_api(monkeypatch):
    captured = []

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
                                    "title": "KDD Full Fetch Paper.",
                                    "year": "2026",
                                    "authors": {"author": [{"text": "Alice"}]},
                                    "ee": "https://doi.org/10.1145/example",
                                }
                            }
                        ]
                    }
                }
            }

    def fake_get(_url, params=None, **_kwargs):
        captured.append(params)
        return Response()

    monkeypatch.setattr("auto_research.auto_find.sources.requests.get", fake_get)

    papers = fetch_dblp_venue(
        {"id": "kdd", "name": "SIGKDD", "address": "https://dblp.uni-trier.de/db/conf/kdd/"},
        [2026],
        None,
    )

    assert papers[0]["title"] == "KDD Full Fetch Paper"
    assert papers[0]["year"] == 2026
    assert captured[0]["q"] == "stream:streams/conf/kdd:"
    assert captured[0]["h"] == 1000


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


def test_icml_title_index_falls_back_to_openreview_when_databases_empty(monkeypatch):
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
    assert calls == ["dblp", "pmlr", "openreview"]


def test_new_openreview_supported_title_index_falls_back_when_databases_empty(monkeypatch):
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
    assert calls == ["dblp", "pmlr", "openreview"]


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
    assert calls == ["dblp", "pmlr"]


def test_icml_sample_falls_back_to_openreview_when_databases_empty(monkeypatch):
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
    assert calls == ["dblp", "pmlr", "openreview"]


def test_arxiv_returns_status_for_success(monkeypatch):
    captured = []

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

    def fake_request(url):
        captured.append(url)
        return Response()

    monkeypatch.setattr("auto_research.auto_find.sources._request", fake_request)
    items, status = fetch_arxiv(["cs.AI"], 3, "2026-04-01", "2026-04-30")
    assert len(items) == 1
    assert status["ok"] is True
    assert status["count"] == 1
    assert status["queries"] == ["cat:cs.AI AND submittedDate:[202604010000 TO 202604302359]"]


def test_nature_feed_parses_and_filters_dates(monkeypatch):
    feed = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>AI agents accelerate scientific discovery</title>
        <link rel="alternate" href="https://www.nature.com/articles/s41586-026-00001-1" />
        <published>2026-05-01T00:00:00Z</published>
        <summary>Research article summary.</summary>
        <author><name>Ada Lovelace</name></author>
      </entry>
      <entry>
        <title>Older Nature article</title>
        <link rel="alternate" href="https://www.nature.com/articles/s41586-025-00001-1" />
        <published>2025-01-01T00:00:00Z</published>
      </entry>
    </feed>
    """

    class Response:
        text = feed

    monkeypatch.setattr("auto_research.auto_find.sources._request", lambda _url, **_kwargs: Response())
    items, status = fetch_nature_portfolio(["nature"], ["article"], 10, "2026-01-01", "2026-12-31")

    assert status["ok"] is True
    assert status["count"] == 1
    assert items[0]["source"] == "nature"
    assert items[0]["venue"] == "Nature"
    assert items[0]["metadata"]["journal_tier"] == "0"


def test_nature_fetches_paginated_listing_until_date_boundary(monkeypatch):
    empty_feed = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>"""
    page_1 = """
    <html><body>
      <article>
        <h3><a href="/articles/s41586-026-10652-y">A multi-agent system for automating scientific discovery</a></h3>
        <time>19 May 2026</time>
        <p>Robin automates hypothesis generation and data analysis.</p>
      </article>
    </body></html>
    """
    page_2 = """
    <html><body>
      <article>
        <h3><a href="/articles/s41586-026-10658-6">An AI system to help scientists write expert-level empirical software</a></h3>
        <time>19 May 2026</time>
        <p>An AI system writes empirical software for scientific tasks.</p>
      </article>
    </body></html>
    """
    page_3 = """
    <html><body>
      <article>
        <h3><a href="/articles/s41586-025-00001-1">Older Nature article</a></h3>
        <time>01 Jan 2025</time>
      </article>
    </body></html>
    """

    class Response:
        def __init__(self, text: str):
            self.text = text

    seen_urls: list[str] = []

    def fake_request(url, **_kwargs):
        seen_urls.append(url)
        if "format=feed" in url:
            return Response(empty_feed)
        if "page=2" in url:
            return Response(page_2)
        if "page=3" in url:
            return Response(page_3)
        if "/articles/s41586-" in url:
            return Response("<html><head></head><body></body></html>")
        return Response(page_1)

    monkeypatch.setattr("auto_research.auto_find.sources._request", fake_request)
    items, status = fetch_nature_portfolio(["nature"], ["article"], 10, "2026-01-01", "2026-12-31")

    titles = [item["title"] for item in items]
    assert "A multi-agent system for automating scientific discovery" in titles
    assert "An AI system to help scientists write expert-level empirical software" in titles
    assert "Older Nature article" not in titles
    assert status["count"] == 2
    assert status["pages_scanned"] == 3
    assert any("page=2" in url for url in seen_urls)
    assert any("page=3" in url for url in seen_urls)


def test_science_feed_parses_and_filters_article_type(monkeypatch):
    feed = """<?xml version="1.0" encoding="UTF-8"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns="http://purl.org/rss/1.0/"
      xmlns:dc="http://purl.org/dc/elements/1.1/"
      xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">
      <item>
        <title>AI systems for scientific discovery</title>
        <link>https://www.science.org/doi/abs/10.1126/science.test</link>
        <description>Science, Volume 1, Page 1, May 2026.</description>
        <dc:identifier>doi:10.1126/science.test</dc:identifier>
        <dc:date>2026-05-21T06:00:10Z</dc:date>
        <dc:type>Research Article</dc:type>
        <dc:creator>Ada Lovelace</dc:creator>
        <prism:publicationName>Science</prism:publicationName>
        <prism:doi>10.1126/science.test</prism:doi>
      </item>
      <item>
        <title>Science editorial item</title>
        <dc:date>2026-05-21T06:00:10Z</dc:date>
        <dc:type>Editorial</dc:type>
      </item>
    </rdf:RDF>
    """

    class Response:
        text = feed

    monkeypatch.setattr("auto_research.auto_find.sources._request", lambda _url, **_kwargs: Response())
    items, status = fetch_science_family(["science"], ["Research Article"], 10, "2026-01-01", "2026-12-31")

    assert status["ok"] is True
    assert status["count"] == 1
    assert items[0]["source"] == "science"
    assert items[0]["venue"] == "Science"
    assert items[0]["metadata"]["doi"] == "10.1126/science.test"


def test_science_crossref_fetches_date_range_pages(monkeypatch):
    page_1 = {
        "message": {
            "items": [
                {
                    "DOI": "10.1126/science.a1",
                    "title": ["AI system for scientific discovery"],
                    "container-title": ["Science"],
                    "published-print": {"date-parts": [[2026, 5, 21]]},
                    "URL": "https://doi.org/10.1126/science.a1",
                    "author": [{"given": "Ada", "family": "Lovelace"}],
                }
            ]
        }
    }
    page_2 = {
        "message": {
            "items": [
                {
                    "DOI": "10.1126/science.a2",
                    "title": ["Automated research workflow for empirical science"],
                    "container-title": ["Science"],
                    "published-print": {"date-parts": [[2026, 5, 14]]},
                    "URL": "https://doi.org/10.1126/science.a2",
                }
            ]
        }
    }
    feed = """<?xml version="1.0" encoding="UTF-8"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns="http://purl.org/rss/1.0/"
      xmlns:dc="http://purl.org/dc/elements/1.1/"
      xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">
    </rdf:RDF>
    """

    class Response:
        def __init__(self, text: str = "", data: dict | None = None):
            self.text = text
            self._data = data

        def json(self):
            return self._data

    seen_urls: list[str] = []

    def fake_request(url, **_kwargs):
        seen_urls.append(url)
        if "api.crossref.org" in url and "offset=100" in url:
            return Response(data=page_2)
        if "api.crossref.org" in url:
            return Response(data=page_1)
        return Response(text=feed)

    monkeypatch.setattr("auto_research.auto_find.sources._request", fake_request)
    items, status = fetch_science_family(["science"], ["Research Article"], 200, "2026-05-01", "2026-05-23")

    titles = [item["title"] for item in items]
    assert "AI system for scientific discovery" in titles
    assert "Automated research workflow for empirical science" in titles
    assert status["count"] == 2
    assert status["pages_scanned"] == 2
    assert status["date_coverage"] == {"newest": "2026-05-21", "oldest": "2026-05-14"}
    assert any("api.crossref.org" in url for url in seen_urls)
    assert any("action/showFeed" in url for url in seen_urls)


def test_science_detail_enrichment_fills_missing_abstract(monkeypatch):
    page = """
    <html>
      <head>
        <meta name="citation_doi" content="10.1126/science.detail" />
      </head>
      <body>
        <section class="abstract">
          <h2>Abstract</h2>
          <p>We present an AI system for automated scientific discovery.</p>
        </section>
      </body>
    </html>
    """

    class Response:
        text = page

    monkeypatch.setattr("auto_research.auto_find.sources._request", lambda _url, **_kwargs: Response())
    papers = [{
        "id": "s1",
        "title": "AI system for scientific discovery",
        "abstract": "",
        "url": "https://www.science.org/doi/abs/10.1126/science.detail",
        "pdf_url": "",
        "metadata": {"doi": "10.1126/science.detail"},
    }]

    enriched, stats = enrich_science_details(papers)

    assert stats == {"attempted": 1, "abstracts_filled": 1, "pdfs_filled": 1, "dois_filled": 0}
    assert enriched[0]["abstract"] == "We present an AI system for automated scientific discovery."
    assert enriched[0]["pdf_url"] == "https://www.science.org/doi/pdf/10.1126/science.detail"


def test_arxiv_returns_status_for_failure(monkeypatch):
    def fail(_url):
        raise RuntimeError("network down")

    monkeypatch.setattr("auto_research.auto_find.sources._request", fail)
    items, status = fetch_arxiv(["cs.AI"], 3, "", "")
    assert items == []
    assert status["ok"] is False
    assert "network down" in status["message"]


def test_arxiv_recent_fallback_when_api_unavailable(monkeypatch):
    list_html = """<!doctype html>
    <html><body>
      <h3>Tue, 26 May 2026 (showing first 1 of 1 entries)</h3>
      <dl>
        <dt>
          <a href="/abs/2605.26114" id="2605.26114" title="Abstract">arXiv:2605.26114</a>
          [<a href="/pdf/2605.26114" title="Download PDF">pdf</a>]
        </dt>
        <dd>
          <div class="meta">
            <div class="list-title mathjax"><span class="descriptor">Title:</span> Mobile GUI Agent Research</div>
            <div class="list-authors"><a>Dingbang Wu</a>, <a>Rui Hao</a></div>
            <div class="list-subjects"><span class="descriptor">Subjects:</span> <span class="primary-subject">Artificial Intelligence (cs.AI)</span>; Computation and Language (cs.CL)</div>
          </div>
        </dd>
      </dl>
    </body></html>"""
    abs_html = """<!doctype html>
    <html><head>
      <meta name="citation_title" content="MobileGym: A Verifiable Simulation Platform">
      <meta name="citation_author" content="Wu, Dingbang">
      <meta name="citation_author" content="Hao, Rui">
      <meta name="citation_date" content="2026/05/25">
      <meta name="citation_pdf_url" content="https://arxiv.org/pdf/2605.26114">
      <meta name="citation_arxiv_id" content="2605.26114">
      <meta name="citation_abstract" content="A mobile GUI agent benchmark.">
    </head><body></body></html>"""

    class Response:
        def __init__(self, text):
            self.text = text

    def fake_request(url, timeout=12):
        if "export.arxiv.org" in url:
            raise RuntimeError("api rate limited")
        if "/list/cs.AI/recent" in url:
            return Response(list_html)
        if "/abs/2605.26114" in url:
            return Response(abs_html)
        raise AssertionError(url)

    monkeypatch.setattr("auto_research.auto_find.sources._request", fake_request)
    items, status = fetch_arxiv(["cs.AI"], 5, "2026-05-01", "2026-05-31")

    assert len(items) == 1
    assert status["ok"] is True
    assert status["fallback_used"] is True
    assert status["fallback_pages_fetched"] == 1
    assert items[0]["title"] == "MobileGym: A Verifiable Simulation Platform"
    assert items[0]["abstract"] == "A mobile GUI agent benchmark."
    assert items[0]["metadata"]["published"] == "2026-05-25"


def test_arxiv_paginates_and_dedupes(monkeypatch):
    pages = {
        0: """<?xml version='1.0' encoding='UTF-8'?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>http://arxiv.org/abs/2604.00001v1</id>
            <title>Research agents for literature review</title>
            <summary>Agentic literature review.</summary>
            <published>2026-04-30T00:00:00Z</published>
            <updated>2026-04-30T00:00:00Z</updated>
            <author><name>Alice</name></author>
          </entry>
        </feed>""",
        1: """<?xml version='1.0' encoding='UTF-8'?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>http://arxiv.org/abs/2604.00001v1</id>
            <title>Research agents for literature review</title>
            <summary>Duplicate from another category.</summary>
            <published>2026-04-30T00:00:00Z</published>
            <updated>2026-04-30T00:00:00Z</updated>
            <author><name>Alice</name></author>
          </entry>
          <entry>
            <id>http://arxiv.org/abs/2604.00002v1</id>
            <title>Unrelated optimization note</title>
            <summary>Optimization.</summary>
            <published>2026-04-29T00:00:00Z</published>
            <updated>2026-04-29T00:00:00Z</updated>
            <author><name>Bob</name></author>
          </entry>
        </feed>""",
        2: """<?xml version='1.0' encoding='UTF-8'?>
        <feed xmlns="http://www.w3.org/2005/Atom"></feed>""",
    }

    class Response:
        def __init__(self, text):
            self.text = text

    def fake_request(url):
        if "cat%3Acs.AI" in url:
            return Response(pages[0] if "start=0" in url else pages[2])
        return Response(pages[1] if "start=0" in url else pages[2])

    monkeypatch.setattr("auto_research.auto_find.sources._request", fake_request)
    items, status = fetch_arxiv(["cs.AI", "cs.CL"], 1, "", "")

    assert len(items) == 2
    assert status["pages_fetched"] == 2
    assert status["deduped_count"] == 2
    duplicate = next(item for item in items if item["arxiv_id"] == "2604.00001v1")
    assert duplicate["categories"] == ["cs.AI", "cs.CL"]


def test_biorxiv_returns_status_for_success(monkeypatch):
    class Response:
        def json(self):
            return {
                "collection": [
                    {
                        "doi": "10.1101/2026.05.01.123456",
                        "title": "AI models for biological discovery",
                        "authors": "Alice; Bob",
                        "abstract": "Abstract text.",
                        "date": "2026-05-01",
                        "version": "1",
                        "category": "Bioinformatics",
                        "license": "cc_by",
                        "server": "biorxiv",
                    }
                ]
            }

    monkeypatch.setattr("auto_research.auto_find.sources._request", lambda _url, **_kwargs: Response())
    items, status = fetch_biorxiv(["bioinformatics"], 3, "2026-05-01", "2026-05-31")
    assert len(items) == 1
    assert status["ok"] is True
    assert status["count"] == 1
    assert status["categories"] == ["bioinformatics"]
    assert items[0]["source"] == "biorxiv"
    assert items[0]["venue"] == "bioRxiv"
    assert items[0]["url"] == "https://www.biorxiv.org/content/10.1101/2026.05.01.123456v1"
    assert items[0]["pdf_url"] == "https://www.biorxiv.org/content/10.1101/2026.05.01.123456v1.full.pdf"


def test_biorxiv_returns_status_for_failure(monkeypatch):
    def fail(_url, **_kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("auto_research.auto_find.sources._request", fail)
    items, status = fetch_biorxiv(["bioinformatics"], 3, "2026-05-01", "2026-05-31")
    assert items == []
    assert status["ok"] is False
    assert "network down" in status["message"]


def test_biorxiv_paginates_filters_and_dedupes(monkeypatch):
    relevant = {
        "doi": "10.1101/2026.05.01.123456",
        "title": "Research agents for cell biology",
        "authors": "Alice",
        "abstract": "Agentic biology.",
        "date": "2026-05-01",
        "version": "1",
        "category": "Bioinformatics",
    }
    unrelated = {
        "doi": "10.1101/2026.05.01.999999",
        "title": "Unrelated ecology note",
        "date": "2026-05-01",
        "version": "1",
        "category": "Ecology",
    }
    duplicate = {
        **relevant,
        "abstract": "Duplicate record.",
        "version": "2",
    }
    second_relevant = {
        "doi": "10.1101/2026.05.02.000001",
        "title": "Neural methods for biological datasets",
        "authors": "Bob",
        "abstract": "Neural methods.",
        "date": "2026-05-02",
        "version": "1",
        "category": "Bioinformatics",
    }
    page_1 = [relevant] + [unrelated for _ in range(99)]
    page_2 = [duplicate, second_relevant]

    class Response:
        def __init__(self, records):
            self.records = records

        def json(self):
            return {"collection": self.records}

    seen_urls: list[str] = []

    def fake_request(url, **_kwargs):
        seen_urls.append(url)
        return Response(page_2 if "/100/json" in url else page_1)

    monkeypatch.setattr("auto_research.auto_find.sources._request", fake_request)
    items, status = fetch_biorxiv(["bioinformatics"], 10, "2026-05-01", "2026-05-31")

    assert len(items) == 2
    assert status["pages_fetched"] == 2
    assert status["raw_count"] == 102
    assert status["deduped_count"] == 2
    assert all(item["category"] == "Bioinformatics" for item in items)
    assert any("/100/json" in url for url in seen_urls)


def test_tfidf_ranker_uses_research_profile_and_boosted_title():
    papers = [
        {"id": "p1", "title": "Research agents for literature review", "abstract": "A system for academic paper triage.", "category": "cs.AI"},
        {"id": "p2", "title": "Image segmentation", "abstract": "Vision model for medical scans.", "category": "cs.CV"},
    ]

    selected, report = rank_papers_tfidf(papers, "academic research agents literature review", global_limit=1)

    assert selected[0]["id"] == "p1"
    assert selected[0]["local_score"] > 0
    assert report["selected_count"] == 1


def test_tfidf_ranker_boosts_research_automation_and_penalizes_avoid_topics():
    papers = [
        {
            "id": "agent",
            "title": "Agent Evaluation for Academic Research Automation",
            "abstract": "We evaluate information-seeking LLM agents for literature review, paper triage, and experiment planning.",
            "category": "cs.AI",
        },
        {
            "id": "education",
            "title": "Agentic AI Ecosystems in Higher Education",
            "abstract": "Conversational AI tools for students in education settings.",
            "category": "cs.AI",
        },
        {
            "id": "vision",
            "title": "Vision-Language Image Generation Benchmark",
            "abstract": "A computer vision benchmark for image generation and segmentation.",
            "category": "cs.CV",
        },
    ]

    selected, _report = rank_papers_tfidf(
        papers,
        "LLM agents for academic research automation, literature review, paper triage, information-seeking agent evaluation, RAG",
        global_limit=3,
    )

    assert selected[0]["id"] == "agent"
    assert selected[0]["local_positive_matches"]
    assert selected[-1]["id"] == "vision"


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
