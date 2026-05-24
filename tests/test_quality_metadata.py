from auto_research.auto_find.pipeline import _evaluate_items
from auto_research.auto_find.quality import attach_quality_metadata
from auto_research.models import AppConfig


class DisabledLLM:
    enabled = False


def test_attach_quality_metadata_matches_nature_family_journal():
    item = {
        "id": "nature_1",
        "source": "nature",
        "title": "Nature paper",
        "venue": "Nature",
        "year": 2026,
        "metadata": {"journal_slug": "nature"},
    }

    attach_quality_metadata(item)

    assert item["quality_kind"] == "journal"
    assert item["quality_tier"] == "flagship"
    assert item["quality_bonus_available"] == 0.2
    assert item["quality_bonus"] == 0.0
    assert item["metadata"]["quality"]["quality_source"] == "journal_quality_levels.json"


def test_attach_quality_metadata_matches_conference_label():
    item = {
        "id": "iclr_1",
        "source": "openreview",
        "title": "ICLR paper",
        "venue": "ICLR",
        "venue_id": "openreview_iclr",
        "year": 2025,
        "category": "ICLR 2025 Oral",
        "metadata": {"venue_id": "openreview_iclr"},
    }

    attach_quality_metadata(item)

    assert item["quality_kind"] == "conference"
    assert item["quality_tier"] == "oral"
    assert item["quality_bonus_available"] == 0.3


def test_filter3_applies_quality_bonus_only_after_fit_threshold():
    cfg = AppConfig(provider="mock", research_interest="materials discovery")
    high_fit = {
        "id": "nature_high",
        "source": "nature",
        "title": "Generative materials discovery",
        "abstract": "Materials discovery with generative models.",
        "fit_score": 8,
        "diversity_score": 6,
        "quality_bonus_available": 0.2,
    }
    low_fit = {
        "id": "nature_low",
        "source": "nature",
        "title": "Unrelated note",
        "abstract": "Unrelated.",
        "fit_score": 5,
        "diversity_score": 6,
        "quality_bonus_available": 0.2,
    }

    evaluated = _evaluate_items([high_fit, low_fit], cfg, DisabledLLM(), "articles", log=lambda _msg: None)
    by_id = {item["id"]: item for item in evaluated}

    assert by_id["nature_high"]["quality_bonus"] == 0.2
    assert by_id["nature_high"]["score"] == 7.7
    assert by_id["nature_low"]["quality_bonus"] == 0.0
    assert by_id["nature_low"]["score"] == 4.75
