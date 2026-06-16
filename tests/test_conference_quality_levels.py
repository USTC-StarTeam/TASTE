import json

from find_support import CONFERENCE_QUALITY_TABLE


def test_conference_quality_levels_table_is_valid():
    data = json.loads(CONFERENCE_QUALITY_TABLE.read_text(encoding="utf-8"))
    max_bonus = data["bonus_policy"]["max_bonus"]
    tier_bonuses = {name: tier["bonus"] for name, tier in data["tiers"].items()}

    assert CONFERENCE_QUALITY_TABLE.as_posix().endswith("modules/finding/data/quality/conference_quality_levels.json")
    assert data["bonus_policy"]["apply_only_if_fit_score_at_least"] == 6.0
    assert tier_bonuses["oral"] > tier_bonuses["spotlight"] > tier_bonuses["regular"]
    assert tier_bonuses["award"] == max_bonus

    for conference in data["conferences"].values():
        assert conference["names"]
        assert "years" in conference
        for year_data in conference["years"].values():
            for label, mapping in year_data["label_aliases"].items():
                assert label == label.strip().lower()
                assert mapping["tier"] in data["tiers"]
                assert 0.0 <= mapping["bonus"] <= max_bonus
                assert mapping["bonus"] == tier_bonuses[mapping["tier"]]


def test_conference_quality_levels_seed_cached_conference_labels():
    data = json.loads(CONFERENCE_QUALITY_TABLE.read_text(encoding="utf-8"))

    iclr_2025 = data["conferences"]["iclr"]["years"]["2025"]["label_aliases"]
    assert iclr_2025["iclr 2025 oral"]["bonus"] == 0.3
    assert iclr_2025["iclr 2025 spotlight"]["bonus"] == 0.2
    assert iclr_2025["iclr 2025 poster"]["bonus"] == 0.0

    neurips_2025 = data["conferences"]["neurips"]["years"]["2025"]["label_aliases"]
    assert neurips_2025["neurips 2025 oral"]["bonus"] == 0.3
    assert neurips_2025["neurips 2025 spotlight"]["bonus"] == 0.2
    assert neurips_2025["neurips 2025 poster"]["bonus"] == 0.0

    icml_2025 = data["conferences"]["icml"]["years"]["2025"]["label_aliases"]
    assert icml_2025["spotlight poster"]["bonus"] == 0.2
