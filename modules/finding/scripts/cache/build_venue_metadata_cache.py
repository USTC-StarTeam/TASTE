from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from support.find_support import catalog_by_id
from support.find_support import fetch_selected_venue_details, fetch_venue_title_index, fetch_venue_title_index_all, venue_metadata_audit_from_papers
from finding_runtime.llm import keyword_category
from finding_runtime.paths import LOCAL_DATABASE_DIR, write_json_cache
from finding_runtime.paths import display_path
from venue_metadata_policy import policy_summary, priority_venue_policy_for_audit


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


_NO_OFFICIAL_CATEGORY_STATUSES = {"no_official_categories", "missing_categories", "no_or_partial_categories", "unknown", ""}
_DBLP_TITLE_INDEX_SCOPE = "dblp_current_index_not_official_accepted_list"


def _official_title_index_scope(adapter: str) -> str:
    adapter_key = str(adapter or "").lower()
    if adapter_key.startswith("icml_official_virtual"):
        return "official_icml_virtual_metadata"
    if adapter_key.startswith("icml_downloads"):
        return "official_icml_downloads_title_index"
    if adapter_key.startswith("openreview"):
        return "official_openreview_metadata"
    return ""


def _source_has_official_categories(audit: dict[str, Any]) -> bool:
    status = str(audit.get("category_status") or "").lower()
    adapter = str(audit.get("source_adapter") or audit.get("adapter") or "").lower()
    source_scope = str(audit.get("source_scope") or "").lower()
    if adapter.startswith("neurips_official_papers") or source_scope == "official_neurips_papers_index":
        return False
    return bool(audit.get("has_official_categories")) and status not in _NO_OFFICIAL_CATEGORY_STATUSES


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _abstract_unavailable_verified(paper: dict[str, Any]) -> bool:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    return bool(metadata.get("abstract_unavailable_verified"))


def _missing_abstract_count(papers: list[dict[str, Any]]) -> int:
    return sum(1 for row in papers if isinstance(row, dict) and not _clean_text(row.get("abstract")) and not _abstract_unavailable_verified(row))


def _abstract_unavailable_count(papers: list[dict[str, Any]]) -> int:
    return sum(1 for row in papers if isinstance(row, dict) and not _clean_text(row.get("abstract")) and _abstract_unavailable_verified(row))


def _missing_title_count(papers: list[dict[str, Any]]) -> int:
    return sum(1 for row in papers if isinstance(row, dict) and not _clean_text(row.get("title")))


def _category_total(categories: list[dict[str, Any]]) -> int:
    total = 0
    for row in categories:
        if isinstance(row, dict):
            total += _as_int(row.get("count"), 0)
    return total


def _is_dblp_title_index(venue_id: str, adapter: str) -> bool:
    # Venue ids such as dblp_icml are catalog keys, not proof that the
    # underlying cache came from DBLP. The source adapter is the authority.
    adapter_key = str(adapter or "").lower()
    return adapter_key.startswith("dblp")


