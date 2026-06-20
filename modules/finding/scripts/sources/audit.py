from __future__ import annotations

import os
from datetime import datetime
from typing import Any


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
