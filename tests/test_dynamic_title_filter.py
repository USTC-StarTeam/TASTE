from auto_research.auto_find.pipeline import _dynamic_title_prune, _title_filter_groups


def _paper(pid: str, category: str, fit: float = 8.0) -> dict:
    return {
        "id": pid,
        "venue": "ICLR",
        "year": 2026,
        "primary_area": category,
        "category": category,
        "title": f"{category} paper {pid}",
        "fit_score": fit,
        "diversity_score": max(0.0, fit - 1.0),
        "score": fit,
    }


def test_title_filter_groups_compute_ratio_per_venue_year():
    items = [_paper(f"a{i}", "large") for i in range(8)] + [_paper(f"b{i}", "small") for i in range(2)]

    groups = {group["category"]: group for group in _title_filter_groups(items)}

    assert groups["large"]["category_size"] == 8
    assert groups["large"]["venue_year_total"] == 10
    assert groups["large"]["category_ratio"] == 0.8
    assert groups["large"]["policy"]["label"] == "heated"
    assert groups["small"]["category_ratio"] == 0.2
    assert groups["small"]["policy"]["label"] == "moderate"


def test_dynamic_title_prune_is_stricter_for_heated_categories():
    all_items = [_paper(f"a{i}", "large") for i in range(8)] + [_paper(f"b{i}", "small") for i in range(2)]
    groups = _title_filter_groups(all_items)
    selected = [
        _paper("a_high", "large", 8.0),
        _paper("a_low", "large", 7.0),
        _paper("b_low", "small", 7.0),
    ]
    logs: list[str] = []

    kept = _dynamic_title_prune(selected, groups, logs.append, "ICLR")

    kept_ids = {item["id"] for item in kept}
    assert "a_high" in kept_ids
    assert "a_low" not in kept_ids
    assert "b_low" in kept_ids
