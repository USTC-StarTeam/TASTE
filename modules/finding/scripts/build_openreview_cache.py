from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
FRAMEWORK_SCRIPTS = ROOT / "framework" / "scripts"
if str(FRAMEWORK_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(FRAMEWORK_SCRIPTS))
from taste_pythonpath import ensure_taste_pythonpath
ensure_taste_pythonpath(ROOT)

import requests

from auto_research.paths import LOCAL_DATABASE_DIR
from find_support import HEADERS, stable_id


DEFAULT_OUTPUT_ROOT = LOCAL_DATABASE_DIR
OPENREVIEW_VENUES: dict[str, dict[str, Any]] = {
    "iclr": {
        "aliases": ["iclr", "openreview_iclr"],
        "venue_id": "openreview_iclr",
        "venue": "ICLR",
        "full_name": "International Conference on Learning Representations",
        "openreview_prefix": "ICLR.cc",
        "default_years": [2026, 2025],
        "blocked_title_terms": [],
    },
    "neurips": {
        "aliases": ["neurips", "nips", "openreview_neurips"],
        "venue_id": "openreview_neurips",
        "venue": "NeurIPS",
        "full_name": "Conference on Neural Information Processing Systems",
        "openreview_prefix": "NeurIPS.cc",
        "default_years": [2026, 2025],
        "blocked_title_terms": ["neurips"],
    },
}
VENUE_ALIASES = {
    alias: key
    for key, spec in OPENREVIEW_VENUES.items()
    for alias in spec["aliases"]
}


def venue_spec(venue: str) -> dict[str, Any]:
    key = VENUE_ALIASES.get(str(venue or "").strip().lower())
    if not key:
        supported = ", ".join(sorted(VENUE_ALIASES))
        raise ValueError(f"unsupported OpenReview venue {venue!r}; supported: {supported}")
    return OPENREVIEW_VENUES[key]


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _content_raw(content: dict[str, Any], key: str) -> Any:
    value = content.get(key, "")
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _content_text(content: dict[str, Any], key: str) -> str:
    value = _content_raw(content, key)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if str(item).strip())
    if isinstance(value, dict):
        return str(value.get("text") or value.get("value") or "")
    return str(value or "")


def _content_list(content: dict[str, Any], key: str) -> list[str]:
    value = _content_raw(content, key)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _first_content_text(content: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = _clean_text(_content_text(content, key))
        if value:
            return value
    return ""


def _collect_keywords(content: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    keywords: list[str] = []
    for key in ["keywords", "keyword"]:
        for keyword in _content_list(content, key):
            normalized = _clean_text(keyword)
            lowered = normalized.lower()
            if normalized and lowered not in seen:
                seen.add(lowered)
                keywords.append(normalized)
    return keywords


def _looks_like_paper_title(value: object, spec: dict[str, Any]) -> bool:
    text = _clean_text(value)
    if len(text) < 8:
        return False
    lowered = text.lower()
    blocked = ["main navigation", "skip to", "successful page load", "openreview", "papers"]
    blocked.extend(str(item).lower() for item in spec.get("blocked_title_terms", []))
    return not any(item == lowered or lowered.startswith(item) for item in blocked)


def _note_url(note: dict[str, Any]) -> tuple[str, str]:
    note_id = str(note.get("id") or "")
    forum = str(note.get("forum") or note_id)
    url = f"https://openreview.net/forum?id={forum or note_id}"
    pdf_url = f"https://openreview.net/pdf?id={note_id}" if note_id else ""
    return url, pdf_url


def _normalize_note(note: dict[str, Any], year: int, spec: dict[str, Any], openreview_venueid: str) -> dict[str, Any] | None:
    content = note.get("content", {}) or {}
    if not isinstance(content, dict):
        return None
    title = _clean_text(_content_text(content, "title"))
    if not _looks_like_paper_title(title, spec):
        return None

    url, pdf_url = _note_url(note)
    primary_area = _first_content_text(
        content,
        [
            "primary_area",
            "primary area",
            "Primary Area",
            "subject_area",
            "subject area",
            "Subject Area",
            "area",
            "Area",
        ],
    )
    track = _first_content_text(content, ["track", "Track", "venue", "Venue"])
    category = primary_area or track or _first_content_text(content, ["category", "Category"])
    keywords = _collect_keywords(content)

    metadata = {
        "venue_id": spec["venue_id"],
        "openreview_venueid": openreview_venueid,
        "note_id": str(note.get("id") or ""),
        "forum": str(note.get("forum") or ""),
        "number": note.get("number"),
        "cdate": note.get("cdate"),
        "mdate": note.get("mdate"),
        "original": note.get("original"),
        "invitation": note.get("invitation"),
        "content_keys": sorted(str(key) for key in content.keys()),
    }
    return {
        "id": stable_id("paper", url),
        "source": "openreview",
        "title": title,
        "authors": ", ".join(_content_list(content, "authors")),
        "abstract": _clean_text(_content_text(content, "abstract")),
        "url": url,
        "pdf_url": pdf_url,
        "venue": spec["venue"],
        "year": year,
        "category": category,
        "primary_area": primary_area,
        "track": track,
        "keywords": keywords,
        "classification_source": "official" if category or keywords else "llm_inferred",
        "metadata": metadata,
    }


def _request_json(url: str, params: dict[str, Any], timeout: int, retries: int) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(8.0, 1.5 * (attempt + 1)))
    raise RuntimeError(f"OpenReview request failed: {last_error}") from last_error


def _fetch_notes(
    year: int,
    spec: dict[str, Any],
    *,
    api_version: int,
    page_size: int,
    timeout: int,
    retries: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]], str]:
    openreview_venueid = f"{spec['openreview_prefix']}/{year}/Conference"
    notes: list[dict[str, Any]] = []
    seen: set[str] = set()
    invitations = [None] if api_version == 2 else [f"{openreview_venueid}/-/Blind_Submission", f"{openreview_venueid}/-/Submission"]
    for invitation in invitations:
        offset = 0
        for _page in range(max_pages):
            if api_version == 2:
                url = "https://api2.openreview.net/notes"
                params = {
                    "content.venueid": openreview_venueid,
                    "details": "replyCount,invitation,original",
                    "limit": page_size,
                    "offset": offset,
                }
            else:
                url = "https://api.openreview.net/notes"
                params = {"invitation": invitation, "limit": page_size, "offset": offset}
            data = _request_json(url, params, timeout, retries)
            page_notes = data.get("notes", [])
            if not isinstance(page_notes, list) or not page_notes:
                break
            added = 0
            for note in page_notes:
                note_id = str(note.get("id") or note.get("forum") or "")
                if not note_id or note_id in seen:
                    continue
                seen.add(note_id)
                notes.append(note)
                added += 1
            if added == 0 or len(page_notes) < page_size:
                break
            offset += page_size
        if notes and api_version == 1:
            break
    return notes, openreview_venueid


