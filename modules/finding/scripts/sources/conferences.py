from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests
from auto_research.paths import REFERENCE_ROOT, WORKFLOW_RUNTIME_DIR
from bs4 import BeautifulSoup

from sources.audit import _attach_venue_metadata_audit, _venue_metadata_audit, venue_metadata_audit_from_papers
from sources.common import (
    _clean_text,
    stable_id,
    _presentation_type_from_text,
    _presentation_type_from_url,
    _set_presentation_metadata,
    _title_key,
)
from sources.parsing import _looks_like_paper_title


ICLR2026_GUIDE_URLS = (
    "https://raw.githubusercontent.com/JenniferZhao0531/ICLR2026-Guide-CN/main/ICLR2026_all_papers.json",
    "https://raw.githubusercontent.com/JenniferZhao0531/ICLR2026-Guide-CN/master/ICLR2026_all_papers.json",
)


def _iclr2026_guide_cache_path() -> Any:
    return WORKFLOW_RUNTIME_DIR / "finding_cache" / "ICLR2026_all_papers.json"


def _load_iclr2026_guide_payload() -> tuple[dict[str, Any], str]:
    local_path = REFERENCE_ROOT / "ICLR2026-Guide-CN" / "ICLR2026_all_papers.json"
    if local_path.exists():
        return json.loads(local_path.read_text(encoding="utf-8")), str(local_path)

    cache_path = _iclr2026_guide_cache_path()
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8")), str(cache_path)

    if os.environ.get("DISABLE_ICLR2026_GUIDE_NETWORK", "0").lower() in {"1", "true", "yes", "on"}:
        return {}, ""

    last_error = ""
    for url in ICLR2026_GUIDE_URLS:
        try:
            response = requests.get(url, headers={"User-Agent": "TASTE-Finding/1.0"}, timeout=30)
            response.raise_for_status()
            payload = response.json()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return payload, url
        except Exception as exc:
            last_error = str(exc)[:240]
            time.sleep(0.2)
    if last_error:
        return {"_error": last_error}, ""
    return {}, ""


def fetch_openreview_iclr_2026(max_items: int) -> list[dict]:
    raw, source_url = _load_iclr2026_guide_payload()
    if not isinstance(raw, dict) or not isinstance(raw.get("papers"), list):
        return []
    papers_raw = raw.get("papers", [])
    try:
        limit = int(max_items or 0)
    except Exception:
        limit = 0
    if limit <= 0:
        limit = len(papers_raw)
    papers = []
    for item in papers_raw[:limit]:
        if not isinstance(item, dict):
            continue
        primary_area = item.get("primary_area") or item.get("primary_area_en") or ""
        category = primary_area or item.get("category") or ""
        papers.append({
            "id": stable_id("paper", item.get("id") or item.get("title", "")),
            "source": "openreview",
            "title": item.get("title", "Untitled"),
            "authors": "",
            "abstract": item.get("abstract", ""),
            "url": item.get("url", ""),
            "pdf_url": item.get("pdf_url", ""),
            "venue": "ICLR",
            "year": 2026,
            "primary_area": primary_area,
            "category": category,
            "track": item.get("tier", ""),
            "keywords": item.get("keywords") if isinstance(item.get("keywords"), list) else [],
            "classification_source": "official",
            "metadata": {
                "primary_area": item.get("primary_area", ""),
                "primary_area_en": item.get("primary_area_en", ""),
                "subcategory": item.get("category", ""),
                "tier": item.get("tier", ""),
                "source_record_id": item.get("id", ""),
            },
        })
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    total = int(meta.get("total_accepted") or meta.get("total") or len(papers_raw))
    truncated = len(papers) < total
    audit = _venue_metadata_audit(
        status="partial" if truncated else "complete",
        title_index_completeness_status="partial" if truncated else "complete",
        source_verified=bool(source_url),
        complete=not truncated,
        title_index_complete=not truncated,
        official_metadata_complete=True,
        adapter="openreview_iclr2026_guide",
        source_url=source_url,
        requested_years=[2026],
        paper_count=len(papers),
        source_total_count=total,
        missing_abstract_count=sum(1 for paper in papers if not str(paper.get("abstract") or "").strip()),
        has_abstracts=all(str(paper.get("abstract") or "").strip() for paper in papers) if papers else False,
        any_abstracts=any(str(paper.get("abstract") or "").strip() for paper in papers),
        has_official_categories=any(str(paper.get("primary_area") or paper.get("category") or "").strip() for paper in papers),
        category_status="official_or_cached_categories",
        source_scope="official_openreview_metadata",
        official_title_index_verified=True,
        official_accepted_list_verified=True,
        truncated=truncated,
        completeness_basis=(
            "ICLR 2026 accepted-paper metadata loaded from the project reference copy or the README-declared "
            "ICLR2026-Guide-CN OpenReview mirror. It includes titles, abstracts, OpenReview URLs, and official primary areas; "
            "OpenReview direct API may still be unavailable from this host."
        ),
    )
    return _attach_venue_metadata_audit(papers, audit)


