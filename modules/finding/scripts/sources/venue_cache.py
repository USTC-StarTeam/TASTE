from __future__ import annotations

import json
import os
from pathlib import Path

from auto_research.paths import WORKFLOW_RUNTIME_DIR, LEGACY_RUNS_DIR, ROOT, RUNS_DIR
from sources.common import is_icml_venue


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
