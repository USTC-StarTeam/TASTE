from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import requests

from finding_runtime.paths import LOCAL_DATABASE_DIR, write_json_cache
from support.find_support import HEADERS, stable_id
from support.find_support import OPENREVIEW_VENUE_PATTERNS
from support.find_support import VENUE_METADATA_AUDIT_KEY
from support.find_support import fetch_icml_official_virtual_2026
from support.find_support import fetch_neurips_title_index
from support.find_support import fetch_openreview_iclr_2026
from support.find_support import venue_metadata_audit_from_papers


DEFAULT_OUTPUT_ROOT = LOCAL_DATABASE_DIR
OPENREVIEW_VENUES: dict[str, dict[str, Any]] = {
    "iclr": {
        "aliases": ["iclr", "openreview_iclr"],
        "venue_id": "openreview_iclr",
        "venue": "ICLR",
        "full_name": "International Conference on Learning Representations",
        "openreview_patterns": ["ICLR.cc/{year}/Conference"],
        "default_years": [2026, 2025],
        "blocked_title_terms": [],
    },
    "neurips": {
        "aliases": ["neurips", "nips", "openreview_neurips"],
        "venue_id": "openreview_neurips",
        "venue": "NeurIPS",
        "full_name": "Conference on Neural Information Processing Systems",
        "openreview_patterns": ["NeurIPS.cc/{year}/Conference"],
        "default_years": [2025],
        "blocked_title_terms": ["neurips"],
    },
    "icml": {
        "aliases": ["icml", "dblp_icml", "openreview_icml"],
        "venue_id": "dblp_icml",
        "venue": "ICML",
        "full_name": "International Conference on Machine Learning",
        "openreview_patterns": ["ICML.cc/{year}/Conference"],
        "default_years": [2026],
        "blocked_title_terms": ["icml"],
    },
}


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return slug or "venue"


def _venue_label(alias: str) -> str:
    known = {
        "iclr": "ICLR",
        "neurips": "NeurIPS",
        "nips": "NeurIPS",
        "icml": "ICML",
        "aistats": "AISTATS",
        "uai": "UAI",
        "colt": "COLT",
        "corl": "CoRL",
        "colm": "COLM",
        "rlc": "RLC",
        "log": "LoG",
        "midl": "MIDL",
        "tmlr": "TMLR",
    }
    return known.get(alias.lower(), alias.upper())


def _expanded_openreview_venues() -> dict[str, dict[str, Any]]:
    venues = {key: dict(spec) for key, spec in OPENREVIEW_VENUES.items()}
    for keywords, patterns in OPENREVIEW_VENUE_PATTERNS:
        aliases = [_slug(keyword) for keyword in keywords if str(keyword or "").strip()]
        if not aliases:
            continue
        key = aliases[0]
        if key in venues:
            merged_aliases = list(dict.fromkeys([*venues[key].get("aliases", []), *aliases]))
            venues[key]["aliases"] = merged_aliases
            venues[key]["openreview_patterns"] = list(dict.fromkeys([*venues[key].get("openreview_patterns", []), *patterns]))
            continue
        full_name = next((keyword for keyword in keywords if " " in keyword), aliases[0])
        venues[key] = {
            "aliases": aliases + [f"openreview_{key}"],
            "venue_id": f"openreview_{key}",
            "venue": _venue_label(key),
            "full_name": str(full_name),
            "openreview_patterns": list(patterns),
            "default_years": [date.today().year],
            "blocked_title_terms": [key],
        }
    return venues


def _venue_aliases() -> dict[str, str]:
    return {
        alias: key
        for key, spec in _expanded_openreview_venues().items()
        for alias in spec["aliases"]
    }


VENUE_ALIASES = _venue_aliases()


def venue_spec(venue: str) -> dict[str, Any]:
    key = VENUE_ALIASES.get(str(venue or "").strip().lower())
    if not key:
        supported = ", ".join(sorted(VENUE_ALIASES))
        raise ValueError(f"unsupported OpenReview venue {venue!r}; supported: {supported}")
    return _expanded_openreview_venues()[key]


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
    category = primary_area or _first_content_text(content, ["category", "Category"])
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


def _probe_timeout(timeout: int) -> int:
    return max(2, min(8, int(timeout or 8)))


def _openreview_venue_ids_for_spec(spec: dict[str, Any], year: int) -> list[str]:
    venue_ids: list[str] = []
    for pattern in spec.get("openreview_patterns") or []:
        text = str(pattern or "").strip()
        if not text:
            continue
        venue_ids.append(text.format(year=year) if "{year}" in text else text)
    return list(dict.fromkeys(venue_ids))


