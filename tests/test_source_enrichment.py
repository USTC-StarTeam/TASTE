from unittest.mock import patch

from auto_research.auto_find.sources import enrich_pmlr_details, fetch_venue_title_index_all


def test_full_title_index_uses_dblp_as_base_and_enriches_from_openreview():
    venue = {
        "id": "ccf_ai_conference_a_icml_international_conference_on_machine_learning",
        "name": "ICML",
        "full_name": "International Conference on Machine Learning",
        "address": "https://dblp.org/db/conf/icml/",
    }
    dblp_papers = [
        {
            "id": "dblp_1",
            "source": "dblp",
            "title": "Research Agent Evaluation",
            "authors": "A. Author",
            "abstract": "",
            "url": "https://dblp.org/rec/conf/icml/example",
            "pdf_url": "",
            "venue": "ICML",
            "year": 2025,
            "category": "",
            "classification_source": "llm_inferred",
            "metadata": {"venue_id": venue["id"]},
        },
        {
            "id": "dblp_2",
            "source": "dblp",
            "title": "Unmatched Systems Paper",
            "authors": "B. Author",
            "abstract": "",
            "url": "https://dblp.org/rec/conf/icml/other",
            "pdf_url": "",
            "venue": "ICML",
            "year": 2025,
            "category": "",
            "classification_source": "llm_inferred",
            "metadata": {"venue_id": venue["id"]},
        },
    ]
    openreview_papers = [
        {
            "id": "or_1",
            "source": "openreview",
            "title": "Research Agent Evaluation",
            "authors": "A. Author",
            "abstract": "Evaluates agents for research workflows.",
            "url": "https://openreview.net/forum?id=abc",
            "pdf_url": "https://openreview.net/pdf?id=abc",
            "venue": "ICML",
            "year": 2025,
            "category": "agents",
            "primary_area": "agents",
            "track": "main",
            "keywords": ["agents", "evaluation"],
            "classification_source": "official",
            "metadata": {"openreview_venueid": "ICML.cc/2025/Conference"},
        }
    ]

    with patch("auto_research.auto_find.sources.fetch_dblp_venue", return_value=dblp_papers) as fetch_dblp, patch("auto_research.auto_find.sources.fetch_openreview_venue", return_value=openreview_papers), patch("auto_research.auto_find.sources.fetch_pmlr_index", return_value=[]), patch("auto_research.auto_find.sources.fetch_icml_downloads", return_value=[]):
        papers, adapter = fetch_venue_title_index_all(venue, [2025])

    fetch_dblp.assert_called_once_with(venue, [2025], None)
    assert adapter == "dblp+openreview:1"
    assert len(papers) == 2
    enriched = papers[0]
    assert enriched["id"] == "dblp_1"
    assert enriched["source"] == "dblp"
    assert enriched["abstract"] == "Evaluates agents for research workflows."
    assert enriched["url"] == "https://dblp.org/rec/conf/icml/example"
    assert enriched["pdf_url"] == "https://openreview.net/pdf?id=abc"
    assert enriched["category"] == "agents"
    assert enriched["primary_area"] == "agents"
    assert enriched["classification_source"] == "official"
    assert enriched["metadata"]["enrichment_sources"] == ["openreview"]
    assert papers[1]["id"] == "dblp_2"
    assert papers[1]["category"] == ""


def test_full_title_index_falls_back_when_dblp_base_empty():
    venue = {
        "id": "ccf_ai_conference_a_icml_international_conference_on_machine_learning",
        "name": "ICML",
        "full_name": "International Conference on Machine Learning",
        "address": "https://dblp.org/db/conf/icml/",
    }
    pmlr_papers = [{"id": "pmlr_1", "source": "pmlr", "title": "PMLR Paper", "year": 2025}]

    with patch("auto_research.auto_find.sources.fetch_dblp_venue", return_value=[]), patch("auto_research.auto_find.sources.fetch_pmlr_index", return_value=pmlr_papers), patch("auto_research.auto_find.sources.fetch_openreview_venue", return_value=[]), patch("auto_research.auto_find.sources.fetch_icml_downloads", return_value=[]):
        papers, adapter = fetch_venue_title_index_all(venue, [2025])

    assert adapter == "pmlr"
    assert papers == pmlr_papers


def test_full_title_index_prefers_direct_pmlr_when_available():
    venue = {
        "id": "ccf_ai_conference_a_icml_international_conference_on_machine_learning",
        "name": "ICML",
        "full_name": "International Conference on Machine Learning",
        "address": "https://dblp.org/db/conf/icml/",
    }
    dblp_papers = [
        {
            "id": "dblp_1",
            "source": "dblp",
            "title": "Research Agent Evaluation",
            "authors": "A. Author",
            "abstract": "",
            "url": "",
            "pdf_url": "",
            "venue": "ICML",
            "year": 2025,
            "category": "",
            "classification_source": "llm_inferred",
            "metadata": {"venue_id": venue["id"]},
        }
    ]
    pmlr_papers = [
        {
            "id": "pmlr_1",
            "source": "pmlr",
            "title": "Research Agent Evaluation",
            "authors": "",
            "abstract": "",
            "url": "https://proceedings.mlr.press/v267/example.html",
            "pdf_url": "https://proceedings.mlr.press/v267/example.pdf",
            "venue": "ICML",
            "year": 2025,
            "category": "",
            "classification_source": "llm_inferred",
            "metadata": {"pmlr_url": "https://proceedings.mlr.press/v267/"},
        }
    ]

    with patch("auto_research.auto_find.sources.fetch_dblp_venue", return_value=dblp_papers), patch("auto_research.auto_find.sources.fetch_openreview_venue", return_value=[]), patch("auto_research.auto_find.sources.fetch_pmlr_index", return_value=pmlr_papers), patch("auto_research.auto_find.sources.fetch_icml_downloads", return_value=[]):
        papers, adapter = fetch_venue_title_index_all(venue, [2025])

    assert adapter == "pmlr"
    assert papers[0]["source"] == "pmlr"
    assert papers[0]["url"] == "https://proceedings.mlr.press/v267/example.html"
    assert papers[0]["pdf_url"] == "https://proceedings.mlr.press/v267/example.pdf"


def test_pmlr_detail_enrichment_extracts_abstract_from_landing_page():
    class Response:
        text = """
        <html>
          <body>
            <h1>Research Agent Evaluation</h1>
            <h4>Abstract</h4>
            <p>We evaluate agents for research workflows.</p>
            <h4>Cite this Paper</h4>
            <a href="example.pdf">Download PDF</a>
          </body>
        </html>
        """

    papers = [
        {
            "id": "dblp_1",
            "source": "dblp",
            "title": "Research Agent Evaluation",
            "abstract": "",
            "url": "",
            "pdf_url": "",
            "metadata": {
                "source_records": {
                    "pmlr": {
                        "url": "https://proceedings.mlr.press/v267/example.html",
                    }
                }
            },
        }
    ]

    with patch("auto_research.auto_find.sources._request", return_value=Response()):
        enriched, stats = enrich_pmlr_details(papers)

    assert stats == {"attempted": 1, "abstracts_filled": 1, "urls_filled": 1, "pdfs_filled": 1}
    assert enriched[0]["url"] == "https://proceedings.mlr.press/v267/example.html"
    assert enriched[0]["abstract"] == "We evaluate agents for research workflows."
    assert enriched[0]["pdf_url"] == "https://proceedings.mlr.press/v267/example.pdf"
    assert enriched[0]["metadata"]["abstract_source"] == "pmlr"
