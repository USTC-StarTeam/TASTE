from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from auto_research.paths import DATA_DIR, REFERENCE_ROOT


FIELD_LABELS = {
    "ARCH_DCP_SS": "Architecture / Distributed / Storage",
    "CN": "Computer Networks",
    "NIS": "Network and Information Security",
    "TCSE_SS_PDL": "Software Engineering / Systems / PL",
    "DM_CS": "Database / Data Mining / IR",
    "TCS": "Theory",
    "CGAndMT": "Graphics and Multimedia",
    "AI": "Artificial Intelligence",
    "HCIAndPC": "HCI and Pervasive Computing",
    "Cross_Compre_Emerging": "Interdisciplinary / Emerging",
}

ADDRESS_OVERRIDES = {
    ("HPCA", "IEEE International Symposium on High Performance Computer Architecture"): "https://dblp.uni-trier.de/db/conf/hpca/",
}
NAME_ALIASES = {
    "kdd": "sigkdd",
}


def _safe_id(*parts: str) -> str:
    joined = "_".join(str(part or "").strip().lower() for part in parts)
    allowed = []
    for ch in joined:
        allowed.append(ch if ch.isalnum() else "_")
    return "_".join(filter(None, "".join(allowed).split("_")))


def _venue_name_key(name: str) -> str:
    normalized = str(name or "").strip().lower()
    return _safe_id(NAME_ALIASES.get(normalized, normalized))


def load_ccf_catalog() -> list[dict[str, Any]]:
    ccf_path = REFERENCE_ROOT / "openccf" / "data" / "ccf.json"
    if not ccf_path.exists():
        return []

    raw = json.loads(ccf_path.read_text(encoding="utf-8"))
    venues: list[dict[str, Any]] = []
    current_year = date.today().year
    default_years = list(range(current_year, current_year - 8, -1))

    for field_key, field_data in raw.items():
        field_label = FIELD_LABELS.get(field_key, field_key)
        for venue_type_key, venue_type in (("conf", "conference"), ("journals", "journal")):
            for rank, items in field_data.get(venue_type_key, {}).items():
                for item in items:
                    name = item.get("name") or item.get("full_name") or "unknown"
                    full_name = item.get("full_name", name)
                    address = ADDRESS_OVERRIDES.get((name, full_name), item.get("address", ""))
                    venue_id = _safe_id("ccf", field_key, venue_type, rank, name, item.get("full_name", ""))
                    venues.append({
                        "id": venue_id,
                        "source": "ccf",
                        "name": name,
                        "full_name": full_name,
                        "type": venue_type,
                        "rank": rank,
                        "field": field_label,
                        "field_key": field_key,
                        "address": address,
                        "years": default_years,
                        "classification_source": "llm_inferred",
                    })
    return venues


def _load_json_catalog(path: Path, source_label: str = "") -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return []
    venues: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            continue
        venue = dict(item)
        venue.setdefault("source", source_label or "custom")
        venue.setdefault("full_name", venue["name"])
        venue.setdefault("type", "conference")
        venue.setdefault("rank", "high-level")
        venue.setdefault("field", "Artificial Intelligence")
        venue.setdefault("field_key", "AI")
        venue.setdefault("address", "")
        venue.setdefault("years", list(range(date.today().year, date.today().year - 5, -1)))
        venue.setdefault("classification_source", "llm_inferred")
        venues.append(venue)
    return venues


def load_default_catalog() -> list[dict[str, Any]]:
    return _load_json_catalog(DATA_DIR / "default_venues.json", "default")


def load_packaged_ccf_catalog() -> list[dict[str, Any]]:
    return _load_json_catalog(DATA_DIR / "ccf_venues.json", "ccf")


def load_custom_catalog() -> list[dict[str, Any]]:
    return _load_json_catalog(DATA_DIR / "custom_venues.json", "custom")


def load_openreview_catalog() -> list[dict[str, Any]]:
    venues: list[dict[str, Any]] = []
    iclr_json = REFERENCE_ROOT / "ICLR2026-Guide-CN" / "ICLR2026_all_papers.json"
    if iclr_json.exists():
        venues.append({
            "id": "openreview_iclr",
            "source": "openreview",
            "name": "ICLR",
            "full_name": "International Conference on Learning Representations",
            "type": "conference",
            "rank": "high-level",
            "field": "Artificial Intelligence",
            "field_key": "AI",
            "address": "",
            "years": [2026, 2025, 2024, 2023],
            "classification_source": "llm_inferred",
        })
    return venues


def load_catalog() -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    ccf_venues = load_packaged_ccf_catalog() + load_ccf_catalog()
    ccf_names = {_venue_name_key(venue.get("name", "")) for venue in ccf_venues}
    default_venues = [
        venue
        for venue in load_default_catalog()
        if venue.get("id") == "openreview_iclr" or _venue_name_key(venue.get("name", "")) not in ccf_names
    ]
    for venue in default_venues + load_openreview_catalog() + ccf_venues + load_custom_catalog():
        by_id[venue["id"]] = venue
    return sorted(by_id.values(), key=lambda item: (item["source"], item["field"], item["type"], item["rank"], item["name"]))


def catalog_by_id() -> dict[str, dict[str, Any]]:
    catalog = {venue["id"]: venue for venue in load_catalog()}
    if "openreview_iclr" in catalog:
        catalog["openreview_iclr_2026"] = {**catalog["openreview_iclr"], "id": "openreview_iclr_2026"}
    return catalog
