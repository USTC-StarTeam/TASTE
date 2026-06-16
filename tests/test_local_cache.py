import json
from pathlib import Path
from tempfile import TemporaryDirectory

from build_openreview_cache import build_openreview_year, venue_spec
from find_support import cache_directory, load_cached_venue_year, write_venue_year_cache


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


def test_openreview_builder_uses_shared_venue_specs(monkeypatch):
    def fake_fetch_notes(year, spec, *, api_version, page_size, timeout, retries, max_pages):
        assert api_version == 2
        return [
            {
                "id": f"note_{spec['venue']}_{year}",
                "forum": f"forum_{spec['venue']}_{year}",
                "number": 1,
                "content": {
                    "title": {"value": f"Reliable {spec['venue']} Research Automation"},
                    "authors": {"value": ["A. Researcher", "B. Builder"]},
                    "abstract": {"value": "A paper about reliable research automation."},
                    "primary_area": {"value": "Research automation"},
                    "keywords": {"value": ["automation", "evaluation"]},
                },
            }
        ], f"{spec['openreview_prefix']}/{year}/Conference"

    monkeypatch.setattr("build_openreview_cache._fetch_notes", fake_fetch_notes)
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        neurips_path = build_openreview_year("openreview_neurips", 2025, output_root=root)
        iclr_path = build_openreview_year("iclr", 2025, output_root=root)
        neurips = json.loads(neurips_path.read_text(encoding="utf-8"))
        iclr = json.loads(iclr_path.read_text(encoding="utf-8"))

    assert venue_spec("nips")["venue_id"] == "openreview_neurips"
    assert neurips_path == root / "openreview_neurips" / "2025" / "papers.json"
    assert iclr_path == root / "openreview_iclr" / "2025" / "papers.json"
    assert neurips["venue"] == "NeurIPS"
    assert neurips["openreview_venueid"] == "NeurIPS.cc/2025/Conference"
    assert iclr["venue"] == "ICLR"
    assert iclr["openreview_venueid"] == "ICLR.cc/2025/Conference"
    assert neurips["papers"][0]["category"] == "Research automation"
    assert iclr["papers"][0]["keywords"] == ["automation", "evaluation"]