def _normalize_metadata_audit(
    audit: dict[str, Any] | None,
    papers: list[dict[str, Any]],
    categories: list[dict[str, Any]],
    *,
    venue_id: str,
    adapter: str,
    paper_count: int | None = None,
) -> dict[str, Any]:
    normalized = dict(audit or {})
    expected = _as_int(normalized.get("expected_paper_count") or normalized.get("paper_count") or paper_count or len(papers), len(papers))
    missing_titles = _missing_title_count(papers)
    missing_abstracts = _missing_abstract_count(papers)
    abstract_unavailable = _abstract_unavailable_count(papers)
    category_total = _category_total(categories)
    dblp_title_index = _is_dblp_title_index(venue_id, adapter)
    official_scope = _official_title_index_scope(adapter)

    category_status = str(normalized.get("category_status") or "").lower()
    has_official_categories = _source_has_official_categories(normalized)
    neurips_track_only = str(adapter or "").lower().startswith("neurips_official_papers") or str(normalized.get("source_scope") or "").lower() == "official_neurips_papers_index"
    if neurips_track_only:
        has_official_categories = False
        category_status = "no_official_categories"
    elif not category_status:
        has_official_categories = bool(categories and category_total == expected and not dblp_title_index)
        category_status = "official_or_cached_categories" if has_official_categories else "no_official_categories" if categories else "missing_categories"
    elif category_status in _NO_OFFICIAL_CATEGORY_STATUSES:
        has_official_categories = False

    source_complete_hint = bool(normalized.get("complete")) if "complete" in normalized else True
    title_index_complete = bool(
        source_complete_hint
        and expected > 0
        and len(papers) == expected
        and missing_titles == 0
        and (not has_official_categories or (bool(categories) and category_total == expected))
    )
    source_verified_hint = bool(normalized.get("source_verified")) if "source_verified" in normalized else title_index_complete
    indexed_abstract_enrichment = bool(
        normalized.get("abstract_enrichment_complete")
        and normalized.get("publisher_doi_seed_verified")
        and missing_abstracts == 0
    )

    normalized.update({
        "schema_version": 1,
        "status": "complete" if title_index_complete else "partial",
        "source_verified": bool(source_verified_hint and title_index_complete),
        "complete": title_index_complete,
        "adapter": adapter,
        "source_adapter": normalized.get("source_adapter") or adapter,
        "venue_id": venue_id,
        "venue": normalized.get("venue") or "",
        "paper_count": len(papers),
        "expected_paper_count": expected,
        "category_count": len(categories),
        "category_total_count": category_total,
        "categorized_paper_count": category_total,
        "category_coverage": (category_total / expected) if expected else 0.0,
        "missing_title_count": missing_titles,
        "missing_abstract_count": missing_abstracts,
        "official_abstract_unavailable_count": abstract_unavailable,
        "has_abstracts": bool(papers) and missing_abstracts == 0,
        "any_abstracts": bool(papers) and missing_abstracts < len(papers),
        "has_official_categories": has_official_categories,
        "category_status": category_status,
    })
    if official_scope:
        normalized["source_scope"] = official_scope
        normalized["official_title_index_verified"] = title_index_complete
        if official_scope in {"official_icml_downloads_title_index", "official_icml_virtual_metadata", "official_openreview_metadata"}:
            normalized["official_accepted_list_verified"] = title_index_complete
        elif "official_accepted_list_verified" not in normalized:
            normalized["official_accepted_list_verified"] = None
    elif dblp_title_index:
        if indexed_abstract_enrichment:
            normalized["source_scope"] = "acm_doi_seed_with_indexed_abstracts"
        else:
            normalized["source_scope"] = normalized.get("source_scope") or _DBLP_TITLE_INDEX_SCOPE
        normalized["official_title_index_verified"] = False
        normalized["official_accepted_list_verified"] = False
    else:
        normalized["source_scope"] = normalized.get("source_scope") or ""
        normalized.setdefault("official_title_index_verified", title_index_complete)
        normalized.setdefault("official_accepted_list_verified", None)
    return normalized