def _dblp_page_url(url: str) -> str:
    cleaned = (url or "").strip()
    for prefix in ("https://dblp.org", "http://dblp.org", "http://dblp.uni-trier.de"):
        if cleaned.startswith(prefix):
            return cleaned.replace(prefix, "https://dblp.uni-trier.de", 1)
    return cleaned


def _dblp_stream_id(address: str) -> str:
    text = (address or "").strip()
    match = re.search(r"/db/(conf|journals)/([^/#?]+)", text)
    if not match:
        return ""
    return f"{match.group(1)}/{match.group(2)}"


def _dblp_authors(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("author", [])
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict):
                result.append(str(item.get("text") or item.get("#text") or ""))
            else:
                result.append(str(item))
        return ", ".join(author for author in result if author)
    if isinstance(value, str):
        return value
    return ""


def _dblp_hits_payload(response: Any) -> tuple[list[dict], dict[str, int]]:
    data = response.json().get("result", {}).get("hits", {})
    hits = data.get("hit", [])
    if isinstance(hits, dict):
        hits = [hits]
    stats: dict[str, int] = {}
    for source_key, target_key in (("@total", "total"), ("@sent", "sent"), ("@first", "first")):
        try:
            stats[target_key] = int(data.get(source_key) or 0)
        except Exception:
            stats[target_key] = 0
    return hits if isinstance(hits, list) else [], stats


def _dblp_stream_query(stream_id: str, year: int | None = None) -> str:
    query = f"stream:streams/{stream_id}:"
    if year is not None:
        query = f"{query} {year}"
    return query


def _content_value(content: dict, key: str) -> str:
    value = content.get(key, "")
    if isinstance(value, dict):
        return str(value.get("value") or "")
    return str(value or "")


def _content_list(content: dict, key: str) -> list[str]:
    value = content.get(key, [])
    if isinstance(value, dict):
        value = value.get("value", [])
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


def _content_first_value(content: dict, keys: list[str]) -> str:
    for key in keys:
        value = _content_value(content, key).strip()
        if value:
            return value
    lowered = {str(key).lower().replace(" ", "_"): key for key in content.keys()}
    for key in keys:
        actual = lowered.get(str(key).lower().replace(" ", "_"))
        if actual:
            value = _content_value(content, actual).strip()
            if value:
                return value
    return ""


def _content_first_list(content: dict, keys: list[str]) -> list[str]:
    for key in keys:
        values = [item.strip() for item in _content_list(content, key) if item.strip()]
        if values:
            return values
    lowered = {str(key).lower().replace(" ", "_"): key for key in content.keys()}
    for key in keys:
        actual = lowered.get(str(key).lower().replace(" ", "_"))
        if actual:
            values = [item.strip() for item in _content_list(content, actual) if item.strip()]
            if values:
                return values
    return []


