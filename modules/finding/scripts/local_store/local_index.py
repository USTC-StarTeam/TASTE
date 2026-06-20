from __future__ import annotations

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
