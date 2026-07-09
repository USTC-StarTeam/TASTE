from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FINDING_RUNTIME = ROOT / "modules" / "finding" / "scripts" / "core" / "finding_runtime.py"
FINDING_SCRIPTS = ROOT / "modules" / "finding" / "scripts"
FINDING_CORE = FINDING_SCRIPTS / "core"
FINDING_FLOW = FINDING_SCRIPTS / "flow"


def load_finding_runtime():
    spec = importlib.util.spec_from_file_location("finding_runtime_markdown_contract", FINDING_RUNTIME)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_finding_pipeline_modules():
    for path in (FINDING_CORE, FINDING_FLOW, FINDING_SCRIPTS):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)
    import support  # type: ignore
    import pipeline  # type: ignore

    return support, pipeline


def test_find_markdown_appends_track_presentation_to_venue_year_line():
    runtime = load_finding_runtime()

    markdown = runtime.paper_markdown([
        {
            "title": "Track-only paper",
            "venue": "ICLR",
            "year": 2026,
            "track": "Poster",
        }
    ])

    assert "- **会议/年份**: ICLR 2026 / Poster" in markdown
    assert "会议展示类型" not in markdown


def test_find_markdown_strips_venue_year_prefix_from_presentation_label():
    runtime = load_finding_runtime()

    markdown = runtime.paper_markdown([
        {
            "title": "Label paper",
            "venue": "ICML",
            "year": 2026,
            "presentation_label": "ICML 2026 Spotlight",
        }
    ])

    assert "- **会议/年份**: ICML 2026 / Spotlight" in markdown


def test_find_markdown_uses_metadata_presentation_type():
    runtime = load_finding_runtime()

    markdown = runtime.paper_markdown([
        {
            "title": "Metadata paper",
            "venue": "NeurIPS",
            "year": 2025,
            "metadata": {"presentation_type": "oral"},
        }
    ])

    assert "- **会议/年份**: NeurIPS 2025 / Oral" in markdown


def test_neurips_virtual_presentation_enrichment_feeds_oral_bonus(monkeypatch):
    support, pipeline = load_finding_pipeline_modules()

    class Response:
        def __init__(self, text: str):
            self.text = text

    def fake_request(url: str, timeout: int = 12):
        if "/events/oral" in url:
            return Response('<a href="/virtual/2025/oral/123">NeurIPS Oral Protein Paper</a>')
        return Response("")

    monkeypatch.setattr(support, "_NEURIPS_VIRTUAL_PRESENTATION_INDEX_CACHE", {})
    monkeypatch.setattr(support, "_request", fake_request)
    base = {
        "title": "NeurIPS Oral Protein Paper",
        "venue": "NeurIPS",
        "year": 2025,
        "track": "Main Conference Track",
        "url": "https://papers.nips.cc/paper_files/paper/2025/hash/test-Abstract-Conference.html",
        "metadata": {"detail_url": "https://papers.nips.cc/paper_files/paper/2025/hash/test-Abstract-Conference.html"},
    }

    enriched = support._enrich_neurips_official_with_virtual_presentations([base], 2025)
    paper = enriched[0]

    assert paper["presentation_type"] == "oral"
    assert paper["presentation_label"] == "NeurIPS 2025 Oral"
    assert pipeline._presentation_bonus(paper) == (0.45, "发表类型: oral +0.45")


def test_neurips_spotlight_page_context_overrides_poster_url(monkeypatch):
    support, pipeline = load_finding_pipeline_modules()

    class Response:
        def __init__(self, text: str):
            self.text = text

    def fake_request(url: str, timeout: int = 12):
        if "/events/spotlight" in url:
            return Response('<a href="/virtual/2025/poster/456">NeurIPS Spotlight Protein Paper</a>')
        return Response("")

    monkeypatch.setattr(support, "_NEURIPS_VIRTUAL_PRESENTATION_INDEX_CACHE", {})
    monkeypatch.setattr(support, "_request", fake_request)
    base = {
        "title": "NeurIPS Spotlight Protein Paper",
        "venue": "NeurIPS",
        "year": 2025,
        "track": "Main Conference Track",
        "url": "https://papers.nips.cc/paper_files/paper/2025/hash/test-Abstract-Conference.html",
        "metadata": {"detail_url": "https://papers.nips.cc/paper_files/paper/2025/hash/test-Abstract-Conference.html"},
    }

    paper = support._enrich_neurips_official_with_virtual_presentations([base], 2025)[0]

    assert paper["presentation_type"] == "spotlight"
    assert paper["presentation_label"] == "NeurIPS 2025 Spotlight"
    assert pipeline._presentation_bonus(paper) == (0.20, "发表类型: spotlight/highlight +0.20")