def _icml_event_paper_title(text: object) -> str:
    title = _clean_text(str(text or ""))
    lowered = title.lower()
    if not title or lowered in {"view full details", "view details", "details"}:
        return ""
    if lowered.startswith(("select year", "getting started", "schedule", "tutorials", "main conference", "community", "exhibitors", "organizers")):
        return ""
    return title if _looks_like_paper_title(title) else ""


def _pmlr_detail_url(paper: dict) -> str:
    if paper.get("url"):
        return str(paper.get("url") or "")
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    if metadata.get("pmlr_url"):
        return str(metadata.get("pmlr_url") or "")
    source_records = metadata.get("source_records") if isinstance(metadata.get("source_records"), dict) else {}
    pmlr_record = source_records.get("pmlr") if isinstance(source_records.get("pmlr"), dict) else {}
    return str(pmlr_record.get("url") or "")


def _extract_pmlr_abstract(soup: BeautifulSoup) -> str:
    abstract_node = soup.find(id=re.compile("abstract", re.I))
    if abstract_node:
        text = _clean_text(abstract_node.get_text(" ", strip=True))
        if text.lower().startswith("abstract "):
            text = text[len("abstract "):].strip()
        if text:
            return text
    heading = soup.find(lambda tag: tag.name in {"h2", "h3", "h4", "h5"} and _clean_text(tag.get_text(" ", strip=True)).lower() == "abstract")
    if heading:
        parts: list[str] = []
        for sibling in heading.next_siblings:
            if getattr(sibling, "name", None) in {"h1", "h2", "h3", "h4", "h5", "hr"}:
                break
            text = _clean_text(sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling))
            if text:
                parts.append(text)
        text = _clean_text(" ".join(parts))
        if text:
            return text
    bibtex = soup.find(string=re.compile(r"abstract\s*=", re.I))
    if bibtex:
        match = re.search(r"abstract\s*=\s*\{(.+?)\}\s*\}", str(bibtex), flags=re.I | re.S)
        if match:
            return _clean_text(match.group(1))
    return ""