def build_openreview_year(
    venue: str,
    year: int,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    page_size: int = 1000,
    timeout: int = 30,
    retries: int = 2,
    max_pages: int = 20,
) -> Path:
    spec = venue_spec(venue)
    try:
        notes, openreview_venueid = _fetch_notes(year, spec, api_version=2, page_size=page_size, timeout=timeout, retries=retries, max_pages=max_pages)
        source_adapter = "openreview_api2"
    except Exception:
        notes, openreview_venueid = _fetch_notes(year, spec, api_version=1, page_size=page_size, timeout=timeout, retries=retries, max_pages=max_pages)
        source_adapter = "openreview_api1"

    papers: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for note in notes:
        paper = _normalize_note(note, year, spec, openreview_venueid)
        if not paper or paper["url"] in seen_urls:
            continue
        seen_urls.add(paper["url"])
        papers.append(paper)

    category_counts: dict[str, int] = {}
    for paper in papers:
        category = paper.get("category") or "(uncategorized)"
        category_counts[category] = category_counts.get(category, 0) + 1

    payload = {
        "schema_version": 1,
        "venue_id": spec["venue_id"],
        "venue": spec["venue"],
        "full_name": spec["full_name"],
        "year": year,
        "source": "openreview",
        "source_adapter": source_adapter,
        "openreview_venueid": openreview_venueid,
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "paper_count": len(papers),
        "category_counts": dict(sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))),
        "papers": papers,
    }

    target = output_root / spec["venue_id"] / str(year) / "papers.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def parse_years(value: str) -> list[int]:
    return [int(part) for part in re.split(r"[,\s]+", value.strip()) if part]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build local OpenReview paper index JSON files.")
    parser.add_argument("--venue", default="iclr", help="Venue alias, e.g. iclr/openreview_iclr/neurips/openreview_neurips.")
    parser.add_argument("--years", default="", help="Comma/space separated years; defaults to the venue defaults.")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Output root, default: runtime/local_database.")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-pages", type=int, default=20)
    args = parser.parse_args()

    spec = venue_spec(args.venue)
    years = parse_years(args.years) if args.years.strip() else list(spec["default_years"])
    for year in years:
        target = build_openreview_year(
            venue=args.venue,
            year=year,
            output_root=Path(args.output_root),
            page_size=max(1, args.page_size),
            timeout=max(1, args.timeout),
            retries=max(0, args.retries),
            max_pages=max(1, args.max_pages),
        )
        print(f"Wrote {target}")


if __name__ == "__main__":
    main()