def _probe_openreview_route(url: str, base_params: dict[str, Any], *, route: str, openreview_venueid: str, timeout: int) -> dict[str, Any]:
    params = dict(base_params)
    params["limit"] = 1
    params["offset"] = 0
    started = time.monotonic()
    audit: dict[str, Any] = {
        "route": route,
        "url": url,
        "openreview_venueid": openreview_venueid,
        "params": {key: value for key, value in params.items() if key in {"content.venueid", "invitation", "limit", "offset"}},
        "ok": False,
    }
    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=_probe_timeout(timeout))
        audit["status_code"] = response.status_code
        audit["elapsed_sec"] = round(time.monotonic() - started, 3)
        if response.status_code in {401, 403, 429}:
            audit["skip_reason"] = f"http_{response.status_code}"
            return audit
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            audit["skip_reason"] = "non_json_response"
            audit["content_type"] = response.headers.get("content-type", "")
            audit["response_preview"] = (response.text or "")[:160]
            return audit
        notes = payload.get("notes", [])
        note_count = len(notes) if isinstance(notes, list) else 0
        audit["note_count"] = note_count
        if note_count <= 0:
            audit["skip_reason"] = "probe_returned_no_notes"
            return audit
        audit["ok"] = True
        return audit
    except requests.Timeout as exc:
        audit["elapsed_sec"] = round(time.monotonic() - started, 3)
        audit["skip_reason"] = "timeout"
        audit["error"] = str(exc)[:240]
        return audit
    except Exception as exc:
        audit["elapsed_sec"] = round(time.monotonic() - started, 3)
        audit["skip_reason"] = "request_error"
        audit["error"] = str(exc)[:240]
        return audit


def _audit_counts(papers: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "missing_title_count": sum(1 for paper in papers if not _clean_text(paper.get("title"))),
        "missing_abstract_count": sum(1 for paper in papers if not _clean_text(paper.get("abstract"))),
        "missing_url_count": sum(1 for paper in papers if not _clean_text(paper.get("url"))),
    }


def _category_counts(papers: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for paper in papers:
        category = _clean_text(paper.get("primary_area") or paper.get("category") or "(uncategorized)")
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _write_papers_payload(
    spec: dict[str, Any],
    year: int,
    output_root: Path,
    papers: list[dict[str, Any]],
    *,
    source_adapter: str,
    source: str,
    audit: dict[str, Any] | None = None,
) -> Path:
    normalized = [dict(paper) for paper in papers if isinstance(paper, dict) and _clean_text(paper.get("title"))]
    counts = _audit_counts(normalized)
    payload = {
        "schema_version": 1,
        "venue_id": spec["venue_id"],
        "venue": spec["venue"],
        "full_name": spec["full_name"],
        "year": year,
        "source": source,
        "source_adapter": source_adapter,
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "paper_count": len(normalized),
        "category_counts": _category_counts(normalized),
        "metadata_completeness_audit": audit or venue_metadata_audit_from_papers(normalized),
        **counts,
        "papers": normalized,
    }
    target = output_root / spec["venue_id"] / str(year) / "papers.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json_cache(target, payload)
    return target


def _stable_source_papers(venue: str, year: int, max_items: int) -> tuple[list[dict[str, Any]], str]:
    key = VENUE_ALIASES.get(str(venue or "").strip().lower(), str(venue or "").strip().lower())
    if key == "iclr" and int(year) == 2026:
        return fetch_openreview_iclr_2026(max_items), "openreview_reference"
    if key == "neurips":
        return fetch_neurips_title_index(int(year), max_items), "neurips_official_papers"
    if key == "icml" and int(year) == 2026:
        return fetch_icml_official_virtual_2026(max_items), "icml_official_virtual"
    return [], ""


def _fetch_notes(
    year: int,
    spec: dict[str, Any],
    *,
    page_size: int,
    timeout: int,
    retries: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]], str, str, list[dict[str, Any]]]:
    notes: list[dict[str, Any]] = []
    seen: set[str] = set()
    probe_audits: list[dict[str, Any]] = []
    for openreview_venueid in _openreview_venue_ids_for_spec(spec, year):
        routes = [
            (
                "openreview_api2",
                "https://api2.openreview.net/notes",
                {"content.venueid": openreview_venueid, "details": "replyCount,invitation,original"},
            ),
            (
                "openreview_api1_blind_submission",
                "https://api.openreview.net/notes",
                {"invitation": f"{openreview_venueid}/-/Blind_Submission"},
            ),
            (
                "openreview_api1_submission",
                "https://api.openreview.net/notes",
                {"invitation": f"{openreview_venueid}/-/Submission"},
            ),
        ]
        for route, url, base_params in routes:
            probe = _probe_openreview_route(url, base_params, route=route, openreview_venueid=openreview_venueid, timeout=timeout)
            probe_audits.append(probe)
            if not probe.get("ok"):
                continue
            offset = 0
            route_notes: list[dict[str, Any]] = []
            try:
                for _page in range(max_pages):
                    params = dict(base_params)
                    params.update({"limit": page_size, "offset": offset})
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
                        route_notes.append(note)
                        added += 1
                    if added == 0 or len(page_notes) < page_size:
                        break
                    offset += page_size
            except Exception as exc:
                probe["fetch_error"] = str(exc)[:240]
                continue
            if route_notes:
                notes.extend(route_notes)
                return notes, openreview_venueid, route, probe_audits
    reasons = [
        str(audit.get("skip_reason") or audit.get("fetch_error") or audit.get("error") or audit.get("status_code") or "unknown")
        for audit in probe_audits
        if not audit.get("ok") or audit.get("fetch_error")
    ]
    unique_reasons = ", ".join(list(dict.fromkeys(reasons))[:8]) or "no OpenReview venue ids generated"
    raise RuntimeError(f"OpenReview probe failed/skipped for {spec['venue']} {year}: {unique_reasons}")