def _merge_dblp_paper_sources(venue: dict, stream_papers: list[dict], toc_papers: list[dict], max_items: int | None) -> list[dict]:
    if not stream_papers:
        return toc_papers
    if not toc_papers:
        return stream_papers
    merged: list[dict] = []
    seen: set[str] = set()
    for paper in [*toc_papers, *stream_papers]:
        if not isinstance(paper, dict):
            continue
        key = _title_key(paper.get("title")) or str((paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}).get("dblp_key") or paper.get("url") or paper.get("id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(dict(paper))
        if max_items is not None and len(merged) >= max_items:
            break
    stream_audit = venue_metadata_audit_from_papers(stream_papers)
    toc_audit = venue_metadata_audit_from_papers(toc_papers)
    truncated = bool(max_items is not None and len(merged) >= max_items and (len(stream_papers) > len(merged) or len(toc_papers) > len(merged)))
    complete = bool(toc_audit.get("complete")) and not truncated and len(merged) >= max(len(stream_papers), len(toc_papers))
    audit = _venue_metadata_audit(
        status="complete" if complete else "partial",
        title_index_completeness_status="complete" if complete else "partial",
        source_verified=True,
        complete=complete,
        title_index_complete=complete,
        dblp_stream_index_complete=bool(stream_audit.get("dblp_stream_index_complete") or stream_audit.get("complete")),
        dblp_toc_index_complete=bool(toc_audit.get("dblp_toc_index_complete") or toc_audit.get("complete")),
        official_metadata_complete=False,
        adapter="dblp_search_api+dblp_toc",
        source_url=str(toc_audit.get("source_url") or stream_audit.get("source_url") or "https://dblp.org/search/publ/api"),
        stream_audit=stream_audit,
        toc_audit=toc_audit,
        stream_paper_count=len(stream_papers),
        toc_paper_count=len(toc_papers),
        deduped_paper_count=len(merged),
        has_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="dblp_current_index_not_official_accepted_list",
        official_title_index_verified=False,
        official_accepted_list_verified=False,
        truncated=truncated,
        completeness_basis="Merged DBLP stream search with DBLP venue table-of-contents XML/HTML to avoid missing records when one DBLP index view lags another. This is still a DBLP current title index, not an official accepted-paper or ACM proceedings completeness certificate, and it exposes no abstracts or official categories.",
    )
    return _attach_venue_metadata_audit(merged, audit)


def _acl_event_urls(venue: dict, year: int) -> list[str]:
    name = (venue.get("name") or "").lower()
    stems: list[str] = []
    if "emnlp" in name:
        stems = [f"emnlp-{year}", f"findings-{year}"]
    elif "naacl" in name:
        stems = [f"naacl-{year}", f"findings-{year}"]
    else:
        stems = [f"acl-{year}", f"findings-{year}"]
    return [f"https://aclanthology.org/events/{stem}/" for stem in stems]


def _merge_enrichment(base: dict, enrichment: dict, adapter: str) -> dict:
    merged = dict(base)
    metadata = dict(base.get("metadata") or {})
    enrichment_metadata = dict(enrichment.get("metadata") or {})
    sources = metadata.setdefault("enrichment_sources", [])
    if adapter not in sources:
        sources.append(adapter)
    source_records = metadata.setdefault("source_records", {})
    source_records[adapter] = {
        "source": enrichment.get("source", adapter),
        "url": enrichment.get("url", ""),
        "pdf_url": enrichment.get("pdf_url", ""),
        "metadata": enrichment_metadata,
    }
    for key in ["abstract", "url", "pdf_url"]:
        if not merged.get(key) and enrichment.get(key):
            merged[key] = enrichment[key]
    if enrichment.get("url"):
        metadata.setdefault(f"{adapter}_url", enrichment.get("url"))
    if enrichment.get("category") and not merged.get("category"):
        merged["category"] = enrichment["category"]
    for key in ["primary_area", "track", "presentation_type", "presentation_label"]:
        if enrichment.get(key) and not merged.get(key):
            merged[key] = enrichment[key]
    for key in ["presentation_type", "presentation_label", "presentation_source"]:
        if enrichment_metadata.get(key) and not metadata.get(key):
            metadata[key] = enrichment_metadata[key]
    presentation_type = (
        enrichment.get("presentation_type")
        or enrichment_metadata.get("presentation_type")
        or _presentation_type_from_url(enrichment.get("url") or enrichment_metadata.get("detail_url") or enrichment_metadata.get("venue_url"))
        or _presentation_type_from_text(enrichment.get("track") or enrichment_metadata.get("presentation_label"))
    )
    if presentation_type:
        merged["metadata"] = metadata
        _set_presentation_metadata(merged, presentation_type, source=f"{adapter}_enrichment")
        metadata = merged.get("metadata", metadata)
    if isinstance(enrichment.get("keywords"), list):
        keywords = merged.get("keywords") if isinstance(merged.get("keywords"), list) else []
        merged["keywords"] = list(dict.fromkeys([*keywords, *[str(item) for item in enrichment["keywords"] if str(item)]]))
    if enrichment.get("classification_source") == "official":
        merged["classification_source"] = "official"
    merged["metadata"] = metadata
    return merged


def _merge_enrichments(base_papers: list[dict], enrichments: list[tuple[str, list[dict]]]) -> tuple[list[dict], list[str]]:
    merged = [dict(paper) for paper in base_papers]
    by_title_year = {
        (_title_key(paper.get("title", "")), int(paper.get("year") or 0)): index
        for index, paper in enumerate(merged)
        if _title_key(paper.get("title", ""))
    }
    used_adapters: list[str] = []
    for adapter, records in enrichments:
        matched = 0
        for record in records:
            key = (_title_key(record.get("title", "")), int(record.get("year") or 0))
            index = by_title_year.get(key)
            if index is None:
                continue
            merged[index] = _merge_enrichment(merged[index], record, adapter)
            matched += 1
        if matched:
            used_adapters.append(f"{adapter}:{matched}")
    return merged, used_adapters
