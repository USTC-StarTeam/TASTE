import json
from pathlib import Path


JOURNAL_QUALITY_TABLE = Path("auto_research/local_database/journal_quality_levels.json")


def test_journal_quality_levels_table_is_valid():
    data = json.loads(JOURNAL_QUALITY_TABLE.read_text())
    max_bonus = data["bonus_policy"]["max_bonus"]
    tier_bonuses = {name: tier["bonus"] for name, tier in data["tiers"].items()}

    assert data["coverage"]["journal_count"] == 334
    assert data["coverage"]["ccf_journal_count"] == 285
    assert data["coverage"]["nature_family_count"] == 27
    assert data["coverage"]["science_family_count"] == 22
    assert data["bonus_policy"]["apply_only_if_fit_score_at_least"] == 6.0
    assert tier_bonuses["ccf_a"] > tier_bonuses["ccf_b"] > tier_bonuses["ccf_c"]
    assert tier_bonuses["ccf_a"] == max_bonus

    for journal_id, journal in data["journals"].items():
        assert journal_id
        assert journal["names"]
        assert journal["venue_ids"] == [journal_id]
        if journal["source"] == "ccf":
            assert journal["ccf_rank"] in {"A", "B", "C"}
        assert journal["tier"] in data["tiers"]
        assert 0.0 <= journal["bonus"] <= max_bonus
        assert journal["bonus"] == tier_bonuses[journal["tier"]]


def test_journal_quality_levels_seed_ccf_journal_ranks():
    data = json.loads(JOURNAL_QUALITY_TABLE.read_text())
    journals = data["journals"]

    tocs = journals["ccf_arch_dcp_ss_journal_a_tocs_acm_transactions_on_computer_systems"]
    assert tocs["ccf_rank"] == "A"
    assert tocs["tier"] == "ccf_a"
    assert tocs["bonus"] == 0.2

    taas = journals["ccf_arch_dcp_ss_journal_b_taas_acm_transactions_on_autonomous_and_adaptive_systems"]
    assert taas["ccf_rank"] == "B"
    assert taas["tier"] == "ccf_b"
    assert taas["bonus"] == 0.1

    tcc = journals["ccf_arch_dcp_ss_journal_c_tcc_ieee_transactions_on_cloud_computing"]
    assert tcc["ccf_rank"] == "C"
    assert tcc["tier"] == "ccf_c"
    assert tcc["bonus"] == 0.0


def test_journal_quality_levels_seed_nature_science_families():
    data = json.loads(JOURNAL_QUALITY_TABLE.read_text())
    journals = data["journals"]

    assert journals["nature_family_nature"]["tier"] == "flagship"
    assert journals["nature_family_nature"]["bonus"] == 0.2
    assert journals["nature_family_natmachintell"]["tier"] == "family_tier_1"
    assert journals["nature_family_natmachintell"]["bonus"] == 0.15

    assert journals["science_family_science"]["tier"] == "flagship"
    assert journals["science_family_science"]["bonus"] == 0.2
    assert journals["science_family_sciadv"]["tier"] == "family_tier_1"
    assert journals["science_family_sciadv"]["bonus"] == 0.15
    assert journals["science_family_research"]["tier"] == "science_partner"
    assert journals["science_family_research"]["bonus"] == 0.05