def _openreview_live_audit(
    papers: list[dict[str, Any]],
    *,
    spec: dict[str, Any],
    year: int,
    openreview_venueid: str,
    source_adapter: str,
    probe_audits: list[dict[str, Any]],
) -> dict[str, Any]:
    missing_abstracts = sum(1 for paper in papers if not _clean_text(paper.get("abstract")))
    has_categories = any(_clean_text(paper.get("primary_area") or paper.get("category")) for paper in papers)
    audit = {
        "schema_version": 1,
        "status": "partial",
        "title_index_completeness_status": "partial",
        "source_verified": bool(papers),
        "complete": False,
        "title_index_complete": False,
        "official_metadata_complete": bool(papers),
        "adapter": source_adapter,
        "source_adapter": source_adapter,
        "source": "openreview",
        "source_url": "https://openreview.net",
        "openreview_venueid": openreview_venueid,
        "requested_years": [year],
        "paper_count": len(papers),
        "missing_abstract_count": missing_abstracts,
        "has_abstracts": bool(papers) and missing_abstracts == 0,
        "any_abstracts": bool(papers) and missing_abstracts < len(papers),
        "has_official_categories": has_categories,
        "category_status": "official_or_cached_categories" if has_categories else "no_official_categories",
        "source_scope": "openreview_official_venue_notes",
        "official_title_index_verified": bool(papers),
        "official_accepted_list_verified": bool(papers),
        "openreview_probe_status": "passed" if any(audit.get("ok") for audit in probe_audits) else "failed_or_skipped",
        "openreview_probe_audits": probe_audits,
        "openreview_probe_errors": list(dict.fromkeys(
            str(audit.get("skip_reason") or audit.get("fetch_error") or audit.get("error") or audit.get("status_code") or "unknown")
            for audit in probe_audits
            if not audit.get("ok") or audit.get("fetch_error")
        )),
        "completeness_basis": (
            f"{spec['venue']} {year} metadata fetched from OpenReview after a short live probe. "
            "The cache stores all records returned by the successful OpenReview route within the configured page limit; "
            "adapter-level total-count verification is not available from this path."
        ),
    }
    for paper in papers:
        metadata = paper.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata[VENUE_METADATA_AUDIT_KEY] = dict(audit)
    return audit


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
    stable_papers, stable_adapter = _stable_source_papers(venue, year, page_size * max_pages)
    if stable_papers:
        return _write_papers_payload(
            spec,
            year,
            output_root,
            stable_papers,
            source_adapter=stable_adapter,
            source=stable_adapter,
        )

    notes, openreview_venueid, source_adapter, probe_audits = _fetch_notes(
        year,
        spec,
        page_size=page_size,
        timeout=timeout,
        retries=retries,
        max_pages=max_pages,
    )

    papers: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for note in notes:
        paper = _normalize_note(note, year, spec, openreview_venueid)
        if not paper or paper["url"] in seen_urls:
            continue
        seen_urls.add(paper["url"])
        papers.append(paper)

    audit = _openreview_live_audit(
        papers,
        spec=spec,
        year=year,
        openreview_venueid=openreview_venueid,
        source_adapter=source_adapter,
        probe_audits=probe_audits,
    )
    return _write_papers_payload(spec, year, output_root, papers, source_adapter=source_adapter, source="openreview", audit=audit)


def parse_years(value: str) -> list[int]:
    return [int(part) for part in re.split(r"[,\s]+", value.strip()) if part]


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build local conference paper index JSON files, preferring stable official/mirror sources over OpenReview live API.")
    parser.add_argument("--venue", default="iclr", help="Venue alias from OPENREVIEW_VENUE_PATTERNS, e.g. iclr/neurips/icml/aistats/uai/colt/corl/colm/rlc/log/midl/tmlr.")
    parser.add_argument("--years", default="", help="Comma/space separated years; defaults to the venue defaults.")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Output root, default: .runtime/cache/local_database.")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-pages", type=int, default=20)
    args = parser.parse_args(list(argv) if argv is not None else None)

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
    return 0
