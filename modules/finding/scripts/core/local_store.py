from __future__ import annotations


# ---- local venue index ----

# ---- local_index.py ----

from pathlib import Path
from typing import Any

from finding_runtime import LOCAL_DATABASE_DIR
from finding_runtime import display_path
from finding_runtime import read_json_safely


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
    if "icml" in name or "international conference on machine learning" in name:
        candidates.extend(["icml", "dblp_icml"])
    if "sigkdd" in name or "knowledge discovery and data mining" in name:
        candidates.extend(["sigkdd", "kdd", "dblp_kdd"])
    return list(dict.fromkeys(item for item in candidates if item))


def load_local_venue_year(venue: dict[str, Any], year: int, root: Path = LOCAL_DATABASE_DIR) -> dict[str, Any] | None:
    for venue_id in _venue_id_candidates(venue):
        directory = root / venue_id / str(year)
        papers_path = directory / "papers.json"
        summary_path = directory / "category_summary.json"
        if not papers_path.exists() or not summary_path.exists():
            continue
        papers_data = read_json_safely(papers_path, {})
        summary_data = read_json_safely(summary_path, {})
        if not isinstance(papers_data, dict) or not isinstance(summary_data, dict):
            continue
        manifest_path = directory / "manifest.json"
        manifest_data = read_json_safely(manifest_path, {}) if manifest_path.exists() else {}
        if not isinstance(manifest_data, dict):
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
            "venue": papers_data.get("venue") or summary_data.get("venue") or manifest_data.get("venue") or venue.get("name", ""),
            "year": year,
            "directory": display_path(directory),
            "papers_path": display_path(papers_path),
            "category_summary_path": display_path(summary_path),
            "manifest_path": display_path(manifest_path) if manifest_path.exists() else "",
            "manifest": manifest_data,
            "metadata_completeness_audit": metadata_audit,
            "source_adapter": papers_data.get("source_adapter") or summary_data.get("source_adapter") or manifest_data.get("adapter") or "local_database",
            "papers": papers,
            "category_summary": summary_data,
            "paper_count": len(papers),
        }
    return None


# ---- local venue cache ----

# ---- local_cache.py ----

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finding_runtime import LOCAL_DATABASE_DIR
from finding_runtime import write_json_cache




SCHEMA_VERSION = "1.0"


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
    category = str(paper.get("primary_area") or paper.get("category") or "")
    if category and category not in categories:
        categories = [category, *categories]
    row = {
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
    for key in ("presentation_type", "presentation_label", "presentation_source"):
        value = paper.get(key) or metadata.get(key)
        if value:
            row[key] = str(value)
            metadata.setdefault(key, str(value))
    labels = paper.get("presentation_labels") or metadata.get("presentation_labels")
    if isinstance(labels, list):
        cleaned = [str(item).strip() for item in labels if str(item).strip()]
        if cleaned:
            row["presentation_labels"] = cleaned
            metadata.setdefault("presentation_labels", cleaned)
    return row


def build_category_summary(venue: dict[str, Any], year: int, papers: list[dict[str, Any]], adapter: str) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for paper in papers:
        category = str(paper.get("primary_area") or paper.get("category") or "").strip()
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
    papers_data = read_json_safely(papers_path, {})
    papers = papers_data.get("papers", [])
    if not isinstance(papers, list):
        return None
    summary_path = directory / "category_summary.json"
    report_path = directory / "source_report.json"
    summary = read_json_safely(summary_path, {}) if summary_path.exists() else {}
    report = read_json_safely(report_path, {}) if report_path.exists() else {}
    return {
        "venue_id": papers_data.get("venue_id") or venue.get("id", ""),
        "year": year,
        "directory": display_path(directory),
        "papers_path": display_path(papers_path),
        "category_summary_path": display_path(summary_path) if summary_path.exists() else "",
        "source_report_path": display_path(report_path) if report_path.exists() else "",
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
        "cache_directory": display_path(directory),
    }
    category_summary = build_category_summary(venue, year, normalized, adapter)
    write_json_cache(directory / "papers.json", paper_payload)
    write_json_cache(directory / "source_report.json", source_report)
    write_json_cache(directory / "category_summary.json", category_summary)
    return {
        "venue_id": venue.get("id", ""),
        "year": year,
        "directory": display_path(directory),
        "papers_path": display_path(directory / "papers.json"),
        "category_summary_path": display_path(directory / "category_summary.json"),
        "source_report_path": display_path(directory / "source_report.json"),
        "papers": normalized,
        "category_summary": category_summary,
        "source_report": source_report,
        "paper_count": len(normalized),
        "source_adapter": adapter,
    }


# Backward-compatible dotted imports for callers that still use the old package layout.
def _register_compat_aliases(*aliases: str) -> None:
    import sys as _sys
    _module = _sys.modules.get(__name__)
    if _module is None:
        return
    globals().setdefault("__path__", [])
    for _alias in aliases:
        _sys.modules.setdefault(_alias, _module)

_register_compat_aliases('local_store.local_index', 'local_store.local_cache')
