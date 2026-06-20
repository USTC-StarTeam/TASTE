from __future__ import annotations

from typing import Any

from sources.audit import venue_metadata_audit_from_papers


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
