from __future__ import annotations


# ---- catalog.py ----

from catalog.venue_catalog import (
    ADDRESS_OVERRIDES,
    FIELD_LABELS,
    NAME_ALIASES,
    SOURCE_PRIORITY,
    _append_alias,
    _catalog_by_id_cached,
    _catalog_source_priority,
    _copy_catalog_row,
    _load_catalog_cached,
    _load_json_catalog,
    _merge_catalog_by_identity,
    _merge_catalog_entry,
    _merge_year_values,
    _safe_id,
    _venue_alias_record,
    _venue_identity_key,
    _venue_name_key,
    catalog_by_id,
    load_catalog,
    load_ccf_catalog,
    load_custom_catalog,
    load_default_catalog,
    load_openreview_catalog,
    load_packaged_ccf_catalog,
)


# ---- local_index.py ----

from local_store.local_index import (
    _venue_id_candidates,
    load_local_venue_year,
    venue_cache_key,
)


# ---- local_cache.py ----

from local_store.local_cache import (
    SCHEMA_VERSION,
    _category_key,
    _first_existing_directory,
    _json_path,
    build_category_summary,
    cache_directory,
    load_cached_venue_year,
    normalize_cached_paper,
    write_venue_year_cache,
)


# ---- local_rank.py ----

from find_local_rank import rank_papers_tfidf


# ---- quality.py ----

from quality.metadata import (
    CONFERENCE_QUALITY_TABLE,
    JOURNAL_QUALITY_TABLE,
    QUALITY_DATA_DIR,
    _candidate_tables,
    _conference_table,
    _journal_table,
    _label_candidates,
    _load_json,
    _lookup_conference_quality,
    _lookup_journal_quality,
    _metadata,
    _norm,
    _quality_payload,
    _venue_ids,
    _venue_names,
    attach_quality_metadata,
    attach_quality_metadata_many,
)


# ---- category_select.py ----

from selection.category_select import (
    CATEGORY_SELECT_CACHE_MAX_ENTRIES,
    CATEGORY_SELECT_CACHE_SCHEMA_VERSION,
    CATEGORY_SELECT_TEMPERATURE,
    _build_rejected,
    _cache_entries_fingerprint,
    _category_entries,
    _category_select_cache_enabled,
    _category_select_cache_key,
    _category_select_cache_path,
    _compact_entries,
    _fallback_select,
    _interest_text,
    _json_or_none,
    _llm_identity,
    _load_category_select_cache,
    _min_llm_category_recall,
    _normalize_selected_rows,
    _normalized_text,
    _project_cache_dir,
    _store_category_select_cache_entry,
    _supplement_selected_for_recall,
    _use_llm_category_select,
    _valid_cached_selection,
    filter_papers_by_selected_categories,
    select_relevant_categories,
)


# ---- profile_normalize.py ----

from research_profile.normalize import (
    PROFILE_SCHEMA,
    _append_expansion,
    _append_unique,
    _as_optional_string,
    _as_string,
    _as_string_list,
    _augment_safe_expansions,
    _canonical_condition,
    _dedupe_conditional_exclusions,
    _extract_conditional_exclusions,
    _extract_preference_hints,
    _keyword_terms,
    _normalize_conditional_exclusions,
    _normalize_expansions,
    _postprocess_profile,
    _profile_signal_count,
    _split_terms,
    build_stage0_prompt,
    fallback_profile,
    normalize_profile_shape,
    normalize_user_profile,
    profile_retrieval_text,
)


# ---- sources.py ----

import hashlib
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus, urlencode
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup

from auto_research.paths import WORKFLOW_RUNTIME_DIR, LEGACY_RUNS_DIR, ROOT, RUNS_DIR, STATE_DIR


HEADERS = {
    "User-Agent": "research-workflow/0.1"
}


def _positive_int_env(name: str, default: int = 0) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else int(default)


from sources.audit import (
    VENUE_METADATA_AUDIT_KEY,
    _attach_openreview_metadata_audit,
    _attach_venue_metadata_audit,
    _metadata_timeout,
    _venue_metadata_audit,
    venue_metadata_audit_from_papers,
)


from sources.journals import (
    NATURE_JOURNALS,
    SCIENCE_JOURNALS,
    _crossref_authors,
    _crossref_date,
    _crossref_first_text,
    _extract_nature_doi,
    _extract_science_abstract,
    _extract_science_doi,
    _looks_like_xml,
    _nature_feed_url,
    _nature_journal_meta,
    _nature_listing_url,
    _parse_nature_feed,
    _parse_nature_listing_html,
    _parse_science_crossref_items,
    _parse_science_feed,
    _science_abs_url,
    _science_crossref_url,
    _science_feed_url,
    _science_journal_meta,
    _science_pdf_url,
    _xml_attr,
    _xml_text,
)


from sources.conferences import (
    fetch_openreview_iclr_2026,
    _acl_event_urls,
    _content_first_list,
    _content_first_value,
    _content_list,
    _content_value,
    _dblp_authors,
    _dblp_hits_payload,
    _dblp_page_url,
    _dblp_stream_id,
    _dblp_stream_query,
    _extract_pmlr_abstract,
    _icml_event_paper_title,
    _merge_dblp_paper_sources,
    _merge_enrichment,
    _merge_enrichments,
    _pmlr_detail_url,
)


from sources.common import (
    OPENREVIEW_VENUE_PATTERNS,
    _ABSTRACT_UI_CONTROL_RE,
    _clean_text,
    _in_date_range,
    _matches_venue_keyword,
    _openreview_patterns_for_venue,
    _openreview_venue_ids,
    _presentation_display_label,
    _presentation_type_from_text,
    _presentation_type_from_url,
    _set_presentation_metadata,
    _strip_abstract_ui_controls,
    _title_key,
    _venue_text,
    is_acl_family_venue,
    is_cvf_venue,
    is_iclr_venue,
    is_icml_venue,
    is_neurips_venue,
    is_openreview_supported_venue,
    is_pmlr_venue,
    normalize_date,
    stable_id,
)


def _semantic_scholar_cache_path() -> Path:
    return STATE_DIR / "semantic_scholarabstract_cache.json"


def _openalex_cache_path() -> Path:
    return STATE_DIR / "openalex_abstract_cache.json"


from sources.metadata import (
    _SEMANTIC_SCHOLAR_RETRYABLE_STATUS_CODES,
    _acm_ids_from_doi,
    _acm_metadata_from_doi,
    _apply_semantic_scholar_cache,
    _author_family_tokens,
    _dblp_record_metadata,
    _doi_from_url,
    _doi_url,
    _openalex_abstract_from_inverted_index,
    _openalex_author_family_tokens,
    _openalex_cache_key,
    _openalex_candidate_matches,
    _openalex_item_from_payload,
    _openalex_landing_url,
    _openalex_pdf_url,
    _same_metadata_text,
    _semantic_scholar_cache_is_permanent_miss,
    _semantic_scholar_cache_key,
    _semantic_scholar_cache_miss_is_retryable,
    _semantic_scholar_cache_real_abstract,
    _semantic_scholar_errors_retryable,
    _title_token_similarity,
)


