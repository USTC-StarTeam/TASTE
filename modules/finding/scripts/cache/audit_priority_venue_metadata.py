from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from support.find_support import catalog_by_id
from support.find_support import _apply_cached_acm_abstract_sources
from support.find_support import enrich_acm_doi_with_indexed_abstracts
from support.find_support import fetch_venue_title_index_all, venue_metadata_audit_from_papers
from finding_runtime.paths import LOCAL_DATABASE_DIR, display_path, write_json
from cache.build_venue_metadata_cache import _venue_metadata_status_fields, _write_cache


PRIORITY_VENUE_IDS = [
    "openreview_neurips",
    "openreview_iclr",
    "dblp_icml",
    "dblp_kdd",
    "dblp_www",
    "dblp_sigir",
    "dblp_cikm",
    "dblp_aaai",
    "dblp_iccv",
    "dblp_cvpr",
    "dblp_acl",
    "dblp_ijcai",
    "dblp_eccv",
    "dblp_emnlp",
]


# Objective backfill guard for the monitored venue set. A requested year may
# fall back only when the requested year has no usable metadata cache/source
# and the proceedings are objectively not expected to be public yet, or when a
# recurring venue has no edition that year.
EXPECTED_RELEASE_DATES = {
    ("NEURIPS", 2026): "2026-12-06",
    ("NEURIPS", 2025): "2025-12-02",
    ("SIGIR", 2026): "2026-07-20",
    ("CIKM", 2026): "2026-11-09",
    ("ICCV", 2026): "2026-12-31",
    ("ECCV", 2026): "2026-09-08",
    ("ECCV", 2025): "2025-12-31",
    ("IJCAI", 2026): "2026-08-15",
    ("EMNLP", 2026): "2026-11-01",
}


ODD_YEAR_VENUES = {"ICCV"}
EVEN_YEAR_VENUES = {"ECCV"}


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _priority_audit_network_enrichment_enabled() -> bool:
    if _truthy_env("ACM_PRIORITY_AUDIT_CACHE_ONLY"):
        return False
    if _truthy_env("ACM_PRIORITY_AUDIT_NETWORK_ENRICHMENT"):
        return True
    explicit_network_flags = (
        "ACM_PUBLIC_PAGE_SEARCH_FALLBACK",
        "ACM_PUBLIC_PDF_SEARCH_FALLBACK",
        "ACM_OPENALEX_TITLE_FALLBACK",
        "ACM_SEMANTIC_SCHOLAR_FALLBACK",
        "ACM_ARXIV_TITLE_FALLBACK",
        "ACM_ARXIV_WEB_TITLE_FALLBACK",
        "OPENALEX_ACM_RETRY_MISSES",
        "ACM_PDF_RETRY_MISSES",
    )
    return any(_truthy_env(name) for name in explicit_network_flags)


