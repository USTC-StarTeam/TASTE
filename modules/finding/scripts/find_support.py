from __future__ import annotations


# ---- catalog.py ----

import json
from datetime import date
from functools import lru_cache
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


def _copy_catalog_row(row: dict[str, Any]) -> dict[str, Any]:
    copied = dict(row)
    aliases = copied.get("aliases")
    if isinstance(aliases, list):
        copied["aliases"] = [dict(alias) if isinstance(alias, dict) else alias for alias in aliases]
    return copied


@lru_cache(maxsize=1)
def _load_catalog_cached() -> tuple[dict[str, Any], ...]:
    ccf_venues = load_packaged_ccf_catalog() + load_ccf_catalog()
    ccf_names = {_venue_name_key(venue.get("name", "")) for venue in ccf_venues}
    default_venues = [
        venue
        for venue in load_default_catalog()
        if venue.get("id") == "openreview_iclr" or _venue_name_key(venue.get("name", "")) not in ccf_names
    ]
    venues = _merge_catalog_by_identity(default_venues + load_openreview_catalog() + ccf_venues + load_custom_catalog())
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


# ---- local_index.py ----

import json
from pathlib import Path
from typing import Any

from auto_research.paths import LOCAL_DATABASE_DIR


def venue_cache_key(venue: dict[str, Any]) -> str:
    name = str(venue.get("name") or "").strip().lower()
    if name:
        key = "".join(char for char in name if char.isalnum())
        if key:
            return key
    full_name = str(venue.get("full_name") or "").strip().lower()
    words = [word for word in full_name.split() if word[:1].isalnum()]
    if words:
        return "".join(char for char in words[0] if char.isalnum()) or "unknown"
    venue_id = str(venue.get("id") or "unknown").strip().lower()
    return "".join(char if char.isalnum() else "_" for char in venue_id).strip("_") or "unknown"


def _venue_id_candidates(venue: dict[str, Any]) -> list[str]:
    venue_id = str(venue.get("id") or "").strip()
    candidates = []
    cache_key = venue_cache_key(venue)
    if cache_key:
        candidates.append(cache_key)
    if venue_id:
        candidates.append(venue_id)
        for suffix in ("_2026", "_2025", "_2024", "_2023", "_2022"):
            if venue_id.endswith(suffix):
                candidates.append(venue_id[: -len(suffix)])
    name = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    if "iclr" in name or "learning representations" in name:
        candidates.append("openreview_iclr")
    if "neurips" in name or "neural information processing systems" in name:
        candidates.append("openreview_neurips")
    return list(dict.fromkeys(item for item in candidates if item))


def load_local_venue_year(venue: dict[str, Any], year: int, root: Path = LOCAL_DATABASE_DIR) -> dict[str, Any] | None:
    for venue_id in _venue_id_candidates(venue):
        directory = root / venue_id / str(year)
        papers_path = directory / "papers.json"
        summary_path = directory / "category_summary.json"
        if not papers_path.exists() or not summary_path.exists():
            continue
        papers_data = json.loads(papers_path.read_text(encoding="utf-8"))
        summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
        manifest_path = directory / "manifest.json"
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        except Exception:
            manifest_data = {}
        papers = papers_data.get("papers", [])
        if not isinstance(papers, list):
            continue
        metadata_audit = (
            manifest_data.get("audit")
            or manifest_data.get("metadata_completeness_audit")
            or papers_data.get("metadata_completeness_audit")
            or summary_data.get("metadata_completeness_audit")
            or {}
        )
        return {
            "venue_id": venue_id,
            "year": year,
            "directory": str(directory),
            "papers_path": str(papers_path),
            "category_summary_path": str(summary_path),
            "manifest_path": str(manifest_path) if manifest_path.exists() else "",
            "manifest": manifest_data,
            "metadata_completeness_audit": metadata_audit,
            "source_adapter": papers_data.get("source_adapter") or summary_data.get("source_adapter") or manifest_data.get("adapter") or "local_database",
            "papers": papers,
            "category_summary": summary_data,
            "paper_count": len(papers),
        }
    return None


# ---- local_cache.py ----

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from auto_research.storage import read_json, write_json



SCHEMA_VERSION = "1.0"


def _json_path(path: Path) -> str:
    return path.as_posix()


def cache_directory(venue: dict[str, Any], year: int, root: Path = LOCAL_DATABASE_DIR) -> Path:
    return root / venue_cache_key(venue) / str(year)


def _first_existing_directory(venue: dict[str, Any], year: int, root: Path = LOCAL_DATABASE_DIR) -> Path:
    for venue_id in _venue_id_candidates(venue):
        directory = root / venue_id / str(year)
        if (directory / "papers.json").exists():
            return directory
    return cache_directory(venue, year, root)


def normalize_cached_paper(paper: dict[str, Any], venue: dict[str, Any], year: int, adapter: str = "") -> dict[str, Any]:
    metadata = dict(paper.get("metadata") or {})
    metadata.setdefault("venue_id", venue.get("id", ""))
    if adapter:
        metadata.setdefault("source_adapter", adapter)
    categories = paper.get("categories")
    if not isinstance(categories, list):
        categories = []
    category = str(paper.get("primary_area") or paper.get("category") or paper.get("track") or "")
    if category and category not in categories:
        categories = [category, *categories]
    return {
        "id": str(paper.get("id") or ""),
        "source": str(paper.get("source") or adapter or ""),
        "title": str(paper.get("title") or "Untitled"),
        "authors": paper.get("authors") if isinstance(paper.get("authors"), str) else ", ".join(str(item) for item in paper.get("authors", []) if item),
        "abstract": str(paper.get("abstract") or ""),
        "url": str(paper.get("url") or ""),
        "pdf_url": str(paper.get("pdf_url") or ""),
        "venue": str(paper.get("venue") or venue.get("name") or ""),
        "venue_id": str(paper.get("venue_id") or venue.get("id") or ""),
        "year": int(paper.get("year") or year),
        "category": str(paper.get("category") or ""),
        "categories": categories,
        "primary_area": str(paper.get("primary_area") or ""),
        "track": str(paper.get("track") or ""),
        "keywords": paper.get("keywords") if isinstance(paper.get("keywords"), list) else [],
        "classification_source": str(paper.get("classification_source") or "llm_inferred"),
        "metadata": metadata,
    }


def _category_key(paper: dict[str, Any]) -> str:
    return str(paper.get("primary_area") or paper.get("category") or paper.get("track") or "").strip()


def build_category_summary(venue: dict[str, Any], year: int, papers: list[dict[str, Any]], adapter: str) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for paper in papers:
        category = _category_key(paper)
        if category:
            buckets.setdefault(category, []).append(paper)
    counts = Counter({name: len(items) for name, items in buckets.items()})
    return {
        "schema_version": SCHEMA_VERSION,
        "venue_id": venue.get("id", ""),
        "venue": venue.get("name", ""),
        "full_name": venue.get("full_name", venue.get("name", "")),
        "year": year,
        "source": venue.get("source", ""),
        "source_adapter": adapter,
        "paper_count": len(papers),
        "category_count": len(buckets),
        "category_counts": dict(counts),
        "category_summary": [
            {
                "name": name,
                "count": len(items),
                "sample_titles": [str(item.get("title") or "") for item in items[:5]],
                "sample_keywords": [
                    str(keyword)
                    for item in items[:20]
                    for keyword in (item.get("keywords") if isinstance(item.get("keywords"), list) else [])
                ][:20],
            }
            for name, items in sorted(buckets.items(), key=lambda row: (-len(row[1]), row[0].lower()))
        ],
        "built_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source_file": "papers.json",
    }


def load_cached_venue_year(venue: dict[str, Any], year: int, root: Path = LOCAL_DATABASE_DIR) -> dict[str, Any] | None:
    directory = _first_existing_directory(venue, year, root)
    papers_path = directory / "papers.json"
    if not papers_path.exists():
        return None
    papers_data = read_json(papers_path, {})
    papers = papers_data.get("papers", [])
    if not isinstance(papers, list):
        return None
    summary_path = directory / "category_summary.json"
    report_path = directory / "source_report.json"
    summary = read_json(summary_path, {}) if summary_path.exists() else {}
    report = read_json(report_path, {}) if report_path.exists() else {}
    return {
        "venue_id": papers_data.get("venue_id") or venue.get("id", ""),
        "year": year,
        "directory": _json_path(directory),
        "papers_path": _json_path(papers_path),
        "category_summary_path": _json_path(summary_path) if summary_path.exists() else "",
        "source_report_path": _json_path(report_path) if report_path.exists() else "",
        "papers": papers,
        "category_summary": summary,
        "source_report": report,
        "paper_count": len(papers),
        "source_adapter": papers_data.get("source_adapter") or report.get("source_adapter") or "local_database",
    }


def write_venue_year_cache(
    venue: dict[str, Any],
    year: int,
    papers: list[dict[str, Any]],
    adapter: str,
    root: Path = LOCAL_DATABASE_DIR,
) -> dict[str, Any]:
    directory = cache_directory(venue, year, root)
    normalized = [normalize_cached_paper(paper, venue, year, adapter) for paper in papers]
    normalized = [paper for paper in normalized if paper.get("title")]
    fetched_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    paper_payload = {
        "schema_version": SCHEMA_VERSION,
        "venue_id": venue.get("id", ""),
        "venue": venue.get("name", ""),
        "full_name": venue.get("full_name", venue.get("name", "")),
        "year": year,
        "source": venue.get("source", ""),
        "source_adapter": adapter,
        "fetched_at": fetched_at,
        "paper_count": len(normalized),
        "papers": normalized,
    }
    source_report = {
        "schema_version": SCHEMA_VERSION,
        "venue_id": venue.get("id", ""),
        "venue": venue.get("name", ""),
        "year": year,
        "source_adapter": adapter,
        "paper_count": len(normalized),
        "fetched_at": fetched_at,
        "cache_directory": _json_path(directory),
    }
    category_summary = build_category_summary(venue, year, normalized, adapter)
    write_json(directory / "papers.json", paper_payload)
    write_json(directory / "source_report.json", source_report)
    write_json(directory / "category_summary.json", category_summary)
    return {
        "venue_id": venue.get("id", ""),
        "year": year,
        "directory": _json_path(directory),
        "papers_path": _json_path(directory / "papers.json"),
        "category_summary_path": _json_path(directory / "category_summary.json"),
        "source_report_path": _json_path(directory / "source_report.json"),
        "papers": normalized,
        "category_summary": category_summary,
        "source_report": source_report,
        "paper_count": len(normalized),
        "source_adapter": adapter,
    }


# ---- local_rank.py ----

import math
import re
from collections import Counter
from typing import Any, Callable


GENERIC_TERMS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it", "of", "on", "or", "that", "the", "their", "to", "with",
    "ai", "research", "paper", "papers", "tool", "tools", "system", "systems", "model", "models", "method", "methods", "benchmark", "benchmarks",
    "using", "use", "used", "based", "including", "include", "improve", "interested", "prefer", "practical", "generic", "directly",
}


def _tokens(text: str) -> list[str]:
    raw = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9][a-zA-Z0-9_.-]{1,}", (text or "").lower())
    return [term.strip(".,;:!?()[]{}\"'") for term in raw if term.strip(".,;:!?()[]{}\"'") not in GENERIC_TERMS]


def _paper_text(paper: dict[str, Any]) -> str:
    title = str(paper.get("title") or "")
    abstract = str(paper.get("abstract") or "")
    category = str(paper.get("category") or "")
    categories = paper.get("categories") or paper.get("metadata", {}).get("all_categories") or []
    if isinstance(categories, list):
        category_text = " ".join(str(item) for item in categories)
    else:
        category_text = str(categories or "")
    return " ".join([title, title, title, abstract, category, category_text])


def _profile_phrases(profile_text: str) -> list[str]:
    phrases: list[str] = []
    for part in re.split(r"[\n,;，；。.!?、/]+", profile_text or ""):
        text = " ".join(str(part).lower().split())
        if len(text) >= 4:
            phrases.append(text)
    terms = [term for term in _tokens(profile_text) if re.fullmatch(r"[a-zA-Z0-9_.-]+", term)]
    for size in range(2, min(5, len(terms)) + 1):
        for index in range(0, len(terms) - size + 1):
            phrases.append(" ".join(terms[index:index + size]))
    return list(dict.fromkeys(phrase for phrase in phrases if len(phrase) >= 4))


