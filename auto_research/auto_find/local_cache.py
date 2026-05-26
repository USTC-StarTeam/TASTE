from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from auto_research.storage import read_json, write_json

from .local_index import LOCAL_DATABASE_DIR, _venue_id_candidates, venue_cache_key


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
