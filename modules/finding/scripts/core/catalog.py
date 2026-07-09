from __future__ import annotations


# ---- venue catalog ----

# ---- catalog.py ----

import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from finding_runtime import DATA_DIR


NAME_ALIASES = {
    "kdd": "sigkdd",
}


STABLE_VENUE_ALIASES = {
    "dblp_acl": ("acl", "annual meeting of the association for computational linguistics"),
    "dblp_aaai": ("aaai", "aaai conference on artificial intelligence"),
    "dblp_cikm": ("cikm", "acm international conference on information and knowledge management"),
    "dblp_cvpr": ("cvpr", "computer vision and pattern recognition"),
    "dblp_eccv": ("eccv", "european conference on computer vision"),
    "dblp_emnlp": ("emnlp", "empirical methods in natural language processing"),
    "dblp_iccv": ("iccv", "international conference on computer vision"),
    "dblp_icml": ("icml", "international conference on machine learning"),
    "dblp_ijcai": ("ijcai", "international joint conference on artificial intelligence"),
    "dblp_kdd": ("sigkdd", "knowledge discovery and data mining"),
    "dblp_sigir": ("sigir", "information retrieval"),
    "dblp_www": ("www", "world wide web conference"),
    "openreview_iclr": ("iclr", "learning representations"),
    "openreview_neurips": ("neurips", "neural information processing systems"),
}


def _safe_id(*parts: str) -> str:
    joined = "_".join(str(part or "").strip().lower() for part in parts)
    allowed = []
    for ch in joined:
        allowed.append(ch if ch.isalnum() else "_")
    return "_".join(filter(None, "".join(allowed).split("_")))




def _venue_name_key(name: str) -> str:
    normalized = " ".join(str(name or "").strip().lower().split())
    return _safe_id(NAME_ALIASES.get(normalized, normalized))


SOURCE_PRIORITY = {
    "openreview": 0,
    "ccf": 1,
    "default": 2,
    "dblp": 2,
    "custom": 3,
}


def _catalog_source_priority(venue: dict[str, Any]) -> int:
    return SOURCE_PRIORITY.get(str(venue.get("source") or "").strip().lower(), 10)


def _venue_identity_key(venue: dict[str, Any]) -> str:
    full_name = _venue_name_key(str(venue.get("full_name") or ""))
    if full_name:
        return f"full:{full_name}"
    name = _venue_name_key(str(venue.get("name") or venue.get("id") or ""))
    return f"name:{name}"


def _merge_year_values(*values: Any) -> list[int]:
    years: list[int] = []
    for value in values:
        items = value if isinstance(value, list) else [value]
        for item in items:
            try:
                year = int(item)
            except (TypeError, ValueError):
                continue
            if 1900 <= year <= 2100 and year not in years:
                years.append(year)
    return sorted(years, reverse=True)


def _venue_alias_record(venue: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": venue.get("id", ""),
        "source": venue.get("source", ""),
        "name": venue.get("name", ""),
        "full_name": venue.get("full_name", ""),
        "type": venue.get("type", ""),
        "rank": venue.get("rank", ""),
        "field": venue.get("field", ""),
        "field_key": venue.get("field_key", ""),
        "address": venue.get("address", ""),
        "years": _merge_year_values(venue.get("years", [])),
        "classification_source": venue.get("classification_source", ""),
    }


def _append_alias(aliases: list[dict[str, Any]], alias: dict[str, Any], canonical_id: str) -> None:
    alias_id = str(alias.get("id") or "").strip()
    if not alias_id or alias_id == canonical_id:
        return
    if any(str(item.get("id") or "") == alias_id for item in aliases):
        return
    aliases.append(alias)