def _venue_metadata_status_fields(audit: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(audit, dict):
        audit = {}
    title_status = str(audit.get("status") or ("complete" if audit.get("complete") else "partial" if audit else "unknown"))
    title_complete = bool(audit.get("complete"))
    has_abstracts = bool(audit.get("has_abstracts"))
    has_official_categories = _source_has_official_categories(audit)
    category_status = str(audit.get("category_status") or "unknown")
    no_official_categories = category_status.lower() in _NO_OFFICIAL_CATEGORY_STATUSES
    policy = priority_venue_policy_for_audit(audit)
    policy_info = policy_summary(policy)
    full_abstract_required = bool(policy_info.get("full_abstract_required") and not policy_info.get("allow_title_only_verified_cache"))
    official_accepted_list_required = bool(policy_info.get("official_accepted_list_required"))
    official_categories_expected = bool(policy_info.get("official_categories_expected"))
    official_accepted_list_verified = audit.get("official_accepted_list_verified") is True
    indexed_enrichment_allowed = bool(policy_info.get("allow_indexed_abstract_enrichment"))
    abstract_enrichment_complete = bool(audit.get("abstract_enrichment_complete"))
    publisher_doi_seed_verified = bool(audit.get("publisher_doi_seed_verified"))
    indexed_abstracts_usable = bool(indexed_enrichment_allowed and abstract_enrichment_complete and publisher_doi_seed_verified and has_abstracts)
    if policy:
        metadata_ready = (
            title_complete
            and (has_abstracts or not full_abstract_required)
            and (has_official_categories or not official_categories_expected)
            and (official_accepted_list_verified or not official_accepted_list_required or indexed_abstracts_usable)
        )
    else:
        metadata_ready = title_complete and (has_abstracts or has_official_categories)
    if not audit:
        metadata_status = "unknown"
    elif metadata_ready:
        metadata_status = "abstract_enriched_complete" if indexed_abstracts_usable and not official_accepted_list_verified else "complete"
    elif title_complete and full_abstract_required and not has_abstracts:
        metadata_status = "abstract_incomplete"
    elif title_complete and official_categories_expected and not has_official_categories:
        metadata_status = "category_incomplete"
    elif title_complete and official_accepted_list_required and not official_accepted_list_verified:
        metadata_status = "accepted_list_unverified"
    elif title_complete:
        metadata_status = "title_index_only"
    else:
        metadata_status = "partial"

    basis_parts: list[str] = []
    if audit.get("completeness_basis"):
        basis_parts.append(str(audit.get("completeness_basis")))
    if title_complete and not has_abstracts:
        if full_abstract_required:
            basis_parts.append("Priority venue policy requires full official abstracts before this cache can be treated as reusable complete metadata; title-only rows are audit-only and must be enriched from the venue/official proceedings detail source.")
        else:
            basis_parts.append("Title corpus was verified, but this source does not expose abstracts in the title index; The workflow must enrich selected papers before final LLM scoring.")
    if indexed_abstracts_usable:
        basis_parts.append("This venue cache is accepted as indexed-abstract enriched metadata: every row has an ACM DOI seed and a real abstract from the configured indexed DOI metadata sources. It is not labeled as ACM DL HTML full crawl.")
    if title_complete and official_categories_expected and not has_official_categories:
        basis_parts.append("Priority venue policy expects official categories/areas/tracks for this venue; category metadata is missing or untrusted.")
    if title_complete and official_accepted_list_required and not official_accepted_list_verified and not indexed_abstracts_usable:
        basis_parts.append("Priority venue policy requires an official accepted/proceedings list; DBLP/current-index title seeds cannot be treated as verified full venue metadata.")
    if no_official_categories:
        basis_parts.append("No trusted official venue categories were available from this adapter; the workflow skips category pruning and uses title LLM screening over the title corpus.")
    if policy_info.get("fallback_policy"):
        basis_parts.append("Priority venue source policy: " + str(policy_info.get("fallback_policy")))

    return {
        "title_index_completeness_status": title_status,
        "title_index_completeness_ok": title_complete,
        "metadata_completeness_status": metadata_status,
        "metadata_completeness_ok": metadata_ready,
        "metadata_completeness_limited": bool(audit) and not metadata_ready,
        "metadata_completeness_basis": " ".join(part.strip() for part in basis_parts if part).strip(),
        "metadata_source_policy": policy_info,
        "abstract_enrichment_complete": abstract_enrichment_complete,
        "abstract_enrichment_source": audit.get("abstract_enrichment_source") or "",
        "publisher_doi_seed_verified": publisher_doi_seed_verified,
        "has_official_categories": has_official_categories,
        "category_status": audit.get("category_status") or "unknown",
        "has_abstracts": has_abstracts,
        "has_abstracts_in_title_index": has_abstracts,
        "any_abstracts": bool(audit.get("any_abstracts") or has_abstracts),
        "missing_abstract_count": _as_int(audit.get("missing_abstract_count"), 0),
        "cache_verified": title_complete,
    }


def _manifest_metadata_fields(audit: dict[str, Any]) -> dict[str, Any]:
    fields = _venue_metadata_status_fields(audit)
    fields.update({
        "source_verified": bool(audit.get("source_verified")),
        "source_scope": audit.get("source_scope") or "",
        "official_title_index_verified": audit.get("official_title_index_verified"),
        "official_accepted_list_verified": audit.get("official_accepted_list_verified"),
        "completeness_basis": audit.get("completeness_basis") or "",
    })
    return fields


def _paper_category(paper: dict[str, Any], *, allow_local_topic: bool = False) -> str:
    for key in ("primary_area", "category"):
        value = _clean_text(paper.get(key))
        if value and not value.lower().startswith("local topic:"):
            return value
    if not allow_local_topic:
        return ""
    title = _clean_text(paper.get("title"))
    abstract = _clean_text(paper.get("abstract"))
    return keyword_category(title, abstract)


def _normalize_paper(paper: dict[str, Any], venue_id: str, venue_name: str, year: int, *, has_official_categories: bool) -> dict[str, Any]:
    row = dict(paper)
    row["venue"] = row.get("venue") or venue_name
    row["year"] = int(row.get("year") or year)
    category = _paper_category(row, allow_local_topic=False) if has_official_categories else ""
    row["category"] = category
    row["primary_area"] = category
    row.setdefault("track", "")
    row["classification_source"] = row.get("classification_source") or ("local_metadata_category" if has_official_categories else "uncategorized_title_index")
    metadata = dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), dict) else {}
    metadata.setdefault("venue_id", venue_id)
    metadata.setdefault("local_database_cache", True)
    metadata["category_status"] = "official_or_cached_categories" if has_official_categories else "no_official_categories"
    row["metadata"] = metadata
    return row


