from find_pipeline import _apply_quality_bonus
from find_support import attach_quality_metadata


def _strong_item(**updates):
    item = {
        "id": "item_1",
        "title": "Generative materials discovery",
        "abstract": "A real abstract supports the current adaptive topic route and reusable evaluation evidence.",
        "fit_score": 8.0,
        "diversity_score": 6.0,
        "topic_evidence": "strong: direct topic match",
        "topic_evidence_supported": True,
        "category": "materials discovery",
        "reason": "directly relevant to the current research profile",
        "fit_explanation": "direct title and abstract evidence",
    }
    item.update(updates)
    return item


def test_attach_quality_metadata_matches_nature_family_journal():
    item = _strong_item(source="nature", venue="Nature", year=2026, metadata={"journal_slug": "nature"})

    attach_quality_metadata(item)

    assert item["quality_kind"] == "journal"
    assert item["quality_tier"] == "flagship"
    assert item["quality_bonus_available"] == 0.2
    assert item["quality_bonus"] == 0.0
    assert item["metadata"]["quality"]["quality_source"] == "journal_quality_levels.json"

    _apply_quality_bonus(item)
    assert item["quality_bonus"] == 0.2
    assert "journal:flagship" in item["quality_bonus_reason"]


def test_attach_quality_metadata_matches_conference_label_without_double_counting_oral():
    item = _strong_item(
        source="openreview",
        venue="ICLR",
        venue_id="openreview_iclr",
        year=2025,
        category="ICLR 2025 Oral",
        track="oral",
        metadata={"venue_id": "openreview_iclr"},
    )

    attach_quality_metadata(item)
    _apply_quality_bonus(item)

    assert item["quality_kind"] == "conference"
    assert item["quality_tier"] == "oral"
    assert item["quality_bonus_available"] == 0.3
    assert item["quality_bonus"] == 0.45
    assert "oral" in item["quality_bonus_reason"]
    assert "结构化质量表" not in item["quality_bonus_reason"]


def test_attach_quality_metadata_matches_icml_2026_official_oral_label():
    item = _strong_item(
        source="icml_downloads",
        venue="ICML",
        year=2026,
        track="ICML 2026 Oral",
        presentation_type="oral",
        presentation_label="ICML 2026 Oral",
        metadata={
            "venue_id": "ccf_ai_conference_a_icml_international_conference_on_machine_learning",
            "presentation_type": "oral",
            "presentation_label": "ICML 2026 Oral",
        },
    )

    attach_quality_metadata(item)
    _apply_quality_bonus(item)

    assert item["quality_kind"] == "conference"
    assert item["quality_tier"] == "oral"
    assert item["quality_bonus_available"] == 0.3
    assert item["quality_bonus"] == 0.45
    assert "oral" in item["quality_bonus_reason"]
    assert "结构化质量表" not in item["quality_bonus_reason"]


def test_quality_table_bonus_still_requires_strong_relevance_gate():
    item = _strong_item(source="nature", venue="Nature", year=2026, metadata={"journal_slug": "nature"}, fit_score=5.0)
    attach_quality_metadata(item)
    _apply_quality_bonus(item)

    assert item["quality_bonus_available"] == 0.2
    assert item["quality_bonus"] == 0.0