def _load_semantic_scholar_cache() -> dict:
    path = Path(_semantic_scholar_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_semantic_scholar_cache(cache: dict) -> None:
    path = Path(_semantic_scholar_cache_path())
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        return


def _load_openalex_cache() -> dict:
    path = Path(_openalex_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_openalex_cache(cache: dict) -> None:
    path = Path(_openalex_cache_path())
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        return


def enrich_with_openalex(papers: list[dict], limit: int = 80) -> list[dict]:
    cache = _load_openalex_cache()
    cache_changed = False
    for paper in papers[:limit]:
        if paper.get("abstract") and paper.get("pdf_url"):
            continue
        cache_key = _openalex_cache_key(paper)
        doi = _doi_from_url(str(paper.get("doi") or paper.get("url") or paper.get("pdf_url") or ""))
        query = quote_plus(str(paper.get("title") or ""))
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            if cached.get("miss") and cached.get("title_fallback_attempted"):
                continue
            if cached.get("abstract"):
                paper["abstract"] = cached.get("abstract") or ""
            if cached.get("url"):
                paper["url"] = paper.get("url") or cached.get("url") or ""
            if cached.get("pdf_url") and not paper.get("pdf_url"):
                paper["pdf_url"] = cached.get("pdf_url") or ""
            if cached.get("openalex_id") or cached.get("openalex_doi") or cached.get("pdf_url"):
                metadata = paper.setdefault("metadata", {})
                if cached.get("openalex_id"):
                    metadata["openalex_id"] = cached.get("openalex_id") or ""
                if cached.get("openalex_doi"):
                    metadata["openalex_doi"] = cached.get("openalex_doi") or ""
                if cached.get("pdf_url"):
                    metadata["openalex_pdf_url"] = cached.get("pdf_url") or ""
                if doi and cached.get("doi_status") and cached.get("doi_status") != 200 and cached.get("title_fallback_attempted"):
                    metadata["publisher_doi_openalex_status"] = cached.get("doi_status")
                    metadata["openalex_title_fallback_used"] = True
            if paper.get("abstract") and (paper.get("pdf_url") or cached.get("title_fallback_attempted")):
                continue
        if not doi and not query:
            continue
        item: dict = {}
        doi_status = 0
        try:
            if doi:
                doi_url = f"https://api.openalex.org/works/doi:{doi}"
                response = requests.get(doi_url, headers=HEADERS, timeout=_metadata_timeout(6))
                doi_status = response.status_code
                if response.status_code == 200:
                    item = _openalex_item_from_payload(response.json(), paper, from_search=False)
            if not item and query:
                search_url = f"https://api.openalex.org/works?search={query}&per-page=3"
                response = requests.get(search_url, headers=HEADERS, timeout=_metadata_timeout(6))
                if response.status_code == 200:
                    item = _openalex_item_from_payload(response.json(), paper, from_search=True)
            if not item:
                cache[cache_key] = {
                    "title": paper.get("title", ""),
                    "miss": True,
                    "doi_status": doi_status,
                    "title_fallback_attempted": bool(query),
                    "updated_at": datetime.utcnow().isoformat() + "Z",
                }
                cache_changed = True
                continue
            abstract = _openalex_abstract_from_inverted_index(item.get("abstract_inverted_index") or {})
            if abstract:
                paper["abstract"] = abstract
            pdf_url = _openalex_pdf_url(item)
            if pdf_url and not paper.get("pdf_url"):
                paper["pdf_url"] = pdf_url
            landing_url = _openalex_landing_url(item)
            paper["url"] = paper.get("url") or landing_url
            metadata = paper.setdefault("metadata", {})
            metadata["openalex_id"] = item.get("id") or ""
            metadata["openalex_doi"] = item.get("doi") or ""
            metadata["openalex_landing_url"] = landing_url
            metadata["openalex_pdf_url"] = pdf_url
            if doi and doi_status and doi_status != 200:
                metadata["publisher_doi_openalex_status"] = doi_status
                metadata["openalex_title_fallback_used"] = True
            cache[cache_key] = {
                "title": paper.get("title", ""),
                "abstract": paper.get("abstract") or "",
                "url": landing_url,
                "pdf_url": pdf_url,
                "openalex_id": item.get("id") or "",
                "openalex_doi": item.get("doi") or "",
                "doi_status": doi_status,
                "miss": not bool(paper.get("abstract") or pdf_url),
                "title_fallback_attempted": bool(query),
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
            cache_changed = True
            time.sleep(0.1)
        except Exception:
            continue
    if cache_changed:
        _save_openalex_cache(cache)
    return papers


from sources.parsing import (
    _conference_virtual_detail_label,
    _conference_virtual_detail_source,
    _conference_virtual_detail_target,
    _conference_virtual_detail_url,
    _extract_between_markers,
    _extract_conference_virtual_abstract,
    _extract_conference_virtual_authors,
    _extract_icml_virtual_abstract,
    _extract_icml_virtual_authors,
    _icml_virtual_detail_target,
    _jsonld_nodes,
    _looks_like_paper_title,
    _mark_detail_fetch_deferred,
    _name_from_jsonld_author,
    _openreview_pdf_url,
    _parse_neurips_detail,
    _parse_neurips_list,
    _positive_float_env,
)


from sources.preprints import (
    ARXIV_DEFAULT_RECENT_DAYS,
    _append_arxiv_entry,
    _arxiv_date_window,
    _arxiv_entry_authors,
    _arxiv_entry_id,
    _arxiv_fallback_queries,
    _arxiv_search_queries,
    _biorxiv_category_matches,
    _biorxiv_content_url,
    _biorxiv_default_start_date,
    _title_match_queries,
)


from sources.venue_cache import (
    _icml_verified_download_cache,
    _recent_verified_venue_yecache,
    _venue_cache_candidate_paths,
    _verified_venue_yecache_from_paths,
)



def _request(url: str, timeout: int = 12) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response


def _dblp_search_hits(stream_id: str, *, year: int | None, max_items: int | None) -> tuple[list[dict], dict[str, Any]]:
    page_size = 100
    max_pages = max(1, int(os.environ.get("DBLP_SEARCH_MAX_PAGES", "50") or 50))
    request_retries = max(1, int(os.environ.get("DBLP_SEARCH_RETRIES", "3") or 3))
    hits: list[dict] = []
    total = 0
    first_values: list[int] = []
    sent_values: list[int] = []
    errors: list[str] = []
    exhausted = False
    truncated = False
    for page_index in range(max_pages):
        offset = page_index * page_size
        page_hits: list[dict] = []
        stats: dict[str, int] = {}
        last_error = ""
        for attempt in range(request_retries):
            try:
                response = requests.get(
                    "https://dblp.org/search/publ/api",
                    params={"q": _dblp_stream_query(stream_id, year), "h": page_size, "f": offset, "format": "json"},
                    headers=HEADERS,
                    timeout=12,
                )
                response.raise_for_status()
                page_hits, stats = _dblp_hits_payload(response)
                last_error = ""
                break
            except Exception as exc:
                last_error = str(exc)[:240]
                if attempt + 1 < request_retries:
                    time.sleep(0.5 * (attempt + 1))
        if last_error:
            errors.append(last_error)
            return hits, {
                "query_year": year,
                "total": total,
                "sent": sum(sent_values),
                "pages_fetched": page_index,
                "complete": False,
                "error": last_error,
                "errors": errors,
                "truncated": bool(max_items is not None and hits),
            }
        total = max(total, int(stats.get("total") or 0))
        sent = int(stats.get("sent") or len(page_hits) or 0)
        first_values.append(int(stats.get("first") or offset))
        sent_values.append(sent)
        hits.extend(page_hits)
        if max_items is not None and len(hits) >= max_items:
            truncated = len(hits) < total
            break
        if not page_hits or sent <= 0 or len(hits) >= total:
            exhausted = True
            break
        time.sleep(0.15)
    else:
        truncated = bool(total and len(hits) < total)
    return hits, {
        "query_year": year,
        "total": total,
        "sent": sum(sent_values),
        "pages_fetched": len(sent_values),
        "page_size": page_size,
        "first_values": first_values,
        "complete": bool(total and len(hits) >= total and not truncated) or exhausted,
        "truncated": truncated,
        "errors": errors,
    }


def fetch_dblp_stream_api(venue: dict, years: list[int], max_items: int | None) -> list[dict]:
    stream_id = _dblp_stream_id(venue.get("address", ""))
    if not stream_id:
        return []
    wanted_years = [int(year) for year in years if str(year).isdigit()]
    query_years = wanted_years or [None]
    papers: list[dict] = []
    seen: set[str] = set()
    search_audits: list[dict[str, Any]] = []
    for query_year in query_years:
        hits, search_audit = _dblp_search_hits(stream_id, year=query_year, max_items=None if max_items is None else max(max_items * 2, 100))
        search_audits.append(search_audit)
        for hit in hits:
            info = hit.get("info", {}) if isinstance(hit, dict) else {}
            year = str(info.get("year") or "")
            if wanted_years and year not in {str(value) for value in wanted_years}:
                continue
            title = _clean_text(html.unescape(str(info.get("title") or ""))).rstrip(".")
            if not _looks_like_paper_title(title):
                continue
            key = _title_key(title) or str(info.get("key") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            paper_url = str(info.get("ee") or info.get("url") or "")
            doi = str(info.get("doi") or _doi_from_url(paper_url)).strip()
            dblp_record_url = str(info.get("url") or "")
            metadata = _dblp_record_metadata(
                venue.get("id"),
                stream_id=stream_id,
                dblp_record_url=dblp_record_url,
                dblp_key=str(info.get("key") or ""),
                ee=paper_url,
                doi=doi,
            )
            papers.append({
                "id": stable_id("paper", f"{venue.get('id')}:{year}:{title}"),
                "source": "dblp",
                "title": title,
                "authors": _dblp_authors(info.get("authors")),
                "abstract": "",
                "url": paper_url,
                "pdf_url": "",
                "doi": doi,
                "venue": venue.get("name", ""),
                "year": int(year) if year.isdigit() else 0,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": metadata,
            })
            if max_items is not None and len(papers) >= max_items:
                break
        if max_items is not None and len(papers) >= max_items:
            break
    if not papers and max_items is not None and not any(item.get("error") for item in search_audits):
        # DBLP occasionally returns a bounded page that is later filtered to zero
        # usable papers. Retry without a max-items stop before slower fallbacks.
        return fetch_dblp_stream_api(venue, years, None)
    total_hits = sum(int(item.get("total") or 0) for item in search_audits)
    complete = bool(search_audits) and all(bool(item.get("complete")) and not bool(item.get("truncated")) and not item.get("error") for item in search_audits)
    audit = _venue_metadata_audit(
        status="complete" if complete else "partial",
        title_index_completeness_status="complete" if complete else "partial",
        source_verified=bool(search_audits and not any(item.get("error") for item in search_audits)),
        complete=complete,
        title_index_complete=complete,
        dblp_stream_index_complete=complete,
        official_metadata_complete=False,
        adapter="dblp_search_api",
        source_url="https://dblp.org/search/publ/api",
        stream_id=stream_id,
        requested_years=wanted_years,
        query_audits=search_audits,
        search_total_hits=total_hits,
        deduped_paper_count=len(papers),
        has_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="dblp_current_index_not_official_accepted_list",
        official_title_index_verified=False,
        official_accepted_list_verified=False,
        completeness_basis="DBLP paginated stream search over the current DBLP index. This verifies the DBLP title index only; it is not an official venue accepted-paper or ACM proceedings completeness certificate, and it exposes no abstracts or official categories.",
    )
    return _attach_venue_metadata_audit(papers, audit)



def _openreview_notes_paginated(url: str, base_params: dict[str, object], max_items: int) -> list[dict]:
    try:
        requested = int(max_items or 0)
    except (TypeError, ValueError):
        requested = 0
    requested = max(1, requested)
    try:
        page_size = int(os.environ.get("OPENREVIEW_PAGE_SIZE", "1000") or 1000)
    except (TypeError, ValueError):
        page_size = 1000
    page_size = max(1, min(1000, page_size, requested))
    try:
        max_pages = int(os.environ.get("OPENREVIEW_MAX_PAGES", "200") or 200)
    except (TypeError, ValueError):
        max_pages = 200
    notes: list[dict] = []
    offset = 0
    for _page in range(max(1, max_pages)):
        params = dict(base_params)
        params["limit"] = min(page_size, requested - len(notes))
        params["offset"] = offset
        response = requests.get(url, params=params, headers=HEADERS, timeout=12)
        response.raise_for_status()
        batch = response.json().get("notes", [])
        if not isinstance(batch, list) or not batch:
            break
        notes.extend(note for note in batch if isinstance(note, dict))
        if len(notes) >= requested or len(batch) < int(params["limit"]):
            break
        offset += len(batch)
    return notes[:requested]


def fetch_openreview_venue(venue: dict, years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    queried_venue_ids: set[str] = set()
    for year in years:
        venue_ids = _openreview_venue_ids(venue, year)
        for venue_id in venue_ids:
            if venue_id in queried_venue_ids:
                continue
            queried_venue_ids.add(venue_id)
            notes = []
            try:
                notes = _openreview_notes_paginated(
                    "https://api2.openreview.net/notes",
                    {"content.venueid": venue_id, "details": "replyCount,invitation,original"},
                    max_items,
                )
            except Exception:
                notes = []
            if not notes:
                for invitation in [f"{venue_id}/-/Blind_Submission", f"{venue_id}/-/Submission"]:
                    try:
                        notes = _openreview_notes_paginated(
                            "https://api.openreview.net/notes",
                            {"invitation": invitation},
                            max_items,
                        )
                    except Exception:
                        notes = []
                    if notes:
                        break
            for note in notes:
                content = note.get("content", {}) or {}
                title = _clean_text(_content_value(content, "title"))
                if not _looks_like_paper_title(title):
                    continue
                note_id = note.get("id", "")
                forum = note.get("forum", note_id)
                url = f"https://openreview.net/forum?id={forum or note_id}"
                primary_area = _content_first_value(content, ["primary_area", "Primary Area", "area", "Area", "subject_area", "Subject Area"])
                category = primary_area or _content_first_value(content, ["category", "Category", "subject", "Subject"])
                track = _content_first_value(content, ["track", "Track", "venue", "Venue"])
                keywords = _content_first_list(content, ["keywords", "Keywords", "keyword", "Keyword"])
                classification_source = "official" if category or track or keywords else "llm_inferred"
                paper = {
                    "id": stable_id("paper", url),
                    "source": "openreview",
                    "title": title,
                    "authors": ", ".join(_content_list(content, "authors")),
                    "abstract": _clean_text(_content_value(content, "abstract")),
                    "url": url,
                    "pdf_url": f"https://openreview.net/pdf?id={note_id}" if note_id else "",
                    "venue": venue.get("name", ""),
                    "year": year,
                    "primary_area": primary_area,
                    "category": category,
                    "track": track,
                    "keywords": keywords,
                    "classification_source": classification_source,
                    "metadata": {
                        "venue_id": venue.get("id"),
                        "openreview_venueid": venue_id,
                        "primary_area": primary_area,
                        "category": category,
                        "track": track,
                        "keywords": keywords,
                    },
                }
                presentation = _presentation_type_from_text(track or category or primary_area)
                if presentation:
                    _set_presentation_metadata(paper, presentation, source="openreview_venue_text")
                papers.append(paper)
                if len(papers) >= max_items:
                    return _attach_openreview_metadata_audit(papers, list(queried_venue_ids), years)
    return _attach_openreview_metadata_audit(papers, list(queried_venue_ids), years) if papers else papers

def fetch_cvf_openaccess(venue: dict, years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    name = (venue.get("name") or "").upper()
    for year in years:
        url = f"https://openaccess.thecvf.com/{name}{year}?day=all"
        try:
            soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception:
            continue
        for title_node in soup.select("dt.ptitle a[href], dt a[href]"):
            title = _clean_text(title_node.get_text(" ", strip=True))
            if not _looks_like_paper_title(title):
                continue
            paper_url = requests.compat.urljoin(url, title_node["href"])
            pdf_url = paper_url.replace(".html", ".pdf")
            papers.append({
                "id": stable_id("paper", paper_url),
                "source": "cvf_openaccess",
                "title": title,
                "authors": "",
                "abstract": "",
                "url": paper_url,
                "pdf_url": pdf_url,
                "venue": venue.get("name", ""),
                "year": year,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": {"venue_id": venue.get("id"), "cvf_url": url},
            })
            if len(papers) >= max_items:
                return papers
        time.sleep(0.2)
    return papers


def fetch_eccv_virtual(years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    for year in years:
        if year % 2 == 1:
            continue
        list_url = f"https://eccv.ecva.net/virtual/{year}/papers.html"
        try:
            soup = BeautifulSoup(_request(list_url).text, "html.parser")
        except Exception:
            continue
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            title = _clean_text(anchor.get_text(" ", strip=True))
            href = anchor["href"]
            if not _looks_like_paper_title(title):
                continue
            if "/poster/" not in href and "/paper/" not in href:
                continue
            url = requests.compat.urljoin(list_url, href)
            if url in seen:
                continue
            seen.add(url)
            paper = {
                "id": stable_id("paper", url),
                "source": "eccv_virtual",
                "title": title,
                "authors": "",
                "abstract": "",
                "url": url,
                "pdf_url": "",
                "venue": "ECCV",
                "year": year,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": {"virtual_url": list_url, "detail_url": url, "title_index_only": True},
            }
            _set_presentation_metadata(paper, _presentation_type_from_url(url), source="eccv_virtual_url")
            papers.append(paper)
            if len(papers) >= max_items:
                return papers
    return papers


def _fetch_icml_presentation_overrides(year: int) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    sources = [
        ("oral", f"https://icml.cc/virtual/{year}/events/oral"),
        ("spotlight", f"https://icml.cc/virtual/{year}/events/{year}SpotlightPosters"),
    ]
    overrides: dict[str, dict[str, str]] = {}
    audit: dict[str, Any] = {"attempted": True, "sources": [], "counts": {}}
    for presentation_type, url in sources:
        source_report: dict[str, Any] = {"presentation_type": presentation_type, "source_url": url, "matched_titles": 0}
        try:
            soup = BeautifulSoup(_request(url, timeout=30).text, "html.parser")
        except Exception as exc:
            source_report["error"] = str(exc)[:240]
            audit["sources"].append(source_report)
            continue
        seen_for_source: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            title = _icml_event_paper_title(anchor.get_text(" ", strip=True))
            if not title:
                continue
            href = requests.compat.urljoin(url, str(anchor.get("href") or ""))
            if f"/virtual/{year}/" not in href:
                continue
            if not any(part in href for part in ["/poster/", "/oral/", "/paper/", "/spotlight/"]):
                continue
            key = _title_key(title)
            if not key or key in seen_for_source:
                continue
            seen_for_source.add(key)
            existing = overrides.get(key)
            if isinstance(existing, dict) and str(existing.get("presentation_type") or "").lower() == "oral":
                continue
            if isinstance(existing, dict) and presentation_type != "oral":
                continue
            overrides[key] = {
                "presentation_type": presentation_type,
                "presentation_url": href,
                "presentation_source_url": url,
            }
        source_report["matched_titles"] = len(seen_for_source)
        audit["counts"][presentation_type] = len(seen_for_source)
        audit["sources"].append(source_report)
    applied_counts: dict[str, int] = {}
    for row in overrides.values():
        label = str(row.get("presentation_type") or "").strip().lower()
        if label:
            applied_counts[label] = applied_counts.get(label, 0) + 1
    audit["matched_title_count"] = len(overrides)
    audit["applied_counts"] = applied_counts
    return overrides, audit


def fetch_icml_downloads(years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    seen: set[str] = set()
    blocked = [
        "workshop", "tutorial", "exhibitor", "sponsor", "reviewer", "area chair",
        "call for", "organizing committee", "program committee", "policy", "registration",
        "accepted workshops", "accepted tutorials", "social", "mentoring",
    ]
    yeaudits: list[dict[str, Any]] = []
    truncated = False
    for year in years:
        url = f"https://icml.cc/Downloads/{year}"
        presentation_overrides, presentation_audit = _fetch_icml_presentation_overrides(int(year))
        raw_virtual_links = 0
        raw_paper_links = 0
        year_presentation_counts: dict[str, int] = {}
        try:
            soup = BeautifulSoup(_request(url, timeout=30).text, "html.parser")
        except Exception as exc:
            yeaudits.append({"year": year, "source_url": url, "error": str(exc)[:240], "complete": False, "presentation_audit": presentation_audit})
            continue
        for anchor in soup.find_all("a", href=True):
            title = _clean_text(anchor.get_text(" ", strip=True))
            lowered = title.lower()
            href = anchor["href"]
            paper_url = requests.compat.urljoin(url, href)
            if f"/virtual/{year}/" in paper_url:
                raw_virtual_links += 1
            if not _looks_like_paper_title(title):
                continue
            if any(term in lowered for term in blocked):
                continue
            # ICML Downloads includes navigation before the event list; papers/events use /virtual/<year>/... links.
            if f"/virtual/{year}/" not in paper_url:
                continue
            if not any(part in paper_url for part in ["/poster/", "/oral/", "/paper/", "/spotlight/"]):
                continue
            raw_paper_links += 1
            key = _title_key(title)
            if key in seen:
                continue
            seen.add(key)
            presentation_override = presentation_overrides.get(key) if isinstance(presentation_overrides, dict) else None
            metadata = {"venue_id": "dblp_icml", "icml_downloads_url": url, "detail_url": paper_url, "title_index_only": True}
            if isinstance(presentation_override, dict):
                metadata["presentation_url"] = presentation_override.get("presentation_url") or ""
                metadata["presentation_source_url"] = presentation_override.get("presentation_source_url") or ""
            paper = {
                "id": stable_id("paper", f"icml:{year}:{title}"),
                "source": "icml_downloads",
                "title": title,
                "authors": "",
                "abstract": "",
                "url": paper_url,
                "pdf_url": "",
                "venue": "ICML",
                "year": year,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": metadata,
            }
            if isinstance(presentation_override, dict) and presentation_override.get("presentation_type"):
                _set_presentation_metadata(paper, str(presentation_override.get("presentation_type") or ""), source="icml_events_presentation")
            else:
                _set_presentation_metadata(paper, _presentation_type_from_url(paper_url), source="icml_downloads_url")
            applied_presentation = str(paper.get("presentation_type") or "").strip().lower()
            if applied_presentation:
                year_presentation_counts[applied_presentation] = year_presentation_counts.get(applied_presentation, 0) + 1
            papers.append(paper)
            if len(papers) >= max_items:
                truncated = True
                break
        yeaudits.append({
            "year": year,
            "source_url": url,
            "raw_virtual_link_count": raw_virtual_links,
            "raw_paper_link_count": raw_paper_links,
            "deduped_paper_count_so_far": len(papers),
            "complete": not truncated and raw_paper_links > 0,
            "has_official_categories": False,
            "category_status": "no_official_categories",
            "presentation_audit": presentation_audit,
            "presentation_applied_counts": year_presentation_counts,
        })
        if truncated:
            break
        time.sleep(0.2)
    complete = bool(yeaudits) and all(bool(item.get("complete")) and not item.get("error") for item in yeaudits) and not truncated
    audit = _venue_metadata_audit(
        status="complete" if complete else "partial",
        source_verified=bool(yeaudits and not any(item.get("error") for item in yeaudits)),
        complete=complete,
        adapter="icml_downloads",
        source_url=";".join(str(item.get("source_url") or "") for item in yeaudits if item.get("source_url")),
        requested_years=[int(year) for year in years if str(year).isdigit()],
        source_yeaudits=yeaudits,
        deduped_paper_count=len(papers),
        has_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="official_icml_downloads_title_index",
        official_title_index_verified=complete,
        official_accepted_list_verified=complete,
        truncated=truncated,
        completeness_basis="Official ICML Downloads/virtual page is reachable and all qualifying paper links were scanned; no official category metadata is exposed by this adapter.",
        presentation_audits=[item.get("presentation_audit") for item in yeaudits if item.get("presentation_audit")],
        presentation_counts={
            label: sum(int((item.get("presentation_applied_counts") or {}).get(label) or 0) for item in yeaudits)
            for label in sorted({key for item in yeaudits for key in (item.get("presentation_applied_counts") or {}).keys()})
        },
    )
    return _attach_venue_metadata_audit(papers, audit)


def fetch_pmlr_index(venue: dict, years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    venue_name = (venue.get("name") or "").lower()
    known_volumes = {
        "icml": {2023: "v202", 2024: "v235", 2025: "v267"},
    }
    event_links: list[tuple[int, str]] = []
    for year in years:
        volume = known_volumes.get(venue_name, {}).get(year)
        if volume:
            event_links.append((year, f"https://proceedings.mlr.press/{volume}/"))
    try:
        soup = BeautifulSoup(_request("https://proceedings.mlr.press/").text, "html.parser")
    except Exception:
        soup = None
    if soup:
        for anchor in soup.find_all("a", href=True):
            text = _clean_text(anchor.get_text(" ", strip=True)).lower()
            if venue_name not in text:
                continue
            yematch = re.search(r"\b(20\d{2})\b", text)
            if not yematch:
                continue
            year = int(yematch.group(1))
            if year not in years:
                continue
            url = requests.compat.urljoin("https://proceedings.mlr.press/", anchor["href"])
            if (year, url) not in event_links:
                event_links.append((year, url))
    for year, url in event_links:
        try:
            event_soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception:
            continue
        for title_node in event_soup.select("p.title, div.paper p.title"):
            title = _clean_text(title_node.get_text(" ", strip=True))
            if not _looks_like_paper_title(title):
                continue
            parent = title_node.find_parent()
            link = parent.find("a", href=True) if parent else None
            paper_url = requests.compat.urljoin(url, link["href"]) if link else url
            papers.append({
                "id": stable_id("paper", f"{url}:{title}"),
                "source": "pmlr",
                "title": title,
                "authors": "",
                "abstract": "",
                "url": paper_url,
                "pdf_url": paper_url.replace(".html", ".pdf") if paper_url.endswith(".html") else "",
                "venue": venue.get("name", ""),
                "year": year,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": {"venue_id": venue.get("id"), "pmlr_url": url},
            })
            if len(papers) >= max_items:
                return papers
        time.sleep(0.2)
    return papers


def enrich_pmlr_details(papers: list[dict], limit: int | None = None) -> tuple[list[dict], dict]:
    attempted = 0
    abstracts_filled = 0
    urls_filled = 0
    pdfs_filled = 0
    candidates = papers if limit is None else papers[:limit]
    for paper in candidates:
        url = _pmlr_detail_url(paper)
        if not url or "proceedings.mlr.press" not in url:
            continue
        attempted += 1
        if not paper.get("url"):
            paper["url"] = url
            urls_filled += 1
        try:
            soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception:
            continue
        if not paper.get("abstract"):
            abstract = _extract_pmlr_abstract(soup)
            if abstract:
                paper["abstract"] = abstract
                paper.setdefault("metadata", {})["abstract_source"] = "pmlr"
                abstracts_filled += 1
        if not paper.get("pdf_url"):
            pdf_link = soup.find("a", string=re.compile("download pdf", re.I))
            if not pdf_link:
                pdf_link = soup.find("a", href=re.compile(r"\.pdf(?:$|\?)", re.I))
            if pdf_link and pdf_link.get("href"):
                paper["pdf_url"] = requests.compat.urljoin(url, pdf_link["href"])
                pdfs_filled += 1
        if paper.get("abstract") or paper.get("pdf_url"):
            paper.setdefault("metadata", {})["detail_source"] = "pmlr"
        time.sleep(0.1)
    return papers, {
        "attempted": attempted,
        "abstracts_filled": abstracts_filled,
        "urls_filled": urls_filled,
        "pdfs_filled": pdfs_filled,
    }


def fetch_neurips_title_index(year: int, max_items: int, raise_errors: bool = False) -> list[dict]:
    list_url = f"https://neurips.cc/virtual/{year}/papers.html"
    try:
        candidates = _parse_neurips_list(_request(list_url).text, list_url, max_items)
    except Exception:
        if raise_errors:
            raise
        return []

    papers: list[dict] = []
    for detail_url, title in candidates:
        paper = {
            "id": stable_id("paper", detail_url),
            "source": "neurips_virtual",
            "title": title,
            "authors": "",
            "abstract": "",
            "url": detail_url,
            "pdf_url": "",
            "venue": "NeurIPS",
            "year": year,
            "category": "",
            "classification_source": "llm_inferred",
            "metadata": {"venue_url": detail_url, "detail_url": detail_url, "title_index_only": True},
        }
        _set_presentation_metadata(paper, _presentation_type_from_url(detail_url), source="neurips_virtual_url")
        papers.append(paper)
    return papers


def fetch_neurips_details(
    candidates: list[dict],
    year: int,
    *,
    wall_timeout_sec: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[dict]:
    papers: list[dict] = []
    started = time.monotonic()
    wall_timeout = wall_timeout_sec if wall_timeout_sec is not None else _positive_float_env(
        "NEURIPS_DETAIL_WALL_TIMEOUT_SEC",
        _positive_float_env("VENUE_DETAIL_WALL_TIMEOUT_SEC", 90.0),
    )
    request_timeout = int(_positive_float_env("NEURIPS_DETAIL_REQUEST_TIMEOUT_SEC", 12))
    cancel_check = should_cancel or (lambda: False)
    for candidate in candidates:
        detail_url = candidate.get("metadata", {}).get("detail_url") or candidate.get("url", "")
        title = candidate.get("title", "")
        if cancel_check():
            candidate.setdefault("metadata", {})["detail_fetch_deferred"] = True
            candidate["detail_fetch_deferred"] = True
            candidate["detail_fetch_deferred_reason"] = "cancel_requested"
            papers.append(candidate)
            continue
        if wall_timeout > 0 and time.monotonic() - started >= wall_timeout:
            candidate.setdefault("metadata", {})["detail_fetch_deferred"] = True
            candidate["detail_fetch_deferred"] = True
            candidate["detail_fetch_deferred_reason"] = f"wall_timeout_{wall_timeout:.0f}s"
            papers.append(candidate)
            continue
        try:
            detail_html = _request(detail_url, timeout=request_timeout).text
            papers.append(_parse_neurips_detail(detail_html, detail_url, title, year))
            time.sleep(0.2)
        except Exception as exc:
            fallback = {
                "id": stable_id("paper", detail_url),
                "source": "neurips_virtual",
                "title": title,
                "authors": "",
                "abstract": "",
                "url": detail_url,
                "pdf_url": "",
                "venue": "NeurIPS",
                "year": year,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": {"venue_url": detail_url, "detail_parse_error": True, "detail_fetch_error": str(exc)[:240]},
                "detail_fetch_deferred": True,
                "detail_fetch_deferred_reason": "detail_request_failed",
            }
            _set_presentation_metadata(fallback, _presentation_type_from_url(detail_url), source="neurips_virtual_url")
            papers.append(fallback)
    return papers


def fetch_neurips_virtual(year: int, max_items: int, raise_errors: bool = False) -> list[dict]:
    return fetch_neurips_details(fetch_neurips_title_index(year, max_items, raise_errors), year)


def _parse_dblp_yelinks(address: str, years: list[int], max_years: int = 4) -> list[tuple[int, str]]:
    if not address:
        return []
    def direct_links() -> list[tuple[int, str]]:
        cleaned = _dblp_page_url(address.rstrip("/"))
        key = cleaned.split("/")[-1]
        if key == "index.html" and "/" in cleaned:
            key = cleaned.split("/")[-2]
            cleaned = "/".join(cleaned.split("/")[:-1])
        return [(year, f"{cleaned}/{key}{year}.html") for year in years[:max_years]]

    try:
        soup = BeautifulSoup(_request(_dblp_page_url(address)).text, "html.parser")
    except Exception:
        return direct_links()

    wanted = {str(year) for year in years}
    links: list[tuple[int, str]] = []
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        href = anchor["href"]
        if href.startswith("http") and "dblp" not in href:
            continue
        if not href.startswith("http"):
            href = requests.compat.urljoin(address, href)
        href = _dblp_page_url(href)
        if "#" in href:
            continue
        if "/rec/" in href:
            continue
        if "/db/conf/" not in href and "/db/journals/" not in href:
            continue
        if _dblp_stream_id(address) and _dblp_stream_id(href) != _dblp_stream_id(address):
            continue
        if not re.search(r"/(?:conf|journals)/[^/]+/[^/?#]*(?:20\d{2}|19\d{2})[^/?#]*\.html?$", href):
            continue
        matched_years = [year for year in re.findall(r"(20\d{2}|19\d{2})", f"{text} {href}") if year in wanted]
        if not matched_years:
            continue
        year = int(matched_years[0])
        if (year, href) not in links:
            links.append((year, href))
        if len(links) >= max_years:
            break
    if not links:
        links = direct_links()
    return links


def _dblp_toc_papers(venue: dict, years: list[int], max_items: int | None) -> list[dict]:
    papers: list[dict] = []
    links = _parse_dblp_yelinks(venue.get("address", ""), years, max_years=max(4, len(years)))
    page_audits: list[dict[str, Any]] = []

    def reached_limit() -> bool:
        return max_items is not None and len(papers) >= max_items

    for year, url in links:
        if reached_limit():
            break
        url = _dblp_page_url(url)
        page_audit: dict[str, Any] = {"year": year, "url": url, "xml_url": re.sub(r"\.html?$", ".xml", url), "xml_paper_count": 0, "html_paper_count": 0, "status": "started"}
        count_before_page = len(papers)
        xml_url = str(page_audit["xml_url"])
        try:
            xml_text = _request(xml_url).text
            for record in re.findall(r"<(?:article|inproceedings)[^>]*>.*?</(?:article|inproceedings)>", xml_text, flags=re.S):
                title_match = re.search(r"<title>(.*?)</title>", record, flags=re.S)
                if not title_match:
                    continue
                title = _clean_text(html.unescape(re.sub(r"<.*?>", "", title_match.group(1)))).rstrip(".")
                if not _looks_like_paper_title(title):
                    continue
                ee_match = re.search(r"<ee>(.*?)</ee>", record, flags=re.S)
                paper_url = html.unescape(ee_match.group(1).strip()) if ee_match else ""
                key_match = re.match(r'<(?:article|inproceedings)[^>]*\bkey="([^\"]+)"', record)
                dblp_key = html.unescape(key_match.group(1).strip()) if key_match else ""
                authors = ", ".join(_clean_text(html.unescape(author)) for author in re.findall(r"<author[^>]*>(.*?)</author>", record, flags=re.S))
                doi = _doi_from_url(paper_url)
                metadata = _dblp_record_metadata(
                    venue.get("id"),
                    stream_id=_dblp_stream_id(venue.get("address", "")),
                    dblp_url=url,
                    dblp_xml_url=xml_url,
                    dblp_record_url=f"https://dblp.org/rec/{dblp_key}" if dblp_key else "",
                    dblp_key=dblp_key,
                    ee=paper_url,
                    doi=doi,
                )
                papers.append({
                    "id": stable_id("paper", f"{venue.get('id')}:{year}:{title}"),
                    "source": "dblp",
                    "title": title,
                    "authors": authors,
                    "abstract": "",
                    "url": paper_url,
                    "pdf_url": "",
                    "doi": doi,
                    "venue": venue.get("name", ""),
                    "year": year,
                    "category": "",
                    "classification_source": "llm_inferred",
                    "metadata": metadata,
                })
                page_audit["xml_paper_count"] = int(page_audit.get("xml_paper_count") or 0) + 1
                if reached_limit():
                    break
            if len(papers) > count_before_page:
                page_audit["status"] = "xml"
                page_audits.append(page_audit)
                continue
        except Exception as exc:
            page_audit["xml_error"] = str(exc)[:240]
        try:
            soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception as exc:
            page_audit["html_error"] = str(exc)[:240]
            page_audit["status"] = "failed"
            page_audits.append(page_audit)
            continue
        entries = soup.select("li.entry.inproceedings, li.entry.article")
        for entry in entries:
            title_node = entry.select_one("span.title")
            if not title_node:
                continue
            title = title_node.get_text(" ", strip=True).rstrip(".")
            if not _looks_like_paper_title(title):
                continue
            authors = ", ".join(node.get_text(" ", strip=True) for node in entry.select("span[itemprop='name']")[:-1])
            paper_url = ""
            drop = entry.select_one("li.drop-down a[href]")
            if drop:
                paper_url = drop.get("href", "")
            doi = _doi_from_url(paper_url)
            metadata = _dblp_record_metadata(
                venue.get("id"),
                stream_id=_dblp_stream_id(venue.get("address", "")),
                dblp_url=url,
                ee=paper_url,
                doi=doi,
            )
            papers.append({
                "id": stable_id("paper", f"{venue.get('id')}:{year}:{title}"),
                "source": "dblp",
                "title": title,
                "authors": authors,
                "abstract": "",
                "url": paper_url,
                "pdf_url": "",
                "doi": doi,
                "venue": venue.get("name", ""),
                "year": year,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": metadata,
            })
            page_audit["html_paper_count"] = int(page_audit.get("html_paper_count") or 0) + 1
            if reached_limit():
                break
        page_audit["status"] = "html" if len(papers) > count_before_page else "empty"
        page_audits.append(page_audit)
        time.sleep(0.5)
    complete = bool(papers) and bool(page_audits) and not any(str(item.get("status")) == "failed" for item in page_audits) and not (max_items is not None and len(papers) >= max_items)
    audit = _venue_metadata_audit(
        status="complete" if complete else "partial",
        title_index_completeness_status="complete" if complete else "partial",
        source_verified=bool(papers),
        complete=complete,
        title_index_complete=complete,
        dblp_toc_index_complete=complete,
        official_metadata_complete=False,
        adapter="dblp_toc",
        source_url=links[0][1] if links else _dblp_page_url(venue.get("address", "")),
        source_urls=[url for _year, url in links],
        requested_years=[int(year) for year in years if str(year).isdigit()],
        toc_page_audits=page_audits,
        toc_paper_count=len(papers),
        deduped_paper_count=len(papers),
        has_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="dblp_current_index_not_official_accepted_list",
        official_title_index_verified=False,
        official_accepted_list_verified=False,
        completeness_basis="DBLP venue table-of-contents XML/HTML title index. This is a DBLP index, not an official accepted-paper or ACM proceedings completeness certificate, and it exposes no abstracts or official categories.",
    )
    return _attach_venue_metadata_audit(papers, audit)


def fetch_dblp_venue(venue: dict, years: list[int], max_items: int | None) -> list[dict]:
    stream_papers = fetch_dblp_stream_api(venue, years, max_items)
    toc_papers = _dblp_toc_papers(venue, years, max_items)
    papers = _merge_dblp_paper_sources(venue, stream_papers, toc_papers, max_items)
    if papers:
        return papers

    cached = _recent_verified_venue_yecache(venue, years, max_items)
    if cached:
        return cached
    return []


def fetch_acl_anthology(venue: dict, years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    seen: set[str] = set()
    for year in years:
        for url in _acl_event_urls(venue, year):
            try:
                soup = BeautifulSoup(_request(url).text, "html.parser")
            except Exception:
                continue
            for anchor in soup.find_all("a", href=True):
                title = _clean_text(anchor.get_text(" ", strip=True))
                href = anchor["href"]
                if not _looks_like_paper_title(title):
                    continue
                if not re.search(rf"/{year}\.[a-z0-9-]+\.\d+/?$", href):
                    continue
                paper_url = requests.compat.urljoin(url, href)
                if paper_url in seen:
                    continue
                seen.add(paper_url)
                papers.append({
                    "id": stable_id("paper", paper_url),
                    "source": "acl_anthology",
                    "title": title,
                    "authors": "",
                    "abstract": "",
                    "url": paper_url,
                    "pdf_url": paper_url.rstrip("/") + ".pdf",
                    "venue": venue.get("name", "ACL Anthology"),
                    "year": year,
                    "category": "",
                    "classification_source": "llm_inferred",
                    "metadata": {"venue_id": venue.get("id"), "anthology_url": url},
                })
                if len(papers) >= max_items:
                    return papers
            time.sleep(0.2)
    return papers



from sources.source_choice import (
    _choose_best_venue_source,
    _paper_has_official_category,
    _source_has_confident_official_categories,
    _source_is_complete_official_title_index,
    _venue_source_audit,
    _venue_source_category_priority_eligible,
    _venue_source_has_official_categories,
    _venue_source_official_category_count,
    _venue_source_score,
)


def fetch_venue_title_index(venue: dict, years: list[int], max_items: int) -> tuple[list[dict], str]:
    candidates: list[tuple[str, list[dict]]] = []

    if is_openreview_supported_venue(venue):
        papers = fetch_openreview_venue(venue, years, max_items)
        if papers:
            candidates.append(("openreview", papers))
            if _source_has_confident_official_categories(papers, "openreview", max_items) or (max_items and len(papers) >= max_items):
                return papers, "openreview"
        if is_iclr_venue(venue) and 2026 in years:
            papers = fetch_openreview_iclr_2026(max_items)
            if papers:
                candidates.append(("openreview_reference", papers))
                if _source_has_confident_official_categories(papers, "openreview_reference", max_items) or (max_items and len(papers) >= max_items):
                    return papers, "openreview_reference"

    if is_neurips_venue(venue):
        papers: list[dict] = []
        for year in years:
            papers.extend(fetch_neurips_title_index(year, max_items))
            if len(papers) >= max_items:
                break
        if papers:
            candidates.append(("neurips_virtual", papers[:max_items]))

    if is_icml_venue(venue):
        papers = fetch_icml_downloads(years, max_items)
        if papers:
            candidates.append(("icml_downloads", papers))
            if _source_is_complete_official_title_index(papers, "icml_downloads"):
                return _choose_best_venue_source(candidates, max_items)
        cached = _icml_verified_download_cache(venue, years, max_items)
        if cached:
            candidates.append(("icml_downloads_cache", cached))

    if is_acl_family_venue(venue):
        papers = fetch_acl_anthology(venue, years, max_items)
        if papers:
            candidates.append(("acl_anthology", papers))

    if is_cvf_venue(venue):
        papers = fetch_cvf_openaccess(venue, years, max_items)
        if papers:
            candidates.append(("cvf_openaccess", papers))
        if (venue.get("name") or "").upper() == "ECCV":
            papers = fetch_eccv_virtual(years, max_items)
            if papers:
                candidates.append(("eccv_virtual", papers))

    if is_pmlr_venue(venue):
        papers = fetch_pmlr_index(venue, years, max_items)
        if papers:
            candidates.append(("pmlr", papers))

    if venue.get("address"):
        papers = fetch_dblp_venue(venue, years, max_items)
        if papers:
            candidates.append(("dblp", papers))

    return _choose_best_venue_source(candidates, max_items)

def _fetch_enrichment_sources(venue: dict, years: list[int]) -> list[tuple[str, list[dict]]]:
    enrichments: list[tuple[str, list[dict]]] = []
    if is_openreview_supported_venue(venue):
        papers = fetch_openreview_venue(venue, years, 100000)
        if papers:
            enrichments.append(("openreview", papers))
    if is_neurips_venue(venue):
        papers: list[dict] = []
        for year in years:
            papers.extend(fetch_neurips_title_index(year, 100000))
        if papers:
            enrichments.append(("neurips_virtual", papers))
    if is_acl_family_venue(venue):
        papers = fetch_acl_anthology(venue, years, 100000)
        if papers:
            enrichments.append(("acl_anthology", papers))
    if is_cvf_venue(venue):
        papers = fetch_cvf_openaccess(venue, years, 100000)
        if papers:
            enrichments.append(("cvf_openaccess", papers))
        if (venue.get("name") or "").upper() == "ECCV":
            papers = fetch_eccv_virtual(years, 100000)
            if papers:
                enrichments.append(("eccv_virtual", papers))
    if is_icml_venue(venue):
        papers = fetch_icml_downloads(years, 100000)
        if papers:
            enrichments.append(("icml_downloads", papers))
    if is_pmlr_venue(venue):
        papers = fetch_pmlr_index(venue, years, 100000)
        if papers:
            enrichments.append(("pmlr", papers))
    return enrichments


def fetch_venue_title_index_all(venue: dict, years: list[int]) -> tuple[list[dict], str]:
    """Fetch the complete venue/year corpus with official-category sources preferred globally."""
    requested_limit = 100000
    candidates: list[tuple[str, list[dict]]] = []

    if is_openreview_supported_venue(venue):
        papers = fetch_openreview_venue(venue, years, requested_limit)
        if papers:
            candidates.append(("openreview", papers))
            if _source_has_confident_official_categories(papers, "openreview", requested_limit):
                return papers, "openreview"
        if is_iclr_venue(venue) and 2026 in years:
            papers = fetch_openreview_iclr_2026(requested_limit)
            if papers:
                candidates.append(("openreview_reference", papers))
                if _source_has_confident_official_categories(papers, "openreview_reference", requested_limit):
                    return papers, "openreview_reference"

    if is_neurips_venue(venue):
        papers: list[dict] = []
        for year in years:
            papers.extend(fetch_neurips_title_index(year, requested_limit))
        if papers:
            candidates.append(("neurips_virtual", papers))

    if is_acl_family_venue(venue):
        papers = fetch_acl_anthology(venue, years, requested_limit)
        if papers:
            candidates.append(("acl_anthology", papers))

    if is_cvf_venue(venue):
        papers = fetch_cvf_openaccess(venue, years, requested_limit)
        if papers:
            candidates.append(("cvf_openaccess", papers))
        if (venue.get("name") or "").upper() == "ECCV":
            papers = fetch_eccv_virtual(years, requested_limit)
            if papers:
                candidates.append(("eccv_virtual", papers))

    if is_icml_venue(venue):
        papers = fetch_icml_downloads(years, requested_limit)
        if papers:
            candidates.append(("icml_downloads", papers))
            if _source_is_complete_official_title_index(papers, "icml_downloads"):
                return _choose_best_venue_source(candidates, requested_limit)

    if is_pmlr_venue(venue):
        papers = fetch_pmlr_index(venue, years, requested_limit)
        if papers:
            candidates.append(("pmlr", papers))

    if venue.get("address"):
        base_papers = fetch_dblp_venue(venue, years, None)
        if base_papers:
            merged, used_adapters = _merge_enrichments(base_papers, _fetch_enrichment_sources(venue, years))
            adapter = "dblp"
            if used_adapters:
                adapter = f"dblp+{'+'.join(used_adapters)}"
            candidates.append((adapter, merged))

    return _choose_best_venue_source(candidates, requested_limit)

def _fetch_one_conference_virtual_detail(paper: dict, request_timeout: int) -> dict:
    url = _conference_virtual_detail_url(paper)
    detail_label = _conference_virtual_detail_label(paper)
    result = {"abstract_filled": False, "authors_filled": False, "pdf_filled": False, "error": ""}
    try:
        soup = BeautifulSoup(_request(url, timeout=request_timeout).text, "html.parser")
    except Exception as exc:
        result["error"] = str(exc)[:240]
        return result
    metadata = paper.setdefault("metadata", {})
    presentation = _presentation_type_from_url(url)
    if not presentation:
        snippets: list[str] = []
        for selector in ["meta[property='og:title']", "meta[name='twitter:title']"]:
            node = soup.select_one(selector)
            if node and node.get("content"):
                snippets.append(str(node["content"]))
        if soup.title:
            snippets.append(soup.title.get_text(" ", strip=True))
        snippets.extend(node.get_text(" ", strip=True) for node in soup.find_all(["h1", "h2"], limit=4))
        for snippet in snippets:
            presentation = _presentation_type_from_text(snippet)
            if presentation:
                break
    _set_presentation_metadata(paper, presentation, source=f"{detail_label}_url_or_title")
    if not paper.get("authors"):
        authors = _extract_conference_virtual_authors(soup)
        if authors:
            paper["authors"] = ", ".join(authors)
            metadata["authors_source"] = f"{detail_label}_jsonld"
            metadata["virtual_author_count"] = len(authors)
            result["authors_filled"] = True
    if not paper.get("abstract"):
        abstract = _extract_conference_virtual_abstract(soup)
        if abstract:
            paper["abstract"] = abstract
            metadata["abstract_source"] = detail_label
            result["abstract_filled"] = True
    if not paper.get("pdf_url"):
        pdf_link = soup.find("a", href=re.compile(r"\.pdf(?:$|\?)", re.I))
        if pdf_link and pdf_link.get("href"):
            paper["pdf_url"] = requests.compat.urljoin(url, pdf_link["href"])
            result["pdf_filled"] = True
    if paper.get("abstract") or paper.get("authors") or paper.get("pdf_url"):
        metadata["detail_source"] = detail_label
    if not paper.get("pdf_url"):
        metadata.setdefault("full_text_locator_status", "official_virtual_abstract_page_without_pdf_link")
    return result


def _fetch_one_icml_virtual_detail(paper: dict, request_timeout: int) -> dict:
    return _fetch_one_conference_virtual_detail(paper, request_timeout)


def enrich_conference_virtual_details(papers: list[dict], limit: int | None = None, *, wall_timeout_sec: float | None = None, should_cancel: Callable[[], bool] | None = None) -> tuple[list[dict], dict]:
    attempted = 0
    abstracts_filled = 0
    authors_filled = 0
    pdfs_filled = 0
    timed_out = False
    cancelled = False
    candidates = papers if limit is None else papers[:limit]
    targets = [paper for paper in candidates if _conference_virtual_detail_target(paper)]
    started = time.monotonic()
    wall_timeout = wall_timeout_sec if wall_timeout_sec is not None else _positive_float_env(
        "CONFERENCE_VIRTUAL_DETAIL_WALL_TIMEOUT_SEC",
        _positive_float_env("ICML_DETAIL_WALL_TIMEOUT_SEC", _positive_float_env("VENUE_DETAIL_WALL_TIMEOUT_SEC", 180.0)),
    )
    request_timeout = int(_positive_float_env(
        "CONFERENCE_VIRTUAL_DETAIL_REQUEST_TIMEOUT_SEC",
        _positive_float_env("ICML_DETAIL_REQUEST_TIMEOUT_SEC", _metadata_timeout(6)),
    ))
    worker_default = min(16, max(1, len(targets))) if targets else 1
    max_workers = int(_positive_float_env("CONFERENCE_VIRTUAL_DETAIL_WORKERS", _positive_float_env("ICML_DETAIL_WORKERS", worker_default)))
    max_workers = max(1, min(32, max_workers, max(1, len(targets))))
    cancel_check = should_cancel or (lambda: False)
    if not targets:
        return papers, {
            "attempted": 0,
            "abstracts_filled": 0,
            "authors_filled": 0,
            "pdfs_filled": 0,
            "timed_out": False,
            "cancelled": False,
            "wall_timeout_sec": wall_timeout,
            "request_timeout_sec": request_timeout,
            "workers": max_workers,
            "deferred": 0,
            "sources": {},
        }

    source_counts: dict[str, int] = {}
    for paper in targets:
        label = _conference_virtual_detail_label(paper)
        source_counts[label] = source_counts.get(label, 0) + 1

    future_to_paper: dict = {}
    pending_iter = iter(targets)
    completed: set[int] = set()

    def submit_next(executor: ThreadPoolExecutor) -> bool:
        nonlocal attempted, cancelled, timed_out
        if cancel_check():
            cancelled = True
            return False
        if wall_timeout > 0 and time.monotonic() - started >= wall_timeout:
            timed_out = True
            return False
        try:
            paper = next(pending_iter)
        except StopIteration:
            return False
        attempted += 1
        future_to_paper[executor.submit(_fetch_one_conference_virtual_detail, paper, request_timeout)] = paper
        return True

    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        while len(future_to_paper) < max_workers and submit_next(executor):
            pass
        while future_to_paper:
            if cancel_check():
                cancelled = True
                break
            elapsed = time.monotonic() - started
            if wall_timeout > 0 and elapsed >= wall_timeout:
                timed_out = True
                break
            wait_timeout = 1.0
            if wall_timeout > 0:
                wait_timeout = max(0.05, min(wait_timeout, wall_timeout - elapsed))
            done, _pending = wait(list(future_to_paper), timeout=wait_timeout, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                paper = future_to_paper.pop(future)
                completed.add(id(paper))
                try:
                    result = future.result()
                except Exception as exc:
                    paper.setdefault("metadata", {})["detail_fetch_error"] = str(exc)[:240]
                    continue
                if result.get("error"):
                    paper.setdefault("metadata", {})["detail_fetch_error"] = result["error"]
                if result.get("abstract_filled"):
                    abstracts_filled += 1
                if result.get("authors_filled"):
                    authors_filled += 1
                if result.get("pdf_filled"):
                    pdfs_filled += 1
            while len(future_to_paper) < max_workers and submit_next(executor):
                pass
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    for future, paper in list(future_to_paper.items()):
        if future.cancelled():
            paper.setdefault("metadata", {})["detail_fetch_error"] = "future_cancelled"
            continue
        if future.done() and id(paper) not in completed:
            try:
                result = future.result()
            except Exception as exc:
                paper.setdefault("metadata", {})["detail_fetch_error"] = str(exc)[:240]
                continue
            completed.add(id(paper))
            if result.get("error"):
                paper.setdefault("metadata", {})["detail_fetch_error"] = result["error"]
            if result.get("abstract_filled"):
                abstracts_filled += 1
            if result.get("authors_filled"):
                authors_filled += 1
            if result.get("pdf_filled"):
                pdfs_filled += 1

    if cancelled or timed_out:
        reason = "cancel_requested" if cancelled else f"wall_timeout_{wall_timeout:.0f}s"
        for paper in targets:
            if id(paper) not in completed:
                _mark_detail_fetch_deferred(paper, reason)

    return papers, {
        "attempted": attempted,
        "abstracts_filled": abstracts_filled,
        "authors_filled": authors_filled,
        "pdfs_filled": pdfs_filled,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "wall_timeout_sec": wall_timeout,
        "request_timeout_sec": request_timeout,
        "workers": max_workers,
        "deferred": sum(1 for paper in targets if paper.get("detail_fetch_deferred")),
        "sources": source_counts,
    }


def enrich_icml_virtual_details(papers: list[dict], limit: int | None = None, *, wall_timeout_sec: float | None = None, should_cancel: Callable[[], bool] | None = None) -> tuple[list[dict], dict]:
    return enrich_conference_virtual_details(papers, limit=limit, wall_timeout_sec=wall_timeout_sec, should_cancel=should_cancel)

def fetch_selected_venue_details(papers: list[dict], *, should_cancel: Callable[[], bool] | None = None, wall_timeout_sec: float | None = None) -> list[dict]:
    details: list[dict] = []
    neurips_by_year: dict[int, list[dict]] = {}
    conference_virtual: list[dict] = []
    cancel_check = should_cancel or (lambda: False)
    for candidate in papers:
        if cancel_check():
            _mark_detail_fetch_deferred(candidate, 'cancel_requested')
            details.append(candidate)
            continue
        metadata = candidate.get('metadata') if isinstance(candidate.get('metadata'), dict) else {}
        if candidate.get('source') == 'neurips_virtual' and metadata.get('title_index_only'):
            neurips_by_year.setdefault(int(candidate.get('year') or date.today().year), []).append(candidate)
        elif _conference_virtual_detail_target(candidate):
            conference_virtual.append(candidate)
        else:
            details.append(candidate)

    for year, items in neurips_by_year.items():
        if cancel_check():
            for item in items:
                _mark_detail_fetch_deferred(item, 'cancel_requested')
            details.extend(items)
            continue
        details.extend(fetch_neurips_details(items, year, wall_timeout_sec=wall_timeout_sec, should_cancel=cancel_check))
    if conference_virtual:
        enriched, stats = enrich_conference_virtual_details(conference_virtual, wall_timeout_sec=wall_timeout_sec, should_cancel=cancel_check)
        for item in enriched:
            if isinstance(item.get('metadata'), dict):
                item['metadata'].setdefault('detail_fetch_stats', stats)
        details.extend(enriched)
    return details

def fetch_venue_sample(venue: dict, year: int, sample_limit: int = 3) -> dict:
    adapter = "dblp"
    try:
        if is_iclr_venue(venue):
            adapter = "openreview"
            papers = fetch_openreview_venue(venue, [year], sample_limit)
            if not papers and year == 2026:
                adapter = "openreview_reference"
                papers = fetch_openreview_iclr_2026(sample_limit)
            if not papers and venue.get("address"):
                adapter = "dblp"
                papers = fetch_dblp_venue(venue, [year], sample_limit)
        elif is_neurips_venue(venue):
            adapter = "openreview"
            papers = fetch_openreview_venue(venue, [year], sample_limit)
            if not papers:
                adapter = "neurips_virtual"
                papers = fetch_neurips_virtual(year, sample_limit)
            if not papers and venue.get("address"):
                adapter = "dblp"
                papers = fetch_dblp_venue(venue, [year], sample_limit)
        else:
            papers = []
            if venue.get("address"):
                papers = fetch_dblp_venue(venue, [year], sample_limit)
            if not papers and is_iclr_venue(venue):
                adapter = "openreview"
                papers = fetch_openreview_venue(venue, [year], sample_limit)
            elif not papers and is_neurips_venue(venue):
                adapter = "neurips_virtual"
                papers = fetch_neurips_virtual(year, sample_limit)
                if not papers:
                    adapter = "openreview"
                    papers = fetch_openreview_venue(venue, [year], sample_limit)
            elif not papers and is_acl_family_venue(venue):
                adapter = "acl_anthology"
                papers = fetch_acl_anthology(venue, [year], sample_limit)
            elif not papers and is_cvf_venue(venue):
                adapter = "cvf_openaccess"
                papers = fetch_cvf_openaccess(venue, [year], sample_limit)
                if not papers and (venue.get("name") or "").upper() == "ECCV":
                    adapter = "eccv_virtual"
                    papers = fetch_eccv_virtual([year], sample_limit)
            elif not papers and is_pmlr_venue(venue):
                adapter = "pmlr"
                papers = fetch_pmlr_index(venue, [year], sample_limit)
            if not papers and is_openreview_supported_venue(venue):
                adapter = "openreview"
                papers = fetch_openreview_venue(venue, [year], sample_limit)
        samples = [
            {
                "title": paper.get("title", ""),
                "url": paper.get("url", ""),
                "abstract": (paper.get("abstract", "") or "")[:300],
            }
            for paper in papers[:sample_limit]
        ]
        return {
            "venue_id": venue.get("id", ""),
            "year": year,
            "ok": bool(samples),
            "sample_count": len(samples),
            "source_adapter": adapter,
            "message": "ok" if samples else f"No papers fetched via {adapter}.",
            "samples": samples,
        }
    except Exception as exc:
        return {
            "venue_id": venue.get("id", ""),
            "year": year,
            "ok": False,
            "sample_count": 0,
            "source_adapter": adapter,
            "message": str(exc),
            "samples": [],
        }


def enrich_with_semantic_scholar(papers: list[dict], limit: int = 20, api_key: str = "") -> list[dict]:
    headers = dict(HEADERS)
    if api_key:
        headers["x-api-key"] = api_key
    cache = _load_semantic_scholar_cache()
    cache_changed = False

    def _cache_keys(paper: dict) -> list[str]:
        keys: list[str] = []
        metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
        doi = _doi_from_url(str(paper.get("doi") or paper.get("url") or paper.get("pdf_url") or ""))
        doi = doi or _doi_from_url(str(metadata.get("doi") or metadata.get("doi_url") or metadata.get("publisher_url") or ""))
        if doi:
            keys.append("doi:" + doi.lower())
        title_key = _semantic_scholar_cache_key(paper.get("title", ""))
        if title_key:
            keys.append("title:" + title_key)
            keys.append(title_key)  # backward compatibility with the old title-only cache.
        return list(dict.fromkeys(keys))

    def _apply_item(paper: dict, item: dict, *, source: str) -> dict:
        metadata = paper.setdefault("metadata", {})
        abstract = str(item.get("abstract") or "")
        tldr = item.get("tldr") if isinstance(item.get("tldr"), dict) else {}
        tldr_text = str(tldr.get("text") or "")
        if abstract:
            paper["abstract"] = abstract
            metadata["abstract_source"] = source
        elif tldr_text:
            metadata["semantic_scholar_tldr_available"] = True
        if item.get("url"):
            paper["url"] = paper.get("url") or item.get("url") or ""
        pdf = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), dict) else {}
        pdf_url = str(pdf.get("url") or "")
        if pdf_url and not paper.get("pdf_url"):
            paper["pdf_url"] = pdf_url
        if tldr_text:
            metadata["tldr"] = tldr_text
        external = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
        if external:
            metadata["semantic_scholar_external_ids"] = external
            if external.get("ArXiv") and not paper.get("pdf_url"):
                paper["pdf_url"] = f"https://arxiv.org/pdf/{external.get('ArXiv')}"
        if item.get("paperId"):
            metadata["semantic_scholar_paper_id"] = item.get("paperId")
        return {
            "title": paper.get("title", ""),
            "abstract": paper.get("abstract") or "",
            "url": item.get("url") or "",
            "pdf_url": paper.get("pdf_url") or pdf_url,
            "tldr": tldr_text,
            "semantic_scholar_paper_id": item.get("paperId") or "",
            "externalIds": external,
            "source": source,
            "miss": not bool(abstract or paper.get("abstract")),
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }

    fields = "title,abstract,tldr,openAccessPdf,url,externalIds,authors"
    for paper in papers[:limit]:
        if paper.get("abstract"):
            continue
        keys = _cache_keys(paper)
        cached_hit = False
        for key in keys:
            cached = cache.get(key)
            if isinstance(cached, dict):
                _apply_semantic_scholar_cache(paper, cached)
                if cached.get("externalIds") and isinstance(paper.get("metadata"), dict):
                    paper["metadata"].setdefault("semantic_scholar_external_ids", cached.get("externalIds"))
                if paper.get("abstract"):
                    cached_hit = True
                    break
        if cached_hit:
            continue
        if keys and all(_semantic_scholar_cache_is_permanent_miss(cache.get(key)) for key in keys):
            continue

        doi = ""
        for key in keys:
            if key.startswith("doi:"):
                doi = key.split(":", 1)[1]
                break
        urls: list[tuple[str, str]] = []
        if doi:
            urls.append(("semantic_scholar_doi", f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote_plus(doi)}?fields={fields}"))
        query = quote_plus(re.sub(r"[():/\-]", " ", paper.get("title", "")))
        if query:
            urls.append(("semantic_scholar_title", f"https://api.semanticscholar.org/graph/v1/paper/search?query={query}&limit=3&fields={fields}"))

        found_cache: dict | None = None
        lookup_errors: list[str] = []
        for source, url in urls:
            try:
                response = requests.get(url, headers=headers, timeout=_metadata_timeout(6))
                if response.status_code != 200:
                    lookup_errors.append(f"{source}:http_{response.status_code}")
                    continue
                payload = response.json()
                if source.endswith("_title"):
                    rows = payload.get("data", []) if isinstance(payload, dict) else []
                    item = {}
                    for row in rows if isinstance(rows, list) else []:
                        if not isinstance(row, dict):
                            continue
                        row_title = str(row.get("title") or "")
                        if (row_title and _title_token_similarity(paper.get("title"), row_title) >= 0.82) or (not row_title and len(rows) == 1):
                            item = row
                            break
                else:
                    item = payload if isinstance(payload, dict) else {}
                if not item:
                    continue
                found_cache = _apply_item(paper, item, source=source)
                if paper.get("abstract"):
                    break
            except Exception as exc:
                lookup_errors.append(f"{source}:{str(exc)[:120]}")
                continue
        if found_cache:
            for key in keys:
                cache[key] = dict(found_cache)
            cache_changed = True
        else:
            retryable_failure = _semantic_scholar_errors_retryable(lookup_errors)
            if lookup_errors:
                metadata = paper.setdefault("metadata", {})
                metadata["semantic_scholar_lookup_error"] = "; ".join(lookup_errors[:3])
                if retryable_failure:
                    metadata["semantic_scholar_lookup_retryable"] = True
            if not retryable_failure:
                miss = {
                    "title": paper.get("title", ""),
                    "miss": True,
                    "lookup_errors": lookup_errors[:5],
                    "retryable": False,
                    "updated_at": datetime.utcnow().isoformat() + "Z",
                }
                for key in keys:
                    cache[key] = miss
                cache_changed = True
        time.sleep(0.2)
    if cache_changed:
        _save_semantic_scholar_cache(cache)
    return papers


def _arxiv_title_match_cache_path() -> Path:
    return STATE_DIR / "arxiv_title_match_cache.json"


def _load_arxiv_title_match_cache() -> dict:
    path = Path(_arxiv_title_match_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_arxiv_title_match_cache(cache: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        Path(_arxiv_title_match_cache_path()).write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        return


ARXIV_TITLE_MATCH_QUERY_VERSION = "v2_exact_title_multiquery"


def enrich_with_arxiv_title_match(papers: list[dict], limit: int = 40) -> list[dict]:
    # Metadata enrichment only: original venue rows still need final title+abstract LLM scoring.
    cache = _load_arxiv_title_match_cache()
    cache_changed = False
    ns = {"a": "http://www.w3.org/2005/Atom"}
    timeout = max(3, int(os.environ.get("ARXIV_TITLE_MATCH_TIMEOUT_SEC", "8") or 8))
    max_results = max(1, min(5, int(os.environ.get("ARXIV_TITLE_MATCH_MAX_RESULTS", "3") or 3)))
    max_queries_per_paper = max(1, min(3, int(os.environ.get("ARXIV_TITLE_MATCH_MAX_QUERIES", "2") or 2)))
    for paper in papers[:limit]:
        if paper.get("abstract") and paper.get("pdf_url"):
            continue
        title = _clean_text(str(paper.get("title") or ""))
        if not title:
            continue
        cache_key = _semantic_scholar_cache_key(title)
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            if cached.get("abstract") and not paper.get("abstract"):
                paper["abstract"] = cached.get("abstract") or ""
            if cached.get("url"):
                paper["url"] = paper.get("url") or cached.get("url") or ""
            if cached.get("pdf_url"):
                paper["pdf_url"] = paper.get("pdf_url") or cached.get("pdf_url") or ""
            if cached.get("abstract") or cached.get("pdf_url"):
                metadata = paper.setdefault("metadata", {})
                metadata["abstract_source"] = metadata.get("abstract_source") or "arxiv_title_match_cache"
                metadata["arxiv_title_match_id"] = cached.get("arxiv_id") or ""
                metadata["arxiv_title_similarity"] = cached.get("similarity", 0)
                if cached.get("author_overlap"):
                    metadata["arxiv_title_match_author_overlap"] = cached.get("author_overlap")
            if paper.get("abstract") and paper.get("pdf_url"):
                continue
            if cached.get("miss") and cached.get("query_version") == ARXIV_TITLE_MATCH_QUERY_VERSION:
                continue
        queries = _title_match_queries(title)
        if not queries:
            continue
        best = None
        best_similarity = 0.0
        best_query = ""
        expected_authors = _author_family_tokens(paper.get("authors"))
        query_errors: list[str] = []
        for query_text in queries[:max_queries_per_paper]:
            url = "https://export.arxiv.org/api/query?search_query=" + quote_plus(query_text) + f"&sortBy=submittedDate&sortOrder=descending&start=0&max_results={max_results}"
            try:
                root = ET.fromstring(_request_arxiv_page(url, timeout).text)
            except Exception as exc:
                query_errors.append(str(exc)[:120])
                continue
            for entry in root.findall("a:entry", ns):
                candidate_title = " ".join((entry.findtext("a:title", default="", namespaces=ns) or "").split())
                similarity = _title_token_similarity(title, candidate_title)
                if similarity <= best_similarity:
                    continue
                candidate_authors = _arxiv_entry_authors(entry, ns)
                candidate_family = _author_family_tokens(candidate_authors)
                author_overlap = sorted(expected_authors & candidate_family)
                if expected_authors and not author_overlap:
                    continue
                entry_id = entry.findtext("a:id", default="", namespaces=ns) or ""
                abstract = " ".join((entry.findtext("a:summary", default="", namespaces=ns) or "").split())
                best_similarity = similarity
                best_query = query_text
                best = {
                    "title": candidate_title,
                    "abstract": abstract,
                    "url": entry_id,
                    "pdf_url": entry_id.replace("/abs/", "/pdf/") if "/abs/" in entry_id else "",
                    "arxiv_id": _arxiv_entry_id(entry_id),
                    "similarity": similarity,
                    "authors": candidate_authors,
                    "author_overlap": author_overlap,
                }
        if not best or best_similarity < 0.92 or not (best.get("abstract") or paper.get("abstract")):
            metadata = paper.setdefault("metadata", {})
            if query_errors:
                metadata["arxiv_title_match_error"] = "; ".join(query_errors[:3])
            cache[cache_key] = {"title": title, "miss": True, "query_version": ARXIV_TITLE_MATCH_QUERY_VERSION, "updated_at": datetime.utcnow().isoformat() + "Z"}
            cache_changed = True
            continue
        if best.get("abstract") and not paper.get("abstract"):
            paper["abstract"] = best["abstract"]
        paper["url"] = paper.get("url") or best["url"]
        paper["pdf_url"] = paper.get("pdf_url") or best["pdf_url"]
        metadata = paper.setdefault("metadata", {})
        metadata["abstract_source"] = metadata.get("abstract_source") or "arxiv_title_match"
        metadata["arxiv_title_match_id"] = best["arxiv_id"]
        metadata["arxiv_title_similarity"] = round(best_similarity, 4)
        metadata["arxiv_title_match_title"] = best["title"]
        metadata["arxiv_title_match_query"] = best_query
        metadata["arxiv_title_match_author_overlap"] = best.get("author_overlap") or []
        cache[cache_key] = {
            "title": title,
            "abstract": best.get("abstract") or paper.get("abstract") or "",
            "url": best["url"],
            "pdf_url": best["pdf_url"],
            "arxiv_id": best["arxiv_id"],
            "similarity": round(best_similarity, 4),
            "author_overlap": best.get("author_overlap") or [],
            "query_version": ARXIV_TITLE_MATCH_QUERY_VERSION,
            "miss": False,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        cache_changed = True
        time.sleep(0.35)
    if cache_changed:
        _save_arxiv_title_match_cache(cache)
    return papers

def enrich_nature_details(papers: list[dict], limit: int | None = None) -> tuple[list[dict], dict]:
    attempted = 0
    abstracts_filled = 0
    authors_filled = 0
    pdfs_filled = 0
    dois_filled = 0
    candidates = papers if limit is None else papers[:limit]
    for paper in candidates:
        url = str(paper.get("url") or "")
        if not url:
            continue
        attempted += 1
        try:
            soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception:
            continue
        if not paper.get("abstract"):
            abstract_node = soup.select_one("[data-test='abstract'], section[aria-labelledby='Abs1'], #Abs1-content")
            if abstract_node:
                abstract = _clean_text(abstract_node.get_text(" ", strip=True))
                if abstract:
                    paper["abstract"] = abstract
                    abstracts_filled += 1
        if not paper.get("pdf_url"):
            pdf_link = soup.select_one("a[href$='.pdf'], a[href*='.pdf?'], a[href*='/pdf/']")
            if pdf_link and pdf_link.get("href"):
                paper["pdf_url"] = requests.compat.urljoin(url, pdf_link["href"])
                pdfs_filled += 1
        doi = _extract_nature_doi(soup)
        if doi:
            paper.setdefault("metadata", {})["doi"] = doi
            dois_filled += 1
        time.sleep(0.1)
    return papers, {
        "attempted": attempted,
        "abstracts_filled": abstracts_filled,
        "authors_filled": authors_filled,
        "pdfs_filled": pdfs_filled,
        "dois_filled": dois_filled,
    }


def fetch_nature_portfolio(
    journals: list[str],
    article_types: list[str],
    max_items: int | None = None,
    start_date: str = "",
    end_date: str = "",
    enrich_details: bool = True,
) -> tuple[list[dict], dict]:
    start_date = normalize_date(start_date)
    end_date = normalize_date(end_date)
    journals = [journal.strip().strip("/") for journal in journals if journal.strip()] or ["nature"]
    article_types = [item.strip() for item in article_types if item.strip()] or ["article"]
    status = {
        "source": "nature",
        "ok": False,
        "limited": False,
        "count": 0,
        "message": "",
        "journals": journals,
        "article_types": article_types,
        "start_date": start_date,
        "end_date": end_date,
        "errors": [],
        "feeds": [],
        "pages": [],
        "stopped_reason": "",
    }
    by_key: dict[str, dict] = {}
    item_limit = max(1, int(max_items)) if max_items is not None else None
    max_pages = max(1, min(100, (item_limit + 19) // 20 + 5)) if item_limit is not None else None
    max_pages_per_journal = _positive_int_env("NATURE_MAX_PAGES_PER_JOURNAL", 0)
    if max_pages_per_journal:
        max_pages = min(max_pages or max_pages_per_journal, max_pages_per_journal)
    request_timeout = _positive_int_env("NATURE_REQUEST_TIMEOUT_SEC", 12)
    status["max_pages_per_journal"] = max_pages
    status["request_timeout_sec"] = request_timeout

    def reached_limit() -> bool:
        return item_limit is not None and len(by_key) >= item_limit

    def add_papers(papers: list[dict]) -> int:
        added = 0
        for paper in papers:
            published = paper.get("metadata", {}).get("published", "")
            if not _in_date_range(published, start_date, end_date):
                continue
            key = str(paper.get("url") or paper.get("title") or "").lower()
            if key and key not in by_key:
                by_key[key] = paper
                added += 1
            if reached_limit():
                break
        return added

    def older_than_start(papers: list[dict]) -> bool:
        if not start_date:
            return False
        dates = [
            normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10])
            for paper in papers
        ]
        dates = [value for value in dates if value]
        return bool(dates) and max(dates) < start_date

    for slug in journals:
        for article_type in article_types:
            feed_url = _nature_feed_url(slug, article_type)
            feed_report = {"journal": slug, "article_type": article_type, "url": feed_url, "count": 0, "ok": False, "message": ""}
            try:
                page_text = _request(feed_url, timeout=request_timeout).text
                papers = _parse_nature_feed(page_text, slug, article_type, feed_url)
                if not papers and not _looks_like_xml(page_text):
                    papers = _parse_nature_listing_html(page_text, slug, article_type, feed_url)
                feed_report.update({"count": len(papers), "ok": bool(papers), "message": "ok" if papers else "empty feed"})
            except Exception as exc:
                papers = []
                feed_report["message"] = str(exc)
                status["errors"].append(f"{slug}/{article_type}: {exc}")
            status["feeds"].append(feed_report)
            add_papers(papers)
            if reached_limit():
                status["stopped_reason"] = "item limit"
                break

            page = 1
            while max_pages is None or page <= max_pages:
                page_url = _nature_listing_url(slug, article_type, page)
                page_report = {
                    "journal": slug,
                    "article_type": article_type,
                    "page": page,
                    "url": page_url,
                    "count": 0,
                    "added": 0,
                    "ok": False,
                    "message": "",
                }
                try:
                    page_text = _request(page_url, timeout=request_timeout).text
                    page_papers = _parse_nature_listing_html(page_text, slug, article_type, page_url)
                    added = add_papers(page_papers)
                    page_report.update({
                        "count": len(page_papers),
                        "added": added,
                        "ok": bool(page_papers),
                        "message": "ok" if page_papers else "empty page",
                    })
                except Exception as exc:
                    page_papers = []
                    page_report["message"] = str(exc)
                    status["errors"].append(f"{slug}/{article_type}/page{page}: {exc}")
                status["pages"].append(page_report)
                if reached_limit():
                    status["stopped_reason"] = "item limit"
                    break
                if not page_papers:
                    status["stopped_reason"] = "empty page"
                    break
                if older_than_start(page_papers):
                    status["stopped_reason"] = "date boundary"
                    break
                if page > 1 and page_report["added"] == 0:
                    status["stopped_reason"] = "no new items"
                    break
                page += 1
                time.sleep(0.1)
            else:
                status["stopped_reason"] = "safety page limit"
        if reached_limit():
            break
    papers = list(by_key.values())
    if item_limit is not None:
        papers = papers[:item_limit]
    if papers and enrich_details:
        papers, detail_stats = enrich_nature_details(papers, limit=len(papers) if item_limit is None else min(len(papers), item_limit))
        status["detail_enrichment"] = detail_stats
    elif papers:
        status["detail_enrichment"] = {
            "attempted": 0,
            "abstracts_filled": 0,
            "pdfs_filled": 0,
            "dois_filled": 0,
            "skipped": True,
        }
    status["count"] = len(papers)
    status["ok"] = bool(papers)
    status["limited"] = reached_limit()
    page_reports = status.get("pages") if isinstance(status.get("pages"), list) else []
    status["pages_scanned"] = len(page_reports)
    dates = [
        normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10])
        for paper in papers
    ]
    dates = [value for value in dates if value]
    if dates:
        status["date_coverage"] = {
            "newest": max(dates),
            "oldest": min(dates),
        }
    if papers:
        message = "ok"
        if status["pages_scanned"]:
            message += f"; scanned {status['pages_scanned']} listing pages"
        if dates:
            message += f"; date coverage {min(dates)} to {max(dates)}"
        if status.get("stopped_reason"):
            message += f"; stopped: {status['stopped_reason']}"
        status["message"] = message
    elif status["errors"]:
        status["message"] = "Nature feeds unavailable or failed: " + " | ".join(status["errors"][:3])
    else:
        status["message"] = "No Nature items found for selected journals/types/date range."
    return papers, status


def enrich_science_details(papers: list[dict], limit: int | None = None) -> tuple[list[dict], dict]:
    attempted = 0
    abstracts_filled = 0
    authors_filled = 0
    pdfs_filled = 0
    dois_filled = 0
    candidates = papers if limit is None else papers[:limit]
    for paper in candidates:
        metadata = paper.setdefault("metadata", {})
        doi = str(metadata.get("doi") or "").replace("doi:", "").strip()
        url = str(paper.get("url") or _science_abs_url(doi))
        if not url:
            continue
        attempted += 1
        try:
            soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception:
            continue
        if not paper.get("abstract"):
            abstract = _extract_science_abstract(soup)
            if abstract:
                paper["abstract"] = abstract
                metadata["abstract_source"] = "science_detail"
                abstracts_filled += 1
        extracted_doi = _extract_science_doi(soup)
        if extracted_doi and not metadata.get("doi"):
            metadata["doi"] = extracted_doi
            doi = extracted_doi
            dois_filled += 1
        if not paper.get("pdf_url"):
            pdf_url = _science_pdf_url(doi)
            if pdf_url:
                paper["pdf_url"] = pdf_url
                pdfs_filled += 1
        if not paper.get("url") and url:
            paper["url"] = url
        time.sleep(0.1)
    return papers, {
        "attempted": attempted,
        "abstracts_filled": abstracts_filled,
        "authors_filled": authors_filled,
        "pdfs_filled": pdfs_filled,
        "dois_filled": dois_filled,
    }


def fetch_science_family(
    journals: list[str],
    article_types: list[str],
    max_items: int | None = None,
    start_date: str = "",
    end_date: str = "",
) -> tuple[list[dict], dict]:
    start_date = normalize_date(start_date)
    end_date = normalize_date(end_date)
    journals = [journal.strip() for journal in journals if journal.strip()] or ["science"]
    article_types = [item.strip() for item in article_types if item.strip()] or ["Research Article"]
    allowed_types = {item.lower() for item in article_types if item.lower() not in {"all", "*"}}
    status = {
        "source": "science",
        "ok": False,
        "limited": False,
        "count": 0,
        "message": "",
        "journals": journals,
        "article_types": article_types,
        "start_date": start_date,
        "end_date": end_date,
        "errors": [],
        "feeds": [],
        "crossref_pages": [],
        "stopped_reason": "",
    }
    by_key: dict[str, dict] = {}
    item_limit = max(1, int(max_items)) if max_items is not None else None
    rows = 100
    max_crossref_pages = _positive_int_env("SCIENCE_MAX_CROSSREF_PAGES_PER_JOURNAL", 0)
    request_timeout = _positive_int_env("SCIENCE_REQUEST_TIMEOUT_SEC", 20)
    status["max_crossref_pages_per_journal"] = max_crossref_pages or None
    status["request_timeout_sec"] = request_timeout

    def reached_limit() -> bool:
        return item_limit is not None and len(by_key) >= item_limit

    def add_papers(papers: list[dict]) -> int:
        added = 0
        for paper in papers:
            published = paper.get("metadata", {}).get("published", "")
            if not _in_date_range(published, start_date, end_date):
                continue
            key = str(paper.get("metadata", {}).get("doi") or paper.get("url") or paper.get("title") or "").lower()
            if key and key not in by_key:
                by_key[key] = paper
                added += 1
            if reached_limit():
                break
        return added

    for slug in journals:
        journal = _science_journal_meta(slug)
        issn = journal.get("issn", "")
        if issn:
            offset = 0
            crossref_pages_for_journal = 0
            while not reached_limit():
                if max_crossref_pages and crossref_pages_for_journal >= max_crossref_pages:
                    status["stopped_reason"] = "crossref page limit"
                    break
                crossref_url = _science_crossref_url(issn, start_date, end_date, rows, offset)
                page_report = {
                    "journal": slug,
                    "issn": issn,
                    "offset": offset,
                    "rows": rows,
                    "url": crossref_url,
                    "count": 0,
                    "added": 0,
                    "ok": False,
                    "message": "",
                }
                try:
                    response = _request(crossref_url, timeout=request_timeout)
                    payload = response.json()
                    records = payload.get("message", {}).get("items", [])
                    papers = _parse_science_crossref_items(records, slug)
                    added = add_papers(papers)
                    page_report.update({
                        "count": len(papers),
                        "added": added,
                        "ok": bool(papers),
                        "message": "ok" if papers else "empty crossref page",
                    })
                except Exception as exc:
                    records = []
                    page_report["message"] = str(exc)
                    status["errors"].append(f"{slug}/crossref/{offset}: {exc}")
                status["crossref_pages"].append(page_report)
                crossref_pages_for_journal += 1
                if reached_limit():
                    status["stopped_reason"] = "item limit"
                    break
                if not records:
                    status["stopped_reason"] = "empty crossref page"
                    break
                if len(records) < rows and offset > 0:
                    status["stopped_reason"] = "end of crossref results"
                    break
                if page_report["added"] == 0 and offset > 0:
                    status["stopped_reason"] = "no new items"
                    break
                offset += rows
                time.sleep(0.1)
        if reached_limit():
            status["stopped_reason"] = "item limit"
            break

        feed_url = _science_feed_url(slug)
        feed_report = {"journal": slug, "url": feed_url, "count": 0, "ok": False, "message": ""}
        try:
            papers = _parse_science_feed(_request(feed_url, timeout=request_timeout).text, slug, allowed_types, feed_url)
            feed_report.update({"count": len(papers), "ok": bool(papers), "message": "ok" if papers else "empty feed after type filter"})
        except Exception as exc:
            papers = []
            feed_report["message"] = str(exc)
            status["errors"].append(f"{slug}: {exc}")
        status["feeds"].append(feed_report)
        add_papers(papers)
        if reached_limit():
            status["stopped_reason"] = "item limit"
            break
    papers = list(by_key.values())
    if item_limit is not None:
        papers = papers[:item_limit]
    status["count"] = len(papers)
    status["ok"] = bool(papers)
    status["limited"] = reached_limit()
    crossref_reports = status.get("crossref_pages") if isinstance(status.get("crossref_pages"), list) else []
    status["pages_scanned"] = len(crossref_reports)
    dates = [
        normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10])
        for paper in papers
    ]
    dates = [value for value in dates if value]
    if dates:
        status["date_coverage"] = {
            "newest": max(dates),
            "oldest": min(dates),
        }
    if papers:
        message = "ok"
        if status["pages_scanned"]:
            message += f"; scanned {status['pages_scanned']} Crossref pages"
        if dates:
            message += f"; date coverage {min(dates)} to {max(dates)}"
        if status.get("stopped_reason"):
            message += f"; stopped: {status['stopped_reason']}"
        status["message"] = message
    elif status["errors"]:
        status["message"] = "Science feeds unavailable or failed: " + " | ".join(status["errors"][:3])
    else:
        status["message"] = "No Science items found for selected journals/types/date range."
    return papers, status

def fetch_biorxiv(categories: list[str], max_items: int, start_date: str = "", end_date: str = "") -> tuple[list[dict], dict]:
    papers: list[dict] = []
    by_key: dict[str, dict] = {}
    start_date = normalize_date(start_date) or _biorxiv_default_start_date()
    end_date = normalize_date(end_date) or date.today().isoformat()
    categories = [category.strip() for category in (categories or []) if category.strip()] or ["bioinformatics"]
    max_items = max(1, int(max_items or 100))
    request_timeout = _positive_int_env("BIORXIV_REQUEST_TIMEOUT_SEC", 20)
    max_pages = _positive_int_env("BIORXIV_MAX_PAGES", 0)
    window_days = max(1, min(60, _positive_int_env("BIORXIV_WINDOW_DAYS", 30)))
    status = {
        "source": "biorxiv",
        "ok": False,
        "limited": False,
        "count": 0,
        "message": "",
        "categories": categories,
        "start_date": start_date,
        "end_date": end_date,
        "queries": [f"server:biorxiv date:{start_date}..{end_date} categories:{', '.join(categories)}"],
        "errors": [],
        "pages_fetched": 0,
        "deduped_count": 0,
        "raw_count": 0,
        "api_raw_count": 0,
        "matched_category_count": 0,
        "request_timeout_sec": request_timeout,
        "max_pages": max_pages or None,
        "window_days": window_days,
        "windows": [],
    }

    def parse_total(payload: dict) -> int:
        messages = payload.get("messages") if isinstance(payload, dict) else []
        if isinstance(messages, list) and messages:
            message = messages[0] if isinstance(messages[0], dict) else {}
            for field in ("total", "count_new_papers"):
                try:
                    value = int(str(message.get(field) or "0"))
                except (TypeError, ValueError):
                    value = 0
                if value > 0:
                    return value
        return 0

    try:
        start_dt = datetime.fromisoformat(start_date).date()
        end_dt = datetime.fromisoformat(end_date).date()
    except Exception:
        start_dt = date.today() - timedelta(days=30)
        end_dt = date.today()
    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt

    windows: list[tuple[str, str]] = []
    current_end = end_dt
    while current_end >= start_dt:
        current_start = max(start_dt, current_end - timedelta(days=window_days - 1))
        windows.append((current_start.isoformat(), current_end.isoformat()))
        current_end = current_start - timedelta(days=1)

    for window_start, window_end in windows:
        if len(papers) >= max_items:
            status["limited"] = True
            status["stopped_reason"] = "item limit"
            break
        cursor = 0
        window_report = {"start_date": window_start, "end_date": window_end, "pages": 0, "api_records": 0, "matched_records": 0, "total": 0, "errors": []}
        seen_page_keys: set[str] = set()
        while len(papers) < max_items:
            if max_pages and status["pages_fetched"] >= max_pages:
                status["limited"] = True
                status["stopped_reason"] = "page limit"
                break
            url = f"https://api.biorxiv.org/details/biorxiv/{window_start}/{window_end}/{cursor}/json"
            try:
                response = _request(url, timeout=request_timeout)
                data = response.json()
            except Exception as exc:
                message = f"{window_start}..{window_end} cursor={cursor}: {exc}"
                status["errors"].append(message)
                window_report["errors"].append(message)
                break
            records = data.get("collection") if isinstance(data, dict) else []
            if not isinstance(records, list) or not records:
                break
            page_key = "|".join(str(record.get("doi") or record.get("title") or "") for record in records[:3])
            if page_key and page_key in seen_page_keys:
                window_report["errors"].append(f"repeated page at cursor={cursor}")
                break
            seen_page_keys.add(page_key)
            total_for_window = parse_total(data)
            if total_for_window:
                window_report["total"] = total_for_window
            status["pages_fetched"] += 1
            window_report["pages"] += 1
            window_report["api_records"] += len(records)
            status["api_raw_count"] += len(records)
            status["raw_count"] = status["api_raw_count"]
            matched_this_page = 0
            for record in records:
                if not isinstance(record, dict):
                    continue
                published = normalize_date(str(record.get("date") or ""))
                if not _in_date_range(published, start_date, end_date):
                    continue
                category = str(record.get("category") or "").strip()
                if not _biorxiv_category_matches(category, categories):
                    continue
                matched_this_page += 1
                title = " ".join(str(record.get("title") or "").split())
                abstract = " ".join(str(record.get("abstract") or "").split())
                doi = str(record.get("doi") or "").strip()
                version = str(record.get("version") or "").strip()
                key = doi.lower() or title.lower()
                if not key:
                    continue
                paper = by_key.get(key)
                if paper:
                    categories_seen = paper.setdefault("categories", [paper.get("category", "")])
                    if category and category not in categories_seen:
                        categories_seen.append(category)
                    paper.setdefault("metadata", {})["all_categories"] = categories_seen
                    continue
                url = _biorxiv_content_url(doi, version)
                pdf_url = f"{url}.full.pdf" if url else ""
                all_categories = [category] if category else []
                paper = {
                    "id": stable_id("paper", doi or title),
                    "source": "biorxiv",
                    "biorxiv_doi": doi,
                    "title": title,
                    "authors": str(record.get("authors") or ""),
                    "abstract": abstract,
                    "url": url,
                    "pdf_url": pdf_url,
                    "venue": "bioRxiv",
                    "year": int(published[:4]) if published[:4].isdigit() else date.today().year,
                    "category": category,
                    "categories": all_categories,
                    "classification_source": "llm_inferred",
                    "metadata": {
                        "published": published,
                        "biorxiv_category": category,
                        "primary_category": category,
                        "all_categories": all_categories,
                        "doi": doi,
                        "version": version,
                        "license": record.get("license") or "",
                        "server": record.get("server") or "biorxiv",
                        "type": record.get("type") or "",
                        "published_journal": record.get("published") or "",
                        "window_start_date": window_start,
                        "window_end_date": window_end,
                    },
                }
                by_key[key] = paper
                papers.append(paper)
                if len(papers) >= max_items:
                    status["limited"] = True
                    status["stopped_reason"] = "item limit"
                    break
            window_report["matched_records"] += matched_this_page
            status["matched_category_count"] += matched_this_page
            if len(papers) >= max_items:
                break
            cursor += len(records)
            if total_for_window and cursor >= total_for_window:
                break
            time.sleep(0.25)
        status["windows"].append(window_report)
        if status.get("stopped_reason") in {"item limit", "page limit"}:
            break
    status["count"] = len(papers)
    status["deduped_count"] = len(papers)
    status["ok"] = bool(papers)
    if papers:
        dates = [normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10]) for paper in papers]
        dates = [value for value in dates if value]
        if dates:
            status["date_coverage"] = {"newest": max(dates), "oldest": min(dates)}
        if status.get("stopped_reason") == "item limit":
            limit_message = "limited by max_items"
        elif status.get("stopped_reason") == "page limit":
            limit_message = "limited by page limit"
        else:
            limit_message = "fetched available windows"
        status["message"] = (
            f"ok; {limit_message}; windows={len(status['windows'])}; pages_fetched={status['pages_fetched']}; "
            f"api_records={status['api_raw_count']}; matched_records={status['matched_category_count']}; "
            f"queries={'; '.join(status['queries'])}"
        )
    elif status["errors"]:
        status["message"] = "bioRxiv unavailable or query failed: " + " | ".join(status["errors"][:3])
    else:
        status["message"] = f"No bioRxiv papers found; queries={'; '.join(status['queries'])}"
    return papers, status

def _request_arxiv_page(url: str, timeout_sec: int, attempts: int = 3):
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            try:
                return _request(url, timeout=timeout_sec)
            except TypeError:
                return _request(url)
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.8 * attempt)
    if last_error:
        raise last_error
    raise RuntimeError("arXiv request failed")


def fetch_arxiv(categories: list[str], max_items: int, start_date: str = "", end_date: str = "", topic_queries: list[str] | None = None, log=None, progress=None, should_cancel=None, max_queries: int | None = None, per_query_limit: int | None = None, timeout_sec: int | None = None) -> tuple[list[dict], dict]:
    papers: list[dict] = []
    by_key: dict[str, dict] = {}
    start_date, end_date, date_window_source = _arxiv_date_window(start_date, end_date)
    categories = [category.strip() for category in (categories or []) if category.strip()] or ["cs.AI"]
    full_scan = os.environ.get("ARXIV_FULL_SCAN", "1").lower() in {"1", "true", "yes", "on"}
    env_per_query = int(os.environ.get("ARXIV_PER_QUERY_LIMIT", "0") or 0)
    env_max_queries = int(os.environ.get("ARXIV_MAX_QUERIES", "0") or 0)
    env_timeout = int(os.environ.get("ARXIV_TIMEOUT_SEC", "0") or 0)
    env_total_limit = int(os.environ.get("ARXIV_MAX_TOTAL", "0") or 0)
    per_query_limit = max(10, min(100, env_per_query or int(per_query_limit or (100 if full_scan else 50))))
    max_queries = max(1, env_max_queries or int(max_queries or (len(categories) + len(topic_queries or []) if full_scan else 3)))
    arxiv_timeout = max(20, env_timeout or int(timeout_sec or 45))
    total_limit = max(0, env_total_limit or (max_items if not full_scan else 0))
    status = {
        "source": "arxiv",
        "ok": False,
        "limited": False,
        "count": 0,
        "message": "",
        "categories": categories,
        "start_date": start_date,
        "end_date": end_date,
        "date_window_source": date_window_source,
        "default_recent_days": ARXIV_DEFAULT_RECENT_DAYS,
        "queries": [],
        "errors": [],
        "query_limit": max_queries,
        "per_query_limit": per_query_limit,
        "full_scan": full_scan,
        "pages_fetched": 0,
        "deduped_count": 0,
        "total_limit": total_limit,
    }
    queries = _arxiv_search_queries(categories, topic_queries or [], start_date, end_date)[:max_queries]
    fallback_queries = [query for query in _arxiv_fallback_queries(categories, start_date, end_date) if query not in queries]
    ns = {"a": "http://www.w3.org/2005/Atom"}

    def run_query(query_index: int, total_queries: int, query_label: str, query_text: str, *, fallback_query: bool = False) -> None:
        query = quote_plus(query_text)
        status["queries"].append(query_text)
        start = 0
        while True:
            if should_cancel and should_cancel():
                status["errors"].append("cancelled")
                return
            if log:
                log(f"arXiv query {query_index}/{total_queries} [{query_label}] page_start={start}: {query_text[:180]}")
            if progress:
                progress("arxiv", query_index - 1, max(1, total_queries), f"arXiv query {query_index}/{total_queries}: {query_label}, page start {start}")
            url = f"https://export.arxiv.org/api/query?search_query={query}&sortBy=submittedDate&sortOrder=descending&start={start}&max_results={per_query_limit}"
            try:
                root = ET.fromstring(_request_arxiv_page(url, arxiv_timeout).text)
            except Exception as exc:
                error_text = str(exc)
                status["errors"].append(f"{query_label} start={start}: {error_text}")
                if "429" in error_text or "Too Many Requests" in error_text:
                    status["limited"] = True
                    status["message"] = f"arXiv rate limited after {status['pages_fetched']} pages; kept {len(papers)} papers."
                if log:
                    log(f"arXiv query {query_index}/{total_queries} failed at start={start}: {error_text[:240]}")
                return
            status["pages_fetched"] += 1
            entries = root.findall("a:entry", ns)
            if not entries:
                return
            before = len(papers)
            for entry in entries:
                _append_arxiv_entry(papers, by_key, entry, ns, query_label, query_text, start_date, end_date, fallback_query=fallback_query)
                if total_limit and len(papers) >= total_limit:
                    status["limited"] = True
                    status["message"] = f"Reached configured arXiv total_limit={total_limit}; increase ARXIV_MAX_TOTAL or use ARXIV_FULL_SCAN=1 for deeper survey."
                    return
            if log:
                log(f"arXiv query {query_index}/{total_queries} collected {len(papers) - before} new papers on this page; total {len(papers)}")
            if len(entries) < per_query_limit:
                return
            if not full_scan and len(papers) >= max_items:
                return
            start += per_query_limit
            time.sleep(0.5)

    for query_index, (query_label, query_text) in enumerate(queries, 1):
        run_query(query_index, len(queries), query_label, query_text)
        if status.get("message", "").startswith("arXiv rate limited") or (total_limit and len(papers) >= total_limit) or (not full_scan and len(papers) >= max_items):
            break
    if not papers and fallback_queries:
        status["limited"] = True
        if log:
            log("arXiv topic queries returned no usable papers; falling back to category queries")
        for fallback_index, (query_label, query_text) in enumerate(fallback_queries, 1):
            run_query(fallback_index, len(fallback_queries), query_label, query_text, fallback_query=True)
            if papers or status.get("message", "").startswith("arXiv rate limited"):
                break
    status["count"] = len(papers)
    status["deduped_count"] = len(by_key)
    status["ok"] = bool(papers)
    if papers:
        status["message"] = status.get("message") or f"ok; pages_fetched={status['pages_fetched']}; queries={'; '.join(status['queries'])}"
    elif status["limited"]:
        status["message"] = status.get("message") or "arXiv rate limited before returning papers. Retry later or reduce query volume."
    elif status["errors"]:
        status["message"] = "arXiv unavailable or query failed: " + " | ".join(status["errors"][:3])
    else:
        status["message"] = f"No arXiv papers found; queries={'; '.join(status['queries'])}"
    return (papers[:total_limit] if total_limit else papers), status

def fetch_huggingface(
    max_papers: int,
    max_models: int,
    include_papers: bool = True,
    include_models: bool = True,
    start_date: str = "",
    end_date: str = "",
) -> tuple[list[dict], list[dict], dict]:
    papers: list[dict] = []
    models: list[dict] = []
    status = {"source": "huggingface", "ok": False, "limited": False, "message": "", "count": 0}
    start_date = normalize_date(start_date)
    end_date = normalize_date(end_date)
    if include_papers:
        try:
            data = _request("https://huggingface.co/api/daily_papers").json()
            if start_date or end_date:
                status["limited"] = True
                status["message"] = "HuggingFace daily papers API only exposes the current feed; date filtering is limited."
            for item in data[:max_papers]:
                paper = item.get("paper", {})
                paper_id = paper.get("id", "")
                papers.append({
                    "id": stable_id("hfpaper", paper_id or paper.get("title", "")),
                    "source": "huggingface",
                    "title": paper.get("title", "Untitled"),
                    "abstract": paper.get("summary", ""),
                    "url": f"https://huggingface.co/papers/{paper_id}" if paper_id else "",
                    "score": item.get("numComments", 0),
                    "category": "",
                    "classification_source": "llm_inferred",
                    "metadata": {"kind": "paper", "upvotes": paper.get("upvotes", 0)},
                })
        except Exception as exc:
            status.setdefault("errors", []).append(f"daily_papers: {exc}")
            status["message"] = f"HuggingFace daily papers unavailable: {exc}"
    if include_models:
        try:
            data = _request(f"https://huggingface.co/api/models?sort=likes&direction=-1&limit={max(max_models * 5, max_models)}").json()
            for item in data:
                modified = (item.get("lastModified") or item.get("createdAt") or "")[:10]
                if (start_date or end_date) and not _in_date_range(modified, start_date, end_date):
                    continue
                model_id = item.get("id", "")
                models.append({
                    "id": stable_id("hfmodel", model_id),
                    "source": "huggingface",
                    "title": model_id,
                    "abstract": item.get("description", "") or "",
                    "url": f"https://huggingface.co/{model_id}" if model_id else "",
                    "score": item.get("likes", 0),
                    "category": "",
                    "classification_source": "llm_inferred",
                    "metadata": {"kind": "model", "likes": item.get("likes", 0), "downloads": item.get("downloads", 0), "tags": item.get("tags", []), "last_modified": modified},
                })
                if len(models) >= max_models:
                    break
        except Exception as exc:
            status.setdefault("errors", []).append(f"models: {exc}")
            status["message"] = f"HuggingFace models unavailable: {exc}"
    status["count"] = len(papers) + len(models)
    status["ok"] = status["count"] > 0
    if status["ok"]:
        if status.get("errors"):
            status["limited"] = True
            status["message"] = status.get("message") or "partial ok; some HuggingFace endpoints failed"
        elif not status["message"]:
            status["message"] = "ok"
    return papers, models, status


def fetch_github_trending(languages: list[str], since: str, max_items: int, start_date: str = "", end_date: str = "") -> tuple[list[dict], dict]:
    repos: list[dict] = []
    status = {"source": "github", "ok": False, "limited": False, "message": "", "count": 0}
    langs = languages or ["all"]
    start_date = normalize_date(start_date)
    end_date = normalize_date(end_date)
    for language in langs:
        query_parts = ["stars:>1"]
        if language.lower() != "all":
            query_parts.append(f"language:{language}")
        if start_date and end_date:
            query_parts.append(f"created:{start_date}..{end_date}")
        elif start_date:
            query_parts.append(f"created:>={start_date}")
        elif end_date:
            query_parts.append(f"created:<={end_date}")
        url = "https://api.github.com/search/repositories?" + urlencode({
            "q": " ".join(query_parts),
            "sort": "stars",
            "order": "desc",
            "per_page": min(100, max_items),
        })
        try:
            items = _request(url).json().get("items", [])
        except Exception:
            continue
        for item in items:
            repos.append({
                "id": stable_id("repo", item.get("full_name", "")),
                "source": "github",
                "title": item.get("full_name", ""),
                "abstract": item.get("description", "") or "",
                "url": item.get("html_url", ""),
                "score": item.get("stargazers_count", 0),
                "category": item.get("language") or "",
                "classification_source": "llm_inferred",
                "metadata": {
                    "language": item.get("language") or "",
                    "stars": item.get("stargazers_count", 0),
                    "created_at": (item.get("created_at") or "")[:10],
                    "pushed_at": (item.get("pushed_at") or "")[:10],
                },
            })
            if len(repos) >= max_items:
                status.update({"ok": True, "count": len(repos), "message": "ok"})
                return repos[:max_items], status
    if repos:
        status.update({"ok": True, "count": len(repos), "message": "ok"})
        return repos[:max_items], status

    status["limited"] = True
    for language in langs:
        suffix = "" if language.lower() == "all" else f"/{language.lower()}"
        url = f"https://github.com/trending{suffix}?since={since}"
        try:
            soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception:
            continue
        for article in soup.select("article.Box-row"):
            link = article.select_one("h2 a[href]")
            if not link:
                continue
            repo_name = link.get("href", "").strip("/")
            desc = article.select_one("p")
            lang = article.select_one("span[itemprop='programmingLanguage']")
            stars = 0
            stlink = article.select_one("a[href$='/stargazers']")
            if stlink:
                try:
                    stars = int(stlink.get_text(strip=True).replace(",", ""))
                except ValueError:
                    stars = 0
            repos.append({
                "id": stable_id("repo", repo_name),
                "source": "github",
                "title": repo_name,
                "abstract": desc.get_text(" ", strip=True) if desc else "",
                "url": f"https://github.com/{repo_name}",
                "score": stars,
                "category": lang.get_text(strip=True) if lang else "",
                "classification_source": "llm_inferred",
                "metadata": {"language": lang.get_text(strip=True) if lang else "", "stars": stars, "since": since},
            })
            if len(repos) >= max_items:
                status.update({"ok": True, "count": len(repos), "message": "GitHub Search API unavailable; used Trending fallback."})
                return repos, status
        time.sleep(0.5)
    status.update({"ok": bool(repos), "count": len(repos), "message": "GitHub Search API unavailable; used Trending fallback." if repos else "GitHub Search and Trending unavailable or empty."})
    return repos[:max_items], status