def _category_summary(papers: list[dict[str, Any]], sample_size: int = 5, *, has_official_categories: bool = True) -> list[dict[str, Any]]:
    if not has_official_categories:
        return []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for paper in papers:
        category = _paper_category(paper, allow_local_topic=False)
        if category:
            grouped.setdefault(category, []).append(paper)
    rows: list[dict[str, Any]] = []
    for name, items in grouped.items():
        keywords: list[str] = []
        seen_keywords: set[str] = set()
        for item in items:
            for keyword in item.get("keywords") or []:
                text = _clean_text(keyword)[:80]
                key = text.lower()
                if text and key not in seen_keywords:
                    seen_keywords.add(key)
                    keywords.append(text)
                if len(keywords) >= 20:
                    break
            if len(keywords) >= 20:
                break
        rows.append({
            "name": name,
            "count": len(items),
            "sample_titles": [_clean_text(item.get("title"))[:180] for item in items[:sample_size] if _clean_text(item.get("title"))],
            "sample_keywords": keywords,
        })
    return sorted(rows, key=lambda item: (-int(item["count"]), str(item["name"]).lower()))


def _write_cache(venue_id: str, venue: dict[str, Any], year: int, papers: list[dict[str, Any]], adapter: str, audit: dict[str, Any], output_root: Path) -> Path:
    directory = output_root / venue_id / str(year)
    directory.mkdir(parents=True, exist_ok=True)
    existing_manifest = directory / "manifest.json"
    if existing_manifest.exists():
        try:
            existing = json.loads(existing_manifest.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        existing_audit = existing.get("audit") if isinstance(existing, dict) and isinstance(existing.get("audit"), dict) else {}
        existing_count = _as_int(existing.get("paper_count") if isinstance(existing, dict) else 0, 0)
        incoming_complete = bool(audit.get("complete") and audit.get("source_verified") and _venue_metadata_status_fields(audit).get("metadata_completeness_ok"))
        existing_complete = bool(existing_audit.get("complete") and existing_audit.get("source_verified") and _venue_metadata_status_fields(existing_audit).get("metadata_completeness_ok"))
        if existing_complete and (not incoming_complete or len(papers) < existing_count):
            return directory
    has_official_categories = _source_has_official_categories(audit)
    normalized = [_normalize_paper(paper, venue_id, str(venue.get("name") or venue_id), year, has_official_categories=has_official_categories) for paper in papers]
    categories = _category_summary(normalized, has_official_categories=has_official_categories)
    category_counts = {row["name"]: row["count"] for row in categories}
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {
        "schema_version": 1,
        "venue_id": venue_id,
        "venue": venue.get("name") or venue_id,
        "full_name": venue.get("full_name") or "",
        "year": year,
        "source": adapter,
        "source_adapter": adapter,
        "fetched_at": now,
        "paper_count": len(normalized),
        "category_counts": category_counts,
        "metadata_completeness_audit": audit,
        "papers": normalized,
        "category_summary": categories,
        "category_summary_built_at": now,
    }
    write_json_cache(directory / "papers.json", payload)
    summary_payload = {
        "schema_version": 1,
        "venue_id": venue_id,
        "venue": venue.get("name") or venue_id,
        "full_name": venue.get("full_name") or "",
        "year": year,
        "source": adapter,
        "source_adapter": adapter,
        "paper_count": len(normalized),
        "category_count": len(categories),
        "category_counts": category_counts,
        "category_summary": categories,
        "metadata_completeness_audit": audit,
        "built_at": now,
        "source_file": str(directory / "papers.json"),
    }
    write_json_cache(directory / "category_summary.json", summary_payload)
    audit = _normalize_metadata_audit(audit, normalized, categories, venue_id=venue_id, adapter=adapter, paper_count=len(normalized))
    metadata_fields = _manifest_metadata_fields(audit)
    manifest = {
        "schema_version": 1,
        "venue_id": venue_id,
        "venue": venue.get("name") or venue_id,
        "year": year,
        "requested_year": year,
        "effective_year": year,
        "adapter": adapter,
        "source_adapter": adapter,
        "paper_count": len(normalized),
        "category_count": len(categories),
        **metadata_fields,
        "built_at": now,
        "papers_path": display_path(directory / "papers.json"),
        "category_summary_path": display_path(directory / "category_summary.json"),
        "audit": audit,
    }
    write_json_cache(directory / "manifest.json", manifest)
    return directory




def _audit_existing_cache(directory: Path, venue_id: str, year: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    papers_path = directory / "papers.json"
    summary_path = directory / "category_summary.json"
    manifest_path = directory / "manifest.json"
    if not papers_path.exists() or not summary_path.exists():
        raise SystemExit(f"Missing papers/category_summary cache files in {directory}")
    papers_payload = json.loads(papers_path.read_text(encoding="utf-8"))
    summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    papers = papers_payload.get("papers") if isinstance(papers_payload, dict) and isinstance(papers_payload.get("papers"), list) else []
    categories = summary_payload.get("category_summary") if isinstance(summary_payload, dict) and isinstance(summary_payload.get("category_summary"), list) else []
    paper_count = _as_int(papers_payload.get("paper_count") or summary_payload.get("paper_count") or (manifest_payload.get("paper_count") if isinstance(manifest_payload, dict) else 0) or len(papers), len(papers))
    existing_audit = {}
    if isinstance(manifest_payload, dict):
        existing_audit = manifest_payload.get("audit") if isinstance(manifest_payload.get("audit"), dict) else manifest_payload.get("metadata_completeness_audit") if isinstance(manifest_payload.get("metadata_completeness_audit"), dict) else {}
    if not existing_audit and isinstance(papers_payload, dict):
        existing_audit = papers_payload.get("metadata_completeness_audit") if isinstance(papers_payload.get("metadata_completeness_audit"), dict) else {}
    adapter = str(
        (existing_audit.get("adapter") if isinstance(existing_audit, dict) else "")
        or (manifest_payload.get("adapter") if isinstance(manifest_payload, dict) else "")
        or (manifest_payload.get("source_adapter") if isinstance(manifest_payload, dict) else "")
        or papers_payload.get("source_adapter")
        or summary_payload.get("source_adapter")
        or papers_payload.get("source")
        or summary_payload.get("source")
        or "local_database"
    )
    audit = _normalize_metadata_audit(existing_audit, papers, categories, venue_id=venue_id, adapter=adapter, paper_count=paper_count)
    audit["source_url"] = str(papers_payload.get("source") or summary_payload.get("source") or audit.get("source_url") or adapter)
    audit["papers_path"] = display_path(papers_path)
    audit["category_summary_path"] = display_path(summary_path)
    audit["manifest_path"] = display_path(manifest_path) if manifest_path.exists() else ""
    audit["completeness_basis"] = "Existing local Find metadata cache audit: paper count, non-empty titles, abstract availability, and trusted category summary consistency. This verifies Find metadata only, not Read-stage PDF full text."
    return papers_payload, summary_payload, audit, categories


def write_manifest_for_existing_cache(venue_id: str, year: int, output_root: Path = LOCAL_DATABASE_DIR, *, effective_year: int | None = None, requested_year: int | None = None) -> Path:
    effective = int(effective_year or year)
    directory = output_root / venue_id / str(effective)
    papers_payload, summary_payload, audit, categories = _audit_existing_cache(directory, venue_id, effective)
    adapter = str(audit.get("adapter") or papers_payload.get("source_adapter") or summary_payload.get("source_adapter") or "local_database")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    metadata_fields = _manifest_metadata_fields(audit)
    manifest = {
        "schema_version": 1,
        "venue_id": venue_id,
        "venue": papers_payload.get("venue") or summary_payload.get("venue") or venue_id,
        "year": effective,
        "requested_year": int(requested_year or year),
        "effective_year": effective,
        "adapter": adapter,
        "source_adapter": adapter,
        "paper_count": int(papers_payload.get("paper_count") or len(papers_payload.get("papers") or [])),
        "category_count": len(categories),
        **metadata_fields,
        "built_at": now,
        "papers_path": display_path(directory / "papers.json"),
        "category_summary_path": display_path(directory / "category_summary.json"),
        "audit": audit,
    }
    write_json_cache(directory / "manifest.json", manifest)
    return directory

def _audit_with_venue_context(audit: dict[str, Any], venue_id: str, venue: dict[str, Any]) -> dict[str, Any]:
    audit = dict(audit) if isinstance(audit, dict) else {}
    audit.setdefault("venue_id", venue_id)
    audit.setdefault("venue", venue.get("name") or venue_id)
    return audit


def _metadata_ready_or_raise(
    audit: dict[str, Any],
    venue_id: str,
    year: int,
    *,
    papers: list[dict[str, Any]] | None = None,
    adapter: str = "",
) -> dict[str, Any]:
    fields = _venue_metadata_status_fields(audit)
    if fields.get("metadata_completeness_ok"):
        return fields
    raise SystemExit(
        "Refusing to write incomplete venue metadata cache for "
        f"{venue_id} {year}: status={fields.get('metadata_completeness_status')} "
        f"missing_abstracts={fields.get('missing_abstract_count')} "
        f"basis={fields.get('metadata_completeness_basis')}"
    )


def build_cache(
    venue_id: str,
    year: int,
    output_root: Path = LOCAL_DATABASE_DIR,
    max_items: int = 12000,
    *,
    enrich_details: bool = True,
    detail_wall_timeout_sec: float = 0.0,
) -> Path:
    catalog = catalog_by_id()
    venue = catalog.get(venue_id)
    if not venue:
        raise SystemExit(f"Unknown venue id: {venue_id}")
    papers, adapter = fetch_venue_title_index_all(venue, [year]) if max_items <= 0 or max_items >= 100000 else fetch_venue_title_index(venue, [year], max_items)
    audit = _audit_with_venue_context(venue_metadata_audit_from_papers(papers), venue_id, venue)
    if not papers:
        raise SystemExit(f"No venue metadata rows fetched for {venue_id} {year} via {adapter}")
    if not audit.get("source_verified"):
        raise SystemExit(f"Venue metadata source is not verified for {venue_id} {year}: {audit}")
    fields = _venue_metadata_status_fields(audit)
    should_enrich_details = bool(
        enrich_details
        and fields.get("metadata_source_policy")
        and fields.get("metadata_completeness_status") in {"abstract_incomplete", "accepted_list_unverified"}
    )
    if should_enrich_details:
        papers = fetch_selected_venue_details(
            papers,
            wall_timeout_sec=detail_wall_timeout_sec,
        )
        audit = _audit_with_venue_context(venue_metadata_audit_from_papers(papers), venue_id, venue)
    _metadata_ready_or_raise(audit, venue_id, year, papers=papers, adapter=adapter)
    return _write_cache(venue_id, venue, year, papers, adapter, audit, output_root)


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a verified local Find metadata cache for a venue/year.")
    parser.add_argument("--venue-id", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--output-root", default=str(LOCAL_DATABASE_DIR))
    parser.add_argument("--max-items", type=int, default=12000)
    parser.add_argument("--no-enrich-details", action="store_true", help="Do not crawl official detail pages before writing. Priority venues will usually fail without this enrichment.")
    parser.add_argument("--detail-wall-timeout-sec", type=float, default=0.0, help="Optional wall timeout for full official detail enrichment.")
    parser.add_argument("--from-existing", action="store_true", help="Write/refresh manifest.json for an existing papers/category_summary cache without fetching.")
    parser.add_argument("--effective-year", type=int, default=0, help="Existing cache year when it differs from the requested year, e.g. NeurIPS 2026 -> 2025 backfill.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.from_existing:
        directory = write_manifest_for_existing_cache(args.venue_id, args.year, Path(args.output_root), effective_year=args.effective_year or args.year, requested_year=args.year)
    else:
        directory = build_cache(
            args.venue_id,
            args.year,
            Path(args.output_root),
            max_items=max(0, args.max_items),
            enrich_details=not args.no_enrich_details,
            detail_wall_timeout_sec=max(0.0, float(args.detail_wall_timeout_sec or 0.0)),
        )
    print(f"Wrote {directory}")
    return 0
