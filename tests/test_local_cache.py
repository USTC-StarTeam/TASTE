from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from auto_research.auto_find.local_cache import cache_directory, load_cached_venue_year, write_venue_year_cache
from auto_research.auto_find.pipeline import _cached_or_fetched_venue_index
from auto_research.models import AppConfig


class DisabledLLM:
    enabled = False


def _venue():
    return {
        "id": "test_conf",
        "source": "ccf",
        "name": "TESTCONF",
        "full_name": "Test Conference",
        "address": "https://dblp.uni-trier.de/db/conf/testconf/",
    }


def _paper(index: int, category: str = ""):
    return {
        "id": f"paper_{index}",
        "source": "dblp",
        "title": f"Research automation paper {index}",
        "authors": "A. Researcher",
        "abstract": "",
        "url": f"https://example.com/{index}",
        "pdf_url": "",
        "venue": "TESTCONF",
        "year": 2025,
        "category": category,
        "classification_source": "llm_inferred",
        "metadata": {},
    }


def test_write_and_load_local_conference_cache():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        cache = write_venue_year_cache(_venue(), 2025, [_paper(1)], "dblp", root=root)
        loaded = load_cached_venue_year(_venue(), 2025, root=root)

    assert cache["directory"].endswith("/testconf/2025")
    assert cache["paper_count"] == 1
    assert loaded is not None
    assert loaded["paper_count"] == 1
    assert loaded["source_adapter"] == "dblp"
    assert loaded["papers"][0]["venue_id"] == "test_conf"
    assert loaded["source_report"]["paper_count"] == 1


def test_cache_directory_uses_conference_name_not_long_venue_id():
    venue = {
        "id": "ccf_ai_conference_a_icml_international_conference_on_machine_learning",
        "name": "ICML",
        "full_name": "International Conference on Machine Learning",
    }

    assert cache_directory(venue, 2025) == Path("auto_research/local_database/icml/2025").resolve()


def test_cached_venue_index_uses_cache_before_online_fetch():
    cached = {
        "venue_id": "test_conf",
        "year": 2025,
        "papers_path": "/tmp/papers.json",
        "category_summary_path": "",
        "source_report_path": "/tmp/source_report.json",
        "papers": [_paper(1), _paper(2), _paper(3)],
        "category_summary": {},
        "source_report": {"source_adapter": "dblp"},
        "paper_count": 3,
        "source_adapter": "dblp",
    }
    with patch("auto_research.auto_find.pipeline.load_cached_venue_year", return_value=cached), patch("auto_research.auto_find.pipeline.fetch_venue_title_index_all") as fetch_all:
        papers, reports, adapter, used_category_filter = _cached_or_fetched_venue_index(
            _venue(),
            [2025],
            AppConfig(provider="mock", research_interest="research automation", max_fetch_papers=1),
            DisabledLLM(),
            log=lambda _msg: None,
        )

    fetch_all.assert_not_called()
    assert len(papers) == 3
    assert reports[0]["cache_status"] == "hit"
    assert reports[0]["category_filter_skipped"] is True
    assert adapter == "local_cache:dblp"
    assert used_category_filter is False


def test_cache_build_fetches_full_year_not_max_fetch_limit():
    fetched = [_paper(1), _paper(2), _paper(3)]
    with patch("auto_research.auto_find.pipeline.load_cached_venue_year", return_value=None), patch("auto_research.auto_find.pipeline.fetch_venue_title_index_all", return_value=(fetched, "dblp")) as fetch_all, patch("auto_research.auto_find.pipeline.write_venue_year_cache") as write_cache:
        write_cache.return_value = {
            "venue_id": "test_conf",
            "year": 2025,
            "papers_path": "/tmp/papers.json",
            "category_summary_path": "/tmp/category_summary.json",
            "source_report_path": "/tmp/source_report.json",
            "papers": fetched,
            "category_summary": {},
            "source_report": {"source_adapter": "dblp"},
            "paper_count": 3,
            "source_adapter": "dblp",
        }
        papers, reports, adapter, used_category_filter = _cached_or_fetched_venue_index(
            _venue(),
            [2025],
            AppConfig(provider="mock", research_interest="research automation", max_fetch_papers=1),
            DisabledLLM(),
            log=lambda _msg: None,
        )

    fetch_all.assert_called_once_with(_venue(), [2025])
    assert len(papers) == 3
    assert reports[0]["cache_status"] == "built"
    assert adapter == "local_cache:dblp"
    assert used_category_filter is False