def _merge_catalog_entry(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    primary, secondary = left, right
    if (_catalog_source_priority(right), str(right.get("id") or "")) < (_catalog_source_priority(left), str(left.get("id") or "")):
        primary, secondary = right, left

    merged = dict(primary)
    merged["years"] = _merge_year_values(primary.get("years", []), secondary.get("years", []))
    if not merged.get("address") and str(merged.get("source") or "").lower() != "openreview":
        merged["address"] = secondary.get("address", "")

    metadata = dict(secondary.get("metadata") or {})
    metadata.update(dict(primary.get("metadata") or {}))
    merged_sources = []
    for venue in (primary, secondary):
        source = str(venue.get("source") or "").strip()
        if source and source not in merged_sources:
            merged_sources.append(source)
    if merged_sources:
        metadata["merged_sources"] = merged_sources
    if metadata:
        merged["metadata"] = metadata

    aliases: list[dict[str, Any]] = []
    for venue in (primary, secondary):
        for alias in venue.get("aliases", []) if isinstance(venue.get("aliases"), list) else []:
            if isinstance(alias, dict):
                _append_alias(aliases, alias, str(merged.get("id") or ""))
        _append_alias(aliases, _venue_alias_record(venue), str(merged.get("id") or ""))
    if aliases:
        merged["aliases"] = aliases
    else:
        merged.pop("aliases", None)
    return merged


def _merge_catalog_by_identity(venues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    identity_to_id: dict[str, str] = {}
    for venue in venues:
        venue_id = str(venue.get("id") or "").strip()
        if not venue_id:
            continue
        identity = _venue_identity_key(venue)
        existing_id = identity_to_id.get(identity)
        if existing_id and existing_id in by_id:
            merged = _merge_catalog_entry(by_id[existing_id], venue)
            merged_id = str(merged.get("id") or existing_id)
            if merged_id != existing_id:
                by_id.pop(existing_id, None)
            by_id[merged_id] = merged
            identity_to_id[identity] = merged_id
        else:
            by_id[venue_id] = venue
            identity_to_id[identity] = venue_id
    return list(by_id.values())


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


def _copy_catalog_row(row: dict[str, Any]) -> dict[str, Any]:
    copied = dict(row)
    aliases = copied.get("aliases")
    if isinstance(aliases, list):
        copied["aliases"] = [dict(alias) if isinstance(alias, dict) else alias for alias in aliases]
    return copied


@lru_cache(maxsize=1)
def _load_catalog_cached() -> tuple[dict[str, Any], ...]:
    ccf_venues = _load_json_catalog(DATA_DIR / "ccf_venues.json", "ccf")
    ccf_names = {_venue_name_key(venue.get("name", "")) for venue in ccf_venues}
    default_venues = [
        venue
        for venue in _load_json_catalog(DATA_DIR / "default_venues.json", "default")
        if venue.get("id") == "openreview_iclr" or _venue_name_key(venue.get("name", "")) not in ccf_names
    ]
    venues = _merge_catalog_by_identity(default_venues + ccf_venues + _load_json_catalog(DATA_DIR / "custom_venues.json", "custom"))
    return tuple(sorted(venues, key=lambda item: (item["source"], item["field"], item["type"], item["rank"], item["name"])))


def load_catalog() -> list[dict[str, Any]]:
    return [_copy_catalog_row(venue) for venue in _load_catalog_cached()]


@lru_cache(maxsize=1)
def _catalog_by_id_cached() -> dict[str, dict[str, Any]]:
    catalog = {venue["id"]: _copy_catalog_row(venue) for venue in _load_catalog_cached()}
    for venue in list(catalog.values()):
        canonical_id = str(venue.get("id") or "")
        for alias in venue.get("aliases", []) if isinstance(venue.get("aliases"), list) else []:
            if not isinstance(alias, dict):
                continue
            alias_id = str(alias.get("id") or "").strip()
            if alias_id and alias_id not in catalog:
                catalog[alias_id] = {**venue, "id": alias_id, "canonical_id": canonical_id}
    for stable_id, needles in STABLE_VENUE_ALIASES.items():
        if stable_id in catalog:
            continue
        for canonical_id, venue in list(catalog.items()):
            text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
            if all(needle in text for needle in needles):
                catalog[stable_id] = {**venue, "id": stable_id, "canonical_id": canonical_id}
                break
    if "openreview_iclr" in catalog:
        catalog["openreview_iclr_2026"] = {**catalog["openreview_iclr"], "id": "openreview_iclr_2026"}
    custom_fallbacks = {
        "dblp_icml": {
            "id": "dblp_icml",
            "source": "dblp",
            "name": "ICML",
            "full_name": "International Conference on Machine Learning",
            "type": "conference",
            "rank": "high-level",
            "field": "Artificial Intelligence / Machine Learning",
            "field_key": "AI",
            "address": "https://dblp.uni-trier.de/db/conf/icml/",
            "classification_source": "topic_policy",
        },
        "dblp_kdd": {
            "id": "dblp_kdd",
            "source": "dblp",
            "name": "KDD",
            "full_name": "ACM SIGKDD Conference on Knowledge Discovery and Data Mining",
            "type": "conference",
            "rank": "high-level",
            "field": "Data Mining / Recommendation",
            "field_key": "DM_CS",
            "address": "https://dblp.uni-trier.de/db/conf/kdd/",
            "classification_source": "topic_policy",
        },
    }
    for venue_id, venue in custom_fallbacks.items():
        catalog.setdefault(venue_id, {**venue, "years": list(range(date.today().year, date.today().year - 5, -1))})
    if "openreview_neurips" not in catalog:
        for venue in catalog.values():
            text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
            if "neurips" in text or "neural information processing systems" in text:
                catalog["openreview_neurips"] = {**venue, "id": "openreview_neurips", "source": "openreview"}
                break
    return catalog


def catalog_by_id() -> dict[str, dict[str, Any]]:
    return {venue_id: _copy_catalog_row(venue) for venue_id, venue in _catalog_by_id_cached().items()}


# Backward-compatible dotted imports for callers that still use the old package layout.
def _register_compat_aliases(*aliases: str) -> None:
    import sys as _sys
    _module = _sys.modules.get(__name__)
    if _module is None:
        return
    globals().setdefault("__path__", [])
    for _alias in aliases:
        _sys.modules.setdefault(_alias, _module)

_register_compat_aliases('catalog.venue_catalog')