def rank_papers_tfidf(
    papers: list[dict[str, Any]],
    query: str,
    *,
    per_category_limit: int = 100,
    global_limit: int = 200,
    ranking_bonus: Callable[[dict[str, Any]], float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not papers:
        return [], {"method": "adaptive_profile_similarity", "input_count": 0, "selected_count": 0}

    global_cap = len(papers) if int(global_limit or 0) <= 0 else max(1, min(len(papers), int(global_limit)))
    per_category_cap = len(papers) if int(per_category_limit or 0) <= 0 else max(1, int(per_category_limit))

    profile_signals = _tokens(query)
    profile_phrases = _profile_phrases(query)
    if not profile_signals:
        selected = []
        for index, paper in enumerate(papers[:global_cap]):
            row = dict(
                paper,
                local_score=0.0,
                local_tfidf_score=0.0,
                local_phrase_adjustment=0.0,
                local_profile_phrase_match_count=0,
                local_rank=index + 1,
                local_filter_reason="No research profile text; kept by source order.",
            )
            bonus = max(0.0, float(ranking_bonus(row))) if ranking_bonus else 0.0
            row["local_quality_bonus"] = round(bonus, 6)
            row["local_rank_score"] = round(bonus, 6)
            selected.append(row)
        selected.sort(key=lambda item: float(item.get("local_rank_score") or 0), reverse=True)
        for index, item in enumerate(selected, 1):
            item["local_rank"] = index
        return selected, {"method": "adaptive_profile_similarity", "input_count": len(papers), "selected_count": len(selected), "global_limit": global_limit, "effective_global_limit": global_cap, "adaptive_profile_signal_count": 0, "adaptive_profile_phrase_count": 0, "profile_signal_source": "current research_interest/profile", "ranking_bonus_applied": bool(ranking_bonus)}

    paper_texts = [_paper_text(paper) for paper in papers]
    doc_terms = [_tokens(text) for text in paper_texts]
    doc_freq: Counter[str] = Counter()
    for terms in doc_terms:
        doc_freq.update(set(terms))

    total_docs = len(papers)
    query_counts = Counter(profile_signals)
    query_weights = {
        term: (1.0 + math.log(count)) * (math.log((total_docs + 1) / (doc_freq.get(term, 0) + 1)) + 1.0)
        for term, count in query_counts.items()
    }
    query_norm = math.sqrt(sum(weight * weight for weight in query_weights.values())) or 1.0

    ranked: list[dict[str, Any]] = []
    for paper, text, terms in zip(papers, paper_texts, doc_terms, strict=False):
        counts = Counter(terms)
        dot = 0.0
        doc_norm_sq = 0.0
        for term, count in counts.items():
            idf = math.log((total_docs + 1) / (doc_freq.get(term, 0) + 1)) + 1.0
            weight = (1.0 + math.log(count)) * idf
            doc_norm_sq += weight * weight
            dot += weight * query_weights.get(term, 0.0)
        tfidf_score = dot / ((math.sqrt(doc_norm_sq) or 1.0) * query_norm)
        lowered = (text or "").lower()
        phrase_matches = [phrase for phrase in profile_phrases if phrase in lowered]
        adjustment = min(0.35, 0.06 * len(phrase_matches))
        score = max(0.0, tfidf_score + adjustment)
        row = dict(paper)
        row["local_score"] = round(score, 6)
        row["local_tfidf_score"] = round(tfidf_score, 6)
        row["local_phrase_adjustment"] = round(adjustment, 6)
        row["local_profile_phrase_match_count"] = len(phrase_matches)
        row["local_filter_reason"] = "Adaptive recall similarity from the current research interest/profile; used only for candidate retrieval, not as strong evidence."
        bonus = max(0.0, float(ranking_bonus(row))) if ranking_bonus else 0.0
        row["local_quality_bonus"] = round(bonus, 6)
        row["local_rank_score"] = round(score + bonus, 6)
        ranked.append(row)

    ranked.sort(key=lambda item: (float(item.get("local_rank_score") or item.get("local_score") or 0), float(item.get("local_score") or 0)), reverse=True)
    by_category: dict[str, list[dict[str, Any]]] = {}
    for item in ranked:
        category = str(item.get("category") or item.get("metadata", {}).get("primary_category") or "unknown")
        bucket = by_category.setdefault(category, [])
        if len(bucket) < per_category_cap:
            bucket.append(item)

    balanced = [item for bucket in by_category.values() for item in bucket]
    balanced.sort(key=lambda item: (float(item.get("local_rank_score") or item.get("local_score") or 0), float(item.get("local_score") or 0)), reverse=True)
    selected = balanced[:global_cap]

    # Some venue adapters, such as proceedings-style ICML/DBLP sources, do not
    # expose meaningful fine-grained categories. In that case a small
    # per-category cap can silently dominate the global recall target and keep
    # only the first ~200 papers even when the Find page asks for a much larger
    # detail-scoring pool. Preserve the balancing behavior, then fill the
    # remaining global budget from the source-ranked list.
    if len(selected) < min(global_cap, len(ranked)):
        seen = {str(item.get("id") or item.get("url") or item.get("title") or "") for item in selected}
        for item in ranked:
            key = str(item.get("id") or item.get("url") or item.get("title") or "")
            if key in seen:
                continue
            selected.append(item)
            seen.add(key)
            if len(selected) >= global_cap:
                break

    for index, item in enumerate(selected, 1):
        item["local_rank"] = index

    return selected, {
        "method": "adaptive_profile_similarity",
        "input_count": len(papers),
        "selected_count": len(selected),
        "global_limit": global_limit,
        "effective_global_limit": global_cap,
        "per_category_limit": per_category_limit,
        "effective_per_category_limit": per_category_cap,
        "balanced_selected_count": len(balanced),
        "category_counts": {category: len(items) for category, items in by_category.items()},
        "adaptive_profile_signal_count": len(profile_signals),
        "adaptive_profile_phrase_count": len(profile_phrases),
        "profile_signal_source": "current research_interest/profile",
        "ranking_bonus_applied": bool(ranking_bonus),
    }


# ---- quality.py ----

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from auto_research.paths import DATA_DIR, LEGACY_LOCAL_DATABASE_DIR, LOCAL_DATABASE_DIR


QUALITY_DATA_DIR = Path(os.environ.get("TASTE_QUALITY_DATA_DIR") or DATA_DIR / "quality").expanduser()
CONFERENCE_QUALITY_TABLE = QUALITY_DATA_DIR / "conference_quality_levels.json"
JOURNAL_QUALITY_TABLE = QUALITY_DATA_DIR / "journal_quality_levels.json"


def _candidate_tables(path: Path) -> list[Path]:
    return [
        path,
        LOCAL_DATABASE_DIR / path.name,
        LEGACY_LOCAL_DATABASE_DIR / path.name,
    ]


def _load_json(path: Path) -> dict[str, Any]:
    for candidate in _candidate_tables(path):
        if not candidate.exists():
            continue
        data = json.loads(candidate.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    return {}


@lru_cache(maxsize=1)
def _conference_table() -> dict[str, Any]:
    return _load_json(CONFERENCE_QUALITY_TABLE)


@lru_cache(maxsize=1)
def _journal_table() -> dict[str, Any]:
    return _load_json(JOURNAL_QUALITY_TABLE)


def _norm(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[_/|:;,\-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _metadata(item: dict) -> dict:
    metadata = item.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _venue_ids(item: dict) -> set[str]:
    metadata = _metadata(item)
    ids = {
        str(item.get("venue_id") or ""),
        str(metadata.get("venue_id") or ""),
    }
    return {value for value in ids if value}


def _venue_names(item: dict) -> set[str]:
    return {
        _norm(item.get("venue")),
        _norm(item.get("source")),
    } - {""}


def _label_candidates(item: dict) -> set[str]:
    metadata = _metadata(item)
    values = [
        item.get("track"),
        item.get("category"),
        item.get("primary_area"),
        metadata.get("track"),
        metadata.get("category"),
        metadata.get("primary_area"),
        metadata.get("presentation_type"),
        metadata.get("presentation_label"),
    ]
    source_records = metadata.get("source_records")
    if isinstance(source_records, dict):
        for record in source_records.values():
            if isinstance(record, dict):
                values.extend([
                    record.get("track"),
                    record.get("category"),
                    record.get("primary_area"),
                    record.get("presentation_type"),
                    record.get("presentation_label"),
                ])
    return {_norm(value) for value in values if _norm(value)}


def _quality_payload(kind: str, source_file: str, tier: str, bonus: object, reason: str) -> dict[str, Any]:
    try:
        numeric_bonus = float(bonus)
    except (TypeError, ValueError):
        numeric_bonus = 0.0
    return {
        "quality_kind": kind,
        "quality_tier": tier or "unknown",
        "quality_bonus_available": round(max(0.0, numeric_bonus), 2),
        "quality_bonus": 0.0,
        "quality_source": source_file,
        "quality_reason": reason,
    }


def _lookup_conference_quality(item: dict) -> dict[str, Any] | None:
    table = _conference_table()
    conferences = table.get("conferences")
    if not isinstance(conferences, dict):
        return None
    venue_ids = _venue_ids(item)
    venue_names = _venue_names(item)
    labels = _label_candidates(item)
    year = str(item.get("year") or "")
    for key, conference in conferences.items():
        if not isinstance(conference, dict):
            continue
        table_ids = {str(value) for value in conference.get("venue_ids", []) if value}
        table_names = {_norm(value) for value in conference.get("names", []) if _norm(value)}
        if not (venue_ids & table_ids or venue_names & table_names or _norm(key) in venue_names):
            continue
        years = conference.get("years") if isinstance(conference.get("years"), dict) else {}
        aliases = years.get(year, {}).get("label_aliases", {}) if isinstance(years.get(year), dict) else {}
        if isinstance(aliases, dict):
            for label in labels:
                mapping = aliases.get(label)
                if isinstance(mapping, dict):
                    return _quality_payload(
                        "conference",
                        "conference_quality_levels.json",
                        str(mapping.get("tier") or "unknown"),
                        mapping.get("bonus", 0.0),
                        f"Matched conference label '{label}' for {key} {year}.",
                    )
        return _quality_payload(
            "conference",
            "conference_quality_levels.json",
            "unknown",
            0.0,
            f"Conference quality table has {key} {year}, but no item label matched.",
        )
    return None


def _lookup_journal_quality(item: dict) -> dict[str, Any] | None:
    table = _journal_table()
    journals = table.get("journals")
    if not isinstance(journals, dict):
        return None
    metadata = _metadata(item)
    source = str(item.get("source") or "")
    slug = str(metadata.get("journal_slug") or "").strip()
    candidates = set(_venue_ids(item))
    if source == "nature" and slug:
        candidates.add(f"nature_family_{slug}")
    if source == "science" and slug:
        candidates.add(f"science_family_{slug}")
    for journal_id in candidates:
        journal = journals.get(journal_id)
        if isinstance(journal, dict):
            return _quality_payload(
                "journal",
                "journal_quality_levels.json",
                str(journal.get("tier") or "unknown"),
                journal.get("bonus", 0.0),
                str(journal.get("notes") or f"Matched journal quality table entry {journal_id}."),
            )
    venue_name = _norm(item.get("venue"))
    if venue_name:
        for journal_id, journal in journals.items():
            if not isinstance(journal, dict):
                continue
            names = {_norm(value) for value in journal.get("names", []) if _norm(value)}
            if venue_name in names:
                return _quality_payload(
                    "journal",
                    "journal_quality_levels.json",
                    str(journal.get("tier") or "unknown"),
                    journal.get("bonus", 0.0),
                    str(journal.get("notes") or f"Matched journal quality table entry {journal_id}."),
                )
    return None


def attach_quality_metadata(item: dict) -> dict:
    quality = _lookup_journal_quality(item) or _lookup_conference_quality(item)
    if not quality:
        return item
    item.update(quality)
    metadata = item.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata["quality"] = dict(quality)
    return item


def attach_quality_metadata_many(items: list[dict]) -> list[dict]:
    return [attach_quality_metadata(item) for item in items]


# ---- category_select.py ----

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from auto_research.llm import LLMClient, fallback_score
from auto_research.models import AppConfig
from auto_research.paths import ROOT, WORKFLOW_RUNTIME_DIR
from auto_research.storage import read_json, write_json


def _interest_text(config: AppConfig) -> str:
    return "\n".join(part for part in [config.research_interest, config.researcher_profile] if part).strip()


def _category_entries(category_summary: dict[str, Any]) -> list[dict[str, Any]]:
    entries = category_summary.get("category_summary", [])
    if not isinstance(entries, list):
        return []
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        result.append({
            "name": name,
            "count": int(entry.get("count") or 0),
            "sample_titles": [str(item) for item in (entry.get("sample_titles") or [])[:5]],
            "sample_keywords": [str(item) for item in (entry.get("sample_keywords") or [])[:20]],
        })
    return result


def _compact_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": entry["name"],
            "count": entry["count"],
            "sample_titles": entry["sample_titles"],
            "sample_keywords": entry["sample_keywords"],
        }
        for entry in entries
    ]


CATEGORY_SELECT_TEMPERATURE = 0.0
CATEGORY_SELECT_CACHE_SCHEMA_VERSION = "find_category_select_cache_v2"
CATEGORY_SELECT_CACHE_MAX_ENTRIES = 2000


def _use_llm_category_select(config: AppConfig | None = None) -> bool:
    disabled = os.environ.get("DISABLE_LLM_CATEGORY_SELECT")
    if disabled is not None and disabled.lower() in {"1", "true", "yes", "on"}:
        return False
    value = os.environ.get("USE_LLM_CATEGORY_SELECT")
    if value is not None:
        return value.lower() in {"1", "true", "yes", "on", "force"}
    provider = str(getattr(config, "provider", "") or "").lower()
    return provider not in {"", "mock"}


def _json_or_none(llm: LLMClient, prompt: str, *, temperature: float | None = None) -> Any | None:
    if not hasattr(llm, "json_or_none"):
        return None
    try:
        return llm.json_or_none(prompt, temperature=temperature)
    except TypeError:
        return llm.json_or_none(prompt)


def _project_cache_dir() -> Any | None:
    project = (os.environ.get("PROJECT_ID") or os.environ.get("DEFAULT_PROJECT_ID") or "").strip()
    if not project:
        return None
    root = Path(os.environ.get("WORKSPACE_ROOT") or ROOT).expanduser()
    return root / "projects" / project / "planning" / "finding" / "cache"


def _category_select_cache_enabled(config: AppConfig | None = None) -> bool:
    disabled = os.environ.get("DISABLE_FIND_CATEGORY_SELECT_CACHE")
    if disabled is not None and disabled.lower() in {"1", "true", "yes", "on"}:
        return False
    explicit = os.environ.get("USE_FIND_CATEGORY_SELECT_CACHE")
    if explicit is not None:
        return explicit.lower() in {"1", "true", "yes", "on", "force"}
    return _project_cache_dir() is not None


def _category_select_cache_path(config: AppConfig | None = None) -> Any | None:
    if not _category_select_cache_enabled(config):
        return None
    project_dir = _project_cache_dir()
    if project_dir is not None:
        return project_dir / "category_selection.json"
    return WORKFLOW_RUNTIME_DIR / "state" / "find_category_selection_cache.json"


def _normalized_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _llm_identity(llm: LLMClient) -> dict[str, Any]:
    summary = llm.summary() if hasattr(llm, "summary") else {}
    if not isinstance(summary, dict):
        summary = {}
    return {
        "role": str(summary.get("role") or getattr(llm, "role", "") or ""),
        "provider": str(summary.get("provider") or getattr(llm, "provider", "") or ""),
        "base_url": str(summary.get("base_url") or getattr(llm, "base_url", "") or ""),
        "model": str(summary.get("model") or getattr(llm, "model", "") or ""),
        "api_mode": str(summary.get("api_mode") or getattr(llm, "api_mode", "") or ""),
    }


def _cache_entries_fingerprint(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted = []
    for entry in entries:
        compacted.append({
            "name": _normalized_text(entry.get("name")),
            "count": int(entry.get("count") or 0),
            "sample_titles": [_normalized_text(item) for item in (entry.get("sample_titles") or [])[:5]],
            "sample_keywords": [_normalized_text(item) for item in (entry.get("sample_keywords") or [])[:20]],
        })
    return compacted


def _category_select_cache_key(
    category_summary: dict[str, Any],
    config: AppConfig,
    llm: LLMClient,
    entries: list[dict[str, Any]],
    max_categories: int,
) -> str:
    payload = {
        "schema": CATEGORY_SELECT_CACHE_SCHEMA_VERSION,
        "temperature": CATEGORY_SELECT_TEMPERATURE,
        "max_categories": int(max_categories),
        "interest": _normalized_text(_interest_text(config)),
        "venue_id": _normalized_text(category_summary.get("venue_id")),
        "venue": _normalized_text(category_summary.get("venue")),
        "year": _normalized_text(category_summary.get("year")),
        "paper_count": int(category_summary.get("paper_count") or 0),
        "llm": _llm_identity(llm),
        "entries": _cache_entries_fingerprint(entries),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_category_select_cache(config: AppConfig | None = None) -> dict[str, Any]:
    path = _category_select_cache_path(config)
    if path is None:
        return {}
    try:
        payload = read_json(path, {})
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _valid_cached_selection(cached: Any, entries: list[dict[str, Any]], max_categories: int) -> dict[str, Any] | None:
    if not isinstance(cached, dict):
        return None
    if cached.get("schema") != CATEGORY_SELECT_CACHE_SCHEMA_VERSION:
        return None
    valid_names = {entry["name"].lower(): entry["name"] for entry in entries}
    selected = _normalize_selected_rows(cached.get("selected_categories"), valid_names)
    if not selected:
        return None
    selected = selected[:max_categories]
    rejected = _build_rejected(entries, selected, cached.get("rejected_categories"))
    selected_names = {item["name"] for item in selected}
    selected_count = sum(int(entry.get("count") or 0) for entry in entries if entry.get("name") in selected_names)
    return {
        "selected_categories": selected,
        "rejected_categories": rejected,
        "selected_paper_count": selected_count,
    }


def _store_category_select_cache_entry(config: AppConfig, cache_key: str, selection: dict[str, Any]) -> None:
    path = _category_select_cache_path(config)
    if path is None or not cache_key:
        return
    payload = _load_category_select_cache(config)
    entries = payload.get("entries") if isinstance(payload.get("entries"), dict) else {}
    entries[cache_key] = {
        "schema": CATEGORY_SELECT_CACHE_SCHEMA_VERSION,
        "selected_categories": selection.get("selected_categories") or [],
        "rejected_categories": selection.get("rejected_categories") or [],
    }
    if len(entries) > CATEGORY_SELECT_CACHE_MAX_ENTRIES:
        keys = list(entries.keys())[-CATEGORY_SELECT_CACHE_MAX_ENTRIES:]
        entries = {key: entries[key] for key in keys}
    payload = {
        "schema": CATEGORY_SELECT_CACHE_SCHEMA_VERSION,
        "entries": entries,
    }
    try:
        write_json(path, payload)
    except Exception:
        return


def _normalize_selected_rows(rows: Any, valid_names: dict[str, str]) -> list[dict[str, str]]:
    if not isinstance(rows, list):
        return []
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            name = row
            reason = ""
        elif isinstance(row, dict):
            name = str(row.get("name") or row.get("category") or "").strip()
            reason = str(row.get("reason") or "").strip()
        else:
            continue
        canonical = valid_names.get(name.lower())
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        selected.append({"name": canonical, "reason": reason})
    return selected



def _min_llm_category_recall(entries: list[dict[str, Any]], max_categories: int) -> int:
    if len(entries) < 6:
        return 0
    try:
        configured = int(os.environ.get("VENUE_CATEGORY_SELECT_MIN_CATEGORIES", "6") or 6)
    except Exception:
        configured = 6
    return max(0, min(max_categories, len(entries), configured))


def _supplement_selected_for_recall(
    selected: list[dict[str, str]],
    entries: list[dict[str, Any]],
    config: AppConfig,
    max_categories: int,
) -> list[dict[str, str]]:
    target = _min_llm_category_recall(entries, max_categories)
    if target <= 0 or len(selected) >= target:
        return selected[:max_categories]
    supplemented = [dict(item) for item in selected[:max_categories]]
    seen = {item["name"] for item in supplemented}
    fallback_rows = _fallback_select(entries, config, max_categories)
    for row in fallback_rows:
        name = row.get("name", "")
        if not name or name in seen:
            continue
        reason = row.get("reason") or "Deterministic high-recall category supplement."
        supplemented.append({"name": name, "reason": f"High-recall deterministic supplement after LLM category selection; {reason}"})
        seen.add(name)
        if len(supplemented) >= target or len(supplemented) >= max_categories:
            return supplemented
    for entry in sorted(entries, key=lambda row: (-int(row.get("count") or 0), str(row.get("name") or ""))):
        name = entry["name"]
        if name in seen:
            continue
        supplemented.append({"name": name, "reason": "High-recall deterministic supplement after LLM category selection."})
        seen.add(name)
        if len(supplemented) >= target or len(supplemented) >= max_categories:
            break
    return supplemented[:max_categories]


def _fallback_select(entries: list[dict[str, Any]], config: AppConfig, max_categories: int) -> list[dict[str, str]]:
    interest = _interest_text(config)
    if not entries:
        return []
    if not interest:
        return [{"name": entry["name"], "reason": "No research profile configured; keeping category for recall."} for entry in entries[:max_categories]]

    scored = []
    for entry in entries:
        text = " ".join([
            entry["name"],
            " ".join(entry.get("sample_titles") or []),
            " ".join(entry.get("sample_keywords") or []),
        ])
        score = fallback_score(interest, text, "")
        scored.append((score, entry))
    scored.sort(key=lambda item: (item[0], item[1].get("count", 0)), reverse=True)
    def row(score: float, entry: dict[str, Any], prefix: str) -> dict[str, str]:
        return {
            "name": entry["name"],
            "reason": f"{prefix}; adaptive_score={round(score, 3)}.",
        }

    return [
        row(score, entry, "High-recall adaptive profile/category fallback")
        for score, entry in scored[:max_categories]
    ]


def _build_rejected(entries: list[dict[str, Any]], selected: list[dict[str, str]], explicit_rejected: Any = None) -> list[dict[str, str]]:
    selected_names = {item["name"] for item in selected}
    valid_names = {entry["name"].lower(): entry["name"] for entry in entries}
    rejected = _normalize_selected_rows(explicit_rejected, valid_names)
    rejected_names = {item["name"] for item in rejected}
    for entry in entries:
        name = entry["name"]
        if name not in selected_names and name not in rejected_names:
            rejected.append({"name": name, "reason": "Not selected for the current research profile."})
    return rejected


def select_relevant_categories(
    category_summary: dict[str, Any],
    config: AppConfig,
    llm: LLMClient,
    max_categories: int = 6,
) -> dict[str, Any]:
    entries = _category_entries(category_summary)
    valid_names = {entry["name"].lower(): entry["name"] for entry in entries}
    max_categories = max(1, min(max_categories, len(entries) or 1))
    interest = _interest_text(config)

    fallback_used = True
    selected = _fallback_select(entries, config, max_categories)
    rejected: list[dict[str, str]] = []
    llm_error = ""
    selection_mode = "deterministic_adaptive_profile_recall"
    cache_key = ""
    use_llm_selection = _use_llm_category_select(config) and llm.enabled and interest and entries

    if use_llm_selection:
        cache_key = _category_select_cache_key(category_summary, config, llm, entries, max_categories)
        cache_payload = _load_category_select_cache(config)
        cache_entries = cache_payload.get("entries") if isinstance(cache_payload.get("entries"), dict) else {}
        cached_selection = _valid_cached_selection(cache_entries.get(cache_key), entries, max_categories)
        if cached_selection:
            selected = cached_selection["selected_categories"]
            rejected = cached_selection["rejected_categories"]
            fallback_used = False
            selection_mode = "llm_adaptive_category_select"

    if fallback_used and use_llm_selection:
        prompt = f"""
You select venue categories for a targeted academic paper scan.

Research interest/profile:
{interest}

Venue: {category_summary.get("venue", "")} {category_summary.get("year", "")}
Total papers: {category_summary.get("paper_count", "")}

Available categories as JSON:
{json.dumps(_compact_entries(entries), ensure_ascii=False)}

Return strict JSON:
{{
  "selected_categories": [
    {{"name": "exact category name", "reason": "concise reason"}}
  ],
  "rejected_categories": [
    {{"name": "exact category name", "reason": "concise reason"}}
  ]
}}

Rules:
- Select categories that are likely to contain papers directly useful for the research profile.
- Use exact category names from the available categories list.
- Derive relevance from the current research interest/profile only; do not use a fixed global topic list.
- Prefer recall over precision at this category stage. The final paper-level LLM evidence gate will decide strong recommendations.
- Do not select a category only because it contains generic words like model, benchmark, AI, data, or theory; explain the concrete profile route it may contain.
- Include adjacent categories when the samples suggest they may hide relevant papers whose titles are not obvious.
- Usually select 2-{max_categories} categories unless the profile is very broad.
- Reasons should be brief and specific.
"""
        data = _json_or_none(llm, prompt, temperature=CATEGORY_SELECT_TEMPERATURE)
        if isinstance(data, dict):
            llm_selected = _normalize_selected_rows(data.get("selected_categories"), valid_names)
            if llm_selected:
                selected = _supplement_selected_for_recall(llm_selected[:max_categories], entries, config, max_categories)
                rejected = _build_rejected(entries, selected, data.get("rejected_categories"))
                fallback_used = False
                selection_mode = "llm_adaptive_category_select"
                if cache_key:
                    _store_category_select_cache_entry(config, cache_key, {
                        "selected_categories": selected,
                        "rejected_categories": rejected,
                    })
            else:
                llm_error = "LLM returned no valid selected_categories."
        else:
            llm_error = "LLM did not return valid JSON."

    if not rejected:
        rejected = _build_rejected(entries, selected)

    selected_names = {item["name"] for item in selected}
    selected_count = sum(entry["count"] for entry in entries if entry["name"] in selected_names)
    return {
        "venue_id": category_summary.get("venue_id", ""),
        "venue": category_summary.get("venue", ""),
        "year": category_summary.get("year", ""),
        "paper_count": category_summary.get("paper_count", 0),
        "category_count": len(entries),
        "selected_paper_count": selected_count,
        "selected_categories": selected,
        "rejected_categories": rejected,
        "fallback_used": fallback_used,
        "selection_mode": selection_mode,
        "llm_error": llm_error,
    }


def filter_papers_by_selected_categories(papers: list[dict[str, Any]], selection: dict[str, Any]) -> list[dict[str, Any]]:
    selected_names = {str(item.get("name") or "") for item in selection.get("selected_categories", []) if isinstance(item, dict)}
    if not selected_names:
        return []
    return [
        paper
        for paper in papers
        if str(paper.get("primary_area") or paper.get("category") or paper.get("track") or "") in selected_names
    ]


# ---- profile_normalize.py ----

import json
import re
from typing import Any

from auto_research.llm import LLMClient
from auto_research.models import AppConfig


PROFILE_SCHEMA: dict[str, Any] = {
    "explicit_profile": {
        "research_interest_summary": "",
        "researcher_background": None,
    },
    "explicit_retrieval_signals": {
        "core_concepts": [],
        "method_terms": [],
        "application_terms": [],
        "domain_terms": [],
        "excluded_terms": [],
    },
    "safe_expansions": {
        "synonyms_or_abbreviations": [
            {
                "term": "",
                "source_term": "",
                "expansion_type": "synonym",
                "reason": "",
            }
        ],
    },
    "filtering_hints": {
        "hard_exclusions": [],
        "conditional_exclusions": [
            {
                "terms": [],
                "condition": "",
            }
        ],
        "soft_penalties": [],
        "must_keep_if_present": [],
        "preference_hints": [],
    },
    "uncertainty": {
        "ambiguous_terms": [],
        "needs_clarification": False,
    },
}


def _as_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_optional_string(value: Any) -> str | None:
    text = _as_string(value)
    return text or None


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _as_string(item)
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _normalize_expansions(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        term = _as_string(item.get("term"))
        source_term = _as_string(item.get("source_term"))
        if not term or not source_term:
            continue
        expansion_type = _as_string(item.get("expansion_type")) or "synonym"
        if expansion_type not in {"synonym", "abbreviation", "closely_related"}:
            expansion_type = "closely_related"
        reason = _as_string(item.get("reason")) or f"Expansion provided for explicit source term: {source_term}."
        key = (term.lower(), source_term.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "term": term,
            "source_term": source_term,
            "expansion_type": expansion_type,
            "reason": reason,
        })
    return result


def _normalize_conditional_exclusions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for item in value:
        if isinstance(item, str):
            terms = [item]
            condition = item
        elif isinstance(item, dict):
            terms = _as_string_list(item.get("terms"))
            condition = _as_string(item.get("condition"))
        else:
            continue
        if not terms or not condition:
            continue
        key = (tuple(term.lower() for term in terms), condition.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append({"terms": terms, "condition": condition})
    return result


def _canonical_condition(value: str) -> str:
    text = _as_string(value).lower()
    text = re.sub(r"^unless\s+(?:they\s+)?directly support\s+", "exclude only if they do not directly support ", text)
    text = re.sub(r"^unless\s+(?:it\s+)?directly supports\s+", "exclude only if they do not directly support ", text)
    text = re.sub(r"^unless\s+", "exclude only if the exception is not met: ", text)
    text = text.replace("do not directly supports", "do not directly support")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .")


def _dedupe_conditional_exclusions(value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for item in value:
        terms = _as_string_list(item.get("terms"))
        condition = _as_string(item.get("condition"))
        if not terms or not condition:
            continue
        canonical_terms = tuple(sorted(term.lower() for term in terms))
        canonical_condition = _canonical_condition(condition)
        key = (canonical_terms, canonical_condition)
        if key in seen:
            continue
        seen.add(key)
        if condition.lower().startswith("unless "):
            condition = canonical_condition
        result.append({"terms": terms, "condition": condition})
    return result


def normalize_profile_shape(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    explicit_profile = data.get("explicit_profile") if isinstance(data.get("explicit_profile"), dict) else {}
    explicit_signals = data.get("explicit_retrieval_signals") if isinstance(data.get("explicit_retrieval_signals"), dict) else {}
    safe_expansions = data.get("safe_expansions") if isinstance(data.get("safe_expansions"), dict) else {}
    filtering_hints = data.get("filtering_hints") if isinstance(data.get("filtering_hints"), dict) else {}
    uncertainty = data.get("uncertainty") if isinstance(data.get("uncertainty"), dict) else {}

    return {
        "explicit_profile": {
            "research_interest_summary": _as_string(explicit_profile.get("research_interest_summary")),
            "researcher_background": _as_optional_string(explicit_profile.get("researcher_background")),
        },
        "explicit_retrieval_signals": {
            "core_concepts": _as_string_list(explicit_signals.get("core_concepts")),
            "method_terms": _as_string_list(explicit_signals.get("method_terms")),
            "application_terms": _as_string_list(explicit_signals.get("application_terms")),
            "domain_terms": _as_string_list(explicit_signals.get("domain_terms")),
            "excluded_terms": _as_string_list(explicit_signals.get("excluded_terms")),
        },
        "safe_expansions": {
            "synonyms_or_abbreviations": _normalize_expansions(safe_expansions.get("synonyms_or_abbreviations")),
        },
        "filtering_hints": {
            "hard_exclusions": _as_string_list(filtering_hints.get("hard_exclusions")),
            "conditional_exclusions": _normalize_conditional_exclusions(filtering_hints.get("conditional_exclusions")),
            "soft_penalties": _as_string_list(filtering_hints.get("soft_penalties")),
            "must_keep_if_present": _as_string_list(filtering_hints.get("must_keep_if_present")),
            "preference_hints": _as_string_list(filtering_hints.get("preference_hints")),
        },
        "uncertainty": {
            "ambiguous_terms": _as_string_list(uncertainty.get("ambiguous_terms")),
            "needs_clarification": bool(uncertainty.get("needs_clarification")),
        },
    }


def _keyword_terms(text: str) -> list[str]:
    terms = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9][a-zA-Z0-9_.-]{2,}", text or "")
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        cleaned = term.strip(".,;:!?()[]{}\"'")
        if cleaned and cleaned.lower() not in seen:
            result.append(cleaned)
            seen.add(cleaned.lower())
    return result


def _split_terms(text: str) -> list[str]:
    cleaned = re.sub(r"\b(or|and)\b", ",", text, flags=re.IGNORECASE)
    return [
        term.strip(" .;:")
        for term in cleaned.split(",")
        if term.strip(" .;:")
    ]


def _append_unique(items: list[str], value: str) -> None:
    text = _as_string(value)
    if text and text not in items:
        items.append(text)


def _append_expansion(expansions: list[dict[str, str]], term: str, source_term: str, expansion_type: str, reason: str) -> None:
    if not term or not source_term:
        return
    key = (term.lower(), source_term.lower())
    if any((item["term"].lower(), item["source_term"].lower()) == key for item in expansions):
        return
    expansions.append({
        "term": term,
        "source_term": source_term,
        "expansion_type": expansion_type,
        "reason": reason,
    })


def _extract_conditional_exclusions(raw_text: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for match in re.finditer(r"\b(?:avoid|exclude|skip|filter out)\s+(.+?)\s+unless\s+(.+?)(?:[.;\n]|$)", raw_text, flags=re.IGNORECASE):
        terms = _split_terms(match.group(1))
        unless_clause = _as_string(match.group(2))
        lowered = unless_clause.lower()
        if lowered.startswith(("it directly supports ", "they directly support ", "directly supports ", "directly support ")):
            target = re.sub(r"^(it\s+|they\s+)?directly supports?\s+", "", unless_clause, flags=re.IGNORECASE)
            condition = f"exclude only if they do not directly support {target}"
        elif lowered.startswith(("it is directly related to ", "they are directly related to ", "directly related to ")):
            target = re.sub(r"^(it is\s+|they are\s+)?directly related to\s+", "", unless_clause, flags=re.IGNORECASE)
            condition = f"exclude only if they are not directly related to {target}"
        else:
            condition = f"exclude only if the exception is not met: {unless_clause}"
        if terms and condition:
            results.append({"terms": terms, "condition": condition})
    return results


def _extract_preference_hints(raw_text: str) -> list[str]:
    hints: list[str] = []
    for match in re.finditer(r"\bprefer\s+(.+?)(?:[.;\n]|$)", raw_text, flags=re.IGNORECASE):
        for term in _split_terms(match.group(1)):
            _append_unique(hints, f"prefer {term}")
    return hints


def _augment_safe_expansions(profile: dict[str, Any], raw_text: str) -> None:
    explicit_profile = profile["explicit_profile"]
    explicit_signals = profile["explicit_retrieval_signals"]
    source_text = " ".join([
        raw_text,
        explicit_profile.get("research_interest_summary") or "",
        " ".join(explicit_signals.get("core_concepts") or []),
        " ".join(explicit_signals.get("method_terms") or []),
        " ".join(explicit_signals.get("application_terms") or []),
        " ".join(explicit_signals.get("domain_terms") or []),
    ]).lower()
    expansions = profile["safe_expansions"]["synonyms_or_abbreviations"]
    rules = [
        (
            "paper discovery",
            "paper recommendation",
            "closely_related",
            "Common retrieval wording for systems that surface relevant papers.",
        ),
        (
            "literature review automation",
            "automated literature review",
            "synonym",
            "Equivalent wording often used in paper titles and abstracts.",
        ),
        (
            "academic research automation",
            "research assistant agent",
            "closely_related",
            "Common term for agent systems that assist research workflows.",
        ),
        (
            "academic research automation",
            "AI scientist",
            "closely_related",
            "Common term for systems that automate parts of scientific research.",
        ),
        (
            "retrieval augmented generation",
            "RAG",
            "abbreviation",
            "Standard abbreviation for retrieval augmented generation.",
        ),
        (
            "large language model",
            "LLM",
            "abbreviation",
            "Standard abbreviation for large language model.",
        ),
    ]
    for source_term, term, expansion_type, reason in rules:
        if source_term in source_text:
            _append_expansion(expansions, term, source_term, expansion_type, reason)


def _postprocess_profile(profile: dict[str, Any], config: AppConfig) -> dict[str, Any]:
    raw_text = "\n".join(part for part in [config.research_interest, config.researcher_profile] if part)
    filtering_hints = profile["filtering_hints"]
    conditional_exclusions = filtering_hints["conditional_exclusions"]

    for exclusion in _extract_conditional_exclusions(raw_text):
        if exclusion not in conditional_exclusions:
            conditional_exclusions.append(exclusion)
    filtering_hints["conditional_exclusions"] = _dedupe_conditional_exclusions(conditional_exclusions)

    conditional_terms = {term.lower() for item in filtering_hints["conditional_exclusions"] for term in item.get("terms", [])}
    filtering_hints["hard_exclusions"] = [
        term for term in filtering_hints["hard_exclusions"]
        if " unless " not in term.lower() and term.lower() not in conditional_terms
    ]
    profile["explicit_retrieval_signals"]["excluded_terms"] = [
        term for term in profile["explicit_retrieval_signals"]["excluded_terms"]
        if " unless " not in term.lower() and term.lower() not in conditional_terms
    ]

    for hint in _extract_preference_hints(raw_text):
        _append_unique(filtering_hints["preference_hints"], hint)

    preference_terms = {
        term.removeprefix("prefer ").lower()
        for term in filtering_hints["preference_hints"]
    }
    profile["uncertainty"]["ambiguous_terms"] = [
        term for term in profile["uncertainty"]["ambiguous_terms"]
        if term.lower() not in preference_terms
    ]

    if profile_retrieval_text(profile):
        profile["uncertainty"]["needs_clarification"] = False

    _augment_safe_expansions(profile, raw_text)
    return profile


def fallback_profile(config: AppConfig) -> dict[str, Any]:
    interest = _as_string(config.research_interest)
    background = _as_optional_string(config.researcher_profile)
    terms = _keyword_terms(f"{interest}\n{background or ''}")[:24]
    profile = normalize_profile_shape({
        "explicit_profile": {
            "research_interest_summary": interest,
            "researcher_background": background,
        },
        "explicit_retrieval_signals": {
            "core_concepts": terms,
            "method_terms": [],
            "application_terms": [],
            "domain_terms": [],
            "excluded_terms": [],
        },
        "safe_expansions": {"synonyms_or_abbreviations": []},
        "filtering_hints": {
            "hard_exclusions": [],
            "conditional_exclusions": [],
            "soft_penalties": [],
            "must_keep_if_present": [],
            "preference_hints": [],
        },
        "uncertainty": {
            "ambiguous_terms": [],
            "needs_clarification": not bool(interest or background),
        },
    })
    return _postprocess_profile(profile, config)


def build_stage0_prompt(config: AppConfig) -> str:
    schema = json.dumps(PROFILE_SCHEMA, ensure_ascii=False, indent=2)
    return f"""
Your task is to convert the user's free-text research interest and researcher profile into a structured JSON profile for downstream paper retrieval and filtering.

Important rules:
- Extract only research-relevant information.
- Do not invent research interests.
- Do not recommend papers.
- Do not choose conferences, tracks, fields, or arXiv categories.
- Do not invent ranking or filtering preferences.
- Do not polish or rewrite the user's intent beyond concise normalization.
- Preserve uncertainty explicitly.
- Separate explicit user statements from safe retrieval expansions.
- Safe expansions must be conservative: direct synonyms, standard abbreviations, or clearly adjacent retrieval terms only.
- Every safe expansion must include term, source_term, expansion_type, and reason. The source_term must come from explicit user input.
- Preserve conditional exclusions. For "avoid X unless Y", do not put X in hard_exclusions; put it in conditional_exclusions with the condition.
- Use soft_penalties for disliked topics that may still be useful if strongly relevant.
- Use preference_hints for ranking/filtering preferences such as practical systems, reproducible pipelines, or lightweight experiments.
- Do not put preferences in ambiguous_terms unless the system genuinely cannot interpret them for retrieval or ranking.
- Set needs_clarification=true only when missing or ambiguous information would block retrieval.
- If a field is missing, use an empty list or null.
- Output valid JSON only.

User input:

[INTEREST]
{config.research_interest}
[/INTEREST]

[RESEARCHER_PROFILE]
{config.researcher_profile}
[/RESEARCHER_PROFILE]

Return JSON with exactly this schema:
{schema}
""".strip()


def _profile_signal_count(profile: dict[str, Any]) -> int:
    signals = profile.get("explicit_retrieval_signals", {}) if isinstance(profile, dict) else {}
    expansions = profile.get("safe_expansions", {}) if isinstance(profile, dict) else {}
    count = 0
    for key in ["core_concepts", "method_terms", "application_terms", "domain_terms"]:
        count += len(_as_string_list(signals.get(key)))
    count += len(_normalize_expansions(expansions.get("synonyms_or_abbreviations")))
    return count


def normalize_user_profile(config: AppConfig, llm: LLMClient) -> tuple[dict[str, Any], bool, str]:
    if not (config.research_interest or config.researcher_profile):
        return fallback_profile(config), True, ""
    if not llm.enabled:
        return fallback_profile(config), True, "LLM is not configured; used deterministic fallback."
    data = llm.json_or_none(build_stage0_prompt(config))
    if data is None:
        return fallback_profile(config), True, "LLM did not return valid JSON; used deterministic fallback."
    profile = _postprocess_profile(normalize_profile_shape(data), config)
    retrieval_text = profile_retrieval_text(profile)
    if not retrieval_text:
        return fallback_profile(config), True, "LLM returned an empty profile; used deterministic fallback."
    raw_text = "\n".join(part for part in [config.research_interest, config.researcher_profile] if part)
    raw_terms = _keyword_terms(raw_text)
    if len(raw_terms) >= 8 and _profile_signal_count(profile) < 4:
        return fallback_profile(config), True, "LLM profile was too sparse for the provided research interest; used deterministic fallback."
    return profile, False, ""


def profile_retrieval_text(profile: dict[str, Any]) -> str:
    explicit_profile = profile.get("explicit_profile", {})
    explicit_signals = profile.get("explicit_retrieval_signals", {})
    safe_expansions = profile.get("safe_expansions", {})
    filtering_hints = profile.get("filtering_hints", {})
    parts: list[str] = []
    for value in [
        explicit_profile.get("research_interest_summary"),
        explicit_profile.get("researcher_background"),
    ]:
        text = _as_string(value)
        if text:
            parts.append(text)
    for key in ["core_concepts", "method_terms", "application_terms", "domain_terms"]:
        parts.extend(_as_string_list(explicit_signals.get(key)))
    parts.extend(item["term"] for item in _normalize_expansions(safe_expansions.get("synonyms_or_abbreviations")))
    hard_exclusions = _as_string_list(filtering_hints.get("hard_exclusions"))
    conditional_terms = {
        term.lower()
        for item in _normalize_conditional_exclusions(filtering_hints.get("conditional_exclusions"))
        for term in item.get("terms", [])
    }
    excluded_terms = [
        term for term in _as_string_list(explicit_signals.get("excluded_terms"))
        if term.lower() not in conditional_terms
    ]
    if hard_exclusions or excluded_terms:
        exclusions = list(dict.fromkeys([*excluded_terms, *hard_exclusions]))
        parts.append("Excluded topics: " + ", ".join(exclusions))
    for item in _normalize_conditional_exclusions(filtering_hints.get("conditional_exclusions")):
        parts.append(f"Conditional exclusion: reject {', '.join(item['terms'])} {item['condition']}.")
    soft_penalties = _as_string_list(filtering_hints.get("soft_penalties"))
    if soft_penalties:
        parts.append("Soft penalties: " + ", ".join(soft_penalties))
    preference_hints = _as_string_list(filtering_hints.get("preference_hints"))
    if preference_hints:
        parts.append("Preference hints: " + ", ".join(preference_hints))
    return "\n".join(dict.fromkeys(part for part in parts if part))


# ---- sources.py ----

import hashlib
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus, urlencode
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup

from auto_research.paths import WORKFLOW_RUNTIME_DIR, LEGACY_RUNS_DIR, REFERENCE_ROOT, ROOT, RUNS_DIR, STATE_DIR


HEADERS = {
    "User-Agent": "research-workflow/0.1"
}


VENUE_METADATA_AUDIT_KEY = "venue_metadata_audit"


def _venue_metadata_audit(**kwargs: Any) -> dict[str, Any]:
    audit = {
        "schema_version": 1,
        "status": kwargs.pop("status", "unknown"),
        "source_verified": bool(kwargs.pop("source_verified", False)),
        "complete": bool(kwargs.pop("complete", False)),
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }
    audit.update({key: value for key, value in kwargs.items() if value is not None})
    return audit


def _attach_venue_metadata_audit(papers: list[dict], audit: dict[str, Any]) -> list[dict]:
    for paper in papers:
        metadata = paper.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata[VENUE_METADATA_AUDIT_KEY] = dict(audit)
    return papers


def venue_metadata_audit_from_papers(papers: list[dict]) -> dict[str, Any]:
    audits: list[dict[str, Any]] = []
    for paper in papers:
        metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
        audit = metadata.get(VENUE_METADATA_AUDIT_KEY) if isinstance(metadata, dict) else None
        if isinstance(audit, dict):
            audits.append(audit)
    if not audits:
        return {}
    merged = dict(audits[0])
    merged["paper_count"] = len(papers)
    merged["complete"] = all(bool(audit.get("complete")) for audit in audits)
    statuses = list(dict.fromkeys(str(audit.get("status") or "unknown") for audit in audits))
    merged["status"] = statuses[0] if len(statuses) == 1 else "mixed"
    missing_abstracts = sum(1 for paper in papers if not str((paper if isinstance(paper, dict) else {}).get("abstract") or "").strip())
    merged["missing_abstract_count"] = missing_abstracts
    merged["has_abstracts"] = bool(papers) and missing_abstracts == 0
    merged["any_abstracts"] = bool(papers) and missing_abstracts < len(papers)
    if any("has_official_categories" in audit for audit in audits):
        merged["has_official_categories"] = all(bool(audit.get("has_official_categories")) for audit in audits)
    if any("official_title_index_verified" in audit for audit in audits):
        merged["official_title_index_verified"] = all(bool(audit.get("official_title_index_verified")) for audit in audits)
    if any("official_accepted_list_verified" in audit for audit in audits):
        merged["official_accepted_list_verified"] = all(bool(audit.get("official_accepted_list_verified")) for audit in audits)
    source_scopes = [str(audit.get("source_scope") or "") for audit in audits if audit.get("source_scope")]
    if source_scopes:
        merged["source_scope"] = source_scopes[0] if len(set(source_scopes)) == 1 else "mixed"
    return merged


def _metadata_timeout(default: int = 6) -> int:
    try:
        value = int(float(os.environ.get("METADATA_TIMEOUT_SEC", "") or default))
    except Exception:
        value = default
    return max(2, min(30, value))


NATURE_JOURNALS: dict[str, dict[str, str]] = {
    "nature": {"name": "Nature", "tier": "0", "group": "flagship"},
    "natmachintell": {"name": "Nature Machine Intelligence", "tier": "1", "group": "ai_computational"},
    "natcomputsci": {"name": "Nature Computational Science", "tier": "1", "group": "ai_computational"},
    "nmeth": {"name": "Nature Methods", "tier": "1", "group": "ai_computational"},
    "nbt": {"name": "Nature Biotechnology", "tier": "1", "group": "ai_computational"},
    "natbiomedeng": {"name": "Nature Biomedical Engineering", "tier": "1", "group": "ai_computational"},
    "ncomms": {"name": "Nature Communications", "tier": "1", "group": "ai_computational"},
    "nmat": {"name": "Nature Materials", "tier": "2", "group": "ai_science_materials"},
    "nchem": {"name": "Nature Chemistry", "tier": "2", "group": "ai_science_materials"},
    "natchemeng": {"name": "Nature Chemical Engineering", "tier": "2", "group": "ai_science_materials"},
    "natcatal": {"name": "Nature Catalysis", "tier": "2", "group": "ai_science_materials"},
    "natsynth": {"name": "Nature Synthesis", "tier": "2", "group": "ai_science_materials"},
    "nphys": {"name": "Nature Physics", "tier": "2", "group": "ai_science_materials"},
    "natelectron": {"name": "Nature Electronics", "tier": "2", "group": "ai_science_materials"},
    "nnano": {"name": "Nature Nanotechnology", "tier": "2", "group": "ai_science_materials"},
    "nphoton": {"name": "Nature Photonics", "tier": "2", "group": "ai_science_materials"},
    "nenergy": {"name": "Nature Energy", "tier": "2", "group": "ai_science_materials"},
    "nm": {"name": "Nature Medicine", "tier": "3", "group": "broad_interdisciplinary"},
    "ng": {"name": "Nature Genetics", "tier": "3", "group": "broad_interdisciplinary"},
    "neuro": {"name": "Nature Neuroscience", "tier": "3", "group": "broad_interdisciplinary"},
    "nathumbehav": {"name": "Nature Human Behaviour", "tier": "3", "group": "broad_interdisciplinary"},
    "nclimate": {"name": "Nature Climate Change", "tier": "3", "group": "broad_interdisciplinary"},
    "sustainability": {"name": "Nature Sustainability", "tier": "3", "group": "broad_interdisciplinary"},
    "ngeo": {"name": "Nature Geoscience", "tier": "3", "group": "broad_interdisciplinary"},
    "natecolevol": {"name": "Nature Ecology & Evolution", "tier": "3", "group": "broad_interdisciplinary"},
    "s41545": {"name": "Nature Water", "tier": "3", "group": "broad_interdisciplinary"},
    "s43016": {"name": "Nature Food", "tier": "3", "group": "broad_interdisciplinary"},
}


SCIENCE_JOURNALS: dict[str, dict[str, str]] = {
    "science": {"name": "Science", "tier": "0", "group": "science_core", "issn": "0036-8075"},
    "sciadv": {"name": "Science Advances", "tier": "1", "group": "science_core", "issn": "2375-2548"},
    "scirobotics": {"name": "Science Robotics", "tier": "1", "group": "ai_robotics_engineering", "issn": "2470-9476"},
    "stm": {"name": "Science Translational Medicine", "tier": "2", "group": "bio_medicine", "issn": "1946-6234"},
    "sciimmunol": {"name": "Science Immunology", "tier": "2", "group": "bio_medicine", "issn": "2470-9468"},
    "stke": {"name": "Science Signaling", "tier": "2", "group": "bio_medicine", "issn": "1937-9145"},
    "adi": {"name": "Advanced Devices & Instrumentation", "tier": "SPJ", "group": "science_partner_journals"},
    "bmr": {"name": "Biomaterials Research", "tier": "SPJ", "group": "science_partner_journals"},
    "bmef": {"name": "BME Frontiers", "tier": "SPJ", "group": "science_partner_journals"},
    "csbj": {"name": "Computational and Structural Biotechnology Journal", "tier": "SPJ", "group": "science_partner_journals"},
    "csbr": {"name": "Computational and Structural Biotechnology Reports", "tier": "SPJ", "group": "science_partner_journals"},
    "ehs": {"name": "Ecosystem Health and Sustainability", "tier": "SPJ", "group": "science_partner_journals"},
    "energymatadv": {"name": "Energy Material Advances", "tier": "SPJ", "group": "science_partner_journals"},
    "hds": {"name": "Health Data Science", "tier": "SPJ", "group": "science_partner_journals"},
    "icomputing": {"name": "Intelligent Computing", "tier": "SPJ", "group": "science_partner_journals"},
    "jemdr": {"name": "Journal of EMDR Practice and Research", "tier": "SPJ", "group": "science_partner_journals"},
    "remotesensing": {"name": "Journal of Remote Sensing", "tier": "SPJ", "group": "science_partner_journals"},
    "olar": {"name": "Ocean-Land-Atmosphere Research", "tier": "SPJ", "group": "science_partner_journals"},
    "research": {"name": "Research", "tier": "SPJ", "group": "science_partner_journals"},
    "space": {"name": "Space: Science & Technology", "tier": "SPJ", "group": "science_partner_journals"},
    "ultrafastscience": {"name": "Ultrafast Science", "tier": "SPJ", "group": "science_partner_journals"},
    "plantphenomics": {"name": "Plant Phenomics", "tier": "SPJ", "group": "science_partner_journals", "status": "migrated"},
}

def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _clean_text(value: str) -> str:
    return " ".join((value or "").split())


_ABSTRACT_UI_CONTROL_RE = re.compile(
    r"(?:\s*(?:show\s+(?:more|less)|read\s+(?:more|less)|显示更多|显示较少|展开|收起)\s*[。.]?\s*)+$",
    re.IGNORECASE,
)


def _strip_abstract_ui_controls(value: object) -> str:
    return _ABSTRACT_UI_CONTROL_RE.sub("", _clean_text(str(value or ""))).strip()


def _presentation_type_from_url(url: object) -> str:
    text = str(url or "").lower()
    if re.search(r"/(?:best[-_]?paper|award)(?:/|$)", text):
        return "best paper/award"
    if re.search(r"/oral(?:/|$)", text):
        return "oral"
    if re.search(r"/(?:spotlight|highlight)(?:/|$)", text):
        return "spotlight"
    if re.search(r"/poster(?:/|$)", text):
        return "poster"
    return ""


def _presentation_type_from_text(value: object) -> str:
    text = _clean_text(str(value or "")).lower()
    if not text:
        return ""
    if re.search(r"\b(best|award|outstanding|distinguished)[-\s]+paper\b", text):
        return "best paper/award"
    if re.search(r"\boral\b", text):
        return "oral"
    if re.search(r"\bspotlight\b|\bhighlight\b", text):
        return "spotlight"
    if re.search(r"\bposter\b", text):
        return "poster"
    return ""


def _presentation_display_label(venue: object, year: object, presentation_type: str) -> str:
    label = str(presentation_type or "").strip()
    if not label:
        return ""
    display = " ".join(part for part in [str(venue or "").strip(), str(year or "").strip(), label.title()] if part).strip()
    return display or label


def _set_presentation_metadata(paper: dict, presentation_type: str, *, source: str) -> None:
    label = str(presentation_type or "").strip().lower()
    if not label:
        return
    metadata = paper.setdefault("metadata", {})
    display = _presentation_display_label(paper.get("venue"), paper.get("year"), label)
    paper.setdefault("track", display)
    paper.setdefault("presentation_type", label)
    paper.setdefault("presentation_label", display)
    if isinstance(metadata, dict):
        metadata.setdefault("presentation_type", label)
        metadata.setdefault("presentation_label", display)
        metadata.setdefault("presentation_source", source)


def _title_key(value: str) -> str:
    text = html.unescape(_clean_text(value)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _semantic_scholar_cache_path() -> Path:
    return STATE_DIR / "semantic_scholarabstract_cache.json"


def _openalex_cache_path() -> Path:
    return STATE_DIR / "openalex_abstract_cache.json"


def _semantic_scholar_cache_key(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", (title or "").strip().lower())
    return hashlib.sha1(cleaned.encode("utf-8", errors="ignore")).hexdigest()


def _load_semantic_scholar_cache() -> dict:
    path = Path(_semantic_scholar_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_semantic_scholar_cache(cache: dict) -> None:
    path = Path(_semantic_scholar_cache_path())
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        return


def _load_openalex_cache() -> dict:
    path = Path(_openalex_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_openalex_cache(cache: dict) -> None:
    path = Path(_openalex_cache_path())
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        return


def _same_metadata_text(left: object, right: object) -> bool:
    return " ".join(str(left or "").split()).casefold() == " ".join(str(right or "").split()).casefold()


def _semantic_scholar_cache_real_abstract(cached: dict) -> str:
    abstract = str(cached.get("abstract") or "").strip()
    if not abstract:
        return ""
    tldr = str(cached.get("tldr") or "").strip()
    source = str(cached.get("source") or cached.get("abstract_source") or "").lower()
    if "tldr" in source:
        return ""
    if tldr and _same_metadata_text(abstract, tldr):
        return ""
    return abstract


def _apply_semantic_scholar_cache(paper: dict, cached: dict) -> None:
    abstract = _semantic_scholar_cache_real_abstract(cached)
    if abstract:
        paper["abstract"] = abstract
        paper.setdefault("metadata", {})["abstract_source"] = cached.get("source") or "semantic_scholar_cache"
    if cached.get("url"):
        paper["url"] = paper.get("url") or cached.get("url") or ""
    if cached.get("pdf_url"):
        paper["pdf_url"] = paper.get("pdf_url") or cached.get("pdf_url") or ""
    if cached.get("tldr"):
        paper.setdefault("metadata", {})["tldr"] = cached.get("tldr") or ""


_SEMANTIC_SCHOLAR_RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


def _semantic_scholar_errors_retryable(errors: object) -> bool:
    if not isinstance(errors, list):
        return False
    for error in errors:
        lowered = str(error or "").lower()
        if any(f"http_{code}" in lowered for code in _SEMANTIC_SCHOLAR_RETRYABLE_STATUS_CODES):
            return True
        if any(token in lowered for token in ("timeout", "timed out", "connection reset", "temporarily", "rate limit")):
            return True
    return False


def _semantic_scholar_cache_miss_is_retryable(cached: object) -> bool:
    if not isinstance(cached, dict) or not cached.get("miss"):
        return False
    return bool(cached.get("retryable") or cached.get("temporary_failure") or _semantic_scholar_errors_retryable(cached.get("lookup_errors")))


def _semantic_scholar_cache_is_permanent_miss(cached: object) -> bool:
    return isinstance(cached, dict) and bool(cached.get("miss")) and not _semantic_scholar_cache_miss_is_retryable(cached)


def _doi_from_url(value: str) -> str:
    text = (value or "").strip()
    match = re.search(r"10\.\d{4,9}/[^\s\"<>]+", text)
    if not match:
        return ""
    return match.group(0).rstrip(".,);]")




def _doi_url(doi: str) -> str:
    doi = (doi or "").strip()
    return f"https://doi.org/{doi}" if doi else ""


def _acm_ids_from_doi(doi: str) -> tuple[str, str]:
    match = re.match(r"10\.1145/(\d+)(?:\.(\d+))?", (doi or "").strip(), flags=re.I)
    if not match:
        return "", ""
    proceedings_id = match.group(1) or ""
    article_id = match.group(2) or proceedings_id
    return proceedings_id, article_id


def _acm_metadata_from_doi(doi: str) -> dict[str, str]:
    proceedings_id, article_id = _acm_ids_from_doi(doi)
    if not article_id:
        return {}
    return {
        "doi": doi,
        "doi_url": _doi_url(doi),
        "acm_proceedings_id": proceedings_id,
        "acm_article_id": article_id,
        "acm_abs_url": f"https://dl.acm.org/doi/abs/{doi}",
        "acm_pdf_url": f"https://dl.acm.org/doi/pdf/{doi}",
        "acm_epdf_url": f"https://dl.acm.org/doi/epdf/{doi}",
        "acm_full_html_url": f"https://dl.acm.org/doi/fullHtml/{doi}",
        "acm_legacy_pdf_url": f"https://dl.acm.org/ft_gateway.cfm?id={article_id}&type=pdf",
    }


def _dblp_record_metadata(
    venue_id: object,
    *,
    stream_id: str = "",
    dblp_url: str = "",
    dblp_xml_url: str = "",
    dblp_record_url: str = "",
    dblp_key: str = "",
    ee: str = "",
    doi: str = "",
) -> dict:
    doi = (doi or _doi_from_url(ee)).strip()
    metadata: dict[str, object] = {"venue_id": venue_id}
    if stream_id:
        metadata["dblp_stream"] = stream_id
    if dblp_url:
        metadata["dblp_url"] = dblp_url
    if dblp_xml_url:
        metadata["dblp_xml_url"] = dblp_xml_url
    if dblp_record_url:
        metadata["dblp_record_url"] = dblp_record_url
    if dblp_key:
        metadata["dblp_key"] = dblp_key
    if ee:
        metadata["publisher_url"] = ee
    if doi:
        if doi.lower().startswith("10.1145/"):
            metadata.update(_acm_metadata_from_doi(doi))
        else:
            metadata.update({"doi": doi, "doi_url": _doi_url(doi)})
    return metadata


def _openalex_pdf_url(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    primary = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
    candidates = [primary.get("pdf_url") or ""]
    open_access = item.get("open_access") if isinstance(item.get("open_access"), dict) else {}
    candidates.append(open_access.get("oa_url") or "")
    for loc in item.get("locations") or []:
        if isinstance(loc, dict):
            candidates.append(loc.get("pdf_url") or "")
    for url in candidates:
        text = str(url or "").strip()
        if text and (".pdf" in text.lower() or "/pdf/" in text.lower()):
            return text
    return ""


def _openalex_landing_url(item: dict) -> str:
    primary = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
    return str(primary.get("landing_page_url") or item.get("doi") or item.get("id") or "")


def _title_token_similarity(a: object, b: object) -> float:
    left = set(_title_key(str(a or "")).split())
    right = set(_title_key(str(b or "")).split())
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def _author_family_tokens(value: object) -> set[str]:
    if isinstance(value, str):
        raw_names = re.split(r",|;| and ", value)
    elif isinstance(value, list):
        raw_names = [str(item) for item in value]
    else:
        raw_names = []
    tokens: set[str] = set()
    for name in raw_names:
        parts = re.findall(r"[A-Za-z][A-Za-z'-]+", name.lower())
        if parts:
            tokens.add(parts[-1])
    return tokens


def _openalex_author_family_tokens(item: dict) -> set[str]:
    tokens: set[str] = set()
    for authorship in item.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") if isinstance(authorship.get("author"), dict) else {}
        name = str(author.get("display_name") or "")
        tokens.update(_author_family_tokens(name))
    return tokens


def _openalex_candidate_matches(paper: dict, item: dict) -> bool:
    item_title = item.get("display_name") or item.get("title")
    similarity = _title_token_similarity(paper.get("title"), item_title)
    expected_authors = _author_family_tokens(paper.get("authors"))
    candidate_authors = _openalex_author_family_tokens(item)
    if expected_authors:
        return similarity >= 0.82 and bool(expected_authors & candidate_authors)
    return similarity >= 0.95


def _openalex_item_from_payload(payload: dict, paper: dict, *, from_search: bool) -> dict:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("results"), list):
        for item in payload.get("results") or []:
            if isinstance(item, dict) and (not from_search or _openalex_candidate_matches(paper, item)):
                return item
        return {}
    return payload if not from_search or _openalex_candidate_matches(paper, payload) else {}

def _openalex_abstract_from_inverted_index(index: dict) -> str:
    if not isinstance(index, dict) or not index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, offsets in index.items():
        if not isinstance(offsets, list):
            continue
        for offset in offsets:
            try:
                positions.append((int(offset), str(word)))
            except Exception:
                continue
    if not positions:
        return ""
    return _clean_text(" ".join(word for _offset, word in sorted(positions)))


def _openalex_cache_key(paper: dict) -> str:
    doi = _doi_from_url(str(paper.get("doi") or paper.get("url") or paper.get("pdf_url") or ""))
    if doi:
        return f"doi:{doi.lower()}"
    return f"title:{_semantic_scholar_cache_key(paper.get('title', ''))}"


def enrich_with_openalex(papers: list[dict], limit: int = 80) -> list[dict]:
    cache = _load_openalex_cache()
    cache_changed = False
    for paper in papers[:limit]:
        if paper.get("abstract") and paper.get("pdf_url"):
            continue
        cache_key = _openalex_cache_key(paper)
        doi = _doi_from_url(str(paper.get("doi") or paper.get("url") or paper.get("pdf_url") or ""))
        query = quote_plus(str(paper.get("title") or ""))
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            if cached.get("miss") and cached.get("title_fallback_attempted"):
                continue
            if cached.get("abstract"):
                paper["abstract"] = cached.get("abstract") or ""
            if cached.get("url"):
                paper["url"] = paper.get("url") or cached.get("url") or ""
            if cached.get("pdf_url") and not paper.get("pdf_url"):
                paper["pdf_url"] = cached.get("pdf_url") or ""
            if cached.get("openalex_id") or cached.get("openalex_doi") or cached.get("pdf_url"):
                metadata = paper.setdefault("metadata", {})
                if cached.get("openalex_id"):
                    metadata["openalex_id"] = cached.get("openalex_id") or ""
                if cached.get("openalex_doi"):
                    metadata["openalex_doi"] = cached.get("openalex_doi") or ""
                if cached.get("pdf_url"):
                    metadata["openalex_pdf_url"] = cached.get("pdf_url") or ""
                if doi and cached.get("doi_status") and cached.get("doi_status") != 200 and cached.get("title_fallback_attempted"):
                    metadata["publisher_doi_openalex_status"] = cached.get("doi_status")
                    metadata["openalex_title_fallback_used"] = True
            if paper.get("abstract") and (paper.get("pdf_url") or cached.get("title_fallback_attempted")):
                continue
        if not doi and not query:
            continue
        item: dict = {}
        doi_status = 0
        try:
            if doi:
                doi_url = f"https://api.openalex.org/works/doi:{doi}"
                response = requests.get(doi_url, headers=HEADERS, timeout=_metadata_timeout(6))
                doi_status = response.status_code
                if response.status_code == 200:
                    item = _openalex_item_from_payload(response.json(), paper, from_search=False)
            if not item and query:
                search_url = f"https://api.openalex.org/works?search={query}&per-page=3"
                response = requests.get(search_url, headers=HEADERS, timeout=_metadata_timeout(6))
                if response.status_code == 200:
                    item = _openalex_item_from_payload(response.json(), paper, from_search=True)
            if not item:
                cache[cache_key] = {
                    "title": paper.get("title", ""),
                    "miss": True,
                    "doi_status": doi_status,
                    "title_fallback_attempted": bool(query),
                    "updated_at": datetime.utcnow().isoformat() + "Z",
                }
                cache_changed = True
                continue
            abstract = _openalex_abstract_from_inverted_index(item.get("abstract_inverted_index") or {})
            if abstract:
                paper["abstract"] = abstract
            pdf_url = _openalex_pdf_url(item)
            if pdf_url and not paper.get("pdf_url"):
                paper["pdf_url"] = pdf_url
            landing_url = _openalex_landing_url(item)
            paper["url"] = paper.get("url") or landing_url
            metadata = paper.setdefault("metadata", {})
            metadata["openalex_id"] = item.get("id") or ""
            metadata["openalex_doi"] = item.get("doi") or ""
            metadata["openalex_landing_url"] = landing_url
            metadata["openalex_pdf_url"] = pdf_url
            if doi and doi_status and doi_status != 200:
                metadata["publisher_doi_openalex_status"] = doi_status
                metadata["openalex_title_fallback_used"] = True
            cache[cache_key] = {
                "title": paper.get("title", ""),
                "abstract": paper.get("abstract") or "",
                "url": landing_url,
                "pdf_url": pdf_url,
                "openalex_id": item.get("id") or "",
                "openalex_doi": item.get("doi") or "",
                "doi_status": doi_status,
                "miss": not bool(paper.get("abstract") or pdf_url),
                "title_fallback_attempted": bool(query),
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
            cache_changed = True
            time.sleep(0.1)
        except Exception:
            continue
    if cache_changed:
        _save_openalex_cache(cache)
    return papers


def _looks_like_paper_title(value: str) -> bool:
    text = _clean_text(value)
    lowered = text.lower()
    if len(text) < 8:
        return False
    blocked = [
        "main navigation",
        "skip to",
        "successful page load",
        "openreview",
        "neurips 2025",
        "papers",
        "proceedings of",
        "companion proceedings of",
        "front matter",
        "preface",
        "table of contents",
    ]
    return not any(item == lowered or lowered.startswith(item) for item in blocked)


def normalize_date(value: str = "") -> str:
    text = (value or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    match = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return date(year, month, day).isoformat()
    return text


def _in_date_range(value: str, start_date: str = "", end_date: str = "") -> bool:
    current = normalize_date((value or "")[:10])
    start = normalize_date(start_date)
    end = normalize_date(end_date)
    if not current:
        return True
    if start and current < start:
        return False
    if end and current > end:
        return False
    return True


def fetch_openreview_iclr_2026(max_items: int) -> list[dict]:
    path = REFERENCE_ROOT / "ICLR2026-Guide-CN" / "ICLR2026_all_papers.json"
    if not path.exists():
        return []
    data = path.read_text(encoding="utf-8")
    import json
    raw = json.loads(data)
    papers = []
    for item in raw.get("papers", [])[:max_items]:
        papers.append({
            "id": stable_id("paper", item.get("id") or item.get("title", "")),
            "source": "openreview",
            "title": item.get("title", "Untitled"),
            "authors": "",
            "abstract": item.get("abstract", ""),
            "url": item.get("url", ""),
            "pdf_url": item.get("pdf_url", ""),
            "venue": "ICLR",
            "year": 2026,
            "category": item.get("primary_area") or item.get("category") or "",
            "classification_source": "official",
            "metadata": {
                "primary_area": item.get("primary_area", ""),
                "subcategory": item.get("category", ""),
                "tier": item.get("tier", ""),
            },
        })
    return papers


def is_neurips_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return "neurips" in text or "neural information processing systems" in text


def is_acl_family_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return any(key in text for key in ["acl", "emnlp", "naacl", "association for computational linguistics"])


def is_iclr_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')} {venue.get('address', '')}".lower()
    return "iclr" in text or "learning representations" in text


def is_cvf_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return any(key in text for key in ["cvpr", "iccv", "eccv"])


def is_pmlr_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return any(key in text for key in ["icml", "aistats", "colt", "uai"])


def is_icml_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return "icml" in text or "international conference on machine learning" in text




OPENREVIEW_VENUE_PATTERNS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("neurips", "neural information processing systems"), ("NeurIPS.cc/{year}/Conference",)),
    (("iclr", "learning representations"), ("ICLR.cc/{year}/Conference",)),
    (("icml", "international conference on machine learning"), ("ICML.cc/{year}/Conference",)),
    (("aistats", "artificial intelligence and statistics"), ("aistats.org/AISTATS/{year}/Conference",)),
    (("uai", "uncertainty in artificial intelligence"), ("auai.org/UAI/{year}/Conference",)),
    (("colt", "conference on learning theory"), ("learningtheory.org/COLT/{year}/Conference",)),
    (("corl", "conference on robot learning"), ("robot-learning.org/CoRL/{year}/Conference",)),
    (("colm", "conference on language modeling"), ("colmweb.org/COLM/{year}/Conference",)),
    (("rlc", "reinforcement learning conference"), ("rl-conference.cc/RLC/{year}/Conference",)),
    (("log", "learning on graphs"), ("logconference.io/LOG/{year}/Conference",)),
    (("midl", "medical imaging with deep learning"), ("MIDL.io/{year}/Conference",)),
    (("tmlr", "transactions on machine learning research"), ("TMLR",)),
]


def _venue_text(venue: dict) -> str:
    return f"{venue.get('name', '')} {venue.get('full_name', '')} {venue.get('address', '')}".lower()


def _matches_venue_keyword(text: str, keyword: str) -> bool:
    keyword = keyword.lower()
    if " " in keyword:
        return keyword in text
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None


def _openreview_patterns_for_venue(venue: dict) -> list[str]:
    text = _venue_text(venue)
    patterns: list[str] = []
    for keywords, venue_patterns in OPENREVIEW_VENUE_PATTERNS:
        if any(_matches_venue_keyword(text, keyword) for keyword in keywords):
            patterns.extend(venue_patterns)
    return patterns


def is_openreview_supported_venue(venue: dict) -> bool:
    return bool(_openreview_patterns_for_venue(venue))


def _openreview_venue_ids(venue: dict, year: int) -> list[str]:
    venue_ids = []
    for pattern in _openreview_patterns_for_venue(venue):
        venue_ids.append(pattern.format(year=year) if "{year}" in pattern else pattern)
    return list(dict.fromkeys(venue_ids))

def _request(url: str, timeout: int = 12) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response


def _dblp_page_url(url: str) -> str:
    cleaned = (url or "").strip()
    for prefix in ("https://dblp.org", "http://dblp.org", "http://dblp.uni-trier.de"):
        if cleaned.startswith(prefix):
            return cleaned.replace(prefix, "https://dblp.uni-trier.de", 1)
    return cleaned


def _dblp_stream_id(address: str) -> str:
    text = (address or "").strip()
    match = re.search(r"/db/(conf|journals)/([^/#?]+)", text)
    if not match:
        return ""
    return f"{match.group(1)}/{match.group(2)}"


def _dblp_authors(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("author", [])
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict):
                result.append(str(item.get("text") or item.get("#text") or ""))
            else:
                result.append(str(item))
        return ", ".join(author for author in result if author)
    if isinstance(value, str):
        return value
    return ""


def _dblp_hits_payload(response: requests.Response) -> tuple[list[dict], dict[str, int]]:
    data = response.json().get("result", {}).get("hits", {})
    hits = data.get("hit", [])
    if isinstance(hits, dict):
        hits = [hits]
    stats: dict[str, int] = {}
    for source_key, target_key in (("@total", "total"), ("@sent", "sent"), ("@first", "first")):
        try:
            stats[target_key] = int(data.get(source_key) or 0)
        except Exception:
            stats[target_key] = 0
    return hits if isinstance(hits, list) else [], stats


def _dblp_stream_query(stream_id: str, year: int | None = None) -> str:
    query = f"stream:streams/{stream_id}:"
    if year is not None:
        query = f"{query} {year}"
    return query


def _dblp_search_hits(stream_id: str, *, year: int | None, max_items: int | None) -> tuple[list[dict], dict[str, Any]]:
    page_size = 100
    max_pages = max(1, int(os.environ.get("DBLP_SEARCH_MAX_PAGES", "50") or 50))
    request_retries = max(1, int(os.environ.get("DBLP_SEARCH_RETRIES", "3") or 3))
    hits: list[dict] = []
    total = 0
    first_values: list[int] = []
    sent_values: list[int] = []
    errors: list[str] = []
    exhausted = False
    truncated = False
    for page_index in range(max_pages):
        offset = page_index * page_size
        page_hits: list[dict] = []
        stats: dict[str, int] = {}
        last_error = ""
        for attempt in range(request_retries):
            try:
                response = requests.get(
                    "https://dblp.org/search/publ/api",
                    params={"q": _dblp_stream_query(stream_id, year), "h": page_size, "f": offset, "format": "json"},
                    headers=HEADERS,
                    timeout=12,
                )
                response.raise_for_status()
                page_hits, stats = _dblp_hits_payload(response)
                last_error = ""
                break
            except Exception as exc:
                last_error = str(exc)[:240]
                if attempt + 1 < request_retries:
                    time.sleep(0.5 * (attempt + 1))
        if last_error:
            errors.append(last_error)
            return hits, {
                "query_year": year,
                "total": total,
                "sent": sum(sent_values),
                "pages_fetched": page_index,
                "complete": False,
                "error": last_error,
                "errors": errors,
                "truncated": bool(max_items is not None and hits),
            }
        total = max(total, int(stats.get("total") or 0))
        sent = int(stats.get("sent") or len(page_hits) or 0)
        first_values.append(int(stats.get("first") or offset))
        sent_values.append(sent)
        hits.extend(page_hits)
        if max_items is not None and len(hits) >= max_items:
            truncated = len(hits) < total
            break
        if not page_hits or sent <= 0 or len(hits) >= total:
            exhausted = True
            break
        time.sleep(0.15)
    else:
        truncated = bool(total and len(hits) < total)
    return hits, {
        "query_year": year,
        "total": total,
        "sent": sum(sent_values),
        "pages_fetched": len(sent_values),
        "page_size": page_size,
        "first_values": first_values,
        "complete": bool(total and len(hits) >= total and not truncated) or exhausted,
        "truncated": truncated,
        "errors": errors,
    }


def fetch_dblp_stream_api(venue: dict, years: list[int], max_items: int | None) -> list[dict]:
    stream_id = _dblp_stream_id(venue.get("address", ""))
    if not stream_id:
        return []
    wanted_years = [int(year) for year in years if str(year).isdigit()]
    query_years = wanted_years or [None]
    papers: list[dict] = []
    seen: set[str] = set()
    search_audits: list[dict[str, Any]] = []
    for query_year in query_years:
        hits, search_audit = _dblp_search_hits(stream_id, year=query_year, max_items=None if max_items is None else max(max_items * 2, 100))
        search_audits.append(search_audit)
        for hit in hits:
            info = hit.get("info", {}) if isinstance(hit, dict) else {}
            year = str(info.get("year") or "")
            if wanted_years and year not in {str(value) for value in wanted_years}:
                continue
            title = _clean_text(html.unescape(str(info.get("title") or ""))).rstrip(".")
            if not _looks_like_paper_title(title):
                continue
            key = _title_key(title) or str(info.get("key") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            paper_url = str(info.get("ee") or info.get("url") or "")
            doi = str(info.get("doi") or _doi_from_url(paper_url)).strip()
            dblp_record_url = str(info.get("url") or "")
            metadata = _dblp_record_metadata(
                venue.get("id"),
                stream_id=stream_id,
                dblp_record_url=dblp_record_url,
                dblp_key=str(info.get("key") or ""),
                ee=paper_url,
                doi=doi,
            )
            papers.append({
                "id": stable_id("paper", f"{venue.get('id')}:{year}:{title}"),
                "source": "dblp",
                "title": title,
                "authors": _dblp_authors(info.get("authors")),
                "abstract": "",
                "url": paper_url,
                "pdf_url": "",
                "doi": doi,
                "venue": venue.get("name", ""),
                "year": int(year) if year.isdigit() else 0,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": metadata,
            })
            if max_items is not None and len(papers) >= max_items:
                break
        if max_items is not None and len(papers) >= max_items:
            break
    if not papers and max_items is not None and not any(item.get("error") for item in search_audits):
        # DBLP occasionally returns a bounded page that is later filtered to zero
        # usable papers. Retry without a max-items stop before slower fallbacks.
        return fetch_dblp_stream_api(venue, years, None)
    total_hits = sum(int(item.get("total") or 0) for item in search_audits)
    complete = bool(search_audits) and all(bool(item.get("complete")) and not bool(item.get("truncated")) and not item.get("error") for item in search_audits)
    audit = _venue_metadata_audit(
        status="complete" if complete else "partial",
        title_index_completeness_status="complete" if complete else "partial",
        source_verified=bool(search_audits and not any(item.get("error") for item in search_audits)),
        complete=complete,
        title_index_complete=complete,
        dblp_stream_index_complete=complete,
        official_metadata_complete=False,
        adapter="dblp_search_api",
        source_url="https://dblp.org/search/publ/api",
        stream_id=stream_id,
        requested_years=wanted_years,
        query_audits=search_audits,
        search_total_hits=total_hits,
        deduped_paper_count=len(papers),
        has_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="dblp_current_index_not_official_accepted_list",
        official_title_index_verified=False,
        official_accepted_list_verified=False,
        completeness_basis="DBLP paginated stream search over the current DBLP index. This verifies the DBLP title index only; it is not an official venue accepted-paper or ACM proceedings completeness certificate, and it exposes no abstracts or official categories.",
    )
    return _attach_venue_metadata_audit(papers, audit)


def _content_value(content: dict, key: str) -> str:
    value = content.get(key, "")
    if isinstance(value, dict):
        return str(value.get("value") or "")
    return str(value or "")


def _content_list(content: dict, key: str) -> list[str]:
    value = content.get(key, [])
    if isinstance(value, dict):
        value = value.get("value", [])
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


def _content_first_value(content: dict, keys: list[str]) -> str:
    for key in keys:
        value = _content_value(content, key).strip()
        if value:
            return value
    lowered = {str(key).lower().replace(" ", "_"): key for key in content.keys()}
    for key in keys:
        actual = lowered.get(str(key).lower().replace(" ", "_"))
        if actual:
            value = _content_value(content, actual).strip()
            if value:
                return value
    return ""


def _content_first_list(content: dict, keys: list[str]) -> list[str]:
    for key in keys:
        values = [item.strip() for item in _content_list(content, key) if item.strip()]
        if values:
            return values
    lowered = {str(key).lower().replace(" ", "_"): key for key in content.keys()}
    for key in keys:
        actual = lowered.get(str(key).lower().replace(" ", "_"))
        if actual:
            values = [item.strip() for item in _content_list(content, actual) if item.strip()]
            if values:
                return values
    return []


def _attach_openreview_metadata_audit(papers: list[dict], venue_ids: list[str], years: list[int]) -> list[dict]:
    missing_abstracts = sum(1 for paper in papers if not str(paper.get("abstract") or "").strip())
    has_categories = any(str(paper.get("classification_source") or "").lower() == "official" and (paper.get("primary_area") or paper.get("category")) for paper in papers)
    audit = _venue_metadata_audit(
        status="partial",
        title_index_completeness_status="partial",
        source_verified=bool(papers),
        complete=False,
        title_index_complete=False,
        official_metadata_complete=bool(papers),
        adapter="openreview",
        openreview_venueids=list(dict.fromkeys(venue_ids)),
        requested_years=list(dict.fromkeys(int(year) for year in years)),
        paper_count=len(papers),
        missing_abstract_count=missing_abstracts,
        has_abstracts=bool(papers) and missing_abstracts == 0,
        any_abstracts=bool(papers) and missing_abstracts < len(papers),
        has_official_categories=has_categories,
        category_status="official_or_cached_categories" if has_categories else "no_official_categories",
        source_scope="openreview_official_venue_notes",
        official_title_index_verified=True,
        official_accepted_list_verified=True,
        completeness_basis="OpenReview official venue notes were fetched and title/abstract/category metadata was parsed; source remains partial until an adapter-level total-count audit verifies every record.",
    )
    return _attach_venue_metadata_audit(papers, audit)


def _openreview_notes_paginated(url: str, base_params: dict[str, object], max_items: int) -> list[dict]:
    try:
        requested = int(max_items or 0)
    except (TypeError, ValueError):
        requested = 0
    requested = max(1, requested)
    try:
        page_size = int(os.environ.get("OPENREVIEW_PAGE_SIZE", "1000") or 1000)
    except (TypeError, ValueError):
        page_size = 1000
    page_size = max(1, min(1000, page_size, requested))
    try:
        max_pages = int(os.environ.get("OPENREVIEW_MAX_PAGES", "200") or 200)
    except (TypeError, ValueError):
        max_pages = 200
    notes: list[dict] = []
    offset = 0
    for _page in range(max(1, max_pages)):
        params = dict(base_params)
        params["limit"] = min(page_size, requested - len(notes))
        params["offset"] = offset
        response = requests.get(url, params=params, headers=HEADERS, timeout=12)
        response.raise_for_status()
        batch = response.json().get("notes", [])
        if not isinstance(batch, list) or not batch:
            break
        notes.extend(note for note in batch if isinstance(note, dict))
        if len(notes) >= requested or len(batch) < int(params["limit"]):
            break
        offset += len(batch)
    return notes[:requested]


def fetch_openreview_venue(venue: dict, years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    queried_venue_ids: set[str] = set()
    for year in years:
        venue_ids = _openreview_venue_ids(venue, year)
        for venue_id in venue_ids:
            if venue_id in queried_venue_ids:
                continue
            queried_venue_ids.add(venue_id)
            notes = []
            try:
                notes = _openreview_notes_paginated(
                    "https://api2.openreview.net/notes",
                    {"content.venueid": venue_id, "details": "replyCount,invitation,original"},
                    max_items,
                )
            except Exception:
                notes = []
            if not notes:
                for invitation in [f"{venue_id}/-/Blind_Submission", f"{venue_id}/-/Submission"]:
                    try:
                        notes = _openreview_notes_paginated(
                            "https://api.openreview.net/notes",
                            {"invitation": invitation},
                            max_items,
                        )
                    except Exception:
                        notes = []
                    if notes:
                        break
            for note in notes:
                content = note.get("content", {}) or {}
                title = _clean_text(_content_value(content, "title"))
                if not _looks_like_paper_title(title):
                    continue
                note_id = note.get("id", "")
                forum = note.get("forum", note_id)
                url = f"https://openreview.net/forum?id={forum or note_id}"
                primary_area = _content_first_value(content, ["primary_area", "Primary Area", "area", "Area", "subject_area", "Subject Area"])
                category = primary_area or _content_first_value(content, ["category", "Category", "subject", "Subject"])
                track = _content_first_value(content, ["track", "Track", "venue", "Venue"])
                keywords = _content_first_list(content, ["keywords", "Keywords", "keyword", "Keyword"])
                classification_source = "official" if category or track or keywords else "llm_inferred"
                paper = {
                    "id": stable_id("paper", url),
                    "source": "openreview",
                    "title": title,
                    "authors": ", ".join(_content_list(content, "authors")),
                    "abstract": _clean_text(_content_value(content, "abstract")),
                    "url": url,
                    "pdf_url": f"https://openreview.net/pdf?id={note_id}" if note_id else "",
                    "venue": venue.get("name", ""),
                    "year": year,
                    "primary_area": primary_area,
                    "category": category,
                    "track": track,
                    "keywords": keywords,
                    "classification_source": classification_source,
                    "metadata": {
                        "venue_id": venue.get("id"),
                        "openreview_venueid": venue_id,
                        "primary_area": primary_area,
                        "category": category,
                        "track": track,
                        "keywords": keywords,
                    },
                }
                presentation = _presentation_type_from_text(track or category or primary_area)
                if presentation:
                    _set_presentation_metadata(paper, presentation, source="openreview_venue_text")
                papers.append(paper)
                if len(papers) >= max_items:
                    return _attach_openreview_metadata_audit(papers, list(queried_venue_ids), years)
    return _attach_openreview_metadata_audit(papers, list(queried_venue_ids), years) if papers else papers

def fetch_cvf_openaccess(venue: dict, years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    name = (venue.get("name") or "").upper()
    for year in years:
        url = f"https://openaccess.thecvf.com/{name}{year}?day=all"
        try:
            soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception:
            continue
        for title_node in soup.select("dt.ptitle a[href], dt a[href]"):
            title = _clean_text(title_node.get_text(" ", strip=True))
            if not _looks_like_paper_title(title):
                continue
            paper_url = requests.compat.urljoin(url, title_node["href"])
            pdf_url = paper_url.replace(".html", ".pdf")
            papers.append({
                "id": stable_id("paper", paper_url),
                "source": "cvf_openaccess",
                "title": title,
                "authors": "",
                "abstract": "",
                "url": paper_url,
                "pdf_url": pdf_url,
                "venue": venue.get("name", ""),
                "year": year,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": {"venue_id": venue.get("id"), "cvf_url": url},
            })
            if len(papers) >= max_items:
                return papers
        time.sleep(0.2)
    return papers


def fetch_eccv_virtual(years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    for year in years:
        if year % 2 == 1:
            continue
        list_url = f"https://eccv.ecva.net/virtual/{year}/papers.html"
        try:
            soup = BeautifulSoup(_request(list_url).text, "html.parser")
        except Exception:
            continue
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            title = _clean_text(anchor.get_text(" ", strip=True))
            href = anchor["href"]
            if not _looks_like_paper_title(title):
                continue
            if "/poster/" not in href and "/paper/" not in href:
                continue
            url = requests.compat.urljoin(list_url, href)
            if url in seen:
                continue
            seen.add(url)
            paper = {
                "id": stable_id("paper", url),
                "source": "eccv_virtual",
                "title": title,
                "authors": "",
                "abstract": "",
                "url": url,
                "pdf_url": "",
                "venue": "ECCV",
                "year": year,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": {"virtual_url": list_url, "detail_url": url, "title_index_only": True},
            }
            _set_presentation_metadata(paper, _presentation_type_from_url(url), source="eccv_virtual_url")
            papers.append(paper)
            if len(papers) >= max_items:
                return papers
    return papers


def _icml_event_paper_title(text: object) -> str:
    title = _clean_text(str(text or ""))
    lowered = title.lower()
    if not title or lowered in {"view full details", "view details", "details"}:
        return ""
    if lowered.startswith(("select year", "getting started", "schedule", "tutorials", "main conference", "community", "exhibitors", "organizers")):
        return ""
    return title if _looks_like_paper_title(title) else ""


def _fetch_icml_presentation_overrides(year: int) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    sources = [
        ("oral", f"https://icml.cc/virtual/{year}/events/oral"),
        ("spotlight", f"https://icml.cc/virtual/{year}/events/{year}SpotlightPosters"),
    ]
    overrides: dict[str, dict[str, str]] = {}
    audit: dict[str, Any] = {"attempted": True, "sources": [], "counts": {}}
    for presentation_type, url in sources:
        source_report: dict[str, Any] = {"presentation_type": presentation_type, "source_url": url, "matched_titles": 0}
        try:
            soup = BeautifulSoup(_request(url, timeout=30).text, "html.parser")
        except Exception as exc:
            source_report["error"] = str(exc)[:240]
            audit["sources"].append(source_report)
            continue
        seen_for_source: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            title = _icml_event_paper_title(anchor.get_text(" ", strip=True))
            if not title:
                continue
            href = requests.compat.urljoin(url, str(anchor.get("href") or ""))
            if f"/virtual/{year}/" not in href:
                continue
            if not any(part in href for part in ["/poster/", "/oral/", "/paper/", "/spotlight/"]):
                continue
            key = _title_key(title)
            if not key or key in seen_for_source:
                continue
            seen_for_source.add(key)
            existing = overrides.get(key)
            if isinstance(existing, dict) and str(existing.get("presentation_type") or "").lower() == "oral":
                continue
            if isinstance(existing, dict) and presentation_type != "oral":
                continue
            overrides[key] = {
                "presentation_type": presentation_type,
                "presentation_url": href,
                "presentation_source_url": url,
            }
        source_report["matched_titles"] = len(seen_for_source)
        audit["counts"][presentation_type] = len(seen_for_source)
        audit["sources"].append(source_report)
    applied_counts: dict[str, int] = {}
    for row in overrides.values():
        label = str(row.get("presentation_type") or "").strip().lower()
        if label:
            applied_counts[label] = applied_counts.get(label, 0) + 1
    audit["matched_title_count"] = len(overrides)
    audit["applied_counts"] = applied_counts
    return overrides, audit


def fetch_icml_downloads(years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    seen: set[str] = set()
    blocked = [
        "workshop", "tutorial", "exhibitor", "sponsor", "reviewer", "area chair",
        "call for", "organizing committee", "program committee", "policy", "registration",
        "accepted workshops", "accepted tutorials", "social", "mentoring",
    ]
    yeaudits: list[dict[str, Any]] = []
    truncated = False
    for year in years:
        url = f"https://icml.cc/Downloads/{year}"
        presentation_overrides, presentation_audit = _fetch_icml_presentation_overrides(int(year))
        raw_virtual_links = 0
        raw_paper_links = 0
        year_presentation_counts: dict[str, int] = {}
        try:
            soup = BeautifulSoup(_request(url, timeout=30).text, "html.parser")
        except Exception as exc:
            yeaudits.append({"year": year, "source_url": url, "error": str(exc)[:240], "complete": False, "presentation_audit": presentation_audit})
            continue
        for anchor in soup.find_all("a", href=True):
            title = _clean_text(anchor.get_text(" ", strip=True))
            lowered = title.lower()
            href = anchor["href"]
            paper_url = requests.compat.urljoin(url, href)
            if f"/virtual/{year}/" in paper_url:
                raw_virtual_links += 1
            if not _looks_like_paper_title(title):
                continue
            if any(term in lowered for term in blocked):
                continue
            # ICML Downloads includes navigation before the event list; papers/events use /virtual/<year>/... links.
            if f"/virtual/{year}/" not in paper_url:
                continue
            if not any(part in paper_url for part in ["/poster/", "/oral/", "/paper/", "/spotlight/"]):
                continue
            raw_paper_links += 1
            key = _title_key(title)
            if key in seen:
                continue
            seen.add(key)
            presentation_override = presentation_overrides.get(key) if isinstance(presentation_overrides, dict) else None
            metadata = {"venue_id": "dblp_icml", "icml_downloads_url": url, "detail_url": paper_url, "title_index_only": True}
            if isinstance(presentation_override, dict):
                metadata["presentation_url"] = presentation_override.get("presentation_url") or ""
                metadata["presentation_source_url"] = presentation_override.get("presentation_source_url") or ""
            paper = {
                "id": stable_id("paper", f"icml:{year}:{title}"),
                "source": "icml_downloads",
                "title": title,
                "authors": "",
                "abstract": "",
                "url": paper_url,
                "pdf_url": "",
                "venue": "ICML",
                "year": year,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": metadata,
            }
            if isinstance(presentation_override, dict) and presentation_override.get("presentation_type"):
                _set_presentation_metadata(paper, str(presentation_override.get("presentation_type") or ""), source="icml_events_presentation")
            else:
                _set_presentation_metadata(paper, _presentation_type_from_url(paper_url), source="icml_downloads_url")
            applied_presentation = str(paper.get("presentation_type") or "").strip().lower()
            if applied_presentation:
                year_presentation_counts[applied_presentation] = year_presentation_counts.get(applied_presentation, 0) + 1
            papers.append(paper)
            if len(papers) >= max_items:
                truncated = True
                break
        yeaudits.append({
            "year": year,
            "source_url": url,
            "raw_virtual_link_count": raw_virtual_links,
            "raw_paper_link_count": raw_paper_links,
            "deduped_paper_count_so_far": len(papers),
            "complete": not truncated and raw_paper_links > 0,
            "has_official_categories": False,
            "category_status": "no_official_categories",
            "presentation_audit": presentation_audit,
            "presentation_applied_counts": year_presentation_counts,
        })
        if truncated:
            break
        time.sleep(0.2)
    complete = bool(yeaudits) and all(bool(item.get("complete")) and not item.get("error") for item in yeaudits) and not truncated
    audit = _venue_metadata_audit(
        status="complete" if complete else "partial",
        source_verified=bool(yeaudits and not any(item.get("error") for item in yeaudits)),
        complete=complete,
        adapter="icml_downloads",
        source_url=";".join(str(item.get("source_url") or "") for item in yeaudits if item.get("source_url")),
        requested_years=[int(year) for year in years if str(year).isdigit()],
        source_yeaudits=yeaudits,
        deduped_paper_count=len(papers),
        has_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="official_icml_downloads_title_index",
        official_title_index_verified=complete,
        official_accepted_list_verified=complete,
        truncated=truncated,
        completeness_basis="Official ICML Downloads/virtual page is reachable and all qualifying paper links were scanned; no official category metadata is exposed by this adapter.",
        presentation_audits=[item.get("presentation_audit") for item in yeaudits if item.get("presentation_audit")],
        presentation_counts={
            label: sum(int((item.get("presentation_applied_counts") or {}).get(label) or 0) for item in yeaudits)
            for label in sorted({key for item in yeaudits for key in (item.get("presentation_applied_counts") or {}).keys()})
        },
    )
    return _attach_venue_metadata_audit(papers, audit)


def fetch_pmlr_index(venue: dict, years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    venue_name = (venue.get("name") or "").lower()
    known_volumes = {
        "icml": {2023: "v202", 2024: "v235", 2025: "v267"},
    }
    event_links: list[tuple[int, str]] = []
    for year in years:
        volume = known_volumes.get(venue_name, {}).get(year)
        if volume:
            event_links.append((year, f"https://proceedings.mlr.press/{volume}/"))
    try:
        soup = BeautifulSoup(_request("https://proceedings.mlr.press/").text, "html.parser")
    except Exception:
        soup = None
    if soup:
        for anchor in soup.find_all("a", href=True):
            text = _clean_text(anchor.get_text(" ", strip=True)).lower()
            if venue_name not in text:
                continue
            yematch = re.search(r"\b(20\d{2})\b", text)
            if not yematch:
                continue
            year = int(yematch.group(1))
            if year not in years:
                continue
            url = requests.compat.urljoin("https://proceedings.mlr.press/", anchor["href"])
            if (year, url) not in event_links:
                event_links.append((year, url))
    for year, url in event_links:
        try:
            event_soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception:
            continue
        for title_node in event_soup.select("p.title, div.paper p.title"):
            title = _clean_text(title_node.get_text(" ", strip=True))
            if not _looks_like_paper_title(title):
                continue
            parent = title_node.find_parent()
            link = parent.find("a", href=True) if parent else None
            paper_url = requests.compat.urljoin(url, link["href"]) if link else url
            papers.append({
                "id": stable_id("paper", f"{url}:{title}"),
                "source": "pmlr",
                "title": title,
                "authors": "",
                "abstract": "",
                "url": paper_url,
                "pdf_url": paper_url.replace(".html", ".pdf") if paper_url.endswith(".html") else "",
                "venue": venue.get("name", ""),
                "year": year,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": {"venue_id": venue.get("id"), "pmlr_url": url},
            })
            if len(papers) >= max_items:
                return papers
        time.sleep(0.2)
    return papers


def _pmlr_detail_url(paper: dict) -> str:
    if paper.get("url"):
        return str(paper.get("url") or "")
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    if metadata.get("pmlr_url"):
        return str(metadata.get("pmlr_url") or "")
    source_records = metadata.get("source_records") if isinstance(metadata.get("source_records"), dict) else {}
    pmlr_record = source_records.get("pmlr") if isinstance(source_records.get("pmlr"), dict) else {}
    return str(pmlr_record.get("url") or "")

def _extract_pmlr_abstract(soup: BeautifulSoup) -> str:
    abstract_node = soup.find(id=re.compile("abstract", re.I))
    if abstract_node:
        text = _clean_text(abstract_node.get_text(" ", strip=True))
        if text.lower().startswith("abstract "):
            text = text[len("abstract "):].strip()
        if text:
            return text
    heading = soup.find(lambda tag: tag.name in {"h2", "h3", "h4", "h5"} and _clean_text(tag.get_text(" ", strip=True)).lower() == "abstract")
    if heading:
        parts: list[str] = []
        for sibling in heading.next_siblings:
            if getattr(sibling, "name", None) in {"h1", "h2", "h3", "h4", "h5", "hr"}:
                break
            text = _clean_text(sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling))
            if text:
                parts.append(text)
        text = _clean_text(" ".join(parts))
        if text:
            return text
    bibtex = soup.find(string=re.compile(r"abstract\s*=", re.I))
    if bibtex:
        match = re.search(r"abstract\s*=\s*\{(.+?)\}\s*\}", str(bibtex), flags=re.I | re.S)
        if match:
            return _clean_text(match.group(1))
    return ""

def enrich_pmlr_details(papers: list[dict], limit: int | None = None) -> tuple[list[dict], dict]:
    attempted = 0
    abstracts_filled = 0
    urls_filled = 0
    pdfs_filled = 0
    candidates = papers if limit is None else papers[:limit]
    for paper in candidates:
        url = _pmlr_detail_url(paper)
        if not url or "proceedings.mlr.press" not in url:
            continue
        attempted += 1
        if not paper.get("url"):
            paper["url"] = url
            urls_filled += 1
        try:
            soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception:
            continue
        if not paper.get("abstract"):
            abstract = _extract_pmlr_abstract(soup)
            if abstract:
                paper["abstract"] = abstract
                paper.setdefault("metadata", {})["abstract_source"] = "pmlr"
                abstracts_filled += 1
        if not paper.get("pdf_url"):
            pdf_link = soup.find("a", string=re.compile("download pdf", re.I))
            if not pdf_link:
                pdf_link = soup.find("a", href=re.compile(r"\.pdf(?:$|\?)", re.I))
            if pdf_link and pdf_link.get("href"):
                paper["pdf_url"] = requests.compat.urljoin(url, pdf_link["href"])
                pdfs_filled += 1
        if paper.get("abstract") or paper.get("pdf_url"):
            paper.setdefault("metadata", {})["detail_source"] = "pmlr"
        time.sleep(0.1)
    return papers, {
        "attempted": attempted,
        "abstracts_filled": abstracts_filled,
        "urls_filled": urls_filled,
        "pdfs_filled": pdfs_filled,
    }


def _openreview_pdf_url(url: str) -> str:
    match = re.search(r"openreview\.net/forum\?id=([^&#]+)", url or "")
    if not match:
        return ""
    return f"https://openreview.net/pdf?id={match.group(1)}"


def _extract_between_markers(text: str, start: str, markers: list[str]) -> str:
    index = text.lower().find(start.lower())
    if index < 0:
        return ""
    body = text[index + len(start):]
    end_positions = [body.lower().find(marker.lower()) for marker in markers]
    end_positions = [pos for pos in end_positions if pos >= 0]
    if end_positions:
        body = body[: min(end_positions)]
    return "\n".join(line.strip() for line in body.splitlines() if line.strip()).strip()


def _parse_neurips_detail(html: str, url: str, fallback_title: str, year: int) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title_candidates = [fallback_title]
    for selector in [
        "meta[property='og:title']",
        "meta[name='twitter:title']",
    ]:
        node = soup.select_one(selector)
        if node and node.get("content"):
            title_candidates.append(str(node["content"]))
    title_candidates.extend(node.get_text(" ", strip=True) for node in soup.find_all(["h1", "h2", "h3"]))
    title = next((_clean_text(candidate) for candidate in title_candidates if _looks_like_paper_title(candidate)), fallback_title)
    text = soup.get_text("\n", strip=True)
    abstract = _extract_between_markers(text, "Abstract", ["Show more", "Video", "Chat is not available", "Successful Page Load"])

    authors = ""
    if title and title in text:
        after_title = text.split(title, 1)[1]
        before_abstract = after_title.split("Abstract", 1)[0]
        author_lines = [line.strip(" ·") for line in before_abstract.splitlines() if line.strip(" ·")]
        if author_lines:
            authors = author_lines[0]

    openreview_url = ""
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        label = anchor.get_text(" ", strip=True).lower()
        if "openreview.net" in href or "openreview" in label:
            openreview_url = href
            break

    paper_url = openreview_url or url
    paper = {
        "id": stable_id("paper", paper_url or f"neurips:{year}:{title}"),
        "source": "neurips_virtual",
        "title": title or fallback_title,
        "authors": authors,
        "abstract": abstract,
        "url": paper_url,
        "pdf_url": _openreview_pdf_url(openreview_url),
        "venue": "NeurIPS",
        "year": year,
        "category": "",
        "classification_source": "llm_inferred",
        "metadata": {"venue_url": url, "openreview_url": openreview_url},
    }
    presentation = _presentation_type_from_url(url)
    if not presentation:
        for candidate in title_candidates:
            presentation = _presentation_type_from_text(candidate)
            if presentation:
                break
    _set_presentation_metadata(paper, presentation, source="neurips_virtual_url_or_title")
    return paper


def _parse_neurips_list(html: str, list_url: str, max_items: int) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        title = _clean_text(anchor.get_text(" ", strip=True))
        if not any(part in href for part in ["/poster/", "/oral/", "/spotlight/", "/highlight/", "/paper/"]):
            continue
        if not _looks_like_paper_title(title):
            continue
        detail_url = requests.compat.urljoin(list_url, href)
        if detail_url in seen:
            continue
        seen.add(detail_url)
        candidates.append((detail_url, title))
        if len(candidates) >= max_items:
            break
    return candidates


def fetch_neurips_title_index(year: int, max_items: int, raise_errors: bool = False) -> list[dict]:
    list_url = f"https://neurips.cc/virtual/{year}/papers.html"
    try:
        candidates = _parse_neurips_list(_request(list_url).text, list_url, max_items)
    except Exception:
        if raise_errors:
            raise
        return []

    papers: list[dict] = []
    for detail_url, title in candidates:
        paper = {
            "id": stable_id("paper", detail_url),
            "source": "neurips_virtual",
            "title": title,
            "authors": "",
            "abstract": "",
            "url": detail_url,
            "pdf_url": "",
            "venue": "NeurIPS",
            "year": year,
            "category": "",
            "classification_source": "llm_inferred",
            "metadata": {"venue_url": detail_url, "detail_url": detail_url, "title_index_only": True},
        }
        _set_presentation_metadata(paper, _presentation_type_from_url(detail_url), source="neurips_virtual_url")
        papers.append(paper)
    return papers


def fetch_neurips_details(
    candidates: list[dict],
    year: int,
    *,
    wall_timeout_sec: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[dict]:
    papers: list[dict] = []
    started = time.monotonic()
    wall_timeout = wall_timeout_sec if wall_timeout_sec is not None else _positive_float_env(
        "NEURIPS_DETAIL_WALL_TIMEOUT_SEC",
        _positive_float_env("VENUE_DETAIL_WALL_TIMEOUT_SEC", 90.0),
    )
    request_timeout = int(_positive_float_env("NEURIPS_DETAIL_REQUEST_TIMEOUT_SEC", 12))
    cancel_check = should_cancel or (lambda: False)
    for candidate in candidates:
        detail_url = candidate.get("metadata", {}).get("detail_url") or candidate.get("url", "")
        title = candidate.get("title", "")
        if cancel_check():
            candidate.setdefault("metadata", {})["detail_fetch_deferred"] = True
            candidate["detail_fetch_deferred"] = True
            candidate["detail_fetch_deferred_reason"] = "cancel_requested"
            papers.append(candidate)
            continue
        if wall_timeout > 0 and time.monotonic() - started >= wall_timeout:
            candidate.setdefault("metadata", {})["detail_fetch_deferred"] = True
            candidate["detail_fetch_deferred"] = True
            candidate["detail_fetch_deferred_reason"] = f"wall_timeout_{wall_timeout:.0f}s"
            papers.append(candidate)
            continue
        try:
            detail_html = _request(detail_url, timeout=request_timeout).text
            papers.append(_parse_neurips_detail(detail_html, detail_url, title, year))
            time.sleep(0.2)
        except Exception as exc:
            fallback = {
                "id": stable_id("paper", detail_url),
                "source": "neurips_virtual",
                "title": title,
                "authors": "",
                "abstract": "",
                "url": detail_url,
                "pdf_url": "",
                "venue": "NeurIPS",
                "year": year,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": {"venue_url": detail_url, "detail_parse_error": True, "detail_fetch_error": str(exc)[:240]},
                "detail_fetch_deferred": True,
                "detail_fetch_deferred_reason": "detail_request_failed",
            }
            _set_presentation_metadata(fallback, _presentation_type_from_url(detail_url), source="neurips_virtual_url")
            papers.append(fallback)
    return papers


def fetch_neurips_virtual(year: int, max_items: int, raise_errors: bool = False) -> list[dict]:
    return fetch_neurips_details(fetch_neurips_title_index(year, max_items, raise_errors), year)


def _parse_dblp_yelinks(address: str, years: list[int], max_years: int = 4) -> list[tuple[int, str]]:
    if not address:
        return []
    def direct_links() -> list[tuple[int, str]]:
        cleaned = _dblp_page_url(address.rstrip("/"))
        key = cleaned.split("/")[-1]
        if key == "index.html" and "/" in cleaned:
            key = cleaned.split("/")[-2]
            cleaned = "/".join(cleaned.split("/")[:-1])
        return [(year, f"{cleaned}/{key}{year}.html") for year in years[:max_years]]

    try:
        soup = BeautifulSoup(_request(_dblp_page_url(address)).text, "html.parser")
    except Exception:
        return direct_links()

    wanted = {str(year) for year in years}
    links: list[tuple[int, str]] = []
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        href = anchor["href"]
        if href.startswith("http") and "dblp" not in href:
            continue
        if not href.startswith("http"):
            href = requests.compat.urljoin(address, href)
        href = _dblp_page_url(href)
        if "#" in href:
            continue
        if "/rec/" in href:
            continue
        if "/db/conf/" not in href and "/db/journals/" not in href:
            continue
        if _dblp_stream_id(address) and _dblp_stream_id(href) != _dblp_stream_id(address):
            continue
        if not re.search(r"/(?:conf|journals)/[^/]+/[^/?#]*(?:20\d{2}|19\d{2})[^/?#]*\.html?$", href):
            continue
        matched_years = [year for year in re.findall(r"(20\d{2}|19\d{2})", f"{text} {href}") if year in wanted]
        if not matched_years:
            continue
        year = int(matched_years[0])
        if (year, href) not in links:
            links.append((year, href))
        if len(links) >= max_years:
            break
    if not links:
        links = direct_links()
    return links


def _venue_cache_candidate_paths() -> list[Path]:
    root = ROOT
    roots = [WORKFLOW_RUNTIME_DIR / "finding" / "find_results.json"]
    project_ids = [
        value.strip()
        for value in [os.environ.get("PROJECT_ID"), os.environ.get("PROJECT_ID"), os.environ.get("DEFAULT_PROJECT_ID")]
        if value and value.strip()
    ]
    for project_id in dict.fromkeys(project_ids):
        roots.append(root / "projects" / project_id / "planning" / "finding" / "find_results.json")
    projects_root = root / "projects"
    if not project_ids and projects_root.exists():
        roots.extend(
            sorted(
                projects_root.glob("*/planning/finding/find_results.json"),
                key=lambda path: path.stat().st_mtime if path.exists() else 0,
                reverse=True,
            )[:10]
        )
    for runs_root in [RUNS_DIR, LEGACY_RUNS_DIR]:
        if runs_root.exists():
            roots.extend(sorted(runs_root.glob("find_*/find_results.json"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True))
    seen: set[Path] = set()
    paths: list[Path] = []
    for path in roots:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved in seen or not path.exists():
            continue
        seen.add(resolved)
        paths.append(path)
    return paths


def _verified_venue_yecache_from_paths(venue: dict, years: list[int], max_items: int | None, paths: list[Path]) -> list[dict]:
    venue_id = str(venue.get("id") or "").strip()
    venue_name = str(venue.get("name") or "").strip().upper()
    wanted_years = {int(year) for year in years if str(year).isdigit()}
    if not venue_id or not wanted_years:
        return []
    for find_path in paths:
        try:
            payload = json.loads(find_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        health_rows = payload.get("venue_health_report", []) if isinstance(payload, dict) else []
        verified_years: set[int] = set()
        for row in health_rows if isinstance(health_rows, list) else []:
            if not isinstance(row, dict):
                continue
            row_venue_id = str(row.get("venue_id") or "")
            row_venue = str(row.get("venue") or "").strip().upper()
            if row_venue_id != venue_id and row_venue != venue_name:
                continue
            if not row.get("ok"):
                continue
            if row.get("release_signal_source") not in {"source_observed_available", "cache_source_verified"}:
                continue
            if row.get("metadata_completeness_ok") is not True:
                continue
            for year in row.get("effective_years") or []:
                try:
                    yeint = int(year)
                except Exception:
                    continue
                if yeint in wanted_years:
                    verified_years.add(yeint)
        if not verified_years:
            continue
        rows: list[dict] = []
        for pool in ["raw_title_index", "evaluated_candidates", "title_candidates", "articles", "screened_ranking"]:
            for item in payload.get(pool, []) or []:
                if not isinstance(item, dict):
                    continue
                try:
                    yeint = int(item.get("year") or 0)
                except Exception:
                    continue
                item_venue_id = str((item.get("metadata") or {}).get("venue_id") or "") if isinstance(item.get("metadata"), dict) else ""
                item_venue = str(item.get("venue") or "").strip().upper()
                if yeint not in verified_years:
                    continue
                if item_venue_id != venue_id and item_venue != venue_name:
                    continue
                cached = dict(item)
                metadata = dict(cached.get("metadata") or {}) if isinstance(cached.get("metadata"), dict) else {}
                metadata.update({"venue_id": venue_id, "cache_source_run": payload.get("run_id") or find_path.parent.name, "cache_source_path": str(find_path)})
                cached["metadata"] = metadata
                cached["source"] = cached.get("source") or "venue_cache"
                cached["venue"] = cached.get("venue") or venue.get("name", "")
                rows.append(cached)
                if max_items is not None and len(rows) >= max_items:
                    break
            if max_items is not None and len(rows) >= max_items:
                break
        if rows:
            seen: set[str] = set()
            unique: list[dict] = []
            for item in rows:
                key = str(item.get("title") or item.get("url") or item.get("id") or "").strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                unique.append(item)
                if max_items is not None and len(unique) >= max_items:
                    return unique
            if unique:
                return unique
    return []


def _icml_verified_download_cache(venue: dict, years: list[int], max_items: int | None) -> list[dict]:
    if not is_icml_venue(venue):
        return []
    rows = _verified_venue_yecache_from_paths(venue, years, max_items, _venue_cache_candidate_paths())
    for row in rows:
        metadata = dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), dict) else {}
        metadata["cache_adapter"] = "icml_downloads_verified_cache"
        row["metadata"] = metadata
        row["source"] = row.get("source") or "icml_downloads"
    return rows


def _recent_verified_venue_yecache(venue: dict, years: list[int], max_items: int | None) -> list[dict]:
    if os.environ.get("ALLOW_OLD_RUN_VENUE_CACHE", "0").lower() not in {"1", "true", "yes", "on"}:
        return []
    venue_id = str(venue.get("id") or "").strip()
    venue_name = str(venue.get("name") or "").strip().upper()
    wanted_years = {int(year) for year in years if str(year).isdigit()}
    if not venue_id or not wanted_years:
        return []
    runs_roots = [path for path in [RUNS_DIR, LEGACY_RUNS_DIR] if path.exists()]
    if not runs_roots:
        return []
    for find_path in sorted((item for runs_root in runs_roots for item in runs_root.glob("find_*/find_results.json")), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True):
        try:
            payload = json.loads(find_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        health_rows = payload.get("venue_health_report", []) if isinstance(payload, dict) else []
        verified_years: set[int] = set()
        for row in health_rows if isinstance(health_rows, list) else []:
            if not isinstance(row, dict):
                continue
            if str(row.get("venue_id") or "") != venue_id:
                continue
            if row.get("release_signal_source") != "source_observed_available":
                continue
            if not row.get("ok"):
                continue
            if row.get("metadata_completeness_ok") is not True:
                continue
            for year in row.get("effective_years") or []:
                try:
                    yeint = int(year)
                except Exception:
                    continue
                if yeint in wanted_years:
                    verified_years.add(yeint)
        if not verified_years:
            continue
        rows: list[dict] = []
        for pool in ["raw_title_index", "evaluated_candidates", "title_candidates", "articles", "screened_ranking"]:
            for item in payload.get(pool, []) or []:
                if not isinstance(item, dict):
                    continue
                try:
                    yeint = int(item.get("year") or 0)
                except Exception:
                    continue
                item_venue_id = str((item.get("metadata") or {}).get("venue_id") or "") if isinstance(item.get("metadata"), dict) else ""
                item_venue = str(item.get("venue") or "").strip().upper()
                if yeint not in verified_years:
                    continue
                if item_venue_id != venue_id and item_venue != venue_name:
                    continue
                cached = dict(item)
                metadata = dict(cached.get("metadata") or {}) if isinstance(cached.get("metadata"), dict) else {}
                metadata.update({"venue_id": venue_id, "cache_source_run": payload.get("run_id") or find_path.parent.name, "cache_source_path": str(find_path)})
                cached["metadata"] = metadata
                cached["source"] = cached.get("source") or "dblp"
                cached["venue"] = cached.get("venue") or venue.get("name", "")
                rows.append(cached)
                if max_items is not None and len(rows) >= max_items:
                    break
            if max_items is not None and len(rows) >= max_items:
                break
        if rows:
            seen: set[str] = set()
            unique: list[dict] = []
            for item in rows:
                key = str(item.get("title") or item.get("url") or item.get("id") or "").strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                unique.append(item)
                if max_items is not None and len(unique) >= max_items:
                    return unique
            if unique:
                return unique
    return []


def _dblp_toc_papers(venue: dict, years: list[int], max_items: int | None) -> list[dict]:
    papers: list[dict] = []
    links = _parse_dblp_yelinks(venue.get("address", ""), years, max_years=max(4, len(years)))
    page_audits: list[dict[str, Any]] = []

    def reached_limit() -> bool:
        return max_items is not None and len(papers) >= max_items

    for year, url in links:
        if reached_limit():
            break
        url = _dblp_page_url(url)
        page_audit: dict[str, Any] = {"year": year, "url": url, "xml_url": re.sub(r"\.html?$", ".xml", url), "xml_paper_count": 0, "html_paper_count": 0, "status": "started"}
        count_before_page = len(papers)
        xml_url = str(page_audit["xml_url"])
        try:
            xml_text = _request(xml_url).text
            for record in re.findall(r"<(?:article|inproceedings)[^>]*>.*?</(?:article|inproceedings)>", xml_text, flags=re.S):
                title_match = re.search(r"<title>(.*?)</title>", record, flags=re.S)
                if not title_match:
                    continue
                title = _clean_text(html.unescape(re.sub(r"<.*?>", "", title_match.group(1)))).rstrip(".")
                if not _looks_like_paper_title(title):
                    continue
                ee_match = re.search(r"<ee>(.*?)</ee>", record, flags=re.S)
                paper_url = html.unescape(ee_match.group(1).strip()) if ee_match else ""
                key_match = re.match(r'<(?:article|inproceedings)[^>]*\bkey="([^\"]+)"', record)
                dblp_key = html.unescape(key_match.group(1).strip()) if key_match else ""
                authors = ", ".join(_clean_text(html.unescape(author)) for author in re.findall(r"<author[^>]*>(.*?)</author>", record, flags=re.S))
                doi = _doi_from_url(paper_url)
                metadata = _dblp_record_metadata(
                    venue.get("id"),
                    stream_id=_dblp_stream_id(venue.get("address", "")),
                    dblp_url=url,
                    dblp_xml_url=xml_url,
                    dblp_record_url=f"https://dblp.org/rec/{dblp_key}" if dblp_key else "",
                    dblp_key=dblp_key,
                    ee=paper_url,
                    doi=doi,
                )
                papers.append({
                    "id": stable_id("paper", f"{venue.get('id')}:{year}:{title}"),
                    "source": "dblp",
                    "title": title,
                    "authors": authors,
                    "abstract": "",
                    "url": paper_url,
                    "pdf_url": "",
                    "doi": doi,
                    "venue": venue.get("name", ""),
                    "year": year,
                    "category": "",
                    "classification_source": "llm_inferred",
                    "metadata": metadata,
                })
                page_audit["xml_paper_count"] = int(page_audit.get("xml_paper_count") or 0) + 1
                if reached_limit():
                    break
            if len(papers) > count_before_page:
                page_audit["status"] = "xml"
                page_audits.append(page_audit)
                continue
        except Exception as exc:
            page_audit["xml_error"] = str(exc)[:240]
        try:
            soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception as exc:
            page_audit["html_error"] = str(exc)[:240]
            page_audit["status"] = "failed"
            page_audits.append(page_audit)
            continue
        entries = soup.select("li.entry.inproceedings, li.entry.article")
        for entry in entries:
            title_node = entry.select_one("span.title")
            if not title_node:
                continue
            title = title_node.get_text(" ", strip=True).rstrip(".")
            if not _looks_like_paper_title(title):
                continue
            authors = ", ".join(node.get_text(" ", strip=True) for node in entry.select("span[itemprop='name']")[:-1])
            paper_url = ""
            drop = entry.select_one("li.drop-down a[href]")
            if drop:
                paper_url = drop.get("href", "")
            doi = _doi_from_url(paper_url)
            metadata = _dblp_record_metadata(
                venue.get("id"),
                stream_id=_dblp_stream_id(venue.get("address", "")),
                dblp_url=url,
                ee=paper_url,
                doi=doi,
            )
            papers.append({
                "id": stable_id("paper", f"{venue.get('id')}:{year}:{title}"),
                "source": "dblp",
                "title": title,
                "authors": authors,
                "abstract": "",
                "url": paper_url,
                "pdf_url": "",
                "doi": doi,
                "venue": venue.get("name", ""),
                "year": year,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": metadata,
            })
            page_audit["html_paper_count"] = int(page_audit.get("html_paper_count") or 0) + 1
            if reached_limit():
                break
        page_audit["status"] = "html" if len(papers) > count_before_page else "empty"
        page_audits.append(page_audit)
        time.sleep(0.5)
    complete = bool(papers) and bool(page_audits) and not any(str(item.get("status")) == "failed" for item in page_audits) and not (max_items is not None and len(papers) >= max_items)
    audit = _venue_metadata_audit(
        status="complete" if complete else "partial",
        title_index_completeness_status="complete" if complete else "partial",
        source_verified=bool(papers),
        complete=complete,
        title_index_complete=complete,
        dblp_toc_index_complete=complete,
        official_metadata_complete=False,
        adapter="dblp_toc",
        source_url=links[0][1] if links else _dblp_page_url(venue.get("address", "")),
        source_urls=[url for _year, url in links],
        requested_years=[int(year) for year in years if str(year).isdigit()],
        toc_page_audits=page_audits,
        toc_paper_count=len(papers),
        deduped_paper_count=len(papers),
        has_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="dblp_current_index_not_official_accepted_list",
        official_title_index_verified=False,
        official_accepted_list_verified=False,
        completeness_basis="DBLP venue table-of-contents XML/HTML title index. This is a DBLP index, not an official accepted-paper or ACM proceedings completeness certificate, and it exposes no abstracts or official categories.",
    )
    return _attach_venue_metadata_audit(papers, audit)


def _merge_dblp_paper_sources(venue: dict, stream_papers: list[dict], toc_papers: list[dict], max_items: int | None) -> list[dict]:
    if not stream_papers:
        return toc_papers
    if not toc_papers:
        return stream_papers
    merged: list[dict] = []
    seen: set[str] = set()
    for paper in [*toc_papers, *stream_papers]:
        if not isinstance(paper, dict):
            continue
        key = _title_key(paper.get("title")) or str((paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}).get("dblp_key") or paper.get("url") or paper.get("id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(dict(paper))
        if max_items is not None and len(merged) >= max_items:
            break
    stream_audit = venue_metadata_audit_from_papers(stream_papers)
    toc_audit = venue_metadata_audit_from_papers(toc_papers)
    truncated = bool(max_items is not None and len(merged) >= max_items and (len(stream_papers) > len(merged) or len(toc_papers) > len(merged)))
    complete = bool(toc_audit.get("complete")) and not truncated and len(merged) >= max(len(stream_papers), len(toc_papers))
    audit = _venue_metadata_audit(
        status="complete" if complete else "partial",
        title_index_completeness_status="complete" if complete else "partial",
        source_verified=True,
        complete=complete,
        title_index_complete=complete,
        dblp_stream_index_complete=bool(stream_audit.get("dblp_stream_index_complete") or stream_audit.get("complete")),
        dblp_toc_index_complete=bool(toc_audit.get("dblp_toc_index_complete") or toc_audit.get("complete")),
        official_metadata_complete=False,
        adapter="dblp_search_api+dblp_toc",
        source_url=str(toc_audit.get("source_url") or stream_audit.get("source_url") or "https://dblp.org/search/publ/api"),
        stream_audit=stream_audit,
        toc_audit=toc_audit,
        stream_paper_count=len(stream_papers),
        toc_paper_count=len(toc_papers),
        deduped_paper_count=len(merged),
        has_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="dblp_current_index_not_official_accepted_list",
        official_title_index_verified=False,
        official_accepted_list_verified=False,
        truncated=truncated,
        completeness_basis="Merged DBLP stream search with DBLP venue table-of-contents XML/HTML to avoid missing records when one DBLP index view lags another. This is still a DBLP current title index, not an official accepted-paper or ACM proceedings completeness certificate, and it exposes no abstracts or official categories.",
    )
    return _attach_venue_metadata_audit(merged, audit)


def fetch_dblp_venue(venue: dict, years: list[int], max_items: int | None) -> list[dict]:
    stream_papers = fetch_dblp_stream_api(venue, years, max_items)
    toc_papers = _dblp_toc_papers(venue, years, max_items)
    papers = _merge_dblp_paper_sources(venue, stream_papers, toc_papers, max_items)
    if papers:
        return papers

    cached = _recent_verified_venue_yecache(venue, years, max_items)
    if cached:
        return cached
    return []


def _acl_event_urls(venue: dict, year: int) -> list[str]:
    name = (venue.get("name") or "").lower()
    stems: list[str] = []
    if "emnlp" in name:
        stems = [f"emnlp-{year}", f"findings-{year}"]
    elif "naacl" in name:
        stems = [f"naacl-{year}", f"findings-{year}"]
    else:
        stems = [f"acl-{year}", f"findings-{year}"]
    return [f"https://aclanthology.org/events/{stem}/" for stem in stems]


def fetch_acl_anthology(venue: dict, years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    seen: set[str] = set()
    for year in years:
        for url in _acl_event_urls(venue, year):
            try:
                soup = BeautifulSoup(_request(url).text, "html.parser")
            except Exception:
                continue
            for anchor in soup.find_all("a", href=True):
                title = _clean_text(anchor.get_text(" ", strip=True))
                href = anchor["href"]
                if not _looks_like_paper_title(title):
                    continue
                if not re.search(rf"/{year}\.[a-z0-9-]+\.\d+/?$", href):
                    continue
                paper_url = requests.compat.urljoin(url, href)
                if paper_url in seen:
                    continue
                seen.add(paper_url)
                papers.append({
                    "id": stable_id("paper", paper_url),
                    "source": "acl_anthology",
                    "title": title,
                    "authors": "",
                    "abstract": "",
                    "url": paper_url,
                    "pdf_url": paper_url.rstrip("/") + ".pdf",
                    "venue": venue.get("name", "ACL Anthology"),
                    "year": year,
                    "category": "",
                    "classification_source": "llm_inferred",
                    "metadata": {"venue_id": venue.get("id"), "anthology_url": url},
                })
                if len(papers) >= max_items:
                    return papers
            time.sleep(0.2)
    return papers



def _paper_has_official_category(paper: dict) -> bool:
    if not isinstance(paper, dict):
        return False
    source = str(paper.get("classification_source") or "").lower()
    if source not in {"official", "official_cached", "venue_official", "openreview", "local_metadata_category"}:
        return False
    return bool(str(paper.get("primary_area") or paper.get("category") or paper.get("track") or "").strip())


def _venue_source_official_category_count(papers: list[dict]) -> int:
    return sum(1 for paper in papers if _paper_has_official_category(paper))


def _venue_source_audit(papers: list[dict], adapter: str) -> dict[str, Any]:
    audit = venue_metadata_audit_from_papers(papers)
    if not audit:
        missing_abstracts = sum(1 for paper in papers if not str((paper if isinstance(paper, dict) else {}).get("abstract") or "").strip())
        official_category_count = _venue_source_official_category_count(papers)
        audit = {
            "schema_version": 1,
            "status": "partial",
            "source_verified": bool(papers),
            "complete": False,
            "adapter": adapter,
            "paper_count": len(papers),
            "missing_abstract_count": missing_abstracts,
            "has_abstracts": bool(papers) and missing_abstracts == 0,
            "any_abstracts": bool(papers) and missing_abstracts < len(papers),
            "has_official_categories": official_category_count > 0,
            "category_status": "official_or_cached_categories" if official_category_count else "no_official_categories",
        }
    return audit


def _venue_source_has_official_categories(papers: list[dict], audit: dict[str, Any]) -> bool:
    status = str(audit.get("category_status") or "").lower()
    if status in {"no_official_categories", "missing_categories", "no_or_partial_categories"}:
        return False
    return bool(audit.get("has_official_categories")) or _venue_source_official_category_count(papers) > 0


def _venue_source_category_priority_eligible(papers: list[dict], audit: dict[str, Any], requested_limit: int | None, max_candidate_count: int | None = None) -> bool:
    if not papers or not _venue_source_has_official_categories(papers, audit):
        return False
    count = len(papers)
    if requested_limit and requested_limit > 0 and count >= requested_limit:
        return True
    if count >= 50:
        if not max_candidate_count or max_candidate_count <= 0:
            return True
        return count >= max(50, int(max_candidate_count * 0.10))
    if max_candidate_count and max_candidate_count > 0:
        return max_candidate_count < 50 and count >= max(1, int(max_candidate_count * 0.50))
    return False


def _venue_source_score(papers: list[dict], adapter: str, order: int, requested_limit: int | None, max_candidate_count: int) -> tuple:
    audit = _venue_source_audit(papers, adapter)
    category_priority = _venue_source_category_priority_eligible(papers, audit, requested_limit, max_candidate_count)
    official_title = bool(audit.get("official_title_index_verified") or audit.get("official_accepted_list_verified"))
    complete = bool(audit.get("complete") or audit.get("title_index_complete"))
    any_abstracts = bool(audit.get("any_abstracts") or audit.get("has_abstracts"))
    source_verified = bool(audit.get("source_verified"))
    return (
        1 if category_priority else 0,
        1 if complete else 0,
        1 if official_title else 0,
        1 if any_abstracts else 0,
        1 if source_verified else 0,
        len(papers),
        -order,
    )


def _choose_best_venue_source(candidates: list[tuple[str, list[dict]]], requested_limit: int | None) -> tuple[list[dict], str]:
    nonempty = [(adapter, papers) for adapter, papers in candidates if papers]
    if not nonempty:
        return [], "none"
    max_candidate_count = max(len(papers) for _adapter, papers in nonempty)
    scored = [
        (_venue_source_score(papers, adapter, order, requested_limit, max_candidate_count), adapter, papers)
        for order, (adapter, papers) in enumerate(nonempty)
    ]
    scored.sort(key=lambda row: row[0], reverse=True)
    _score, adapter, papers = scored[0]
    return papers, adapter


def _source_has_confident_official_categories(papers: list[dict], adapter: str, requested_limit: int | None) -> bool:
    audit = _venue_source_audit(papers, adapter)
    max_count = requested_limit if requested_limit and requested_limit > 0 else len(papers)
    return _venue_source_category_priority_eligible(papers, audit, requested_limit, max_count)


def _source_is_complete_official_title_index(papers: list[dict], adapter: str) -> bool:
    audit = _venue_source_audit(papers, adapter)
    return bool(papers) and bool(audit.get("complete") or audit.get("title_index_complete")) and bool(audit.get("official_title_index_verified") or audit.get("official_accepted_list_verified"))


def fetch_venue_title_index(venue: dict, years: list[int], max_items: int) -> tuple[list[dict], str]:
    candidates: list[tuple[str, list[dict]]] = []

    if is_openreview_supported_venue(venue):
        papers = fetch_openreview_venue(venue, years, max_items)
        if papers:
            candidates.append(("openreview", papers))
            if _source_has_confident_official_categories(papers, "openreview", max_items) or (max_items and len(papers) >= max_items):
                return papers, "openreview"
        if is_iclr_venue(venue) and 2026 in years:
            papers = fetch_openreview_iclr_2026(max_items)
            if papers:
                candidates.append(("openreview_reference", papers))
                if _source_has_confident_official_categories(papers, "openreview_reference", max_items) or (max_items and len(papers) >= max_items):
                    return papers, "openreview_reference"

    if is_neurips_venue(venue):
        papers: list[dict] = []
        for year in years:
            papers.extend(fetch_neurips_title_index(year, max_items))
            if len(papers) >= max_items:
                break
        if papers:
            candidates.append(("neurips_virtual", papers[:max_items]))

    if is_icml_venue(venue):
        papers = fetch_icml_downloads(years, max_items)
        if papers:
            candidates.append(("icml_downloads", papers))
            if _source_is_complete_official_title_index(papers, "icml_downloads"):
                return _choose_best_venue_source(candidates, max_items)
        cached = _icml_verified_download_cache(venue, years, max_items)
        if cached:
            candidates.append(("icml_downloads_cache", cached))

    if is_acl_family_venue(venue):
        papers = fetch_acl_anthology(venue, years, max_items)
        if papers:
            candidates.append(("acl_anthology", papers))

    if is_cvf_venue(venue):
        papers = fetch_cvf_openaccess(venue, years, max_items)
        if papers:
            candidates.append(("cvf_openaccess", papers))
        if (venue.get("name") or "").upper() == "ECCV":
            papers = fetch_eccv_virtual(years, max_items)
            if papers:
                candidates.append(("eccv_virtual", papers))

    if is_pmlr_venue(venue):
        papers = fetch_pmlr_index(venue, years, max_items)
        if papers:
            candidates.append(("pmlr", papers))

    if venue.get("address"):
        papers = fetch_dblp_venue(venue, years, max_items)
        if papers:
            candidates.append(("dblp", papers))

    return _choose_best_venue_source(candidates, max_items)

def _merge_enrichment(base: dict, enrichment: dict, adapter: str) -> dict:
    merged = dict(base)
    metadata = dict(base.get("metadata") or {})
    enrichment_metadata = dict(enrichment.get("metadata") or {})
    sources = metadata.setdefault("enrichment_sources", [])
    if adapter not in sources:
        sources.append(adapter)
    source_records = metadata.setdefault("source_records", {})
    source_records[adapter] = {
        "source": enrichment.get("source", adapter),
        "url": enrichment.get("url", ""),
        "pdf_url": enrichment.get("pdf_url", ""),
        "metadata": enrichment_metadata,
    }
    for key in ["abstract", "url", "pdf_url"]:
        if not merged.get(key) and enrichment.get(key):
            merged[key] = enrichment[key]
    if enrichment.get("url"):
        metadata.setdefault(f"{adapter}_url", enrichment.get("url"))
    if enrichment.get("category") and not merged.get("category"):
        merged["category"] = enrichment["category"]
    for key in ["primary_area", "track", "presentation_type", "presentation_label"]:
        if enrichment.get(key) and not merged.get(key):
            merged[key] = enrichment[key]
    for key in ["presentation_type", "presentation_label", "presentation_source"]:
        if enrichment_metadata.get(key) and not metadata.get(key):
            metadata[key] = enrichment_metadata[key]
    presentation_type = (
        enrichment.get("presentation_type")
        or enrichment_metadata.get("presentation_type")
        or _presentation_type_from_url(enrichment.get("url") or enrichment_metadata.get("detail_url") or enrichment_metadata.get("venue_url"))
        or _presentation_type_from_text(enrichment.get("track") or enrichment_metadata.get("presentation_label"))
    )
    if presentation_type:
        merged["metadata"] = metadata
        _set_presentation_metadata(merged, presentation_type, source=f"{adapter}_enrichment")
        metadata = merged.get("metadata", metadata)
    if isinstance(enrichment.get("keywords"), list):
        keywords = merged.get("keywords") if isinstance(merged.get("keywords"), list) else []
        merged["keywords"] = list(dict.fromkeys([*keywords, *[str(item) for item in enrichment["keywords"] if str(item)]]))
    if enrichment.get("classification_source") == "official":
        merged["classification_source"] = "official"
    merged["metadata"] = metadata
    return merged


def _merge_enrichments(base_papers: list[dict], enrichments: list[tuple[str, list[dict]]]) -> tuple[list[dict], list[str]]:
    merged = [dict(paper) for paper in base_papers]
    by_title_year = {
        (_title_key(paper.get("title", "")), int(paper.get("year") or 0)): index
        for index, paper in enumerate(merged)
        if _title_key(paper.get("title", ""))
    }
    used_adapters: list[str] = []
    for adapter, records in enrichments:
        matched = 0
        for record in records:
            key = (_title_key(record.get("title", "")), int(record.get("year") or 0))
            index = by_title_year.get(key)
            if index is None:
                continue
            merged[index] = _merge_enrichment(merged[index], record, adapter)
            matched += 1
        if matched:
            used_adapters.append(f"{adapter}:{matched}")
    return merged, used_adapters


def _fetch_enrichment_sources(venue: dict, years: list[int]) -> list[tuple[str, list[dict]]]:
    enrichments: list[tuple[str, list[dict]]] = []
    if is_openreview_supported_venue(venue):
        papers = fetch_openreview_venue(venue, years, 100000)
        if papers:
            enrichments.append(("openreview", papers))
    if is_neurips_venue(venue):
        papers: list[dict] = []
        for year in years:
            papers.extend(fetch_neurips_title_index(year, 100000))
        if papers:
            enrichments.append(("neurips_virtual", papers))
    if is_acl_family_venue(venue):
        papers = fetch_acl_anthology(venue, years, 100000)
        if papers:
            enrichments.append(("acl_anthology", papers))
    if is_cvf_venue(venue):
        papers = fetch_cvf_openaccess(venue, years, 100000)
        if papers:
            enrichments.append(("cvf_openaccess", papers))
        if (venue.get("name") or "").upper() == "ECCV":
            papers = fetch_eccv_virtual(years, 100000)
            if papers:
                enrichments.append(("eccv_virtual", papers))
    if is_icml_venue(venue):
        papers = fetch_icml_downloads(years, 100000)
        if papers:
            enrichments.append(("icml_downloads", papers))
    if is_pmlr_venue(venue):
        papers = fetch_pmlr_index(venue, years, 100000)
        if papers:
            enrichments.append(("pmlr", papers))
    return enrichments


def fetch_venue_title_index_all(venue: dict, years: list[int]) -> tuple[list[dict], str]:
    """Fetch the complete venue/year corpus with official-category sources preferred globally."""
    requested_limit = 100000
    candidates: list[tuple[str, list[dict]]] = []

    if is_openreview_supported_venue(venue):
        papers = fetch_openreview_venue(venue, years, requested_limit)
        if papers:
            candidates.append(("openreview", papers))
            if _source_has_confident_official_categories(papers, "openreview", requested_limit):
                return papers, "openreview"
        if is_iclr_venue(venue) and 2026 in years:
            papers = fetch_openreview_iclr_2026(requested_limit)
            if papers:
                candidates.append(("openreview_reference", papers))
                if _source_has_confident_official_categories(papers, "openreview_reference", requested_limit):
                    return papers, "openreview_reference"

    if is_neurips_venue(venue):
        papers: list[dict] = []
        for year in years:
            papers.extend(fetch_neurips_title_index(year, requested_limit))
        if papers:
            candidates.append(("neurips_virtual", papers))

    if is_acl_family_venue(venue):
        papers = fetch_acl_anthology(venue, years, requested_limit)
        if papers:
            candidates.append(("acl_anthology", papers))

    if is_cvf_venue(venue):
        papers = fetch_cvf_openaccess(venue, years, requested_limit)
        if papers:
            candidates.append(("cvf_openaccess", papers))
        if (venue.get("name") or "").upper() == "ECCV":
            papers = fetch_eccv_virtual(years, requested_limit)
            if papers:
                candidates.append(("eccv_virtual", papers))

    if is_icml_venue(venue):
        papers = fetch_icml_downloads(years, requested_limit)
        if papers:
            candidates.append(("icml_downloads", papers))
            if _source_is_complete_official_title_index(papers, "icml_downloads"):
                return _choose_best_venue_source(candidates, requested_limit)

    if is_pmlr_venue(venue):
        papers = fetch_pmlr_index(venue, years, requested_limit)
        if papers:
            candidates.append(("pmlr", papers))

    if venue.get("address"):
        base_papers = fetch_dblp_venue(venue, years, None)
        if base_papers:
            merged, used_adapters = _merge_enrichments(base_papers, _fetch_enrichment_sources(venue, years))
            adapter = "dblp"
            if used_adapters:
                adapter = f"dblp+{'+'.join(used_adapters)}"
            candidates.append((adapter, merged))

    return _choose_best_venue_source(candidates, requested_limit)

def _extract_conference_virtual_abstract(soup: BeautifulSoup) -> str:
    selectors = [
        "div[class*='abstract']",
        "section[class*='abstract']",
        "#abstract",
        "meta[name='citation_abstract']",
        "meta[name='description']",
        "meta[property='og:description']",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        text = str(node.get("content") or "") if node.name == "meta" else node.get_text(" ", strip=True)
        text = _clean_text(text)
        if text.lower().startswith("abstract "):
            text = text[len("abstract "):].strip()
        text = _strip_abstract_ui_controls(text)
        if len(text) >= 80:
            return text
    page_text = soup.get_text("\n", strip=True)
    text = _extract_between_markers(
        page_text,
        "Abstract",
        ["Show more", "Show less", "Video", "Chat is not available", "Successful Page Load", "BibTeX", "Supplementary"],
    )
    text = _strip_abstract_ui_controls(text)
    return text if len(text) >= 80 else ""


def _extract_icml_virtual_abstract(soup: BeautifulSoup) -> str:
    return _extract_conference_virtual_abstract(soup)


def _jsonld_nodes(value: object) -> list[dict]:
    nodes: list[dict] = []
    if isinstance(value, dict):
        nodes.append(value)
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict):
                    nodes.extend(_jsonld_nodes(item))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                nodes.extend(_jsonld_nodes(item))
    return nodes


def _name_from_jsonld_author(value: object) -> str:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, dict):
        for key in ["name", "givenName", "familyName"]:
            text = _clean_text(str(value.get(key) or ""))
            if text:
                if key == "givenName" and value.get("familyName"):
                    return _clean_text(f"{text} {value.get('familyName')}")
                return text
    return ""


def _extract_conference_virtual_authors(soup: BeautifulSoup) -> list[str]:
    authors: list[str] = []
    for node in soup.find_all("meta", attrs={"name": "citation_author"}):
        name = _clean_text(str(node.get("content") or ""))
        if name and name not in authors:
            authors.append(name)
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        for node in _jsonld_nodes(payload):
            raw_authors = node.get("author") or node.get("authors") or node.get("creator")
            if not raw_authors:
                continue
            values = raw_authors if isinstance(raw_authors, list) else [raw_authors]
            for value in values:
                name = _name_from_jsonld_author(value)
                if name and name not in authors:
                    authors.append(name)
    return authors


def _extract_icml_virtual_authors(soup: BeautifulSoup) -> list[str]:
    return _extract_conference_virtual_authors(soup)


def _positive_float_env(name: str, default: float = 0.0) -> float:
    try:
        value = float(str(os.environ.get(name, '') or default).strip())
    except Exception:
        value = default
    return value if value > 0 else default


def _mark_detail_fetch_deferred(paper: dict, reason: str) -> None:
    metadata = paper.setdefault('metadata', {})
    metadata['detail_fetch_deferred'] = True
    metadata['detail_fetch_deferred_reason'] = reason
    paper['detail_fetch_deferred'] = True
    paper['detail_fetch_deferred_reason'] = reason


def _conference_virtual_detail_url(paper: dict) -> str:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    return str(metadata.get("detail_url") or metadata.get("venue_url") or paper.get("url") or "").strip()


def _conference_virtual_detail_source(paper: dict) -> str:
    source = str(paper.get("source") or "").strip()
    url = _conference_virtual_detail_url(paper)
    if not url or "/virtual/" not in url:
        return ""
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    if source in {"icml_downloads", "icml_downloads_cache", "eccv_virtual", "neurips_virtual"}:
        if metadata.get("title_index_only") or not (paper.get("abstract") and paper.get("pdf_url")):
            return source
    lowered = url.lower()
    if any(domain in lowered for domain in ["icml.cc/virtual/", "neurips.cc/virtual/", "eccv.ecva.net/virtual/"]):
        if metadata.get("title_index_only") or not (paper.get("abstract") and paper.get("pdf_url")):
            return source or "conference_virtual"
    return ""


def _conference_virtual_detail_target(paper: dict) -> bool:
    return bool(_conference_virtual_detail_source(paper))


def _icml_virtual_detail_target(paper: dict) -> bool:
    return _conference_virtual_detail_source(paper) in {"icml_downloads", "icml_downloads_cache"}


def _conference_virtual_detail_label(paper: dict) -> str:
    source = _conference_virtual_detail_source(paper)
    if source in {"icml_downloads", "icml_downloads_cache"}:
        return "icml_virtual"
    if source and source.endswith("_virtual"):
        return f"{source}_detail"
    return "conference_virtual_detail"


def _fetch_one_conference_virtual_detail(paper: dict, request_timeout: int) -> dict:
    url = _conference_virtual_detail_url(paper)
    detail_label = _conference_virtual_detail_label(paper)
    result = {"abstract_filled": False, "authors_filled": False, "pdf_filled": False, "error": ""}
    try:
        soup = BeautifulSoup(_request(url, timeout=request_timeout).text, "html.parser")
    except Exception as exc:
        result["error"] = str(exc)[:240]
        return result
    metadata = paper.setdefault("metadata", {})
    presentation = _presentation_type_from_url(url)
    if not presentation:
        snippets: list[str] = []
        for selector in ["meta[property='og:title']", "meta[name='twitter:title']"]:
            node = soup.select_one(selector)
            if node and node.get("content"):
                snippets.append(str(node["content"]))
        if soup.title:
            snippets.append(soup.title.get_text(" ", strip=True))
        snippets.extend(node.get_text(" ", strip=True) for node in soup.find_all(["h1", "h2"], limit=4))
        for snippet in snippets:
            presentation = _presentation_type_from_text(snippet)
            if presentation:
                break
    _set_presentation_metadata(paper, presentation, source=f"{detail_label}_url_or_title")
    if not paper.get("authors"):
        authors = _extract_conference_virtual_authors(soup)
        if authors:
            paper["authors"] = ", ".join(authors)
            metadata["authors_source"] = f"{detail_label}_jsonld"
            metadata["virtual_author_count"] = len(authors)
            result["authors_filled"] = True
    if not paper.get("abstract"):
        abstract = _extract_conference_virtual_abstract(soup)
        if abstract:
            paper["abstract"] = abstract
            metadata["abstract_source"] = detail_label
            result["abstract_filled"] = True
    if not paper.get("pdf_url"):
        pdf_link = soup.find("a", href=re.compile(r"\.pdf(?:$|\?)", re.I))
        if pdf_link and pdf_link.get("href"):
            paper["pdf_url"] = requests.compat.urljoin(url, pdf_link["href"])
            result["pdf_filled"] = True
    if paper.get("abstract") or paper.get("authors") or paper.get("pdf_url"):
        metadata["detail_source"] = detail_label
    if not paper.get("pdf_url"):
        metadata.setdefault("full_text_locator_status", "official_virtual_abstract_page_without_pdf_link")
    return result


def _fetch_one_icml_virtual_detail(paper: dict, request_timeout: int) -> dict:
    return _fetch_one_conference_virtual_detail(paper, request_timeout)


def enrich_conference_virtual_details(papers: list[dict], limit: int | None = None, *, wall_timeout_sec: float | None = None, should_cancel: Callable[[], bool] | None = None) -> tuple[list[dict], dict]:
    attempted = 0
    abstracts_filled = 0
    authors_filled = 0
    pdfs_filled = 0
    timed_out = False
    cancelled = False
    candidates = papers if limit is None else papers[:limit]
    targets = [paper for paper in candidates if _conference_virtual_detail_target(paper)]
    started = time.monotonic()
    wall_timeout = wall_timeout_sec if wall_timeout_sec is not None else _positive_float_env(
        "CONFERENCE_VIRTUAL_DETAIL_WALL_TIMEOUT_SEC",
        _positive_float_env("ICML_DETAIL_WALL_TIMEOUT_SEC", _positive_float_env("VENUE_DETAIL_WALL_TIMEOUT_SEC", 180.0)),
    )
    request_timeout = int(_positive_float_env(
        "CONFERENCE_VIRTUAL_DETAIL_REQUEST_TIMEOUT_SEC",
        _positive_float_env("ICML_DETAIL_REQUEST_TIMEOUT_SEC", _metadata_timeout(6)),
    ))
    worker_default = min(16, max(1, len(targets))) if targets else 1
    max_workers = int(_positive_float_env("CONFERENCE_VIRTUAL_DETAIL_WORKERS", _positive_float_env("ICML_DETAIL_WORKERS", worker_default)))
    max_workers = max(1, min(32, max_workers, max(1, len(targets))))
    cancel_check = should_cancel or (lambda: False)
    if not targets:
        return papers, {
            "attempted": 0,
            "abstracts_filled": 0,
            "authors_filled": 0,
            "pdfs_filled": 0,
            "timed_out": False,
            "cancelled": False,
            "wall_timeout_sec": wall_timeout,
            "request_timeout_sec": request_timeout,
            "workers": max_workers,
            "deferred": 0,
            "sources": {},
        }

    source_counts: dict[str, int] = {}
    for paper in targets:
        label = _conference_virtual_detail_label(paper)
        source_counts[label] = source_counts.get(label, 0) + 1

    future_to_paper: dict = {}
    pending_iter = iter(targets)
    completed: set[int] = set()

    def submit_next(executor: ThreadPoolExecutor) -> bool:
        nonlocal attempted, cancelled, timed_out
        if cancel_check():
            cancelled = True
            return False
        if wall_timeout > 0 and time.monotonic() - started >= wall_timeout:
            timed_out = True
            return False
        try:
            paper = next(pending_iter)
        except StopIteration:
            return False
        attempted += 1
        future_to_paper[executor.submit(_fetch_one_conference_virtual_detail, paper, request_timeout)] = paper
        return True

    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        while len(future_to_paper) < max_workers and submit_next(executor):
            pass
        while future_to_paper:
            if cancel_check():
                cancelled = True
                break
            elapsed = time.monotonic() - started
            if wall_timeout > 0 and elapsed >= wall_timeout:
                timed_out = True
                break
            wait_timeout = 1.0
            if wall_timeout > 0:
                wait_timeout = max(0.05, min(wait_timeout, wall_timeout - elapsed))
            done, _pending = wait(list(future_to_paper), timeout=wait_timeout, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                paper = future_to_paper.pop(future)
                completed.add(id(paper))
                try:
                    result = future.result()
                except Exception as exc:
                    paper.setdefault("metadata", {})["detail_fetch_error"] = str(exc)[:240]
                    continue
                if result.get("error"):
                    paper.setdefault("metadata", {})["detail_fetch_error"] = result["error"]
                if result.get("abstract_filled"):
                    abstracts_filled += 1
                if result.get("authors_filled"):
                    authors_filled += 1
                if result.get("pdf_filled"):
                    pdfs_filled += 1
            while len(future_to_paper) < max_workers and submit_next(executor):
                pass
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    for future, paper in list(future_to_paper.items()):
        if future.cancelled():
            paper.setdefault("metadata", {})["detail_fetch_error"] = "future_cancelled"
            continue
        if future.done() and id(paper) not in completed:
            try:
                result = future.result()
            except Exception as exc:
                paper.setdefault("metadata", {})["detail_fetch_error"] = str(exc)[:240]
                continue
            completed.add(id(paper))
            if result.get("error"):
                paper.setdefault("metadata", {})["detail_fetch_error"] = result["error"]
            if result.get("abstract_filled"):
                abstracts_filled += 1
            if result.get("authors_filled"):
                authors_filled += 1
            if result.get("pdf_filled"):
                pdfs_filled += 1

    if cancelled or timed_out:
        reason = "cancel_requested" if cancelled else f"wall_timeout_{wall_timeout:.0f}s"
        for paper in targets:
            if id(paper) not in completed:
                _mark_detail_fetch_deferred(paper, reason)

    return papers, {
        "attempted": attempted,
        "abstracts_filled": abstracts_filled,
        "authors_filled": authors_filled,
        "pdfs_filled": pdfs_filled,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "wall_timeout_sec": wall_timeout,
        "request_timeout_sec": request_timeout,
        "workers": max_workers,
        "deferred": sum(1 for paper in targets if paper.get("detail_fetch_deferred")),
        "sources": source_counts,
    }


def enrich_icml_virtual_details(papers: list[dict], limit: int | None = None, *, wall_timeout_sec: float | None = None, should_cancel: Callable[[], bool] | None = None) -> tuple[list[dict], dict]:
    return enrich_conference_virtual_details(papers, limit=limit, wall_timeout_sec=wall_timeout_sec, should_cancel=should_cancel)

def fetch_selected_venue_details(papers: list[dict], *, should_cancel: Callable[[], bool] | None = None, wall_timeout_sec: float | None = None) -> list[dict]:
    details: list[dict] = []
    neurips_by_year: dict[int, list[dict]] = {}
    conference_virtual: list[dict] = []
    cancel_check = should_cancel or (lambda: False)
    for candidate in papers:
        if cancel_check():
            _mark_detail_fetch_deferred(candidate, 'cancel_requested')
            details.append(candidate)
            continue
        metadata = candidate.get('metadata') if isinstance(candidate.get('metadata'), dict) else {}
        if candidate.get('source') == 'neurips_virtual' and metadata.get('title_index_only'):
            neurips_by_year.setdefault(int(candidate.get('year') or date.today().year), []).append(candidate)
        elif _conference_virtual_detail_target(candidate):
            conference_virtual.append(candidate)
        else:
            details.append(candidate)

    for year, items in neurips_by_year.items():
        if cancel_check():
            for item in items:
                _mark_detail_fetch_deferred(item, 'cancel_requested')
            details.extend(items)
            continue
        details.extend(fetch_neurips_details(items, year, wall_timeout_sec=wall_timeout_sec, should_cancel=cancel_check))
    if conference_virtual:
        enriched, stats = enrich_conference_virtual_details(conference_virtual, wall_timeout_sec=wall_timeout_sec, should_cancel=cancel_check)
        for item in enriched:
            if isinstance(item.get('metadata'), dict):
                item['metadata'].setdefault('detail_fetch_stats', stats)
        details.extend(enriched)
    return details

def fetch_venue_sample(venue: dict, year: int, sample_limit: int = 3) -> dict:
    adapter = "dblp"
    try:
        if is_iclr_venue(venue):
            adapter = "openreview"
            papers = fetch_openreview_venue(venue, [year], sample_limit)
            if not papers and year == 2026:
                adapter = "openreview_reference"
                papers = fetch_openreview_iclr_2026(sample_limit)
            if not papers and venue.get("address"):
                adapter = "dblp"
                papers = fetch_dblp_venue(venue, [year], sample_limit)
        elif is_neurips_venue(venue):
            adapter = "openreview"
            papers = fetch_openreview_venue(venue, [year], sample_limit)
            if not papers:
                adapter = "neurips_virtual"
                papers = fetch_neurips_virtual(year, sample_limit)
            if not papers and venue.get("address"):
                adapter = "dblp"
                papers = fetch_dblp_venue(venue, [year], sample_limit)
        else:
            papers = []
            if venue.get("address"):
                papers = fetch_dblp_venue(venue, [year], sample_limit)
            if not papers and is_iclr_venue(venue):
                adapter = "openreview"
                papers = fetch_openreview_venue(venue, [year], sample_limit)
            elif not papers and is_neurips_venue(venue):
                adapter = "neurips_virtual"
                papers = fetch_neurips_virtual(year, sample_limit)
                if not papers:
                    adapter = "openreview"
                    papers = fetch_openreview_venue(venue, [year], sample_limit)
            elif not papers and is_acl_family_venue(venue):
                adapter = "acl_anthology"
                papers = fetch_acl_anthology(venue, [year], sample_limit)
            elif not papers and is_cvf_venue(venue):
                adapter = "cvf_openaccess"
                papers = fetch_cvf_openaccess(venue, [year], sample_limit)
                if not papers and (venue.get("name") or "").upper() == "ECCV":
                    adapter = "eccv_virtual"
                    papers = fetch_eccv_virtual([year], sample_limit)
            elif not papers and is_pmlr_venue(venue):
                adapter = "pmlr"
                papers = fetch_pmlr_index(venue, [year], sample_limit)
            if not papers and is_openreview_supported_venue(venue):
                adapter = "openreview"
                papers = fetch_openreview_venue(venue, [year], sample_limit)
        samples = [
            {
                "title": paper.get("title", ""),
                "url": paper.get("url", ""),
                "abstract": (paper.get("abstract", "") or "")[:300],
            }
            for paper in papers[:sample_limit]
        ]
        return {
            "venue_id": venue.get("id", ""),
            "year": year,
            "ok": bool(samples),
            "sample_count": len(samples),
            "source_adapter": adapter,
            "message": "ok" if samples else f"No papers fetched via {adapter}.",
            "samples": samples,
        }
    except Exception as exc:
        return {
            "venue_id": venue.get("id", ""),
            "year": year,
            "ok": False,
            "sample_count": 0,
            "source_adapter": adapter,
            "message": str(exc),
            "samples": [],
        }


def enrich_with_semantic_scholar(papers: list[dict], limit: int = 20, api_key: str = "") -> list[dict]:
    headers = dict(HEADERS)
    if api_key:
        headers["x-api-key"] = api_key
    cache = _load_semantic_scholar_cache()
    cache_changed = False

    def _cache_keys(paper: dict) -> list[str]:
        keys: list[str] = []
        metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
        doi = _doi_from_url(str(paper.get("doi") or paper.get("url") or paper.get("pdf_url") or ""))
        doi = doi or _doi_from_url(str(metadata.get("doi") or metadata.get("doi_url") or metadata.get("publisher_url") or ""))
        if doi:
            keys.append("doi:" + doi.lower())
        title_key = _semantic_scholar_cache_key(paper.get("title", ""))
        if title_key:
            keys.append("title:" + title_key)
            keys.append(title_key)  # backward compatibility with the old title-only cache.
        return list(dict.fromkeys(keys))

    def _apply_item(paper: dict, item: dict, *, source: str) -> dict:
        metadata = paper.setdefault("metadata", {})
        abstract = str(item.get("abstract") or "")
        tldr = item.get("tldr") if isinstance(item.get("tldr"), dict) else {}
        tldr_text = str(tldr.get("text") or "")
        if abstract:
            paper["abstract"] = abstract
            metadata["abstract_source"] = source
        elif tldr_text:
            metadata["semantic_scholar_tldr_available"] = True
        if item.get("url"):
            paper["url"] = paper.get("url") or item.get("url") or ""
        pdf = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), dict) else {}
        pdf_url = str(pdf.get("url") or "")
        if pdf_url and not paper.get("pdf_url"):
            paper["pdf_url"] = pdf_url
        if tldr_text:
            metadata["tldr"] = tldr_text
        external = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
        if external:
            metadata["semantic_scholar_external_ids"] = external
            if external.get("ArXiv") and not paper.get("pdf_url"):
                paper["pdf_url"] = f"https://arxiv.org/pdf/{external.get('ArXiv')}"
        if item.get("paperId"):
            metadata["semantic_scholar_paper_id"] = item.get("paperId")
        return {
            "title": paper.get("title", ""),
            "abstract": paper.get("abstract") or "",
            "url": item.get("url") or "",
            "pdf_url": paper.get("pdf_url") or pdf_url,
            "tldr": tldr_text,
            "semantic_scholar_paper_id": item.get("paperId") or "",
            "externalIds": external,
            "source": source,
            "miss": not bool(abstract or paper.get("abstract")),
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }

    fields = "title,abstract,tldr,openAccessPdf,url,externalIds,authors"
    for paper in papers[:limit]:
        if paper.get("abstract"):
            continue
        keys = _cache_keys(paper)
        cached_hit = False
        for key in keys:
            cached = cache.get(key)
            if isinstance(cached, dict):
                _apply_semantic_scholar_cache(paper, cached)
                if cached.get("externalIds") and isinstance(paper.get("metadata"), dict):
                    paper["metadata"].setdefault("semantic_scholar_external_ids", cached.get("externalIds"))
                if paper.get("abstract"):
                    cached_hit = True
                    break
        if cached_hit:
            continue
        if keys and all(_semantic_scholar_cache_is_permanent_miss(cache.get(key)) for key in keys):
            continue

        doi = ""
        for key in keys:
            if key.startswith("doi:"):
                doi = key.split(":", 1)[1]
                break
        urls: list[tuple[str, str]] = []
        if doi:
            urls.append(("semantic_scholar_doi", f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote_plus(doi)}?fields={fields}"))
        query = quote_plus(re.sub(r"[():/\-]", " ", paper.get("title", "")))
        if query:
            urls.append(("semantic_scholar_title", f"https://api.semanticscholar.org/graph/v1/paper/search?query={query}&limit=3&fields={fields}"))

        found_cache: dict | None = None
        lookup_errors: list[str] = []
        for source, url in urls:
            try:
                response = requests.get(url, headers=headers, timeout=_metadata_timeout(6))
                if response.status_code != 200:
                    lookup_errors.append(f"{source}:http_{response.status_code}")
                    continue
                payload = response.json()
                if source.endswith("_title"):
                    rows = payload.get("data", []) if isinstance(payload, dict) else []
                    item = {}
                    for row in rows if isinstance(rows, list) else []:
                        if not isinstance(row, dict):
                            continue
                        row_title = str(row.get("title") or "")
                        if (row_title and _title_token_similarity(paper.get("title"), row_title) >= 0.82) or (not row_title and len(rows) == 1):
                            item = row
                            break
                else:
                    item = payload if isinstance(payload, dict) else {}
                if not item:
                    continue
                found_cache = _apply_item(paper, item, source=source)
                if paper.get("abstract"):
                    break
            except Exception as exc:
                lookup_errors.append(f"{source}:{str(exc)[:120]}")
                continue
        if found_cache:
            for key in keys:
                cache[key] = dict(found_cache)
            cache_changed = True
        else:
            retryable_failure = _semantic_scholar_errors_retryable(lookup_errors)
            if lookup_errors:
                metadata = paper.setdefault("metadata", {})
                metadata["semantic_scholar_lookup_error"] = "; ".join(lookup_errors[:3])
                if retryable_failure:
                    metadata["semantic_scholar_lookup_retryable"] = True
            if not retryable_failure:
                miss = {
                    "title": paper.get("title", ""),
                    "miss": True,
                    "lookup_errors": lookup_errors[:5],
                    "retryable": False,
                    "updated_at": datetime.utcnow().isoformat() + "Z",
                }
                for key in keys:
                    cache[key] = miss
                cache_changed = True
        time.sleep(0.2)
    if cache_changed:
        _save_semantic_scholar_cache(cache)
    return papers


def _arxiv_title_match_cache_path() -> Path:
    return STATE_DIR / "arxiv_title_match_cache.json"


def _load_arxiv_title_match_cache() -> dict:
    path = Path(_arxiv_title_match_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_arxiv_title_match_cache(cache: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        Path(_arxiv_title_match_cache_path()).write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        return


ARXIV_TITLE_MATCH_QUERY_VERSION = "v2_exact_title_multiquery"


def _title_match_queries(title: str) -> list[str]:
    clean_title = _clean_text(" ".join(re.findall(r"[A-Za-z0-9]+", title or "")))
    terms = [
        term
        for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", title or "")
        if len(term) >= 3 and term.lower() not in {"and", "for", "the", "with", "via"}
    ]
    queries: list[str] = []
    if clean_title:
        queries.append(f'ti:"{clean_title}"')
        queries.append(f'all:"{clean_title}"')
    if terms:
        queries.append(" AND ".join(f"ti:{term}" for term in terms[:16]))
        queries.append(" AND ".join(f"all:{term}" for term in terms[:10]))
    deduped: list[str] = []
    for query in queries:
        if query and query not in deduped:
            deduped.append(query)
    return deduped


def _arxiv_entry_authors(entry: ET.Element, ns: dict[str, str]) -> list[str]:
    return [node.text or "" for node in entry.findall("a:author/a:name", ns)]


def enrich_with_arxiv_title_match(papers: list[dict], limit: int = 40) -> list[dict]:
    # Metadata enrichment only: original venue rows still need final title+abstract LLM scoring.
    cache = _load_arxiv_title_match_cache()
    cache_changed = False
    ns = {"a": "http://www.w3.org/2005/Atom"}
    timeout = max(20, int(os.environ.get("ARXIV_TITLE_MATCH_TIMEOUT_SEC", "25") or 25))
    max_results = max(1, min(5, int(os.environ.get("ARXIV_TITLE_MATCH_MAX_RESULTS", "3") or 3)))
    for paper in papers[:limit]:
        if paper.get("abstract") and paper.get("pdf_url"):
            continue
        title = _clean_text(str(paper.get("title") or ""))
        if not title:
            continue
        cache_key = _semantic_scholar_cache_key(title)
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            if cached.get("abstract") and not paper.get("abstract"):
                paper["abstract"] = cached.get("abstract") or ""
            if cached.get("url"):
                paper["url"] = paper.get("url") or cached.get("url") or ""
            if cached.get("pdf_url"):
                paper["pdf_url"] = paper.get("pdf_url") or cached.get("pdf_url") or ""
            if cached.get("abstract") or cached.get("pdf_url"):
                metadata = paper.setdefault("metadata", {})
                metadata["abstract_source"] = metadata.get("abstract_source") or "arxiv_title_match_cache"
                metadata["arxiv_title_match_id"] = cached.get("arxiv_id") or ""
                metadata["arxiv_title_similarity"] = cached.get("similarity", 0)
                if cached.get("author_overlap"):
                    metadata["arxiv_title_match_author_overlap"] = cached.get("author_overlap")
            if paper.get("abstract") and paper.get("pdf_url"):
                continue
            if cached.get("miss") and cached.get("query_version") == ARXIV_TITLE_MATCH_QUERY_VERSION:
                continue
        queries = _title_match_queries(title)
        if not queries:
            continue
        best = None
        best_similarity = 0.0
        best_query = ""
        expected_authors = _author_family_tokens(paper.get("authors"))
        query_errors: list[str] = []
        for query_text in queries:
            url = "https://export.arxiv.org/api/query?search_query=" + quote_plus(query_text) + f"&sortBy=submittedDate&sortOrder=descending&start=0&max_results={max_results}"
            try:
                root = ET.fromstring(_request_arxiv_page(url, timeout).text)
            except Exception as exc:
                query_errors.append(str(exc)[:120])
                continue
            for entry in root.findall("a:entry", ns):
                candidate_title = " ".join((entry.findtext("a:title", default="", namespaces=ns) or "").split())
                similarity = _title_token_similarity(title, candidate_title)
                if similarity <= best_similarity:
                    continue
                candidate_authors = _arxiv_entry_authors(entry, ns)
                candidate_family = _author_family_tokens(candidate_authors)
                author_overlap = sorted(expected_authors & candidate_family)
                if expected_authors and not author_overlap:
                    continue
                entry_id = entry.findtext("a:id", default="", namespaces=ns) or ""
                abstract = " ".join((entry.findtext("a:summary", default="", namespaces=ns) or "").split())
                best_similarity = similarity
                best_query = query_text
                best = {
                    "title": candidate_title,
                    "abstract": abstract,
                    "url": entry_id,
                    "pdf_url": entry_id.replace("/abs/", "/pdf/") if "/abs/" in entry_id else "",
                    "arxiv_id": _arxiv_entry_id(entry_id),
                    "similarity": similarity,
                    "authors": candidate_authors,
                    "author_overlap": author_overlap,
                }
        if not best or best_similarity < 0.92 or not (best.get("abstract") or paper.get("abstract")):
            metadata = paper.setdefault("metadata", {})
            if query_errors:
                metadata["arxiv_title_match_error"] = "; ".join(query_errors[:3])
            cache[cache_key] = {"title": title, "miss": True, "query_version": ARXIV_TITLE_MATCH_QUERY_VERSION, "updated_at": datetime.utcnow().isoformat() + "Z"}
            cache_changed = True
            continue
        if best.get("abstract") and not paper.get("abstract"):
            paper["abstract"] = best["abstract"]
        paper["url"] = paper.get("url") or best["url"]
        paper["pdf_url"] = paper.get("pdf_url") or best["pdf_url"]
        metadata = paper.setdefault("metadata", {})
        metadata["abstract_source"] = metadata.get("abstract_source") or "arxiv_title_match"
        metadata["arxiv_title_match_id"] = best["arxiv_id"]
        metadata["arxiv_title_similarity"] = round(best_similarity, 4)
        metadata["arxiv_title_match_title"] = best["title"]
        metadata["arxiv_title_match_query"] = best_query
        metadata["arxiv_title_match_author_overlap"] = best.get("author_overlap") or []
        cache[cache_key] = {
            "title": title,
            "abstract": best.get("abstract") or paper.get("abstract") or "",
            "url": best["url"],
            "pdf_url": best["pdf_url"],
            "arxiv_id": best["arxiv_id"],
            "similarity": round(best_similarity, 4),
            "author_overlap": best.get("author_overlap") or [],
            "query_version": ARXIV_TITLE_MATCH_QUERY_VERSION,
            "miss": False,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        cache_changed = True
        time.sleep(0.35)
    if cache_changed:
        _save_arxiv_title_match_cache(cache)
    return papers

def _nature_journal_meta(slug: str) -> dict[str, str]:
    slug = (slug or "").strip().strip("/")
    return NATURE_JOURNALS.get(slug, {"name": slug or "Nature Portfolio", "tier": "", "group": "custom"})


def _nature_feed_url(slug: str, article_type: str) -> str:
    params = {"type": article_type or "article", "format": "feed"}
    return f"https://www.nature.com/{slug}/articles?" + urlencode(params)


def _nature_listing_url(slug: str, article_type: str, page: int) -> str:
    params: dict[str, str | int] = {"type": article_type or "article"}
    if page > 1:
        params["page"] = page
    return f"https://www.nature.com/{slug}/articles?" + urlencode(params)


def _looks_like_xml(text: str) -> bool:
    stripped = (text or "").lstrip()[:120].lower()
    return stripped.startswith("<?xml") or stripped.startswith("<feed") or stripped.startswith("<rss")


def _xml_text(node: ET.Element | None, names: list[str]) -> str:
    if node is None:
        return ""
    for name in names:
        found = node.find(name)
        if found is not None and found.text:
            return _clean_text(found.text)
    return ""


def _xml_attr(node: ET.Element | None, names: list[str], attr: str, value: str = "") -> str:
    if node is None:
        return ""
    for name in names:
        for found in node.findall(name):
            if value and found.attrib.get(attr) != value:
                continue
            href = found.attrib.get("href") or found.attrib.get("url") or ""
            if href:
                return href
    return ""


def _parse_nature_feed(xml_text: str, slug: str, article_type: str, feed_url: str) -> list[dict]:
    journal = _nature_journal_meta(slug)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    entries = list(root.findall(".//{http://www.w3.org/2005/Atom}entry"))
    if not entries:
        entries = list(root.findall(".//item"))
    papers: list[dict] = []
    for entry in entries:
        title = _xml_text(entry, ["{http://www.w3.org/2005/Atom}title", "title"])
        if not _looks_like_paper_title(title):
            continue
        url = _xml_attr(entry, ["{http://www.w3.org/2005/Atom}link", "link"], "rel", "alternate")
        if not url:
            url = _xml_text(entry, ["{http://www.w3.org/2005/Atom}id", "guid", "link"])
        url = requests.compat.urljoin(feed_url, url)
        published = _xml_text(entry, ["{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated", "pubDate"])
        summary = _xml_text(entry, ["{http://www.w3.org/2005/Atom}summary", "{http://www.w3.org/2005/Atom}content", "description"])
        authors = []
        for author in entry.findall("{http://www.w3.org/2005/Atom}author"):
            name = _xml_text(author, ["{http://www.w3.org/2005/Atom}name", "name"])
            if name:
                authors.append(name)
        year = int(published[:4]) if published[:4].isdigit() else date.today().year
        papers.append({
            "id": stable_id("nature", url or title),
            "source": "nature",
            "title": title,
            "authors": ", ".join(authors),
            "abstract": summary,
            "url": url,
            "pdf_url": "",
            "venue": journal["name"],
            "year": year,
            "category": article_type,
            "classification_source": "official",
            "metadata": {
                "journal_slug": slug,
                "journal_tier": journal.get("tier", ""),
                "journal_group": journal.get("group", ""),
                "article_type": article_type,
                "published": normalize_date(published[:10]),
                "feed_url": feed_url,
            },
        })
    return papers


def _parse_nature_listing_html(page_text: str, slug: str, article_type: str, page_url: str) -> list[dict]:
    journal = _nature_journal_meta(slug)
    soup = BeautifulSoup(page_text, "html.parser")
    papers: list[dict] = []
    seen: set[str] = set()
    for link in soup.select("article h3 a[href*='/articles/'], article a[href*='/articles/']"):
        title = _clean_text(link.get_text(" ", strip=True))
        if not _looks_like_paper_title(title):
            continue
        url = requests.compat.urljoin(page_url, link.get("href", ""))
        if url in seen:
            continue
        seen.add(url)
        container = link.find_parent("article") or link.find_parent("li") or link.parent
        text = _clean_text(container.get_text(" ", strip=True) if container else "")
        date_match = re.search(r"\b(\d{1,2}\s+[A-Z][a-z]{2}\s+20\d{2})\b", text)
        published = ""
        if date_match:
            try:
                published = datetime.strptime(date_match.group(1), "%d %b %Y").date().isoformat()
            except ValueError:
                published = ""
        summary = ""
        if container:
            for paragraph in container.find_all("p"):
                summary = _clean_text(paragraph.get_text(" ", strip=True))
                if summary and summary != title:
                    break
        papers.append({
            "id": stable_id("nature", url or title),
            "source": "nature",
            "title": title,
            "authors": "",
            "abstract": summary,
            "url": url,
            "pdf_url": "",
            "venue": journal["name"],
            "year": int(published[:4]) if published[:4].isdigit() else date.today().year,
            "category": article_type,
            "classification_source": "official",
            "metadata": {
                "journal_slug": slug,
                "journal_tier": journal.get("tier", ""),
                "journal_group": journal.get("group", ""),
                "article_type": article_type,
                "published": published,
                "listing_url": page_url,
            },
        })
    return papers


def _extract_nature_doi(soup: BeautifulSoup) -> str:
    for selector in ["meta[name='citation_doi']", "meta[name='dc.identifier']", "meta[property='og:url']"]:
        node = soup.select_one(selector)
        if not node or not node.get("content"):
            continue
        content = str(node["content"])
        if selector == "meta[property='og:url']" and "/articles/" in content:
            return content.rstrip("/").rsplit("/", 1)[-1]
        return content.replace("doi:", "").strip()
    return ""


def enrich_nature_details(papers: list[dict], limit: int | None = None) -> tuple[list[dict], dict]:
    attempted = 0
    abstracts_filled = 0
    pdfs_filled = 0
    dois_filled = 0
    candidates = papers if limit is None else papers[:limit]
    for paper in candidates:
        url = str(paper.get("url") or "")
        if not url:
            continue
        attempted += 1
        try:
            soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception:
            continue
        if not paper.get("abstract"):
            abstract_node = soup.select_one("[data-test='abstract'], section[aria-labelledby='Abs1'], #Abs1-content")
            if abstract_node:
                abstract = _clean_text(abstract_node.get_text(" ", strip=True))
                if abstract:
                    paper["abstract"] = abstract
                    abstracts_filled += 1
        if not paper.get("pdf_url"):
            pdf_link = soup.select_one("a[href$='.pdf'], a[href*='.pdf?'], a[href*='/pdf/']")
            if pdf_link and pdf_link.get("href"):
                paper["pdf_url"] = requests.compat.urljoin(url, pdf_link["href"])
                pdfs_filled += 1
        doi = _extract_nature_doi(soup)
        if doi:
            paper.setdefault("metadata", {})["doi"] = doi
            dois_filled += 1
        time.sleep(0.1)
    return papers, {
        "attempted": attempted,
        "abstracts_filled": abstracts_filled,
        "authors_filled": authors_filled,
        "pdfs_filled": pdfs_filled,
        "dois_filled": dois_filled,
    }


def fetch_nature_portfolio(
    journals: list[str],
    article_types: list[str],
    max_items: int | None = None,
    start_date: str = "",
    end_date: str = "",
    enrich_details: bool = True,
) -> tuple[list[dict], dict]:
    start_date = normalize_date(start_date)
    end_date = normalize_date(end_date)
    journals = [journal.strip().strip("/") for journal in journals if journal.strip()] or ["nature"]
    article_types = [item.strip() for item in article_types if item.strip()] or ["article"]
    status = {
        "source": "nature",
        "ok": False,
        "limited": False,
        "count": 0,
        "message": "",
        "journals": journals,
        "article_types": article_types,
        "start_date": start_date,
        "end_date": end_date,
        "errors": [],
        "feeds": [],
        "pages": [],
        "stopped_reason": "",
    }
    by_key: dict[str, dict] = {}
    item_limit = max(1, int(max_items)) if max_items is not None else None
    max_pages = max(1, min(100, (item_limit + 19) // 20 + 5)) if item_limit is not None else None

    def reached_limit() -> bool:
        return item_limit is not None and len(by_key) >= item_limit

    def add_papers(papers: list[dict]) -> int:
        added = 0
        for paper in papers:
            published = paper.get("metadata", {}).get("published", "")
            if not _in_date_range(published, start_date, end_date):
                continue
            key = str(paper.get("url") or paper.get("title") or "").lower()
            if key and key not in by_key:
                by_key[key] = paper
                added += 1
            if reached_limit():
                break
        return added

    def older_than_start(papers: list[dict]) -> bool:
        if not start_date:
            return False
        dates = [
            normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10])
            for paper in papers
        ]
        dates = [value for value in dates if value]
        return bool(dates) and max(dates) < start_date

    for slug in journals:
        for article_type in article_types:
            feed_url = _nature_feed_url(slug, article_type)
            feed_report = {"journal": slug, "article_type": article_type, "url": feed_url, "count": 0, "ok": False, "message": ""}
            try:
                page_text = _request(feed_url).text
                papers = _parse_nature_feed(page_text, slug, article_type, feed_url)
                if not papers and not _looks_like_xml(page_text):
                    papers = _parse_nature_listing_html(page_text, slug, article_type, feed_url)
                feed_report.update({"count": len(papers), "ok": bool(papers), "message": "ok" if papers else "empty feed"})
            except Exception as exc:
                papers = []
                feed_report["message"] = str(exc)
                status["errors"].append(f"{slug}/{article_type}: {exc}")
            status["feeds"].append(feed_report)
            add_papers(papers)
            if reached_limit():
                status["stopped_reason"] = "item limit"
                break

            page = 1
            while max_pages is None or page <= max_pages:
                page_url = _nature_listing_url(slug, article_type, page)
                page_report = {
                    "journal": slug,
                    "article_type": article_type,
                    "page": page,
                    "url": page_url,
                    "count": 0,
                    "added": 0,
                    "ok": False,
                    "message": "",
                }
                try:
                    page_text = _request(page_url).text
                    page_papers = _parse_nature_listing_html(page_text, slug, article_type, page_url)
                    added = add_papers(page_papers)
                    page_report.update({
                        "count": len(page_papers),
                        "added": added,
                        "ok": bool(page_papers),
                        "message": "ok" if page_papers else "empty page",
                    })
                except Exception as exc:
                    page_papers = []
                    page_report["message"] = str(exc)
                    status["errors"].append(f"{slug}/{article_type}/page{page}: {exc}")
                status["pages"].append(page_report)
                if reached_limit():
                    status["stopped_reason"] = "item limit"
                    break
                if not page_papers:
                    status["stopped_reason"] = "empty page"
                    break
                if older_than_start(page_papers):
                    status["stopped_reason"] = "date boundary"
                    break
                if page > 1 and page_report["added"] == 0:
                    status["stopped_reason"] = "no new items"
                    break
                page += 1
                time.sleep(0.1)
            else:
                status["stopped_reason"] = "safety page limit"
        if reached_limit():
            break
    papers = list(by_key.values())
    if item_limit is not None:
        papers = papers[:item_limit]
    if papers and enrich_details:
        papers, detail_stats = enrich_nature_details(papers, limit=len(papers) if item_limit is None else min(len(papers), item_limit))
        status["detail_enrichment"] = detail_stats
    elif papers:
        status["detail_enrichment"] = {
            "attempted": 0,
            "abstracts_filled": 0,
            "pdfs_filled": 0,
            "dois_filled": 0,
            "skipped": True,
        }
    status["count"] = len(papers)
    status["ok"] = bool(papers)
    status["limited"] = reached_limit()
    page_reports = status.get("pages") if isinstance(status.get("pages"), list) else []
    status["pages_scanned"] = len(page_reports)
    dates = [
        normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10])
        for paper in papers
    ]
    dates = [value for value in dates if value]
    if dates:
        status["date_coverage"] = {
            "newest": max(dates),
            "oldest": min(dates),
        }
    if papers:
        message = "ok"
        if status["pages_scanned"]:
            message += f"; scanned {status['pages_scanned']} listing pages"
        if dates:
            message += f"; date coverage {min(dates)} to {max(dates)}"
        if status.get("stopped_reason"):
            message += f"; stopped: {status['stopped_reason']}"
        status["message"] = message
    elif status["errors"]:
        status["message"] = "Nature feeds unavailable or failed: " + " | ".join(status["errors"][:3])
    else:
        status["message"] = "No Nature items found for selected journals/types/date range."
    return papers, status


def _science_journal_meta(slug: str) -> dict[str, str]:
    slug = (slug or "").strip()
    return SCIENCE_JOURNALS.get(slug, {"name": slug or "Science Family", "tier": "", "group": "custom"})


def _science_feed_url(slug: str) -> str:
    return "https://www.science.org/action/showFeed?" + urlencode({"type": "etoc", "feed": "rss", "jc": slug})


def _science_pdf_url(doi: str) -> str:
    doi = (doi or "").replace("doi:", "").strip()
    return f"https://www.science.org/doi/pdf/{doi}" if doi else ""


def _science_abs_url(doi: str, fallback_url: str = "") -> str:
    doi = (doi or "").replace("doi:", "").strip()
    return f"https://www.science.org/doi/abs/{doi}" if doi else fallback_url


def _extract_science_doi(soup: BeautifulSoup) -> str:
    for selector in ["meta[name='citation_doi']", "meta[name='dc.Identifier']", "meta[name='dc.identifier']"]:
        node = soup.select_one(selector)
        if node and node.get("content"):
            return str(node["content"]).replace("doi:", "").strip()
    return ""


def _extract_science_abstract(soup: BeautifulSoup) -> str:
    for selector in [
        "meta[name='description']",
        "meta[property='og:description']",
        "meta[name='citation_abstract']",
    ]:
        node = soup.select_one(selector)
        if node and node.get("content"):
            text = _clean_text(str(node["content"]))
            if text:
                return text
    for selector in [
        "section.abstract",
        "section[class*='abstract']",
        "div.abstract",
        "div[class*='abstract']",
        "[id*='abstract']",
        "[class*='Abstract']",
    ]:
        node = soup.select_one(selector)
        if node:
            text = _clean_text(node.get_text(" ", strip=True))
            text = re.sub(r"^Abstract\s*", "", text, flags=re.I).strip()
            if text:
                return text
    return ""


def enrich_science_details(papers: list[dict], limit: int | None = None) -> tuple[list[dict], dict]:
    attempted = 0
    abstracts_filled = 0
    pdfs_filled = 0
    dois_filled = 0
    candidates = papers if limit is None else papers[:limit]
    for paper in candidates:
        metadata = paper.setdefault("metadata", {})
        doi = str(metadata.get("doi") or "").replace("doi:", "").strip()
        url = str(paper.get("url") or _science_abs_url(doi))
        if not url:
            continue
        attempted += 1
        try:
            soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception:
            continue
        if not paper.get("abstract"):
            abstract = _extract_science_abstract(soup)
            if abstract:
                paper["abstract"] = abstract
                metadata["abstract_source"] = "science_detail"
                abstracts_filled += 1
        extracted_doi = _extract_science_doi(soup)
        if extracted_doi and not metadata.get("doi"):
            metadata["doi"] = extracted_doi
            doi = extracted_doi
            dois_filled += 1
        if not paper.get("pdf_url"):
            pdf_url = _science_pdf_url(doi)
            if pdf_url:
                paper["pdf_url"] = pdf_url
                pdfs_filled += 1
        if not paper.get("url") and url:
            paper["url"] = url
        time.sleep(0.1)
    return papers, {
        "attempted": attempted,
        "abstracts_filled": abstracts_filled,
        "authors_filled": authors_filled,
        "pdfs_filled": pdfs_filled,
        "dois_filled": dois_filled,
    }


def _crossref_date(item: dict) -> str:
    for key in ["published-print", "published-online", "published"]:
        parts = item.get(key, {}).get("date-parts")
        if not parts or not parts[0]:
            continue
        values = [int(part) for part in parts[0]]
        year = values[0]
        month = values[1] if len(values) > 1 else 1
        day = values[2] if len(values) > 2 else 1
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return ""


def _crossref_first_text(value: object) -> str:
    if isinstance(value, list) and value:
        value = value[0]
    return _clean_text(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True))


def _crossref_authors(value: object) -> str:
    if not isinstance(value, list):
        return ""
    authors: list[str] = []
    for author in value[:12]:
        if not isinstance(author, dict):
            continue
        name = _clean_text(" ".join(part for part in [author.get("given", ""), author.get("family", "")] if part))
        if name:
            authors.append(name)
    return ", ".join(authors)


def _science_crossref_url(issn: str, start_date: str, end_date: str, rows: int, offset: int) -> str:
    filters = [f"issn:{issn}", "type:journal-article"]
    if start_date:
        filters.append(f"from-pub-date:{start_date}")
    if end_date:
        filters.append(f"until-pub-date:{end_date}")
    return "https://api.crossref.org/works?" + urlencode({
        "filter": ",".join(filters),
        "rows": max(1, min(100, rows)),
        "offset": max(0, offset),
        "sort": "published",
        "order": "desc",
    })


def _parse_science_crossref_items(items: list[dict], slug: str) -> list[dict]:
    journal = _science_journal_meta(slug)
    papers: list[dict] = []
    for item in items:
        doi = str(item.get("DOI") or "").strip()
        title = _crossref_first_text(item.get("title"))
        if not doi or not _looks_like_paper_title(title):
            continue
        container = _crossref_first_text(item.get("container-title")) or journal["name"]
        published = _crossref_date(item)
        abstract = _crossref_first_text(item.get("abstract"))
        year = int(published[:4]) if published[:4].isdigit() else date.today().year
        papers.append({
            "id": stable_id("science", doi),
            "source": "science",
            "title": title,
            "authors": _crossref_authors(item.get("author")),
            "abstract": abstract,
            "url": _science_abs_url(doi, str(item.get("URL") or "")),
            "pdf_url": _science_pdf_url(doi),
            "venue": container,
            "year": year,
            "category": "journal-article",
            "classification_source": "official",
            "metadata": {
                "journal_slug": slug,
                "journal_tier": journal.get("tier", ""),
                "journal_group": journal.get("group", ""),
                "article_type": "journal-article",
                "doi": doi,
                "published": published,
                "crossref_url": str(item.get("URL") or ""),
            },
        })
    return papers


def _parse_science_feed(xml_text: str, slug: str, allowed_types: set[str], feed_url: str) -> list[dict]:
    journal = _science_journal_meta(slug)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    ns = {
        "rss": "http://purl.org/rss/1.0/",
        "dc": "http://purl.org/dc/elements/1.1/",
        "prism": "http://prismstandard.org/namespaces/basic/2.0/",
        "content": "http://purl.org/rss/1.0/modules/content/",
    }
    papers: list[dict] = []
    for item in root.findall(".//rss:item", ns):
        title = _xml_text(item, ["{http://purl.org/rss/1.0/}title"])
        if not _looks_like_paper_title(title):
            continue
        article_type = _xml_text(item, ["{http://purl.org/dc/elements/1.1/}type"])
        if allowed_types and article_type.lower() not in allowed_types:
            continue
        doi = _xml_text(item, ["{http://prismstandard.org/namespaces/basic/2.0/}doi", "{http://purl.org/dc/elements/1.1/}identifier"]).replace("doi:", "").strip()
        url = _xml_text(item, ["{http://prismstandard.org/namespaces/basic/2.0/}url", "{http://purl.org/rss/1.0/}link"])
        published = _xml_text(item, ["{http://purl.org/dc/elements/1.1/}date", "{http://prismstandard.org/namespaces/basic/2.0/}coverDate"])
        description = _xml_text(item, ["{http://purl.org/rss/1.0/}description", "{http://purl.org/rss/1.0/modules/content/}encoded"])
        authors = _xml_text(item, ["{http://purl.org/dc/elements/1.1/}creator"])
        publication = _xml_text(item, ["{http://prismstandard.org/namespaces/basic/2.0/}publicationName"]) or journal["name"]
        published_date = normalize_date(published[:10])
        year = int(published_date[:4]) if published_date[:4].isdigit() else date.today().year
        canonical_url = _science_abs_url(doi, url)
        papers.append({
            "id": stable_id("science", doi or canonical_url or title),
            "source": "science",
            "title": title,
            "authors": authors,
            "abstract": description,
            "url": canonical_url,
            "pdf_url": _science_pdf_url(doi),
            "venue": publication,
            "year": year,
            "category": article_type,
            "classification_source": "official",
            "metadata": {
                "journal_slug": slug,
                "journal_tier": journal.get("tier", ""),
                "journal_group": journal.get("group", ""),
                "article_type": article_type,
                "doi": doi,
                "published": published_date,
                "feed_url": feed_url,
            },
        })
    return papers


def fetch_science_family(
    journals: list[str],
    article_types: list[str],
    max_items: int | None = None,
    start_date: str = "",
    end_date: str = "",
) -> tuple[list[dict], dict]:
    start_date = normalize_date(start_date)
    end_date = normalize_date(end_date)
    journals = [journal.strip() for journal in journals if journal.strip()] or ["science"]
    article_types = [item.strip() for item in article_types if item.strip()] or ["Research Article"]
    allowed_types = {item.lower() for item in article_types if item.lower() not in {"all", "*"}}
    status = {
        "source": "science",
        "ok": False,
        "limited": False,
        "count": 0,
        "message": "",
        "journals": journals,
        "article_types": article_types,
        "start_date": start_date,
        "end_date": end_date,
        "errors": [],
        "feeds": [],
        "crossref_pages": [],
        "stopped_reason": "",
    }
    by_key: dict[str, dict] = {}
    item_limit = max(1, int(max_items)) if max_items is not None else None
    rows = 100

    def reached_limit() -> bool:
        return item_limit is not None and len(by_key) >= item_limit

    def add_papers(papers: list[dict]) -> int:
        added = 0
        for paper in papers:
            published = paper.get("metadata", {}).get("published", "")
            if not _in_date_range(published, start_date, end_date):
                continue
            key = str(paper.get("metadata", {}).get("doi") or paper.get("url") or paper.get("title") or "").lower()
            if key and key not in by_key:
                by_key[key] = paper
                added += 1
            if reached_limit():
                break
        return added

    for slug in journals:
        journal = _science_journal_meta(slug)
        issn = journal.get("issn", "")
        if issn:
            offset = 0
            while not reached_limit():
                crossref_url = _science_crossref_url(issn, start_date, end_date, rows, offset)
                page_report = {
                    "journal": slug,
                    "issn": issn,
                    "offset": offset,
                    "rows": rows,
                    "url": crossref_url,
                    "count": 0,
                    "added": 0,
                    "ok": False,
                    "message": "",
                }
                try:
                    response = _request(crossref_url, timeout=20)
                    payload = response.json()
                    records = payload.get("message", {}).get("items", [])
                    papers = _parse_science_crossref_items(records, slug)
                    added = add_papers(papers)
                    page_report.update({
                        "count": len(papers),
                        "added": added,
                        "ok": bool(papers),
                        "message": "ok" if papers else "empty crossref page",
                    })
                except Exception as exc:
                    records = []
                    page_report["message"] = str(exc)
                    status["errors"].append(f"{slug}/crossref/{offset}: {exc}")
                status["crossref_pages"].append(page_report)
                if reached_limit():
                    status["stopped_reason"] = "item limit"
                    break
                if not records:
                    status["stopped_reason"] = "empty crossref page"
                    break
                if len(records) < rows and offset > 0:
                    status["stopped_reason"] = "end of crossref results"
                    break
                if page_report["added"] == 0 and offset > 0:
                    status["stopped_reason"] = "no new items"
                    break
                offset += rows
                time.sleep(0.1)
        if reached_limit():
            status["stopped_reason"] = "item limit"
            break

        feed_url = _science_feed_url(slug)
        feed_report = {"journal": slug, "url": feed_url, "count": 0, "ok": False, "message": ""}
        try:
            papers = _parse_science_feed(_request(feed_url).text, slug, allowed_types, feed_url)
            feed_report.update({"count": len(papers), "ok": bool(papers), "message": "ok" if papers else "empty feed after type filter"})
        except Exception as exc:
            papers = []
            feed_report["message"] = str(exc)
            status["errors"].append(f"{slug}: {exc}")
        status["feeds"].append(feed_report)
        add_papers(papers)
        if reached_limit():
            status["stopped_reason"] = "item limit"
            break
    papers = list(by_key.values())
    if item_limit is not None:
        papers = papers[:item_limit]
    status["count"] = len(papers)
    status["ok"] = bool(papers)
    status["limited"] = reached_limit()
    crossref_reports = status.get("crossref_pages") if isinstance(status.get("crossref_pages"), list) else []
    status["pages_scanned"] = len(crossref_reports)
    dates = [
        normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10])
        for paper in papers
    ]
    dates = [value for value in dates if value]
    if dates:
        status["date_coverage"] = {
            "newest": max(dates),
            "oldest": min(dates),
        }
    if papers:
        message = "ok"
        if status["pages_scanned"]:
            message += f"; scanned {status['pages_scanned']} Crossref pages"
        if dates:
            message += f"; date coverage {min(dates)} to {max(dates)}"
        if status.get("stopped_reason"):
            message += f"; stopped: {status['stopped_reason']}"
        status["message"] = message
    elif status["errors"]:
        status["message"] = "Science feeds unavailable or failed: " + " | ".join(status["errors"][:3])
    else:
        status["message"] = "No Science items found for selected journals/types/date range."
    return papers, status

def _biorxiv_default_start_date() -> str:
    return (date.today() - timedelta(days=30)).isoformat()


def _biorxiv_category_matches(category: str, selected: list[str]) -> bool:
    if not selected or any(item.lower() == "all" for item in selected):
        return True
    normalized = category.strip().lower()
    return normalized in {item.strip().lower() for item in selected if item.strip()}


def _biorxiv_content_url(doi: str, version: str = "") -> str:
    if not doi:
        return ""
    suffix = f"v{version}" if str(version).strip() else ""
    return f"https://www.biorxiv.org/content/{doi}{suffix}"


def fetch_biorxiv(categories: list[str], max_items: int, start_date: str = "", end_date: str = "") -> tuple[list[dict], dict]:
    papers: list[dict] = []
    by_key: dict[str, dict] = {}
    start_date = normalize_date(start_date) or _biorxiv_default_start_date()
    end_date = normalize_date(end_date) or date.today().isoformat()
    categories = [category.strip() for category in (categories or []) if category.strip()] or ["bioinformatics"]
    max_items = max(1, int(max_items or 100))
    status = {
        "source": "biorxiv",
        "ok": False,
        "limited": False,
        "count": 0,
        "message": "",
        "categories": categories,
        "start_date": start_date,
        "end_date": end_date,
        "queries": [f"server:biorxiv date:{start_date}..{end_date} categories:{', '.join(categories)}"],
        "errors": [],
        "pages_fetched": 0,
        "deduped_count": 0,
        "raw_count": 0,
    }
    cursor = 0
    while len(papers) < max_items:
        url = f"https://api.biorxiv.org/details/biorxiv/{start_date}/{end_date}/{cursor}/json"
        try:
            response = _request(url, timeout=20)
            data = response.json()
        except Exception as exc:
            status["errors"].append(f"cursor={cursor}: {exc}")
            break
        status["pages_fetched"] += 1
        records = data.get("collection") if isinstance(data, dict) else []
        if not isinstance(records, list) or not records:
            break
        status["raw_count"] += len(records)
        for record in records:
            if not isinstance(record, dict):
                continue
            published = normalize_date(str(record.get("date") or ""))
            if not _in_date_range(published, start_date, end_date):
                continue
            category = str(record.get("category") or "").strip()
            if not _biorxiv_category_matches(category, categories):
                continue
            title = " ".join(str(record.get("title") or "").split())
            abstract = " ".join(str(record.get("abstract") or "").split())
            doi = str(record.get("doi") or "").strip()
            version = str(record.get("version") or "").strip()
            key = doi.lower() or title.lower()
            if not key:
                continue
            paper = by_key.get(key)
            if paper:
                categories_seen = paper.setdefault("categories", [paper.get("category", "")])
                if category and category not in categories_seen:
                    categories_seen.append(category)
                paper.setdefault("metadata", {})["all_categories"] = categories_seen
                continue
            url = _biorxiv_content_url(doi, version)
            pdf_url = f"{url}.full.pdf" if url else ""
            all_categories = [category] if category else []
            paper = {
                "id": stable_id("paper", doi or title),
                "source": "biorxiv",
                "biorxiv_doi": doi,
                "title": title,
                "authors": str(record.get("authors") or ""),
                "abstract": abstract,
                "url": url,
                "pdf_url": pdf_url,
                "venue": "bioRxiv",
                "year": int(published[:4]) if published[:4].isdigit() else date.today().year,
                "category": category,
                "categories": all_categories,
                "classification_source": "llm_inferred",
                "metadata": {
                    "published": published,
                    "biorxiv_category": category,
                    "primary_category": category,
                    "all_categories": all_categories,
                    "doi": doi,
                    "version": version,
                    "license": record.get("license") or "",
                    "server": record.get("server") or "biorxiv",
                    "type": record.get("type") or "",
                    "published_journal": record.get("published") or "",
                },
            }
            by_key[key] = paper
            papers.append(paper)
            if len(papers) >= max_items:
                status["limited"] = True
                break
        if len(records) < 100:
            break
        cursor += len(records)
        time.sleep(0.5)
    status["count"] = len(papers)
    status["deduped_count"] = len(papers)
    status["ok"] = bool(papers)
    if papers:
        limit_message = "limited by max_items" if status["limited"] else "fetched available pages"
        status["message"] = f"ok; {limit_message}; queries={'; '.join(status['queries'])}"
    elif status["errors"]:
        status["message"] = "bioRxiv unavailable or query failed: " + " | ".join(status["errors"][:3])
    else:
        status["message"] = f"No bioRxiv papers found; queries={'; '.join(status['queries'])}"
    return papers, status

def _arxiv_entry_id(entry_id: str) -> str:
    text = (entry_id or "").rstrip("/")
    if "/abs/" in text:
        text = text.rsplit("/abs/", 1)[1]
    if "/pdf/" in text:
        text = text.rsplit("/pdf/", 1)[1]
    return re.sub(r"\.pdf$", "", text)


def _request_arxiv_page(url: str, timeout_sec: int, attempts: int = 3):
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            try:
                return _request(url, timeout=timeout_sec)
            except TypeError:
                return _request(url)
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.8 * attempt)
    if last_error:
        raise last_error
    raise RuntimeError("arXiv request failed")


def _arxiv_fallback_queries(categories: list[str], start_date: str = "", end_date: str = "") -> list[tuple[str, str]]:
    queries = [(category, f"cat:{category}") for category in ([c.strip() for c in categories if c.strip()] or ["cs.AI"])]
    if start_date or end_date:
        start_stamp = (start_date or "1991-01-01").replace("-", "") + "0000"
        end_stamp = (end_date or "3000-01-01").replace("-", "") + "2359"
        queries = [(label, f"{query_text} AND submittedDate:[{start_stamp} TO {end_stamp}]") for label, query_text in queries]
    return queries


def _arxiv_search_queries(categories: list[str], topic_queries: list[str], start_date: str = "", end_date: str = "") -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    seen: set[str] = set()
    cleaned_topics = [" ".join(str(query).split()) for query in (topic_queries or []) if str(query).strip()]
    cleaned_categories = [category.strip() for category in (categories or []) if category.strip()] or ["cs.AI"]
    for topic in cleaned_topics:
        terms = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", topic)[:8]
        topic_expr = " AND ".join(f"all:{term}" for term in terms)
        if not topic_expr:
            continue
        category_expr = " OR ".join(f"cat:{category}" for category in cleaned_categories)
        query_text = f"({topic_expr}) AND ({category_expr})" if category_expr else topic_expr
        if query_text not in seen:
            queries.append((f"topic:{topic}", query_text))
            seen.add(query_text)
    for category in cleaned_categories:
        query_text = f"cat:{category}"
        if query_text not in seen:
            queries.append((category, query_text))
            seen.add(query_text)
    if start_date or end_date:
        start_stamp = (start_date or "1991-01-01").replace("-", "") + "0000"
        end_stamp = (end_date or "3000-01-01").replace("-", "") + "2359"
        queries = [(label, f"{query_text} AND submittedDate:[{start_stamp} TO {end_stamp}]") for label, query_text in queries]
    return queries


def _append_arxiv_entry(papers: list[dict], by_key: dict[str, dict], entry, ns: dict, query_label: str, query_text: str, start_date: str, end_date: str, *, fallback_query: bool = False) -> None:
    published = (entry.findtext("a:published", default="", namespaces=ns) or "")[:10]
    updated = (entry.findtext("a:updated", default="", namespaces=ns) or "")[:10]
    if not _in_date_range(published, start_date, end_date):
        return
    title = " ".join((entry.findtext("a:title", default="", namespaces=ns) or "").split())
    abstract = " ".join((entry.findtext("a:summary", default="", namespaces=ns) or "").split())
    entry_id = entry.findtext("a:id", default="", namespaces=ns) or ""
    arxiv_id = _arxiv_entry_id(entry_id)
    key = arxiv_id or title.lower()
    if not key:
        return
    existing = by_key.get(key)
    if existing:
        categories_seen = existing.setdefault("categories", [existing.get("category", "")])
        category_name = str(query_label).replace("topic:", "")
        if category_name not in categories_seen:
            categories_seen.append(category_name)
        existing.setdefault("metadata", {})["all_categories"] = categories_seen
        return
    pdf_url = entry_id.replace("/abs/", "/pdf/") if "/abs/" in entry_id else ""
    all_categories = [str(query_label).replace("topic:", "")]
    paper = {
        "id": stable_id("paper", entry_id or title),
        "source": "arxiv",
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": ", ".join(author.findtext("a:name", default="", namespaces=ns) or "" for author in entry.findall("a:author", ns)),
        "abstract": abstract,
        "url": entry_id,
        "pdf_url": pdf_url,
        "venue": "arXiv",
        "year": int(published[:4]) if published[:4].isdigit() else date.today().year,
        "category": query_label,
        "categories": all_categories,
        "classification_source": "llm_inferred",
        "metadata": {"published": published, "updated": updated, "arxiv_query": query_text, "arxiv_query_label": query_label, "fallback_query": fallback_query, "all_categories": all_categories},
    }
    by_key[key] = paper
    papers.append(paper)


ARXIV_DEFAULT_RECENT_DAYS = 180


def _arxiv_date_window(start_date: str = "", end_date: str = "", *, today: date | None = None) -> tuple[str, str, str]:
    start = normalize_date(start_date)
    end = normalize_date(end_date)
    if start or end:
        return start, end, "configured"
    current_day = today or date.today()
    return (current_day - timedelta(days=ARXIV_DEFAULT_RECENT_DAYS)).isoformat(), current_day.isoformat(), "default_recent_180_days"


def fetch_arxiv(categories: list[str], max_items: int, start_date: str = "", end_date: str = "", topic_queries: list[str] | None = None, log=None, progress=None, should_cancel=None, max_queries: int | None = None, per_query_limit: int | None = None, timeout_sec: int | None = None) -> tuple[list[dict], dict]:
    papers: list[dict] = []
    by_key: dict[str, dict] = {}
    start_date, end_date, date_window_source = _arxiv_date_window(start_date, end_date)
    categories = [category.strip() for category in (categories or []) if category.strip()] or ["cs.AI"]
    full_scan = os.environ.get("ARXIV_FULL_SCAN", "1").lower() in {"1", "true", "yes", "on"}
    env_per_query = int(os.environ.get("ARXIV_PER_QUERY_LIMIT", "0") or 0)
    env_max_queries = int(os.environ.get("ARXIV_MAX_QUERIES", "0") or 0)
    env_timeout = int(os.environ.get("ARXIV_TIMEOUT_SEC", "0") or 0)
    env_total_limit = int(os.environ.get("ARXIV_MAX_TOTAL", "0") or 0)
    per_query_limit = max(10, min(100, env_per_query or int(per_query_limit or (100 if full_scan else 50))))
    max_queries = max(1, env_max_queries or int(max_queries or (len(categories) + len(topic_queries or []) if full_scan else 3)))
    arxiv_timeout = max(20, env_timeout or int(timeout_sec or 45))
    total_limit = max(0, env_total_limit or (max_items if not full_scan else 0))
    status = {
        "source": "arxiv",
        "ok": False,
        "limited": False,
        "count": 0,
        "message": "",
        "categories": categories,
        "start_date": start_date,
        "end_date": end_date,
        "date_window_source": date_window_source,
        "default_recent_days": ARXIV_DEFAULT_RECENT_DAYS,
        "queries": [],
        "errors": [],
        "query_limit": max_queries,
        "per_query_limit": per_query_limit,
        "full_scan": full_scan,
        "pages_fetched": 0,
        "deduped_count": 0,
        "total_limit": total_limit,
    }
    queries = _arxiv_search_queries(categories, topic_queries or [], start_date, end_date)[:max_queries]
    fallback_queries = [query for query in _arxiv_fallback_queries(categories, start_date, end_date) if query not in queries]
    ns = {"a": "http://www.w3.org/2005/Atom"}

    def run_query(query_index: int, total_queries: int, query_label: str, query_text: str, *, fallback_query: bool = False) -> None:
        query = quote_plus(query_text)
        status["queries"].append(query_text)
        start = 0
        while True:
            if should_cancel and should_cancel():
                status["errors"].append("cancelled")
                return
            if log:
                log(f"arXiv query {query_index}/{total_queries} [{query_label}] page_start={start}: {query_text[:180]}")
            if progress:
                progress("arxiv", query_index - 1, max(1, total_queries), f"arXiv query {query_index}/{total_queries}: {query_label}, page start {start}")
            url = f"https://export.arxiv.org/api/query?search_query={query}&sortBy=submittedDate&sortOrder=descending&start={start}&max_results={per_query_limit}"
            try:
                root = ET.fromstring(_request_arxiv_page(url, arxiv_timeout).text)
            except Exception as exc:
                error_text = str(exc)
                status["errors"].append(f"{query_label} start={start}: {error_text}")
                if "429" in error_text or "Too Many Requests" in error_text:
                    status["limited"] = True
                    status["message"] = f"arXiv rate limited after {status['pages_fetched']} pages; kept {len(papers)} papers."
                if log:
                    log(f"arXiv query {query_index}/{total_queries} failed at start={start}: {error_text[:240]}")
                return
            status["pages_fetched"] += 1
            entries = root.findall("a:entry", ns)
            if not entries:
                return
            before = len(papers)
            for entry in entries:
                _append_arxiv_entry(papers, by_key, entry, ns, query_label, query_text, start_date, end_date, fallback_query=fallback_query)
                if total_limit and len(papers) >= total_limit:
                    status["limited"] = True
                    status["message"] = f"Reached configured arXiv total_limit={total_limit}; increase ARXIV_MAX_TOTAL or use ARXIV_FULL_SCAN=1 for deeper survey."
                    return
            if log:
                log(f"arXiv query {query_index}/{total_queries} collected {len(papers) - before} new papers on this page; total {len(papers)}")
            if len(entries) < per_query_limit:
                return
            if not full_scan and len(papers) >= max_items:
                return
            start += per_query_limit
            time.sleep(0.5)

    for query_index, (query_label, query_text) in enumerate(queries, 1):
        run_query(query_index, len(queries), query_label, query_text)
        if status.get("message", "").startswith("arXiv rate limited") or (total_limit and len(papers) >= total_limit) or (not full_scan and len(papers) >= max_items):
            break
    if not papers and fallback_queries:
        status["limited"] = True
        if log:
            log("arXiv topic queries returned no usable papers; falling back to category queries")
        for fallback_index, (query_label, query_text) in enumerate(fallback_queries, 1):
            run_query(fallback_index, len(fallback_queries), query_label, query_text, fallback_query=True)
            if papers or status.get("message", "").startswith("arXiv rate limited"):
                break
    status["count"] = len(papers)
    status["deduped_count"] = len(by_key)
    status["ok"] = bool(papers)
    if papers:
        status["message"] = status.get("message") or f"ok; pages_fetched={status['pages_fetched']}; queries={'; '.join(status['queries'])}"
    elif status["limited"]:
        status["message"] = status.get("message") or "arXiv rate limited before returning papers. Retry later or reduce query volume."
    elif status["errors"]:
        status["message"] = "arXiv unavailable or query failed: " + " | ".join(status["errors"][:3])
    else:
        status["message"] = f"No arXiv papers found; queries={'; '.join(status['queries'])}"
    return (papers[:total_limit] if total_limit else papers), status

def fetch_huggingface(
    max_papers: int,
    max_models: int,
    include_papers: bool = True,
    include_models: bool = True,
    start_date: str = "",
    end_date: str = "",
) -> tuple[list[dict], list[dict], dict]:
    papers: list[dict] = []
    models: list[dict] = []
    status = {"source": "huggingface", "ok": False, "limited": False, "message": "", "count": 0}
    start_date = normalize_date(start_date)
    end_date = normalize_date(end_date)
    if include_papers:
        try:
            data = _request("https://huggingface.co/api/daily_papers").json()
            if start_date or end_date:
                status["limited"] = True
                status["message"] = "HuggingFace daily papers API only exposes the current feed; date filtering is limited."
            for item in data[:max_papers]:
                paper = item.get("paper", {})
                paper_id = paper.get("id", "")
                papers.append({
                    "id": stable_id("hfpaper", paper_id or paper.get("title", "")),
                    "source": "huggingface",
                    "title": paper.get("title", "Untitled"),
                    "abstract": paper.get("summary", ""),
                    "url": f"https://huggingface.co/papers/{paper_id}" if paper_id else "",
                    "score": item.get("numComments", 0),
                    "category": "",
                    "classification_source": "llm_inferred",
                    "metadata": {"kind": "paper", "upvotes": paper.get("upvotes", 0)},
                })
        except Exception as exc:
            status.setdefault("errors", []).append(f"daily_papers: {exc}")
            status["message"] = f"HuggingFace daily papers unavailable: {exc}"
    if include_models:
        try:
            data = _request(f"https://huggingface.co/api/models?sort=likes&direction=-1&limit={max(max_models * 5, max_models)}").json()
            for item in data:
                modified = (item.get("lastModified") or item.get("createdAt") or "")[:10]
                if (start_date or end_date) and not _in_date_range(modified, start_date, end_date):
                    continue
                model_id = item.get("id", "")
                models.append({
                    "id": stable_id("hfmodel", model_id),
                    "source": "huggingface",
                    "title": model_id,
                    "abstract": item.get("description", "") or "",
                    "url": f"https://huggingface.co/{model_id}" if model_id else "",
                    "score": item.get("likes", 0),
                    "category": "",
                    "classification_source": "llm_inferred",
                    "metadata": {"kind": "model", "likes": item.get("likes", 0), "downloads": item.get("downloads", 0), "tags": item.get("tags", []), "last_modified": modified},
                })
                if len(models) >= max_models:
                    break
        except Exception as exc:
            status.setdefault("errors", []).append(f"models: {exc}")
            status["message"] = f"HuggingFace models unavailable: {exc}"
    status["count"] = len(papers) + len(models)
    status["ok"] = status["count"] > 0
    if status["ok"]:
        if status.get("errors"):
            status["limited"] = True
            status["message"] = status.get("message") or "partial ok; some HuggingFace endpoints failed"
        elif not status["message"]:
            status["message"] = "ok"
    return papers, models, status


def fetch_github_trending(languages: list[str], since: str, max_items: int, start_date: str = "", end_date: str = "") -> tuple[list[dict], dict]:
    repos: list[dict] = []
    status = {"source": "github", "ok": False, "limited": False, "message": "", "count": 0}
    langs = languages or ["all"]
    start_date = normalize_date(start_date)
    end_date = normalize_date(end_date)
    for language in langs:
        query_parts = ["stars:>1"]
        if language.lower() != "all":
            query_parts.append(f"language:{language}")
        if start_date and end_date:
            query_parts.append(f"created:{start_date}..{end_date}")
        elif start_date:
            query_parts.append(f"created:>={start_date}")
        elif end_date:
            query_parts.append(f"created:<={end_date}")
        url = "https://api.github.com/search/repositories?" + urlencode({
            "q": " ".join(query_parts),
            "sort": "stars",
            "order": "desc",
            "per_page": min(100, max_items),
        })
        try:
            items = _request(url).json().get("items", [])
        except Exception:
            continue
        for item in items:
            repos.append({
                "id": stable_id("repo", item.get("full_name", "")),
                "source": "github",
                "title": item.get("full_name", ""),
                "abstract": item.get("description", "") or "",
                "url": item.get("html_url", ""),
                "score": item.get("stargazers_count", 0),
                "category": item.get("language") or "",
                "classification_source": "llm_inferred",
                "metadata": {
                    "language": item.get("language") or "",
                    "stars": item.get("stargazers_count", 0),
                    "created_at": (item.get("created_at") or "")[:10],
                    "pushed_at": (item.get("pushed_at") or "")[:10],
                },
            })
            if len(repos) >= max_items:
                status.update({"ok": True, "count": len(repos), "message": "ok"})
                return repos[:max_items], status
    if repos:
        status.update({"ok": True, "count": len(repos), "message": "ok"})
        return repos[:max_items], status

    status["limited"] = True
    for language in langs:
        suffix = "" if language.lower() == "all" else f"/{language.lower()}"
        url = f"https://github.com/trending{suffix}?since={since}"
        try:
            soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception:
            continue
        for article in soup.select("article.Box-row"):
            link = article.select_one("h2 a[href]")
            if not link:
                continue
            repo_name = link.get("href", "").strip("/")
            desc = article.select_one("p")
            lang = article.select_one("span[itemprop='programmingLanguage']")
            stars = 0
            stlink = article.select_one("a[href$='/stargazers']")
            if stlink:
                try:
                    stars = int(stlink.get_text(strip=True).replace(",", ""))
                except ValueError:
                    stars = 0
            repos.append({
                "id": stable_id("repo", repo_name),
                "source": "github",
                "title": repo_name,
                "abstract": desc.get_text(" ", strip=True) if desc else "",
                "url": f"https://github.com/{repo_name}",
                "score": stars,
                "category": lang.get_text(strip=True) if lang else "",
                "classification_source": "llm_inferred",
                "metadata": {"language": lang.get_text(strip=True) if lang else "", "stars": stars, "since": since},
            })
            if len(repos) >= max_items:
                status.update({"ok": True, "count": len(repos), "message": "GitHub Search API unavailable; used Trending fallback."})
                return repos, status
        time.sleep(0.5)
    status.update({"ok": bool(repos), "count": len(repos), "message": "GitHub Search API unavailable; used Trending fallback." if repos else "GitHub Search and Trending unavailable or empty."})
    return repos[:max_items], status
