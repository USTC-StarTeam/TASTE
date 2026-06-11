from pathlib import Path
from tempfile import TemporaryDirectory

from auto_research.auto_find.local_cache import cache_directory, load_cached_venue_year, write_venue_year_cache


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


def test_write_and_load_runtime_conference_cache():
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


def test_cache_directory_uses_short_conference_name_under_runtime_root():
    venue = {
        "id": "ccf_ai_conference_a_icml_international_conference_on_machine_learning",
        "name": "ICML",
        "full_name": "International Conference on Machine Learning",
    }
    root = Path("/tmp/taste-local-cache-test")

    assert cache_directory(venue, 2025, root=root) == root / "icml" / "2025"