def _caller_cwd() -> Path:
    env_value = str(os.environ.get("FINDING_CALLER_CWD") or "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve()
    return Path.cwd().expanduser().resolve()


def _resolve_cli_path(value: str, *, default: Path) -> Path:
    text = str(value or "").strip()
    if not text:
        return default
    path = Path(text).expanduser()
    if path.is_absolute():
        return path
    return _caller_cwd() / path


def _clean_key(value: object) -> str:
    return str(value or "").strip().upper().replace("SIGKDD", "KDD")


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except Exception:
        return None


def _venue_name(venue: dict[str, Any]) -> str:
    return _clean_key(venue.get("name") or venue.get("id"))


def _objective_unavailable_reason(venue: dict[str, Any], year: int, *, as_of: date, requested_available: bool = False) -> str:
    name = _venue_name(venue)
    if name in ODD_YEAR_VENUES and year % 2 == 0:
        return f"{name} is an odd-year conference; {year} has no regular proceedings edition."
    if name in EVEN_YEAR_VENUES and year % 2 == 1:
        return f"{name} is an even-year conference; {year} has no regular proceedings edition."
    if requested_available:
        return ""
    release = _parse_date(EXPECTED_RELEASE_DATES.get((name, year), ""))
    if release and release > as_of:
        return f"{name} {year} proceedings are not expected before {release.isoformat()}, and no usable requested-year title index was found."
    return ""


def _regular_venue_year(venue: dict[str, Any], year: int) -> bool:
    name = _venue_name(venue)
    if name in ODD_YEAR_VENUES:
        return int(year) % 2 == 1
    if name in EVEN_YEAR_VENUES:
        return int(year) % 2 == 0
    return True


def _year_candidates(
    venue: dict[str, Any],
    requested_year: int,
    *,
    as_of: date,
    max_backfill_years: int,
    requested_available: bool,
) -> list[tuple[int, str]]:
    candidates: list[tuple[int, str]] = []
    fallback_reason = _objective_unavailable_reason(venue, requested_year, as_of=as_of, requested_available=requested_available)
    for year in range(int(requested_year), int(requested_year) - max(0, max_backfill_years) - 1, -1):
        if not _regular_venue_year(venue, year):
            continue
        if year == requested_year:
            if requested_available or not fallback_reason:
                candidates.append((year, "requested"))
            continue
        if fallback_reason:
            candidates.append((year, fallback_reason))
    return candidates


def _cache_manifest(output_root: Path, venue_id: str, year: int) -> dict[str, Any]:
    path = output_root / venue_id / str(year) / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _cached_verified_row(
    venue_id: str,
    venue: dict[str, Any],
    year: int,
    *,
    requested_year: int,
    output_root: Path,
    backfill_basis: str,
) -> dict[str, Any] | None:
    manifest = _cache_manifest(output_root, venue_id, year)
    audit = manifest.get("audit") if isinstance(manifest.get("audit"), dict) else {}
    if not (audit.get("complete") and audit.get("source_verified")):
        return None
    audit = dict(audit)
    audit.setdefault("venue_id", venue_id)
    audit.setdefault("venue", venue.get("name") or venue_id)
    papers_path = output_root / venue_id / str(year) / "papers.json"
    try:
        payload = json.loads(papers_path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    papers = payload.get("papers") if isinstance(payload, dict) and isinstance(payload.get("papers"), list) else []
    paper_count = int(manifest.get("paper_count") or len(papers) or 0)
    fields = _venue_metadata_status_fields(audit)
    if not fields.get("metadata_completeness_ok"):
        return None
    row = {
        "venue_id": venue_id,
        "venue": venue.get("name") or venue_id,
        "requested_year": requested_year,
        "effective_year": year,
        "adapter": manifest.get("adapter") or manifest.get("source_adapter") or audit.get("adapter") or "local_database",
        "paper_count": paper_count,
        "ok": paper_count > 0,
        "backfill_basis": backfill_basis,
        "audit": audit,
        "cache_reused": True,
        "cache_directory": display_path(output_root / venue_id / str(year)),
        **fields,
    }
    if year != requested_year:
        row["year_fallback_used"] = True
        row["year_fallback_reason"] = backfill_basis
    return row


def _audit_ok(audit: dict[str, Any], papers: list[dict[str, Any]]) -> bool:
    if not papers or not audit.get("complete") or not audit.get("source_verified"):
        return False
    return bool(_venue_metadata_status_fields(audit).get("metadata_completeness_ok"))


def _audit_one(
    venue_id: str,
    requested_year: int,
    *,
    output_root: Path,
    write_cache: bool,
    as_of: date,
    max_backfill_years: int,
) -> dict[str, Any]:
    catalog = catalog_by_id()
    venue = catalog.get(venue_id)
    if not venue:
        return {
            "venue_id": venue_id,
            "requested_year": requested_year,
            "effective_year": None,
            "ok": False,
            "adapter": "unknown",
            "paper_count": 0,
            "error": "unknown_venue_id",
        }

    attempts: list[dict[str, Any]] = []
    requested_cache = _cached_verified_row(
        venue_id,
        venue,
        requested_year,
        requested_year=requested_year,
        output_root=output_root,
        backfill_basis="requested",
    )
    requested_available = bool(requested_cache and requested_cache.get("ok"))
    for candidate_year, backfill_basis in _year_candidates(
        venue,
        requested_year,
        as_of=as_of,
        max_backfill_years=max_backfill_years,
        requested_available=requested_available,
    ):
        cached = _cached_verified_row(
            venue_id,
            venue,
            candidate_year,
            requested_year=requested_year,
            output_root=output_root,
            backfill_basis=backfill_basis,
        )
        if cached:
            return cached
        try:
            papers, adapter = fetch_venue_title_index_all(venue, [candidate_year])
            if _priority_audit_network_enrichment_enabled():
                papers, acm_cache_stats = enrich_acm_doi_with_indexed_abstracts(papers)
                local_acm_cache_stats = acm_cache_stats.get("local_cache", {}) if isinstance(acm_cache_stats, dict) else {}
            else:
                papers, local_acm_cache_stats = _apply_cached_acm_abstract_sources(papers)
            audit = venue_metadata_audit_from_papers(papers)
            if isinstance(audit, dict):
                audit = dict(audit)
                audit.setdefault("venue_id", venue_id)
                audit.setdefault("venue", venue.get("name") or venue_id)
                if local_acm_cache_stats.get("abstracts_filled"):
                    audit["local_acm_abstract_cache_stats"] = local_acm_cache_stats
            fields = _venue_metadata_status_fields(audit)
            audit_ok = _audit_ok(audit, papers)
            row = {
                "venue_id": venue_id,
                "venue": venue.get("name") or venue_id,
                "requested_year": requested_year,
                "effective_year": candidate_year,
                "adapter": adapter,
                "paper_count": len(papers),
                "ok": audit_ok,
                "backfill_basis": backfill_basis,
                "audit": audit,
                **fields,
            }
            attempts.append(row)
            if papers:
                if write_cache and audit_ok:
                    directory = _write_cache(venue_id, venue, candidate_year, papers, adapter, audit, output_root)
                    row["cache_directory"] = display_path(directory)
                elif write_cache and papers:
                    row["cache_write_skipped"] = "source audit is partial or unverified; existing verified cache was left untouched"
                if candidate_year == requested_year and not audit_ok and backfill_basis == "requested":
                    fallback_reason = _objective_unavailable_reason(
                        venue,
                        requested_year,
                        as_of=as_of,
                        requested_available=False,
                    )
                    if fallback_reason:
                        row["requested_year_partial_skipped_for_backfill"] = True
                        row["year_fallback_reason"] = fallback_reason
                        continue
                if candidate_year != requested_year:
                    row["year_fallback_used"] = True
                    row["year_fallback_reason"] = backfill_basis
                return row
        except Exception as exc:
            attempts.append({
                "venue_id": venue_id,
                "venue": venue.get("name") or venue_id,
                "requested_year": requested_year,
                "effective_year": candidate_year,
                "ok": False,
                "adapter": "error",
                "paper_count": 0,
                "backfill_basis": backfill_basis,
                "error": str(exc)[:500],
            })
    failure = dict(attempts[-1]) if attempts else {}
    failure.update({
        "venue_id": venue_id,
        "requested_year": requested_year,
        "ok": False,
        "attempts": attempts,
        "suggested_fix": "Repair or add an official venue adapter before using this venue/year as a complete Find corpus.",
    })
    return failure


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit full Find metadata crawling for the 14 priority conference venues.")
    parser.add_argument("--year", type=int, default=date.today().year)
    parser.add_argument("--venue-id", action="append", default=[], help="Limit to one or more venue ids. Defaults to the 14 priority venues.")
    parser.add_argument("--output-root", default=str(LOCAL_DATABASE_DIR))
    parser.add_argument("--report", default="", help="Optional report JSON path. If omitted, no report file is written.")
    parser.add_argument("--write-cache", action="store_true", help="Write verified venue/year caches under output-root.")
    parser.add_argument("--max-backfill-years", type=int, default=3)
    args = parser.parse_args(list(argv) if argv is not None else None)

    venue_ids = args.venue_id or PRIORITY_VENUE_IDS
    output_root = _resolve_cli_path(str(args.output_root or ""), default=LOCAL_DATABASE_DIR)
    report_path = _resolve_cli_path(args.report, default=LOCAL_DATABASE_DIR) if args.report else None
    rows = []
    for index, venue_id in enumerate(venue_ids, 1):
        print(f"[{index}/{len(venue_ids)}] auditing {venue_id} {args.year}", flush=True)
        row = _audit_one(
            venue_id,
            int(args.year),
            output_root=output_root,
            write_cache=bool(args.write_cache),
            as_of=datetime.now(timezone.utc).date(),
            max_backfill_years=max(0, int(args.max_backfill_years)),
        )
        rows.append(row)
        print(
            f"[{index}/{len(venue_ids)}] {venue_id}: ok={bool(row.get('ok'))} "
            f"effective_year={row.get('effective_year')} adapter={row.get('adapter')} "
            f"count={row.get('paper_count')} status={row.get('metadata_completeness_status') or row.get('error') or ''}",
            flush=True,
        )
    payload = {
        "schema_version": 1,
        "requested_year": int(args.year),
        "venue_count": len(rows),
        "ok_count": sum(1 for row in rows if row.get("ok")),
        "failed_count": sum(1 for row in rows if not row.get("ok")),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "venues": rows,
    }
    if report_path is not None:
        write_json(report_path, payload)
    print(json.dumps({
        "report": display_path(report_path) if report_path is not None else "",
        "report_written": report_path is not None,
        "requested_year": payload["requested_year"],
        "venue_count": payload["venue_count"],
        "ok_count": payload["ok_count"],
        "failed_count": payload["failed_count"],
        "failed_venues": [row.get("venue_id") for row in rows if not row.get("ok")],
    }, ensure_ascii=False, indent=2))
    return 0
