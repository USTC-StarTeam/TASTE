from __future__ import annotations


# ---- finding support ----

# ---- catalog.py ----

from catalog import (
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
)


# ---- local_index.py ----

from local_store import (
    _venue_id_candidates,
    load_local_venue_year,
    venue_cache_key,
)
from finding_runtime import display_path


# ---- local_cache.py ----

from local_store import (
    SCHEMA_VERSION,
    _first_existing_directory,
    build_category_summary,
    cache_directory,
    load_cached_venue_year,
    normalize_cached_paper,
    write_venue_year_cache,
)


# ---- local_rank.py ----

from ranking import rank_papers_tfidf


# ---- quality.py ----

from quality import (
    CONFERENCE_QUALITY_TABLE,
    JOURNAL_QUALITY_TABLE,
    QUALITY_DATA_DIR,
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
    attach_quality_metadata,
)


# ---- category_select.py ----

from selection import (
    CATEGORY_SELECT_CACHE_MAX_ENTRIES,
    CATEGORY_SELECT_CACHE_SCHEMA_VERSION,
    CATEGORY_SELECTION_PAPER_TARGET,
    CATEGORY_SELECT_TEMPERATURE,
    _build_rejected,
    _cache_entries_fingerprint,
    _category_entries,
    _category_json_or_error,
    _category_selection_max,
    _category_select_cache_enabled,
    _category_select_cache_key,
    _category_select_cache_path,
    _compact_entries,
    _interest_text,
    _json_or_none,
    _llm_identity,
    _load_category_select_cache,
    _normalize_selected_rows,
    _normalized_text,
    _select_ranked_categories_until_target,
    _store_category_select_cache_entry,
    _use_llm_category_select,
    _valid_cached_selection,
    filter_papers_by_selected_categories,
    select_relevant_categories,
)


# ---- profile_normalize.py ----

from research_profile import (
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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlencode, urljoin, urlparse
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup

from finding_runtime import STATE_DIR, write_json_cache


def _contact_mailto() -> str:
    """Contact email for API polite pools (Crossref/OpenAlex/Europe PMC)."""
    return (
        os.environ.get("FIND_CONTACT_EMAIL")
        or os.environ.get("CROSSREF_MAILTO")
        or os.environ.get("OPENALEX_MAILTO")
        or "taste-finding@example.org"
    ).strip()


def _default_user_agent() -> str:
    # A realistic, identifiable User-Agent. Publisher sites (nature.com) reject
    # obvious bot UAs; API services prefer a contact in the UA (polite pool).
    custom = os.environ.get("FIND_USER_AGENT", "").strip()
    if custom:
        return custom
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/124.0 Safari/537.36 TASTE-finding/1.0 (mailto:{_contact_mailto()})"
    )


HEADERS = {
    "User-Agent": _default_user_agent(),
    "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _positive_int_env(name: str, default: int = 0) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else int(default)


def _date_window_days(start_date: str = "", end_date: str = "") -> int:
    start = normalize_date(start_date)
    end = normalize_date(end_date) or date.today().isoformat()
    if not start:
        return 0
    try:
        return max(0, (date.fromisoformat(end) - date.fromisoformat(start)).days)
    except ValueError:
        return 0


def _coverage_misses_start(dates: list[str], start_date: str) -> bool:
    start = normalize_date(start_date)
    clean_dates = [normalize_date(value[:10]) for value in dates if normalize_date(value[:10])]
    return bool(start and clean_dates and min(clean_dates) > start)


def _coverage_gap_days(dates: list[str], start_date: str) -> int:
    start = normalize_date(start_date)
    clean_dates = [normalize_date(value[:10]) for value in dates if normalize_date(value[:10])]
    if not start or not clean_dates:
        return 0
    try:
        return max(0, (date.fromisoformat(min(clean_dates)) - date.fromisoformat(start)).days)
    except ValueError:
        return 0


from sources import (
    VENUE_METADATA_AUDIT_KEY,
    _attach_openreview_metadata_audit,
    _attach_venue_metadata_audit,
    _metadata_timeout,
    _venue_metadata_audit,
    venue_metadata_audit_from_papers,
)


from sources import (
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


from sources import (
    fetch_openreview_iclr_2026,
    fetch_icml_official_virtual_2026,
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


from sources import (
    OPENREVIEW_VENUE_PATTERNS,
    _ABSTRACT_UI_CONTROL_RE,
    _clean_text,
    _in_date_range,
    _looks_like_paper_title,
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
    is_aaai_venue,
    is_acl_family_venue,
    is_cvf_venue,
    is_iclr_venue,
    is_ijcai_venue,
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


def _openalex_oa_pdf_abstract_cache_path() -> Path:
    return STATE_DIR / "openalex_oa_pdf_abstract_cache.json"


def _acm_pdf_abstract_cache_path() -> Path:
    return STATE_DIR / "acm_pdf_abstract_cache.json"


def _acm_detail_abstract_cache_path() -> Path:
    return STATE_DIR / "acm_detail_abstract_cache.json"


def _chatpaper_abstract_cache_path() -> Path:
    return STATE_DIR / "chatpaper_abstract_cache.json"


def _pure_portal_abstract_cache_path() -> Path:
    return STATE_DIR / "pure_portal_abstract_cache.json"


def _hal_abstract_cache_path() -> Path:
    return STATE_DIR / "hal_abstract_cache.json"


def _author_pdf_abstract_cache_path() -> Path:
    return STATE_DIR / "author_pdf_abstract_cache.json"


def _public_pdf_search_cache_path() -> Path:
    return STATE_DIR / "public_pdf_search_abstract_cache.json"


def _public_publication_page_search_cache_path() -> Path:
    return STATE_DIR / "public_publication_page_abstract_cache.json"


_ACM_DETAIL_CACHE_LOCK = Lock()


def _save_state_cache(path: Path, cache: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        write_json_cache(Path(path), cache if isinstance(cache, dict) else {}, merge_existing=True)
    except Exception:
        return


from sources import (
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
    _save_state_cache(path, cache)


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
    _save_state_cache(path, cache)


def _load_openalex_oa_pdf_abstract_cache() -> dict:
    path = Path(_openalex_oa_pdf_abstract_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_openalex_oa_pdf_abstract_cache(cache: dict) -> None:
    path = Path(_openalex_oa_pdf_abstract_cache_path())
    _save_state_cache(path, cache)


def _load_acm_pdf_abstract_cache() -> dict:
    path = Path(_acm_pdf_abstract_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_acm_pdf_abstract_cache(cache: dict) -> None:
    path = Path(_acm_pdf_abstract_cache_path())
    _save_state_cache(path, cache)


def _load_acm_detail_abstract_cache() -> dict:
    path = Path(_acm_detail_abstract_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_acm_detail_abstract_cache(cache: dict) -> None:
    path = Path(_acm_detail_abstract_cache_path())
    _save_state_cache(path, cache)


def _clean_chatpaper_title(value: object) -> str:
    title = _clean_text(value)
    return re.sub(r"^\d+\.\s*", "", title).strip()


def _load_chatpaper_abstract_cache() -> dict:
    path = Path(_chatpaper_abstract_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    titles = data.get("titles")
    if isinstance(titles, dict):
        normalized: dict[str, Any] = {}
        changed = False
        for key, value in titles.items():
            if not isinstance(value, dict):
                normalized[str(key)] = value
                continue
            title = _clean_chatpaper_title(value.get("title"))
            if title and title != value.get("title"):
                value = dict(value)
                value["title"] = title
                changed = True
            normalized_key = _semantic_scholar_cache_key(title) if title else str(key)
            if normalized_key != key:
                changed = True
            normalized[normalized_key] = value
        if changed:
            data["titles"] = normalized
    return data


def _save_chatpaper_abstract_cache(cache: dict) -> None:
    path = Path(_chatpaper_abstract_cache_path())
    _save_state_cache(path, cache)


def _load_pure_portal_abstract_cache() -> dict:
    path = Path(_pure_portal_abstract_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_pure_portal_abstract_cache(cache: dict) -> None:
    path = Path(_pure_portal_abstract_cache_path())
    _save_state_cache(path, cache)


def _load_hal_abstract_cache() -> dict:
    path = Path(_hal_abstract_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_hal_abstract_cache(cache: dict) -> None:
    path = Path(_hal_abstract_cache_path())
    _save_state_cache(path, cache)


def _load_author_pdf_abstract_cache() -> dict:
    path = Path(_author_pdf_abstract_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_author_pdf_abstract_cache(cache: dict) -> None:
    path = Path(_author_pdf_abstract_cache_path())
    _save_state_cache(path, cache)


def _load_public_pdf_search_cache() -> dict:
    path = Path(_public_pdf_search_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_public_pdf_search_cache(cache: dict) -> None:
    path = Path(_public_pdf_search_cache_path())
    _save_state_cache(path, cache)


def _load_public_publication_page_search_cache() -> dict:
    path = Path(_public_publication_page_search_cache_path())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_public_publication_page_search_cache(cache: dict) -> None:
    path = Path(_public_publication_page_search_cache_path())
    _save_state_cache(path, cache)


def _paper_doi(paper: dict) -> str:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    for value in (
        paper.get("doi"),
        metadata.get("doi"),
        paper.get("url"),
        metadata.get("doi_url"),
        metadata.get("publisher_url"),
        paper.get("pdf_url"),
    ):
        doi = _doi_from_url(str(value or ""))
        if doi:
            return doi.strip().lower()
    return ""


ACM_VENUE_TEXT_MARKERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "kdd": (
        "KDD",
        (
            "proceedings of the acm sigkdd",
            "acm sigkdd",
            "sigkdd",
            "knowledge discovery and data mining",
            "kdd",
        ),
    ),
    "www": (
        "WWW",
        (
            "proceedings of the acm web conference",
            "the acm web conference",
            "the web conference",
            "world wide web conference",
            "www",
        ),
    ),
    "sigir": (
        "SIGIR",
        (
            "proceedings of the annual international acm sigir",
            "acm sigir",
            "sigir",
            "research and development in information retrieval",
        ),
    ),
    "cikm": (
        "CIKM",
        (
            "acm international conference on information and knowledge management",
            "conference on information and knowledge management",
            "information and knowledge management",
            "acm cikm",
            "cikm",
        ),
    ),
}


def _paper_year(paper: dict) -> int:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    for value in (paper.get("year"), metadata.get("year"), metadata.get("publication_year")):
        try:
            year = int(value or 0)
        except Exception:
            year = 0
        if 1900 <= year <= 2200:
            return year
    return 0


def _paper_venue_context(paper: dict) -> str:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    values = [
        paper.get("venue"),
        paper.get("source"),
        metadata.get("venue"),
        metadata.get("venue_id"),
        metadata.get("conference"),
        metadata.get("host_publication"),
        metadata.get("source_url"),
        metadata.get("publisher_url"),
    ]
    return " ".join(_clean_text(value) for value in values if value).lower()


def _infer_acm_venue_key(paper: dict) -> str:
    context = _paper_venue_context(paper)
    if "sigkdd" in context or re.search(r"\bkdd\b", context):
        return "kdd"
    if "the web conference" in context or "world wide web" in context or re.search(r"\bwww\b", context):
        return "www"
    if "sigir" in context or "information retrieval" in context:
        return "sigir"
    if "cikm" in context or "information and knowledge management" in context:
        return "cikm"
    return ""


def _acm_venue_markers_for_paper(paper: dict | None = None) -> list[tuple[str, tuple[str, ...]]]:
    if paper:
        key = _infer_acm_venue_key(paper)
        if key and key in ACM_VENUE_TEXT_MARKERS:
            label, markers = ACM_VENUE_TEXT_MARKERS[key]
            return [(label, markers)]
    return list(ACM_VENUE_TEXT_MARKERS.values())


def _year_markers(year: int) -> tuple[str, ...]:
    if not year:
        return ()
    short = str(year)[-2:]
    return (str(year), f"'{short}", f"’{short}")


def _acm_venue_evidence_from_text(text: str, paper: dict | None = None) -> dict[str, Any]:
    head = _clean_text(text[:12000]).lower()
    if not head:
        return {"verified": False, "venue": "", "year": 0}
    expected_year = _paper_year(paper) if paper else 0
    year_markers = _year_markers(expected_year)
    for label, markers in _acm_venue_markers_for_paper(paper):
        for marker in markers:
            marker_text = marker.lower()
            start = head.find(marker_text)
            while start >= 0:
                end = start + len(marker_text)
                window = head[max(0, start - 160): min(len(head), end + 160)]
                if not expected_year or any(year_marker.lower() in window for year_marker in year_markers):
                    return {
                        "verified": True,
                        "venue": f"{label} {expected_year}" if expected_year else label,
                        "year": expected_year,
                    }
                start = head.find(marker_text, end)
    return {"verified": False, "venue": "", "year": 0}


def _acm_venue_search_labels_for_paper(paper: dict) -> list[str]:
    year = _paper_year(paper)
    venue_key = _infer_acm_venue_key(paper)
    if not venue_key:
        return [f"ACM {year}"] if year else []
    labels: list[str] = []
    label, markers = ACM_VENUE_TEXT_MARKERS[venue_key]
    for label, markers in [(label, markers)]:
        candidates = [label]
        candidates.extend(marker.title() if marker.islower() else marker for marker in markers[:3])
        for candidate in candidates:
            value = _clean_text(candidate)
            if not value or len(value) > 80:
                continue
            if year:
                value = f"{value} {year}"
            if value not in labels:
                labels.append(value)
    if not labels and year:
        labels.append(f"ACM {year}")
    return labels[:4]


def _apply_acm_detail_cached_abstract(paper: dict, cached: dict) -> bool:
    abstract = str(cached.get("abstract") or "").strip()
    if not abstract:
        return False
    paper["abstract"] = abstract
    metadata = paper.setdefault("metadata", {})
    metadata["abstract_source"] = "acm_dl_detail_cache"
    metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "acm_dl_detail"
    metadata["abstract_enrichment_provenance"] = "Abstract extracted from ACM Digital Library detail HTML and reused from local cache."
    if cached.get("pdf_url") and not paper.get("pdf_url"):
        paper["pdf_url"] = str(cached.get("pdf_url") or "")
    if cached.get("url"):
        metadata["acm_detail_url"] = str(cached.get("url") or "")
    metadata["detail_source"] = "acm_dl"
    return True


def _openalex_author_text(item: dict) -> str:
    names: list[str] = []
    for authorship in item.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") if isinstance(authorship.get("author"), dict) else {}
        name = _clean_text(str(author.get("display_name") or ""))
        if name:
            names.append(name)
    return ", ".join(dict.fromkeys(names))


def _apply_openalex_acm_item(paper: dict, item: dict) -> bool:
    metadata = paper.setdefault("metadata", {})
    abstract = _openalex_abstract_from_inverted_index(item.get("abstract_inverted_index") or {})
    abstract_filled = False
    if abstract and not paper.get("abstract"):
        paper["abstract"] = abstract
        metadata["abstract_source"] = "openalex_doi_for_acm"
        abstract_filled = True
    if not paper.get("authors"):
        authors = _openalex_author_text(item)
        if authors:
            paper["authors"] = authors
            metadata["authors_source"] = "openalex_doi_for_acm"
    pdf_url = _openalex_pdf_url(item)
    if pdf_url and not paper.get("pdf_url"):
        paper["pdf_url"] = pdf_url
    landing_url = _openalex_landing_url(item)
    if landing_url and not paper.get("url"):
        paper["url"] = landing_url
    doi = _doi_from_url(str(item.get("doi") or ""))
    if doi:
        metadata["openalex_doi"] = doi
    metadata["openalex_id"] = item.get("id") or ""
    metadata["openalex_landing_url"] = landing_url
    metadata["openalex_pdf_url"] = pdf_url
    metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "openalex_doi_for_acm"
    metadata["abstract_enrichment_provenance"] = "ACM DOI exact match enriched from OpenAlex indexed metadata; not ACM DL HTML."
    metadata["openalex_acm_doi_enriched"] = bool(abstract or landing_url or pdf_url)
    return abstract_filled


def _apply_cached_acm_abstract_sources(papers: list[dict]) -> tuple[list[dict], dict[str, Any]]:
    """Reuse already verified local ACM DOI/title abstract caches before network fallback."""
    openalex_cache = _load_openalex_cache()
    openalex_oa_pdf_cache = _load_openalex_oa_pdf_abstract_cache()
    semantic_cache = _load_semantic_scholar_cache()
    acm_pdf_cache = _load_acm_pdf_abstract_cache()
    acm_detail_cache = _load_acm_detail_abstract_cache()
    chatpaper_cache = _load_chatpaper_abstract_cache()
    hal_cache = _load_hal_abstract_cache()
    public_pdf_search_cache = _load_public_pdf_search_cache()
    public_page_search_cache = _load_public_publication_page_search_cache()
    chatpaper_min_similarity = max(0.85, min(1.0, _positive_float_env("CHATPAPER_TITLE_MIN_SIMILARITY", 0.90)))
    stats: dict[str, Any] = {
        "source": "local_acm_abstract_caches",
        "attempted": 0,
        "skipped_existing_abstract": 0,
        "skipped_non_acm_doi": 0,
        "cache_hits": 0,
        "abstracts_filled": 0,
        "cache_hit_sources": {},
        "missing_after": 0,
    }

    def record_hit(source: str) -> None:
        stats["cache_hits"] = int(stats.get("cache_hits") or 0) + 1
        counts = stats.setdefault("cache_hit_sources", {})
        if isinstance(counts, dict):
            counts[source] = int(counts.get(source) or 0) + 1

    def apply_openalex_cache(paper: dict, cached: dict) -> bool:
        abstract = _clean_text(cached.get("abstract"))
        if not abstract:
            return False
        metadata = paper.setdefault("metadata", {})
        paper["abstract"] = abstract
        if cached.get("url"):
            paper["url"] = paper.get("url") or str(cached.get("url") or "")
        if cached.get("pdf_url") and not paper.get("pdf_url"):
            paper["pdf_url"] = str(cached.get("pdf_url") or "")
        if cached.get("openalex_id"):
            metadata["openalex_id"] = str(cached.get("openalex_id") or "")
        if cached.get("openalex_doi"):
            metadata["openalex_doi"] = str(cached.get("openalex_doi") or "")
        source = "openalex_title_for_acm" if cached.get("title_fallback_attempted") else "openalex_doi_for_acm"
        metadata["abstract_source"] = source
        metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or source
        metadata["abstract_enrichment_provenance"] = (
            "ACM DOI row enriched from cached OpenAlex indexed metadata; not ACM DL HTML."
        )
        return True

    def apply_semantic_cache(paper: dict, cached: dict) -> bool:
        abstract = _semantic_scholar_cache_real_abstract(cached)
        if not abstract:
            return False
        metadata = paper.setdefault("metadata", {})
        paper["abstract"] = abstract
        if cached.get("url"):
            paper["url"] = paper.get("url") or str(cached.get("url") or "")
        if cached.get("pdf_url") and not paper.get("pdf_url"):
            paper["pdf_url"] = str(cached.get("pdf_url") or "")
        if cached.get("externalIds"):
            metadata["semantic_scholar_external_ids"] = cached.get("externalIds")
        if cached.get("semantic_scholar_paper_id"):
            metadata["semantic_scholar_paper_id"] = cached.get("semantic_scholar_paper_id")
        cached_source = str(cached.get("source") or "")
        source = "semantic_scholar_title_for_acm"
        if cached_source in {"semantic_scholar_doi", "semantic_scholar_doi_for_acm"}:
            source = "semantic_scholar_doi_for_acm"
        metadata["abstract_source"] = source
        metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or source
        metadata["abstract_enrichment_provenance"] = (
            "ACM DOI row enriched from cached Semantic Scholar indexed metadata; not ACM DL HTML."
        )
        return True

    def apply_openalex_oa_pdf_cache(paper: dict, cached: dict) -> bool:
        abstract = _clean_text(cached.get("abstract"))
        if not abstract:
            return False
        doi = _paper_doi(paper)
        source_doi = _doi_from_url(str(cached.get("source_doi") or cached.get("openalex_doi") or ""))
        if doi and source_doi and doi != source_doi.lower():
            return False
        item_doi = _doi_from_url(str(cached.get("doi") or cached.get("doi_url") or ""))
        if item_doi and doi != item_doi.lower():
            return False
        title = _clean_text(paper.get("title"))
        cached_title = _clean_text(cached.get("title"))
        if not title or not cached_title or _title_token_similarity(title, cached_title) < 0.96:
            return False
        metadata = paper.setdefault("metadata", {})
        paper["abstract"] = abstract
        if cached.get("pdf_url") and not paper.get("pdf_url"):
            paper["pdf_url"] = str(cached.get("pdf_url") or "")
        metadata["abstract_source"] = "openalex_oa_pdf_for_acm"
        metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "openalex_oa_pdf_for_acm"
        metadata["abstract_enrichment_provenance"] = (
            "ACM DOI row enriched by extracting the abstract from an OpenAlex DOI-exact open-access PDF after strict title and DOI checks; not ACM DL HTML."
        )
        metadata["openalex_oa_pdf_url"] = cached.get("pdf_url") or ""
        metadata["openalex_oa_pdf_title_similarity"] = round(_title_token_similarity(title, cached_title), 4)
        return True

    def apply_public_pdf_search_cache(paper: dict, cached: dict) -> bool:
        abstract = _clean_text(cached.get("abstract"))
        if not abstract:
            return False
        doi = _paper_doi(paper)
        item_doi = _doi_from_url(str(cached.get("doi") or cached.get("doi_url") or ""))
        if item_doi and doi != item_doi.lower():
            return False
        if not item_doi and not bool(cached.get("venue_verified")):
            return False
        title = _clean_text(paper.get("title"))
        cached_title = _clean_text(cached.get("title"))
        similarity = _title_token_similarity(title, cached_title)
        if not title or not cached_title or similarity < 0.96:
            return False
        year = int(paper.get("year") or 0)
        item_year = int(cached.get("year") or 0)
        if not item_doi and year and item_year and item_year != year:
            return False
        metadata = paper.setdefault("metadata", {})
        paper["abstract"] = abstract
        if cached.get("pdf_url") and not paper.get("pdf_url"):
            paper["pdf_url"] = str(cached.get("pdf_url") or "")
        metadata["abstract_source"] = "public_pdf_search_for_acm"
        metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "public_pdf_search_for_acm"
        metadata["abstract_enrichment_provenance"] = (
            "ACM DOI row enriched from an automatically discovered public PDF after strict title match and DOI or venue evidence; search results were used only for URL discovery."
        )
        metadata["public_pdf_search_url"] = cached.get("pdf_url") or ""
        metadata["public_pdf_search_title_similarity"] = round(similarity, 4)
        return True

    def apply_public_page_search_cache(paper: dict, cached: dict) -> bool:
        abstract = _clean_text(cached.get("abstract"))
        if not abstract:
            return False
        doi = _paper_doi(paper)
        cached_doi = _doi_from_url(str(cached.get("doi") or cached.get("doi_url") or ""))
        if cached_doi and doi != cached_doi.lower():
            return False
        if not cached_doi and not bool(cached.get("venue_verified")):
            return False
        title = _clean_text(paper.get("title"))
        cached_title = _clean_text(cached.get("title"))
        similarity = _title_token_similarity(title, cached_title)
        if not title or not cached_title or similarity < 0.96:
            return False
        year = _paper_year(paper)
        cached_year = int(cached.get("year") or 0)
        if not cached_doi and year and cached_year and cached_year != year:
            return False
        metadata = paper.setdefault("metadata", {})
        paper["abstract"] = abstract
        metadata["abstract_source"] = "public_publication_page_for_acm"
        metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "public_publication_page_for_acm"
        metadata["abstract_enrichment_provenance"] = (
            "ACM DOI row enriched from an automatically discovered public publication page after strict title match and DOI or ACM venue/year evidence; search results were used only for URL discovery."
        )
        metadata["public_publication_page_url"] = cached.get("url") or ""
        metadata["public_publication_page_title_similarity"] = round(similarity, 4)
        return True

    def apply_chatpaper_cache(paper: dict, cached: dict) -> bool:
        abstract = _clean_text(cached.get("abstract"))
        if not abstract:
            return False
        title = _clean_text(paper.get("title"))
        cached_title = _clean_text(cached.get("title"))
        if title and cached_title and _title_token_similarity(title, cached_title) < chatpaper_min_similarity:
            return False
        label = _chatpaper_venue_label_for_paper(paper)
        try:
            year = int(paper.get("year") or 0)
        except Exception:
            year = 0
        if label and cached.get("venue") != label:
            return False
        if year and int(cached.get("year") or 0) != year:
            return False
        metadata = paper.setdefault("metadata", {})
        paper["abstract"] = abstract
        metadata["abstract_source"] = "chatpaper_title_for_acm"
        metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "chatpaper_title_for_acm"
        metadata["abstract_enrichment_provenance"] = (
            "ACM DOI row enriched from cached ChatPaper venue-indexed doc-abstract after strict title, venue, and year match; not ACM DL HTML."
        )
        metadata["chatpaper_url"] = cached.get("url") or ""
        return True

    for paper in papers:
        if not isinstance(paper, dict):
            continue
        if _clean_text(paper.get("abstract")):
            stats["skipped_existing_abstract"] = int(stats.get("skipped_existing_abstract") or 0) + 1
            continue
        doi = _paper_doi(paper)
        if not doi.startswith("10.1145/"):
            stats["skipped_non_acm_doi"] = int(stats.get("skipped_non_acm_doi") or 0) + 1
            continue
        stats["attempted"] = int(stats.get("attempted") or 0) + 1
        doi_key = f"doi:{doi}"
        applied_source = ""
        cached_detail = acm_detail_cache.get(doi_key)
        if isinstance(cached_detail, dict) and _apply_acm_detail_cached_abstract(paper, cached_detail):
            applied_source = "acm_dl_detail_cache"
        if not applied_source:
            cached_pdf = acm_pdf_cache.get(doi_key)
            if isinstance(cached_pdf, dict) and _clean_text(cached_pdf.get("abstract")):
                paper["abstract"] = _clean_text(cached_pdf.get("abstract"))
                if cached_pdf.get("pdf_url") and not paper.get("pdf_url"):
                    paper["pdf_url"] = str(cached_pdf.get("pdf_url") or "")
                metadata = paper.setdefault("metadata", {})
                metadata["abstract_source"] = "acm_dl_pdf"
                metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "acm_dl_pdf"
                metadata["abstract_enrichment_provenance"] = (
                    "Abstract extracted from cached ACM Digital Library PDF or ACM PDF gateway."
                )
                applied_source = "acm_dl_pdf"
        if not applied_source:
            cached_openalex = openalex_cache.get(doi_key)
            if isinstance(cached_openalex, dict) and apply_openalex_cache(paper, cached_openalex):
                applied_source = str(paper.get("metadata", {}).get("abstract_source") or "openalex_doi_for_acm")
        if not applied_source:
            cached_openalex_oa_pdf = openalex_oa_pdf_cache.get(doi_key)
            if isinstance(cached_openalex_oa_pdf, dict) and apply_openalex_oa_pdf_cache(paper, cached_openalex_oa_pdf):
                applied_source = "openalex_oa_pdf_for_acm"
        if not applied_source:
            cached_semantic = semantic_cache.get(doi_key)
            if isinstance(cached_semantic, dict) and apply_semantic_cache(paper, cached_semantic):
                applied_source = str(paper.get("metadata", {}).get("abstract_source") or "semantic_scholar_doi_for_acm")
        if not applied_source:
            title_key = _semantic_scholar_cache_key(paper.get("title", ""))
            chatpaper_titles = _chatpaper_cache_titles(chatpaper_cache)
            cached_chatpaper = chatpaper_titles.get(title_key)
            if not isinstance(cached_chatpaper, dict):
                label = _chatpaper_venue_label_for_paper(paper)
                try:
                    year = int(paper.get("year") or 0)
                except Exception:
                    year = 0
                scored_chatpaper = sorted(
                    (
                        (_title_token_similarity(paper.get("title"), item.get("title")), item)
                        for item in chatpaper_titles.values()
                        if isinstance(item, dict)
                        and item.get("abstract")
                        and (not label or item.get("venue") == label)
                        and (not year or int(item.get("year") or 0) == year)
                    ),
                    key=lambda row: row[0],
                    reverse=True,
                )
                if scored_chatpaper and scored_chatpaper[0][0] >= chatpaper_min_similarity:
                    cached_chatpaper = scored_chatpaper[0][1]
            if isinstance(cached_chatpaper, dict) and apply_chatpaper_cache(paper, cached_chatpaper):
                applied_source = "chatpaper_title_for_acm"
        if not applied_source:
            hal_titles = _hal_titles_cache(hal_cache)
            cached_hal = hal_titles.get(_semantic_scholar_cache_key(paper.get("title", "")))
            if isinstance(cached_hal, dict) and not cached_hal.get("miss") and _apply_hal_item(paper, cached_hal):
                applied_source = "hal_title_for_acm"
        if not applied_source:
            cached_public_page = public_page_search_cache.get(doi_key)
            if not isinstance(cached_public_page, dict):
                cached_public_page = public_page_search_cache.get(_semantic_scholar_cache_key(paper.get("title", "")))
            if isinstance(cached_public_page, dict) and not cached_public_page.get("miss") and apply_public_page_search_cache(paper, cached_public_page):
                applied_source = "public_publication_page_for_acm"
        if not applied_source:
            cached_public_pdf = public_pdf_search_cache.get(doi_key)
            if not isinstance(cached_public_pdf, dict):
                cached_public_pdf = public_pdf_search_cache.get(_semantic_scholar_cache_key(paper.get("title", "")))
            if isinstance(cached_public_pdf, dict) and not cached_public_pdf.get("miss") and apply_public_pdf_search_cache(paper, cached_public_pdf):
                applied_source = "public_pdf_search_for_acm"
        if applied_source:
            record_hit(applied_source)
            stats["abstracts_filled"] = int(stats.get("abstracts_filled") or 0) + 1

    stats["missing_after"] = sum(
        1
        for paper in papers
        if isinstance(paper, dict)
        and _paper_doi(paper).startswith("10.1145/")
        and not _clean_text(paper.get("abstract"))
    )
    _refresh_acm_doi_abstract_enrichment_audit(papers, stats)
    return papers, stats


def _chatpaper_request(url: str, timeout: int = 25) -> requests.Response:
    headers = dict(HEADERS)
    headers.setdefault("Referer", "https://chatpaper.com/venues")
    response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    return response


def _chatpaper_id_from_href(href: str) -> str:
    match = re.search(r"[?&]id=(\d+)", str(href or ""))
    return match.group(1) if match else ""


def _chatpaper_paper_id_from_href(href: str) -> str:
    match = re.search(r"/paper/(\d+)", str(href or ""))
    return match.group(1) if match else ""


def _chatpaper_venue_label_for_paper(paper: dict) -> str:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    venue_id = str(metadata.get("venue_id") or "").lower()
    venue = str(paper.get("venue") or metadata.get("venue") or "").lower()
    if "kdd" in venue_id or "sigkdd" in venue or re.search(r"\bkdd\b", venue):
        return "KDD"
    if "www" in venue_id or "the web conference" in venue or re.search(r"\bwww\b", venue):
        return "WWW"
    return ""


def _chatpaper_venue_years_for_papers(papers: list[dict]) -> list[tuple[str, int]]:
    pairs: list[tuple[str, int]] = []
    for paper in papers:
        if not isinstance(paper, dict) or _clean_text(paper.get("abstract")):
            continue
        if not _paper_doi(paper).startswith("10.1145/"):
            continue
        label = _chatpaper_venue_label_for_paper(paper)
        try:
            year = int(paper.get("year") or 0)
        except Exception:
            year = 0
        if label and year:
            pair = (label, year)
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def _chatpaper_cache_titles(cache: dict) -> dict:
    titles = cache.setdefault("titles", {})
    return titles if isinstance(titles, dict) else {}


def _chatpaper_cache_venues(cache: dict) -> dict:
    venues = cache.setdefault("venues", {})
    return venues if isinstance(venues, dict) else {}


def _discover_chatpaper_venue_ids(label: str, year: int, cache: dict) -> tuple[list[int], int]:
    cache_key = f"{label}:{year}"
    venues = _chatpaper_cache_venues(cache)
    cached = venues.get(cache_key)
    if isinstance(cached, dict) and cached.get("track_ids"):
        return [int(value) for value in cached.get("track_ids") or [] if str(value).isdigit()], int(cached.get("expected_count") or 0)

    root = _chatpaper_request("https://chatpaper.com/venues").text
    soup = BeautifulSoup(root, "html.parser")
    target_text = f"{label} {year}".lower()
    first_id = ""
    expected_count = 0
    for link in soup.find_all("a", href=True):
        text = _clean_text(link.get_text(" ", strip=True))
        if target_text not in text.lower():
            continue
        first_id = _chatpaper_id_from_href(str(link.get("href") or ""))
        count_match = re.search(r"\((\d+)\)", text)
        if count_match:
            expected_count = int(count_match.group(1))
        break
    if not first_id:
        venues[cache_key] = {
            "track_ids": [],
            "expected_count": 0,
            "miss": True,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        return [], 0

    page = _chatpaper_request(f"https://chatpaper.com/venues?id={first_id}&page=1")
    track_ids: list[int] = []
    final_id = _chatpaper_id_from_href(str(page.url))
    if final_id:
        track_ids.append(int(final_id))
    track_soup = BeautifulSoup(page.text, "html.parser")
    for link in track_soup.find_all("a", href=True):
        text = _clean_text(link.get_text(" ", strip=True))
        if "Papers" not in text:
            continue
        track_id = _chatpaper_id_from_href(str(link.get("href") or ""))
        if track_id and int(track_id) not in track_ids:
            track_ids.append(int(track_id))
    venues[cache_key] = {
        "track_ids": track_ids,
        "expected_count": expected_count,
        "miss": not bool(track_ids),
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    return track_ids, expected_count


def _chatpaper_paper_links_for_track(track_id: int, *, max_pages: int = 80) -> list[str]:
    links: list[str] = []
    seen_pages_without_new = 0
    for page_index in range(1, max_pages + 1):
        url = f"https://chatpaper.com/venues?id={track_id}&page={page_index}"
        try:
            html_text = _chatpaper_request(url).text
        except Exception:
            break
        found = list(dict.fromkeys(re.findall(r"/paper/\d+", html_text)))
        new_links = [link for link in found if link not in links]
        if not new_links:
            seen_pages_without_new += 1
            if seen_pages_without_new >= 1:
                break
        else:
            seen_pages_without_new = 0
            links.extend(new_links)
        time.sleep(_positive_float_env("CHATPAPER_REQUEST_SPACING_SEC", 0.15))
    return links


def _parse_chatpaper_paper_page(url: str, *, label: str, year: int) -> dict[str, Any]:
    try:
        html_text = _chatpaper_request(url).text
    except Exception as exc:
        return {"url": url, "error": str(exc)[:200], "miss": True}
    soup = BeautifulSoup(html_text, "html.parser")
    title_node = soup.select_one(".doc-name-main")
    abstract_node = soup.select_one("#abstract.doc-abstract") or soup.select_one(".doc-abstract")
    info_node = soup.select_one(".doc-info")
    title = _clean_chatpaper_title(title_node.get_text(" ", strip=True) if title_node else "")
    abstract = _clean_text(abstract_node.get_text(" ", strip=True) if abstract_node else "")
    info_text = _clean_text(info_node.get_text(" ", strip=True) if info_node else "")
    if len(abstract) < 80 or "Sign in for summary" in abstract or "Abstract is missing" in abstract:
        return {"url": url, "title": title, "miss": True, "miss_reason": "missing_or_short_doc_abstract"}
    if label and label.lower() not in info_text.lower():
        return {"url": url, "title": title, "miss": True, "miss_reason": "venue_label_mismatch", "info": info_text[:300]}
    if year and str(year) not in info_text:
        return {"url": url, "title": title, "miss": True, "miss_reason": "year_mismatch", "info": info_text[:300]}
    return {
        "url": url,
        "title": title,
        "abstract": abstract,
        "venue": label,
        "year": year,
        "source": "chatpaper_title_for_acm",
        "info": info_text[:500],
        "miss": False,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }


def _ensure_chatpaper_venue_cache(label: str, year: int, cache: dict) -> dict[str, Any]:
    cache_changed = False
    track_ids, expected_count = _discover_chatpaper_venue_ids(label, year, cache)
    titles = _chatpaper_cache_titles(cache)
    existing_for_venue = [
        item for item in titles.values()
        if isinstance(item, dict) and item.get("venue") == label and int(item.get("year") or 0) == int(year) and item.get("abstract")
    ]
    if expected_count and len(existing_for_venue) >= expected_count:
        return {"track_ids": track_ids, "expected_count": expected_count, "cached_count": len(existing_for_venue), "cache_changed": False}

    max_papers = _positive_int_env("CHATPAPER_MAX_PAPERS", 20000)
    paper_links: list[str] = []
    for track_id in track_ids:
        for link in _chatpaper_paper_links_for_track(track_id):
            if link not in paper_links:
                paper_links.append(link)
            if max_papers > 0 and len(paper_links) >= max_papers:
                break
        if max_papers > 0 and len(paper_links) >= max_papers:
            break
    fetched = 0
    for link in paper_links:
        url = f"https://chatpaper.com{link}"
        paper_id = _chatpaper_paper_id_from_href(link)
        if any(isinstance(item, dict) and item.get("chatpaper_id") == paper_id and item.get("abstract") for item in titles.values()):
            continue
        parsed = _parse_chatpaper_paper_page(url, label=label, year=year)
        fetched += 1
        title = _clean_text(parsed.get("title"))
        if title:
            key = _semantic_scholar_cache_key(title)
            parsed["chatpaper_id"] = paper_id
            titles[key] = parsed
            cache_changed = True
        time.sleep(_positive_float_env("CHATPAPER_REQUEST_SPACING_SEC", 0.15))
    return {
        "track_ids": track_ids,
        "expected_count": expected_count,
        "paper_links": len(paper_links),
        "fetched": fetched,
        "cached_count": sum(
            1 for item in titles.values()
            if isinstance(item, dict) and item.get("venue") == label and int(item.get("year") or 0) == int(year) and item.get("abstract")
        ),
        "cache_changed": cache_changed,
    }


def enrich_acm_doi_with_chatpaper(papers: list[dict], limit: int = 0) -> tuple[list[dict], dict[str, Any]]:
    targets = [
        paper for paper in papers
        if isinstance(paper, dict) and not _clean_text(paper.get("abstract")) and _paper_doi(paper).startswith("10.1145/")
    ]
    if limit and limit > 0:
        targets = targets[:limit]
    stats: dict[str, Any] = {
        "source": "chatpaper_title_for_acm",
        "attempted": len(targets),
        "enabled": True,
        "venue_cache": {},
        "cache_hits": 0,
        "abstracts_filled": 0,
        "mismatches": 0,
    }
    if not targets:
        return papers, stats
    cache = _load_chatpaper_abstract_cache()
    cache.setdefault("schema_version", 1)
    cache["query_version"] = PUBLIC_PDF_SEARCH_QUERY_VERSION
    cache_changed = False
    for label, year in _chatpaper_venue_years_for_papers(targets):
        venue_stats = _ensure_chatpaper_venue_cache(label, year, cache)
        cache_changed = cache_changed or bool(venue_stats.get("cache_changed"))
        stats["venue_cache"][f"{label}:{year}"] = venue_stats
    titles = _chatpaper_cache_titles(cache)
    min_similarity = max(0.85, min(1.0, _positive_float_env("CHATPAPER_TITLE_MIN_SIMILARITY", 0.90)))
    for paper in targets:
        title = _clean_text(paper.get("title"))
        if not title:
            continue
        cached = titles.get(_semantic_scholar_cache_key(title))
        if not isinstance(cached, dict):
            label = _chatpaper_venue_label_for_paper(paper)
            try:
                year = int(paper.get("year") or 0)
            except Exception:
                year = 0
            scored = sorted(
                (
                    (_title_token_similarity(title, item.get("title")), item)
                    for item in titles.values()
                    if isinstance(item, dict)
                    and item.get("abstract")
                    and (not label or item.get("venue") == label)
                    and (not year or int(item.get("year") or 0) == year)
                ),
                key=lambda row: row[0],
                reverse=True,
            )
            if scored and scored[0][0] >= min_similarity:
                cached = scored[0][1]
        if not isinstance(cached, dict) or not cached.get("abstract"):
            continue
        similarity = _title_token_similarity(title, cached.get("title"))
        if similarity < min_similarity:
            stats["mismatches"] = int(stats.get("mismatches") or 0) + 1
            continue
        label = _chatpaper_venue_label_for_paper(paper)
        try:
            year = int(paper.get("year") or 0)
        except Exception:
            year = 0
        if label and cached.get("venue") != label:
            stats["mismatches"] = int(stats.get("mismatches") or 0) + 1
            continue
        if year and int(cached.get("year") or 0) != year:
            stats["mismatches"] = int(stats.get("mismatches") or 0) + 1
            continue
        paper["abstract"] = str(cached.get("abstract") or "")
        metadata = paper.setdefault("metadata", {})
        metadata["abstract_source"] = "chatpaper_title_for_acm"
        metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "chatpaper_title_for_acm"
        metadata["abstract_enrichment_provenance"] = (
            "ACM DOI row enriched from ChatPaper venue-indexed doc-abstract after strict title, venue, and year match; not ACM DL HTML."
        )
        metadata["chatpaper_url"] = cached.get("url") or ""
        metadata["chatpaper_title_similarity"] = round(similarity, 4)
        stats["cache_hits"] = int(stats.get("cache_hits") or 0) + 1
        stats["abstracts_filled"] = int(stats.get("abstracts_filled") or 0) + 1
    if cache_changed:
        _save_chatpaper_abstract_cache(cache)
    _refresh_acm_doi_abstract_enrichment_audit(papers, stats)
    return papers, stats


def _pure_portal_urls_from_env() -> list[str]:
    raw = str(os.environ.get("ACM_PURE_PORTAL_URLS") or "").strip()
    if not raw:
        return []
    urls: list[str] = []
    for part in re.split(r"[\s,]+", raw):
        part = part.strip()
        if part.startswith("http") and part not in urls:
            urls.append(part)
    return urls


def _pure_portal_meta_values(soup: BeautifulSoup, name: str) -> list[str]:
    values: list[str] = []
    for node in soup.find_all("meta"):
        key = str(node.get("name") or node.get("property") or "")
        if key == name:
            value = _clean_text(node.get("content") or "")
            if value:
                values.append(value)
    return values


def _extract_pure_portal_abstract(text: str) -> str:
    marker = "\nAbstract\n"
    if marker not in text:
        return ""
    tail = text.split(marker, 1)[1]
    stops = [
        "\nOriginal language\n",
        "\nFingerprint\n",
        "\nKeywords\n",
        "\nTitle of host publication\n",
        "\nPublisher\n",
        "\nDOIs\n",
    ]
    stop_positions = [tail.find(stop) for stop in stops if tail.find(stop) >= 0]
    if stop_positions:
        tail = tail[:min(stop_positions)]
    abstract = _clean_text(tail)
    return abstract if len(abstract) >= 80 else ""


def _parse_pure_portal_publication(url: str, paper: dict | None = None) -> dict[str, Any]:
    try:
        response = _request(url, timeout=_metadata_timeout(25))
    except Exception as exc:
        return {"url": url, "miss": True, "error": str(exc)[:240], "updated_at": datetime.utcnow().isoformat() + "Z"}
    if response.status_code >= 400:
        return {"url": url, "miss": True, "error": f"http_{response.status_code}", "updated_at": datetime.utcnow().isoformat() + "Z"}
    if "turnstile" in response.text[:3000].lower() or "checking" in response.text[:3000].lower():
        return {"url": url, "miss": True, "error": "pure_portal_challenge_page", "retryable": True, "updated_at": datetime.utcnow().isoformat() + "Z"}
    soup = BeautifulSoup(response.text, "html.parser")
    title = (_pure_portal_meta_values(soup, "citation_title") or [""])[0]
    if not title:
        title_node = soup.find("h1")
        title = _clean_text(title_node.get_text(" ", strip=True) if title_node else "")
    doi = (_pure_portal_meta_values(soup, "citation_doi") or [""])[0]
    host_publication = (_pure_portal_meta_values(soup, "citation_inbook_title") or [""])[0]
    conference = (_pure_portal_meta_values(soup, "citation_conference_title") or [""])[0]
    publication_date = (_pure_portal_meta_values(soup, "citation_publication_date") or [""])[0]
    text = soup.get_text("\n", strip=True)
    abstract = _extract_pure_portal_abstract(text)
    venue_text = " ".join(part for part in [host_publication, conference] if part)
    if not abstract:
        return {"url": url, "title": title, "doi": doi, "miss": True, "miss_reason": "missing_abstract", "updated_at": datetime.utcnow().isoformat() + "Z"}
    venue_evidence = _acm_venue_evidence_from_text(venue_text, paper)
    if not venue_evidence.get("verified"):
        return {
            "url": url,
            "title": title,
            "doi": doi,
            "abstract": abstract,
            "miss": True,
            "miss_reason": "venue_mismatch",
            "host_publication": host_publication,
            "conference": conference,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    year = 0
    year_match = re.search(r"\b(20\d{2})\b", publication_date or venue_text)
    if year_match:
        year = int(year_match.group(1))
    return {
        "url": url,
        "title": title,
        "doi": _doi_from_url(doi) or doi,
        "abstract": abstract,
        "host_publication": host_publication,
        "conference": conference,
        "venue": venue_text or str(venue_evidence.get("venue") or ""),
        "year": year or int(venue_evidence.get("year") or 0),
        "source": "pure_portal_title_for_acm",
        "miss": False,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }


def _apply_pure_portal_item(paper: dict, item: dict, *, min_similarity: float = 0.96) -> bool:
    abstract = _clean_text(item.get("abstract"))
    if not abstract:
        return False
    doi = _paper_doi(paper)
    item_doi = _doi_from_url(str(item.get("doi") or item.get("doi_url") or ""))
    if doi and item_doi and doi != item_doi.lower():
        return False
    title = _clean_text(paper.get("title"))
    item_title = _clean_text(item.get("title"))
    similarity = _title_token_similarity(title, item_title)
    if title and item_title and similarity < min_similarity:
        return False
    year = int(paper.get("year") or 0)
    if year and int(item.get("year") or 0) not in {0, year}:
        return False
    venue_text = _clean_text(item.get("venue") or item.get("host_publication") or item.get("conference"))
    if venue_text and not _acm_venue_evidence_from_text(venue_text, paper).get("verified"):
        return False
    paper["abstract"] = abstract
    metadata = paper.setdefault("metadata", {})
    metadata["abstract_source"] = "pure_portal_title_for_acm"
    metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "pure_portal_title_for_acm"
    metadata["abstract_enrichment_provenance"] = (
        "ACM DOI row enriched from an institution Pure portal publication page after strict DOI, title, venue, and year match; not ACM DL HTML."
    )
    metadata["pure_portal_url"] = item.get("url") or ""
    metadata["pure_portal_title_similarity"] = round(similarity, 4)
    return True


def enrich_acm_doi_with_pure_portal(papers: list[dict], limit: int = 0) -> tuple[list[dict], dict[str, Any]]:
    targets = [
        paper for paper in papers
        if isinstance(paper, dict) and not _clean_text(paper.get("abstract")) and _paper_doi(paper).startswith("10.1145/")
    ]
    if limit and limit > 0:
        targets = targets[:limit]
    stats: dict[str, Any] = {
        "source": "pure_portal_title_for_acm",
        "enabled": True,
        "attempted": len(targets),
        "url_count": 0,
        "parsed": 0,
        "abstracts_filled": 0,
        "mismatches": 0,
    }
    if not targets:
        return papers, stats
    cache = _load_pure_portal_abstract_cache()
    cache.setdefault("schema_version", 1)
    cache_changed = False
    for url in _pure_portal_urls_from_env():
        if not isinstance(cache.get(f"url:{url}"), dict):
            parsed = _parse_pure_portal_publication(url)
            cache[f"url:{url}"] = parsed
            cache_changed = True
            stats["parsed"] = int(stats.get("parsed") or 0) + 1
            doi = _doi_from_url(str(parsed.get("doi") or ""))
            title = _clean_text(parsed.get("title"))
            if doi:
                cache[f"doi:{doi.lower()}"] = parsed
            if title:
                cache[_semantic_scholar_cache_key(title)] = parsed
        stats["url_count"] = int(stats.get("url_count") or 0) + 1
    before = sum(1 for paper in targets if _clean_text(paper.get("abstract")))
    for paper in targets:
        doi = _paper_doi(paper)
        item = cache.get(f"doi:{doi}") if doi else None
        if not isinstance(item, dict):
            item = cache.get(_semantic_scholar_cache_key(paper.get("title", "")))
        if not isinstance(item, dict) or item.get("miss"):
            continue
        if _apply_pure_portal_item(paper, item):
            stats["abstracts_filled"] = int(stats.get("abstracts_filled") or 0) + 1
        else:
            stats["mismatches"] = int(stats.get("mismatches") or 0) + 1
    if cache_changed:
        _save_pure_portal_abstract_cache(cache)
    stats["abstracts_filled"] = max(0, sum(1 for paper in targets if _clean_text(paper.get("abstract"))) - before)
    _refresh_acm_doi_abstract_enrichment_audit(papers, stats)
    return papers, stats


def _hal_titles_cache(cache: dict) -> dict:
    titles = cache.get("titles")
    if not isinstance(titles, dict):
        titles = {}
        cache["titles"] = titles
    return titles


def _hal_title_query(title: str) -> str:
    cleaned = _clean_text(title).replace("\\", " ").replace('"', " ")
    return f'title_t:"{cleaned}"'


def _hal_doc_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _hal_doc_text(doc: dict, field: str) -> str:
    values = _hal_doc_list(doc.get(field))
    return _clean_text(values[0]) if values else ""


def _hal_doc_years(doc: dict) -> set[int]:
    years: set[int] = set()
    for field in ("conferenceStartDateY_i", "conferenceEndDateY_i", "producedDateY_i", "publicationDateY_i"):
        for value in _hal_doc_list(doc.get(field)):
            try:
                year = int(value)
            except (TypeError, ValueError):
                continue
            if 1900 <= year <= 2100:
                years.add(year)
    return years


def _hal_doc_to_item(doc: dict, paper: dict | None = None) -> dict[str, Any]:
    title = _hal_doc_text(doc, "title_s")
    abstract = _hal_doc_text(doc, "abstract_s")
    conference = _hal_doc_text(doc, "conferenceTitle_s")
    doi = _hal_doc_text(doc, "doiId_s")
    years = sorted(_hal_doc_years(doc))
    expected_year = _paper_year(paper) if paper else 0
    year = expected_year if expected_year in years else (years[0] if years else 0)
    if not abstract or len(abstract) < 80:
        return {
            "title": title,
            "uri": doc.get("uri_s") or "",
            "miss": True,
            "miss_reason": "missing_or_short_abstract",
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    venue_evidence = _acm_venue_evidence_from_text(conference, paper)
    if not venue_evidence.get("verified"):
        return {
            "title": title,
            "abstract": abstract,
            "conference": conference,
            "year": year,
            "uri": doc.get("uri_s") or "",
            "miss": True,
            "miss_reason": "venue_mismatch",
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    if expected_year and expected_year not in years:
        return {
            "title": title,
            "abstract": abstract,
            "conference": conference,
            "year": year,
            "uri": doc.get("uri_s") or "",
            "miss": True,
            "miss_reason": "year_mismatch",
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    return {
        "title": title,
        "abstract": abstract,
        "doi": _doi_from_url(doi) or doi,
        "conference": conference,
        "venue": conference or str(venue_evidence.get("venue") or ""),
        "year": year or int(venue_evidence.get("year") or 0),
        "uri": doc.get("uri_s") or "",
        "source": "hal_title_for_acm",
        "miss": False,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }


def _apply_hal_item(paper: dict, item: dict, *, min_similarity: float = 0.96) -> bool:
    abstract = _clean_text(item.get("abstract"))
    if not abstract:
        return False
    title = _clean_text(paper.get("title"))
    item_title = _clean_text(item.get("title"))
    similarity = _title_token_similarity(title, item_title)
    if title and item_title and similarity < min_similarity:
        return False
    doi = _paper_doi(paper)
    item_doi = _doi_from_url(str(item.get("doi") or item.get("doi_url") or ""))
    if doi and item_doi and doi != item_doi.lower():
        return False
    year = int(paper.get("year") or 0)
    if year and int(item.get("year") or 0) not in {0, year}:
        return False
    venue_text = _clean_text(item.get("venue") or item.get("conference"))
    if venue_text and not _acm_venue_evidence_from_text(venue_text, paper).get("verified"):
        return False
    paper["abstract"] = abstract
    metadata = paper.setdefault("metadata", {})
    metadata["abstract_source"] = "hal_title_for_acm"
    metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "hal_title_for_acm"
    metadata["abstract_enrichment_provenance"] = (
        "ACM DOI row enriched from HAL structured metadata after strict title, venue, and year match; not ACM DL HTML."
    )
    metadata["hal_url"] = item.get("uri") or ""
    metadata["hal_title_similarity"] = round(similarity, 4)
    return True


def _fetch_hal_items_for_targets(targets: list[dict], stats: dict[str, Any]) -> list[tuple[dict, dict]]:
    if not targets:
        return []
    batch_size = max(1, _positive_int_env("ACM_HAL_BATCH_SIZE", 20))
    matched: list[tuple[dict, dict]] = []
    for offset in range(0, len(targets), batch_size):
        batch = targets[offset:offset + batch_size]
        query = " OR ".join(_hal_title_query(str(paper.get("title") or "")) for paper in batch)
        params = {
            "q": query,
            "fl": (
                "title_s,abstract_s,conferenceTitle_s,conferenceStartDateY_i,"
                "conferenceEndDateY_i,producedDateY_i,publicationDateY_i,doiId_s,uri_s,docid"
            ),
            "wt": "json",
            "rows": str(max(10, len(batch) * 4)),
        }
        url = "https://api.archives-ouvertes.fr/search/?" + urlencode(params)
        stats["requests"] = int(stats.get("requests") or 0) + 1
        try:
            payload = _request(url, timeout=_metadata_timeout(12)).json()
        except Exception as exc:
            stats.setdefault("errors", []).append(str(exc)[:240])
            continue
        docs = payload.get("response", {}).get("docs", [])
        if not isinstance(docs, list):
            continue
        stats["docs_returned"] = int(stats.get("docs_returned") or 0) + len(docs)
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            item_title = _clean_text(_hal_doc_text(doc, "title_s"))
            if not item_title:
                continue
            best = max(batch, key=lambda paper: _title_token_similarity(str(paper.get("title") or ""), item_title))
            similarity = _title_token_similarity(str(best.get("title") or ""), item_title)
            item = _hal_doc_to_item(doc, best)
            if similarity >= 0.96:
                matched.append((best, item))
    return matched


def enrich_acm_doi_with_hal(papers: list[dict], limit: int = 0) -> tuple[list[dict], dict[str, Any]]:
    targets = [
        paper for paper in papers
        if isinstance(paper, dict) and not _clean_text(paper.get("abstract")) and _paper_doi(paper).startswith("10.1145/")
    ]
    if limit and limit > 0:
        targets = targets[:limit]
    env_limit = _positive_int_env("ACM_HAL_MAX_ITEMS", 0)
    if env_limit > 0:
        targets = targets[:env_limit]
    stats: dict[str, Any] = {
        "source": "hal_title_for_acm",
        "enabled": True,
        "attempted": len(targets),
        "cache_hits": 0,
        "requests": 0,
        "docs_returned": 0,
        "abstracts_filled": 0,
        "mismatches": 0,
        "errors": [],
    }
    if not targets:
        return papers, stats
    cache = _load_hal_abstract_cache()
    cache.setdefault("schema_version", 1)
    titles_cache = _hal_titles_cache(cache)
    cache_changed = False
    uncached: list[dict] = []
    before = sum(1 for paper in targets if _clean_text(paper.get("abstract")))
    for paper in targets:
        key = _semantic_scholar_cache_key(str(paper.get("title") or ""))
        cached = titles_cache.get(key)
        if isinstance(cached, dict):
            if not cached.get("miss") and _apply_hal_item(paper, cached):
                stats["cache_hits"] = int(stats.get("cache_hits") or 0) + 1
            continue
        uncached.append(paper)
    if uncached:
        matched = _fetch_hal_items_for_targets(uncached, stats)
        matched_ids: set[int] = set()
        for paper, item in matched:
            key = _semantic_scholar_cache_key(str(paper.get("title") or ""))
            titles_cache[key] = item
            cache_changed = True
            matched_ids.add(id(paper))
            if item.get("miss"):
                continue
            if not _apply_hal_item(paper, item):
                stats["mismatches"] = int(stats.get("mismatches") or 0) + 1
        for paper in uncached:
            if id(paper) in matched_ids:
                continue
            key = _semantic_scholar_cache_key(str(paper.get("title") or ""))
            titles_cache[key] = {
                "title": paper.get("title") or "",
                "miss": True,
                "miss_reason": "not_found",
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
            cache_changed = True
    if cache_changed:
        _save_hal_abstract_cache(cache)
    stats["abstracts_filled"] = max(0, sum(1 for paper in targets if _clean_text(paper.get("abstract"))) - before)
    _refresh_acm_doi_abstract_enrichment_audit(papers, stats)
    return papers, stats


def _author_pdf_urls_from_env() -> list[str]:
    raw = str(os.environ.get("ACM_EXTERNAL_PDF_URLS") or os.environ.get("ACM_AUTHOR_PDF_URLS") or "").strip()
    if not raw:
        return []
    urls: list[str] = []
    for part in re.split(r"[\s,]+", raw):
        part = part.strip()
        if part.startswith("http") and part not in urls:
            urls.append(part)
    return urls


def _author_pdf_reader_url(url: str) -> str:
    return "https://r.jina.ai/http://" + url


def _extract_author_pdf_abstract(markdown: str) -> str:
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    match = re.search(r"(?im)(?:^|\n)#{0,3}\s*Abstract\s*\n+", text)
    if not match:
        return ""
    tail = text[match.end():]
    stops = [
        r"(?im)\n#{1,3}\s*CCS Concepts\b",
        r"(?im)\n#{1,3}\s*Keywords\b",
        r"(?im)\nACM Reference Format:",
        r"(?im)\n#{1,3}\s*1\s+Introduction\b",
        r"(?im)\n1\s+Introduction\b",
    ]
    positions: list[int] = []
    for pattern in stops:
        stop = re.search(pattern, tail)
        if stop:
            positions.append(stop.start())
    if positions:
        tail = tail[:min(positions)]
    abstract = _clean_text(re.sub(r"\s+", " ", tail))
    return abstract if len(abstract) >= 80 else ""


def _author_pdf_markdown_title(markdown: str) -> str:
    title_match = re.search(r"(?im)^Title:\s*(.+?)\s*$", markdown)
    if title_match:
        return _clean_text(title_match.group(1))
    heading_match = re.search(r"(?im)^#\s+(.+?)\s*$", markdown)
    if heading_match:
        return _clean_text(heading_match.group(1))
    return ""


def _author_pdf_venue_evidence(markdown: str, paper: dict | None = None) -> dict[str, Any]:
    return _acm_venue_evidence_from_text(markdown[:12000], paper)


def _author_pdf_venue_verified(markdown: str, paper: dict | None = None) -> bool:
    return bool(_author_pdf_venue_evidence(markdown, paper).get("verified"))


def _author_pdf_publication_year(text: str, venue_verified: bool, paper: dict | None = None) -> int:
    if venue_verified:
        evidence_year = int(_author_pdf_venue_evidence(text, paper).get("year") or 0)
        return evidence_year or _paper_year(paper or {})
    return 0


def _author_pdf_text_title(text: str, metadata_title: str = "") -> str:
    title = _clean_text(metadata_title)
    lowered_title = title.lower()
    if title and not any(token in lowered_title for token in ("microsoft word", "untitled", "acm template")):
        return title
    lines = [_clean_text(line) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line]
    title_lines: list[str] = []
    for line in lines[:30]:
        lowered = line.lower()
        if lowered == "abstract" or lowered.startswith("abstract "):
            break
        if any(token in lowered for token in ("@","university","institute","proceedings of","acm reference format","doi:", "http://", "https://")):
            if title_lines:
                break
            continue
        if len(line) <= 2:
            continue
        title_lines.append(line)
        if len(" ".join(title_lines)) >= 180 or len(title_lines) >= 4:
            break
    return _clean_text(" ".join(title_lines))


def _fetch_author_pdf_text(url: str, timeout: int, *, max_pages: int = 2) -> tuple[str, str, str]:
    if not url:
        return "", "", "missing_url"
    try:
        import fitz  # type: ignore
    except Exception:
        return "", "", "pymupdf_unavailable"
    try:
        response = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    except Exception as exc:
        return "", "", "request_error:" + str(exc)[:160]
    status = int(getattr(response, "status_code", 0) or 0)
    content_type = str(response.headers.get("content-type") or "").lower()
    data = response.content or b""
    if status != 200:
        return "", "", f"http_{status}:{content_type[:80]}"
    if not data.startswith(b"%PDF"):
        lowered = data[:2048].lower()
        if b"just a moment" in lowered or b"cf-mitigated" in lowered or b"cloudflare" in lowered:
            return "", "", f"challenge_non_pdf:{content_type[:80]}"
        return "", "", f"non_pdf:{content_type[:80]}"
    try:
        document = fitz.open(stream=data, filetype="pdf")
        try:
            metadata = document.metadata if isinstance(document.metadata, dict) else {}
            metadata_title = _clean_text(metadata.get("title") if isinstance(metadata, dict) else "")
            page_count = min(max_pages, len(document))
            text = "\n".join(document[index].get_text() for index in range(page_count))
            return text, metadata_title, ""
        finally:
            document.close()
    except Exception as exc:
        return "", "", "pdf_parse_error:" + str(exc)[:160]


def _parse_author_pdf_publication(url: str) -> dict[str, Any]:
    reader_url = _author_pdf_reader_url(url)
    timeout = _metadata_timeout(30)
    max_pages = _positive_int_env("ACM_EXTERNAL_PDF_MAX_PAGES", 2)
    pdf_text, metadata_title, pdf_error = _fetch_author_pdf_text(url, timeout, max_pages=max_pages)
    if pdf_text:
        title = _author_pdf_text_title(pdf_text, metadata_title)
        abstract = _extract_acm_pdf_abstract_from_text(pdf_text)
        doi_match = re.search(r"\b10\.1145/[0-9]+\.[0-9]+\b", pdf_text[:12000])
        venue_evidence = _author_pdf_venue_evidence(pdf_text)
        venue_verified = bool(venue_evidence.get("verified"))
        if abstract:
            return {
                "url": url,
                "reader_url": reader_url,
                "title": title,
                "doi": doi_match.group(0).lower() if doi_match else "",
                "abstract": abstract,
                "venue": str(venue_evidence.get("venue") or "") if venue_verified else "",
                "venue_verified": venue_verified,
                "year": _author_pdf_publication_year(pdf_text, venue_verified),
                "source": "external_pdf_title_for_acm",
                "extraction_method": "pymupdf",
                "miss": False,
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
    try:
        response = _request(reader_url, timeout=timeout)
    except Exception as exc:
        return {
            "url": url,
            "reader_url": reader_url,
            "miss": True,
            "error": (pdf_error or str(exc))[:240],
            "reader_error": str(exc)[:240],
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    text = response.text or ""
    if "Target URL returned error" in text[:1000] or "Just a moment" in text[:1000]:
        return {
            "url": url,
            "reader_url": reader_url,
            "miss": True,
            "error": "reader_target_error",
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    title = _author_pdf_markdown_title(text)
    abstract = _extract_author_pdf_abstract(text)
    doi_match = re.search(r"\b10\.1145/[0-9]+\.[0-9]+\b", text[:8000])
    venue_evidence = _author_pdf_venue_evidence(text)
    venue_verified = bool(venue_evidence.get("verified"))
    if not abstract:
        return {
            "url": url,
            "reader_url": reader_url,
            "title": title,
            "doi": doi_match.group(0).lower() if doi_match else "",
            "venue_verified": venue_verified,
            "miss": True,
            "miss_reason": "missing_or_short_abstract",
            "pdf_error": pdf_error,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    return {
        "url": url,
        "reader_url": reader_url,
        "title": title,
        "doi": doi_match.group(0).lower() if doi_match else "",
        "abstract": abstract,
        "venue": str(venue_evidence.get("venue") or "") if venue_verified else "",
        "venue_verified": venue_verified,
        "year": _author_pdf_publication_year(text, venue_verified),
        "source": "external_pdf_title_for_acm",
        "extraction_method": "jina_reader",
        "miss": False,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }


def _apply_author_pdf_item(paper: dict, item: dict, *, min_similarity: float = 0.96) -> bool:
    abstract = _clean_text(item.get("abstract"))
    if not abstract:
        return False
    title = _clean_text(paper.get("title"))
    item_title = _clean_text(item.get("title"))
    similarity = _title_token_similarity(title, item_title)
    if not title or not item_title or similarity < min_similarity:
        return False
    doi = _paper_doi(paper)
    item_doi = _doi_from_url(str(item.get("doi") or item.get("doi_url") or ""))
    if item_doi and doi != item_doi.lower():
        return False
    if not item_doi and not bool(item.get("venue_verified")):
        return False
    year = _paper_year(paper)
    item_year = int(item.get("year") or 0)
    if not item_doi and year and item_year and item_year != year:
        return False
    paper["abstract"] = abstract
    metadata = paper.setdefault("metadata", {})
    metadata["abstract_source"] = "external_pdf_title_for_acm"
    metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "external_pdf_title_for_acm"
    metadata["abstract_enrichment_provenance"] = (
        "ACM DOI row enriched from an explicit external PDF converted to text after strict title match and DOI or ACM venue/year evidence; not ACM DL HTML."
    )
    metadata["external_pdf_url"] = item.get("url") or ""
    metadata["external_pdf_reader_url"] = item.get("reader_url") or ""
    metadata["external_pdf_extraction_method"] = item.get("extraction_method") or ""
    metadata["external_pdf_title_similarity"] = round(similarity, 4)
    return True


def _apply_public_pdf_search_item(paper: dict, item: dict, *, min_similarity: float = 0.96) -> bool:
    abstract = _clean_text(item.get("abstract"))
    if not abstract:
        return False
    title = _clean_text(paper.get("title"))
    item_title = _clean_text(item.get("title"))
    similarity = _title_token_similarity(title, item_title)
    if not title or not item_title or similarity < min_similarity:
        return False
    doi = _paper_doi(paper)
    item_doi = _doi_from_url(str(item.get("doi") or item.get("doi_url") or ""))
    if item_doi and doi != item_doi.lower():
        return False
    if not item_doi and not bool(item.get("venue_verified")):
        return False
    year = _paper_year(paper)
    item_year = int(item.get("year") or 0)
    if not item_doi and year and item_year and item_year != year:
        return False
    paper["abstract"] = abstract
    pdf_url = item.get("pdf_url") or item.get("url") or ""
    if pdf_url and not paper.get("pdf_url"):
        paper["pdf_url"] = pdf_url
    metadata = paper.setdefault("metadata", {})
    metadata["abstract_source"] = "public_pdf_search_for_acm"
    metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "public_pdf_search_for_acm"
    metadata["abstract_enrichment_provenance"] = (
        "ACM DOI row enriched from an automatically discovered public PDF after strict title match and DOI or venue evidence; search results were used only for URL discovery."
    )
    metadata["public_pdf_search_url"] = pdf_url
    metadata["public_pdf_search_title_similarity"] = round(similarity, 4)
    return True


PUBLIC_PUBLICATION_PAGE_SEARCH_QUERY_VERSION = "v4_doi_candidate_discovery_and_citation_exports"


def _public_page_search_queries(paper: dict) -> list[str]:
    title = _clean_text(paper.get("title"))
    if not title:
        return []
    doi = _paper_doi(paper)
    labels = _acm_venue_search_labels_for_paper(paper)
    queries = [f"\"{title}\""]
    if doi:
        queries.extend([
            f"\"{title}\" \"{doi}\"",
            f"\"{doi}\"",
        ])
    queries.extend([
        f"\"{title}\" abstract",
        f"\"{title}\" publication",
    ])
    if doi:
        queries.extend([
            f"\"{doi}\" abstract",
            f"\"{doi}\" publication",
        ])
    for label in labels[:3]:
        queries.append(f"\"{title}\" \"{label}\"")
    unique: list[str] = []
    for query in queries:
        if query not in unique:
            unique.append(query)
    return unique


def _public_page_url_allowed(url: str) -> bool:
    if not _public_pdf_url_allowed(url):
        return False
    if _public_pdf_url_looks_like_pdf(url):
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    blocked = (
        "acm.org",
        "dblp.org",
        "crossref.org",
        "openalex.org",
        "unpaywall.org",
        "github.com",
        "raw.githubusercontent.com",
        "arxiv.org",
        "researchr.org",
        "eurekamag.com",
        "youtube.com",
    )
    if any(token in host for token in blocked):
        return False
    page_like_markers = (
        "/publications/",
        "/publication/",
        "/research/",
        "/outputs/",
        "/works/",
        "/handle/",
        "/items/",
        "/article/",
        "/paper/",
    )
    return any(marker in path for marker in page_like_markers) or any(
        token in host for token in ("pure.", "research.", "scholars.", "repository.", "openresearch.")
    )


def _doi_evidence_in_text(doi: object, text: object) -> bool:
    doi_text = _doi_from_url(str(doi or ""))
    if not doi_text:
        return False
    haystack = unquote(str(text or "")).lower()
    return doi_text in haystack or quote_plus(doi_text).lower() in haystack


def _public_page_candidate_urls_from_text(text: str, *, title: str = "", doi: str = "") -> list[str]:
    urls = _public_pdf_candidate_urls_from_text(text, title=title, doi=doi)
    return [url for url in urls if _public_page_url_allowed(url)]


def _meta_values(soup: BeautifulSoup, names: tuple[str, ...]) -> list[str]:
    wanted = {name.lower() for name in names}
    values: list[str] = []
    for node in soup.find_all("meta"):
        key = str(node.get("name") or node.get("property") or "").strip().lower()
        if key in wanted:
            value = _clean_text(node.get("content") or "")
            if value:
                values.append(value)
    return values


def _jsonld_abstract_values(soup: BeautifulSoup) -> list[str]:
    values: list[str] = []
    for node in soup.find_all("script", {"type": re.compile("ld\\+json", re.I)}):
        raw = node.string or node.get_text("", strip=False)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        stack = payload if isinstance(payload, list) else [payload]
        while stack:
            item = stack.pop(0)
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            for key in ("abstract",):
                value = item.get(key)
                if isinstance(value, str) and _clean_text(value):
                    values.append(_clean_text(value))
            for key in ("@graph", "mainEntity", "hasPart"):
                value = item.get(key)
                if isinstance(value, (list, dict)):
                    stack.append(value)
    return values


def _extract_public_page_abstract(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    bib_match = re.search(r"(?is)\babstract\s*=\s*[{\"']\s*(.+?)\s*[}\"']\s*[,}]", normalized)
    if bib_match:
        abstract = _clean_text(bib_match.group(1))
        if len(abstract) >= 80:
            return abstract
    ris_match = re.search(r"(?im)^(?:AB|N2)\s*-\s*(.+?)(?:\n[A-Z0-9]{2}\s*-|\Z)", normalized)
    if ris_match:
        abstract = _clean_text(ris_match.group(1))
        if len(abstract) >= 80:
            return abstract
    match = re.search(r"(?im)(?:^|\n)\s*(?:#{1,4}\s*)?Abstract\s*\n+", normalized)
    if not match:
        return ""
    tail = normalized[match.end():]
    stops = [
        r"(?im)\n\s*(?:#{1,4}\s*)?Keywords?\s*\n",
        r"(?im)\n\s*(?:#{1,4}\s*)?Fingerprint\s*\n",
        r"(?im)\n\s*(?:#{1,4}\s*)?Original language\s*\n",
        r"(?im)\n\s*(?:#{1,4}\s*)?Title of host publication\s*\n",
        r"(?im)\n\s*(?:#{1,4}\s*)?Publisher\s*\n",
        r"(?im)\n\s*(?:#{1,4}\s*)?DOIs?\s*\n",
        r"(?im)\n\s*(?:#{1,4}\s*)?Publication status\s*\n",
        r"(?im)\n\s*(?:#{1,4}\s*)?Access to Document\s*\n",
        r"(?im)\n\s*(?:#{1,4}\s*)?Cite this\s*\n",
        r"(?im)\n\s*(?:#{1,4}\s*)?References\s*\n",
    ]
    positions = [stop.start() for pattern in stops if (stop := re.search(pattern, tail))]
    if positions:
        tail = tail[:min(positions)]
    abstract = _clean_text(tail)
    return abstract if len(abstract) >= 80 else ""


def _public_page_citation_export_urls(soup: BeautifulSoup, base_url: str, *, max_links: int = 3) -> list[str]:
    urls: list[str] = []
    markers = (
        "bibtex",
        "endnote",
        "ris",
        "citation",
        "citationexport",
        "export",
        "download citation",
    )
    for node in soup.find_all(["a", "link"], href=True):
        href = urljoin(base_url, str(node.get("href") or ""))
        text = " ".join(
            str(value or "")
            for value in (
                node.get_text(" ", strip=True) if hasattr(node, "get_text") else "",
                node.get("href"),
                node.get("title"),
                node.get("type"),
                node.get("rel"),
                node.get("class"),
            )
        ).lower()
        if not any(marker in text for marker in markers):
            continue
        if not _public_pdf_url_allowed(href) or _public_pdf_url_looks_like_pdf(href):
            continue
        if href not in urls:
            urls.append(href)
        if len(urls) >= max_links:
            break
    return urls


def _citation_export_field(text: str, field: str) -> str:
    pattern = r"(?is)\b%s\s*=\s*[\{\"']\s*(.+?)\s*[\}\"']\s*[,}]" % re.escape(field)
    match = re.search(pattern, text)
    return _clean_text(match.group(1)) if match else ""


def _parse_public_page_citation_export_text(text: str, url: str = "") -> dict[str, Any]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    title = _citation_export_field(normalized, "title")
    doi = _citation_export_field(normalized, "doi")
    year = _citation_export_field(normalized, "year")
    if not title:
        match = re.search(r"(?im)^(?:TI|T1)\s*-\s*(.+)$", normalized)
        if match:
            title = _clean_text(match.group(1))
    if not doi:
        match = re.search(r"(?im)^DO\s*-\s*(.+)$", normalized)
        if match:
            doi = _clean_text(match.group(1))
    if not year:
        match = re.search(r"(?im)^(?:PY|Y1)\s*-\s*(20\d{2})", normalized)
        if match:
            year = match.group(1)
    abstract = _extract_public_page_abstract(normalized)
    return {
        "url": url,
        "title": title,
        "doi": _doi_from_url(doi) or doi,
        "year": int(year) if str(year).isdigit() else 0,
        "abstract": abstract,
    }


def _public_page_title(soup: BeautifulSoup) -> str:
    title = (_meta_values(soup, ("citation_title", "dc.title", "dcterms.title")) or [""])[0]
    if title:
        return title
    heading = soup.find("h1")
    if heading:
        return _clean_text(heading.get_text(" ", strip=True))
    if soup.title:
        return _clean_text(soup.title.get_text(" ", strip=True))
    return ""


def _parse_public_publication_page(url: str, paper: dict | None = None) -> dict[str, Any]:
    timeout = _positive_int_env("ACM_PUBLIC_PAGE_SEARCH_TIMEOUT_SEC", _metadata_timeout(12))
    try:
        response = _public_pdf_request_once(url, timeout=timeout)
    except Exception as exc:
        return {
            "url": url,
            "miss": True,
            "source": "public_publication_page_for_acm",
            "error": str(exc)[:200],
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    content_type = str(response.headers.get("content-type") or "").lower()
    text_head = (response.text or "")[:1000].lower()
    if "html" not in content_type and "<html" not in text_head:
        return {
            "url": url,
            "miss": True,
            "source": "public_publication_page_for_acm",
            "error": f"non_html:{content_type[:80]}",
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    if any(token in text_head for token in ("just a moment", "cf-mitigated", "cloudflare", "turnstile")):
        return {
            "url": url,
            "miss": True,
            "source": "public_publication_page_for_acm",
            "error": "challenge_page",
            "retryable": True,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    soup = BeautifulSoup(response.text or "", "html.parser")
    title = _public_page_title(soup)
    doi = (_meta_values(soup, ("citation_doi", "dc.identifier", "dcterms.identifier")) or [""])[0]
    venue_parts = _meta_values(soup, (
        "citation_conference_title",
        "citation_inbook_title",
        "citation_journal_title",
        "citation_publication_title",
    ))
    publication_date = (_meta_values(soup, ("citation_publication_date", "dc.date", "dcterms.date")) or [""])[0]
    abstract_values = _meta_values(soup, (
        "citation_abstract",
        "dc.description.abstract",
        "dcterms.abstract",
    ))
    abstract = ""
    for value in abstract_values + _jsonld_abstract_values(soup):
        value = _clean_text(value)
        if len(value) >= 80:
            abstract = value
            break
    page_text = soup.get_text("\n", strip=True)
    if not abstract:
        abstract = _extract_public_page_abstract(page_text)
    citation_export_url = ""
    if not abstract:
        export_limit = _positive_int_env("ACM_PUBLIC_PAGE_CITATION_EXPORT_LINKS", 3)
        for export_url in _public_page_citation_export_urls(soup, url, max_links=export_limit):
            try:
                export_response = _public_pdf_request_once(export_url, timeout=timeout)
            except Exception:
                continue
            export_item = _parse_public_page_citation_export_text(export_response.text or "", export_url)
            if not export_item.get("abstract"):
                continue
            abstract = str(export_item.get("abstract") or "")
            if not title and export_item.get("title"):
                title = str(export_item.get("title") or "")
            if not _doi_from_url(doi) and export_item.get("doi"):
                doi = str(export_item.get("doi") or "")
            if not publication_date and export_item.get("year"):
                publication_date = str(export_item.get("year") or "")
            citation_export_url = export_url
            break
    if not abstract:
        return {
            "url": url,
            "title": title,
            "doi": _doi_from_url(doi) or doi,
            "miss": True,
            "source": "public_publication_page_for_acm",
            "miss_reason": "missing_structured_or_body_abstract",
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    venue_text = " ".join(venue_parts + [page_text[:3000]])
    venue_evidence = _acm_venue_evidence_from_text(venue_text, paper)
    year = 0
    year_match = re.search(r"\b(20\d{2})\b", publication_date or str(venue_evidence.get("venue") or ""))
    if year_match:
        year = int(year_match.group(1))
    return {
        "url": url,
        "title": title,
        "doi": _doi_from_url(doi) or doi,
        "abstract": abstract,
        "venue": str(venue_evidence.get("venue") or ""),
        "venue_verified": bool(venue_evidence.get("verified")),
        "year": year or int(venue_evidence.get("year") or 0),
        "source": "public_publication_page_for_acm",
        "citation_export_url": citation_export_url,
        "miss": False,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }


def _apply_public_publication_page_item(paper: dict, item: dict, *, min_similarity: float = 0.96) -> bool:
    abstract = _clean_text(item.get("abstract"))
    if not abstract:
        return False
    title = _clean_text(paper.get("title"))
    item_title = _clean_text(item.get("title"))
    similarity = _title_token_similarity(title, item_title)
    if not title or not item_title or similarity < min_similarity:
        return False
    doi = _paper_doi(paper)
    item_doi = _doi_from_url(str(item.get("doi") or item.get("doi_url") or ""))
    if item_doi and doi != item_doi.lower():
        return False
    if not item_doi and not bool(item.get("venue_verified")):
        return False
    year = _paper_year(paper)
    item_year = int(item.get("year") or 0)
    if not item_doi and year and item_year and item_year != year:
        return False
    paper["abstract"] = abstract
    metadata = paper.setdefault("metadata", {})
    metadata["abstract_source"] = "public_publication_page_for_acm"
    metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "public_publication_page_for_acm"
    metadata["abstract_enrichment_provenance"] = (
        "ACM DOI row enriched from an automatically discovered public publication page after strict title match and DOI or ACM venue/year evidence; search results were used only for URL discovery."
    )
    metadata["public_publication_page_url"] = item.get("url") or ""
    if item.get("citation_export_url"):
        metadata["public_publication_page_citation_export_url"] = item.get("citation_export_url") or ""
    metadata["public_publication_page_title_similarity"] = round(similarity, 4)
    return True


def enrich_acm_doi_with_public_publication_page_search(papers: list[dict], limit: int = 0) -> tuple[list[dict], dict[str, Any]]:
    targets = [
        paper for paper in papers
        if isinstance(paper, dict) and not _clean_text(paper.get("abstract")) and _paper_doi(paper).startswith("10.1145/")
    ]
    if limit and limit > 0:
        targets = targets[:limit]
    stats: dict[str, Any] = {
        "source": "public_publication_page_for_acm",
        "enabled": True,
        "attempted": len(targets),
        "search_requests": 0,
        "candidate_urls": 0,
        "page_requests": 0,
        "cache_hits": 0,
        "abstracts_filled": 0,
        "mismatches": 0,
        "misses": 0,
        "errors": [],
    }
    if not targets:
        return papers, stats
    cache = _load_public_publication_page_search_cache()
    cache.setdefault("schema_version", 1)
    cache_changed = False
    retry_misses = str(os.environ.get("ACM_PUBLIC_PAGE_SEARCH_RETRY_MISSES") or "").strip().lower() in {"1", "true", "yes", "on"}
    timeout = _positive_int_env("ACM_PUBLIC_PAGE_SEARCH_TIMEOUT_SEC", _metadata_timeout(12))
    query_limit = _positive_int_env("ACM_PUBLIC_PAGE_SEARCH_QUERY_LIMIT", 3)
    max_results = _positive_int_env("ACM_PUBLIC_PAGE_SEARCH_MAX_RESULTS", 8)
    max_page_requests_per_paper = _positive_int_env("ACM_PUBLIC_PAGE_SEARCH_MAX_PAGE_REQUESTS_PER_PAPER", 4)
    max_seconds_per_paper = _positive_float_env("ACM_PUBLIC_PAGE_SEARCH_MAX_SECONDS_PER_PAPER", 25.0)
    spacing = _positive_float_env("ACM_PUBLIC_PAGE_SEARCH_SPACING_SEC", 0.2)
    for paper in targets:
        started = time.time()
        doi = _paper_doi(paper)
        doi_key = f"doi:{doi}"
        title_key = _semantic_scholar_cache_key(paper.get("title", ""))
        cached = cache.get(doi_key)
        if not isinstance(cached, dict):
            cached = cache.get(title_key)
        if isinstance(cached, dict):
            if cached.get("abstract") and _apply_public_publication_page_item(paper, cached):
                stats["cache_hits"] += 1
                stats["abstracts_filled"] += 1
                continue
            cached_version = str(cached.get("query_version") or "")
            if cached.get("miss") and cached_version == PUBLIC_PUBLICATION_PAGE_SEARCH_QUERY_VERSION and not retry_misses and not cached.get("retryable"):
                stats["cache_hits"] += 1
                continue
        candidate_urls: list[str] = []
        search_errors: list[str] = []
        for query in _public_page_search_queries(paper)[:query_limit]:
            if max_seconds_per_paper > 0 and time.time() - started >= max_seconds_per_paper:
                search_errors.append("paper_time_budget_exceeded_before_search")
                break
            for reader_url in _public_search_reader_urls(query):
                if max_seconds_per_paper > 0 and time.time() - started >= max_seconds_per_paper:
                    search_errors.append("paper_time_budget_exceeded_before_search_reader")
                    break
                try:
                    stats["search_requests"] += 1
                    response = _request(reader_url, timeout=timeout)
                    for url in _public_page_candidate_urls_from_text(response.text or "", title=_clean_text(paper.get("title")), doi=doi):
                        if url not in candidate_urls:
                            candidate_urls.append(url)
                        if len(candidate_urls) >= max_results:
                            break
                except Exception as exc:
                    search_errors.append(str(exc)[:180])
                if len(candidate_urls) >= max_results:
                    break
            if len(candidate_urls) >= max_results:
                break
            if spacing > 0:
                time.sleep(spacing)
        candidate_urls = _public_pdf_sort_candidate_urls(candidate_urls, paper)[:max_results]
        stats["candidate_urls"] += len(candidate_urls)
        search_incomplete = (
            len(_public_page_search_queries(paper)) > query_limit
            or any("time_budget_exceeded" in str(error) for error in search_errors)
        )
        accepted: dict[str, Any] = {}
        candidate_errors: list[str] = []
        page_requests = 0
        for url in candidate_urls:
            if max_page_requests_per_paper > 0 and page_requests >= max_page_requests_per_paper:
                candidate_errors.append("paper_page_request_budget_exceeded")
                break
            if max_seconds_per_paper > 0 and time.time() - started >= max_seconds_per_paper:
                candidate_errors.append("paper_time_budget_exceeded_before_page")
                break
            stats["page_requests"] += 1
            page_requests += 1
            parsed = _parse_public_publication_page(url, paper)
            if parsed.get("miss"):
                candidate_errors.append(f"{url}:{parsed.get('miss_reason') or parsed.get('error') or 'miss'}")
            elif _apply_public_publication_page_item(paper, parsed):
                accepted = parsed
                break
            else:
                stats["mismatches"] += 1
                candidate_errors.append(f"{url}:identity_mismatch")
            if spacing > 0:
                time.sleep(spacing)
        if accepted:
            cache_item = dict(accepted)
            cache_item["miss"] = False
            cache_item["source"] = "public_publication_page_for_acm"
            cache_item["query_version"] = PUBLIC_PUBLICATION_PAGE_SEARCH_QUERY_VERSION
            cache_item["updated_at"] = datetime.utcnow().isoformat() + "Z"
            cache[doi_key] = cache_item
            cache[title_key] = cache_item
            stats["abstracts_filled"] += 1
            cache_changed = True
        else:
            retryable = (
                not candidate_urls
                or search_incomplete
                or any("time_budget_exceeded" in str(error) for error in candidate_errors + search_errors)
                or any(_acm_pdf_error_retryable([error]) for error in candidate_errors + search_errors)
            )
            cache[doi_key] = {
                "title": paper.get("title", ""),
                "source": "public_publication_page_for_acm",
                "query_version": PUBLIC_PUBLICATION_PAGE_SEARCH_QUERY_VERSION,
                "miss": True,
                "miss_reason": "no_verified_public_publication_page_abstract" if candidate_urls else "no_candidate_public_publication_page_under_budget",
                "candidate_count": len(candidate_urls),
                "errors": (candidate_errors + search_errors)[:6],
                "retryable": retryable,
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
            stats["misses"] += 1
            if candidate_errors or search_errors:
                stats["errors"].append({"doi": doi, "errors": (candidate_errors + search_errors)[:4]})
            cache_changed = True
    if cache_changed:
        _save_public_publication_page_search_cache(cache)
    _refresh_acm_doi_abstract_enrichment_audit(papers, stats)
    return papers, stats


PUBLIC_PDF_SEARCH_QUERY_VERSION = "v11_doi_candidate_discovery"


def _public_search_reader_urls(query: str) -> list[str]:
    encoded = quote_plus(query)
    templates = {
        "duckduckgo": "https://r.jina.ai/http://https://duckduckgo.com/html/?q={query}",
        "yahoo": "https://r.jina.ai/http://https://search.yahoo.com/search?p={query}",
        "bing": "https://r.jina.ai/http://https://www.bing.com/search?q={query}",
        "ecosia": "https://r.jina.ai/http://https://www.ecosia.org/search?q={query}",
        "qwant": "https://r.jina.ai/http://https://www.qwant.com/?q={query}&t=web",
        "brave": "https://r.jina.ai/http://https://search.brave.com/search?q={query}",
        "mojeek": "https://r.jina.ai/http://https://www.mojeek.com/search?q={query}",
    }
    raw = str(os.environ.get("ACM_PUBLIC_SEARCH_ENGINES") or "duckduckgo,yahoo").strip()
    names = [part.strip().lower() for part in re.split(r"[\s,]+", raw) if part.strip()]
    urls: list[str] = []
    for name in names:
        template = templates.get(name)
        if not template:
            continue
        url = template.format(query=encoded)
        if url not in urls:
            urls.append(url)
    if not urls:
        urls.append(templates["duckduckgo"].format(query=encoded))
    return urls


def _public_pdf_search_reader_url(query: str) -> str:
    return _public_search_reader_urls(query)[0]


def _public_pdf_request_once(url: str, timeout: int) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    return response


def _public_pdf_decode_search_url(url: str) -> str:
    text = _clean_text(url).strip("()[]{}<>'\"")
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except Exception:
        return text
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        values = parse_qs(parsed.query).get("uddg") or []
        if values:
            text = unquote(values[0]).strip()
    elif parsed.netloc.endswith("r.search.yahoo.com"):
        match = re.search(r"/RU=([^/]+)", parsed.path)
        if match:
            text = unquote(match.group(1)).strip()
    elif parsed.netloc.endswith("bing.com"):
        values = parse_qs(parsed.query).get("u") or parse_qs(parsed.query).get("url") or []
        if values and str(values[0]).startswith(("http://", "https://")):
            text = unquote(values[0]).strip()
    return _public_pdf_canonicalize_url(text)


def _public_pdf_canonicalize_url(url: str) -> str:
    text = _clean_text(url).strip("()[]{}<>'\"")
    if not text:
        return ""
    text = re.sub(r"[),.;]+$", "", text)
    try:
        parsed = urlparse(text)
    except Exception:
        return text
    host = parsed.netloc.lower()
    if host == "arxiv.org" and parsed.path.startswith("/abs/"):
        arxiv_id = parsed.path.split("/abs/", 1)[1].strip("/")
        if arxiv_id:
            return f"https://arxiv.org/pdf/{arxiv_id}"
    if host == "github.com":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 5 and parts[2] in {"blob", "raw"}:
            owner, repo, _kind, branch = parts[:4]
            rest = "/".join(parts[4:])
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{rest}"
    return text


def _public_pdf_url_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower()
    blocked_hosts = (
        "duckduckgo.com",
        "search.yahoo.com",
        "r.search.yahoo.com",
        "yahoo.com",
        "google.",
        "bing.com",
        "ecosia.org",
        "qwant.com",
        "startpage.com",
        "search.brave.com",
        "mojeek.com",
        "doi.org",
        "dl.acm.org",
        "openreview.net",
        "semanticscholar.org",
        "researchgate.net",
        "sci-hub",
        "papers.nips.cc",
        "nips.cc",
        "neurips.cc",
        "usenix.org",
        "ieee.org",
        "computer.org",
        "springer.com",
        "biorxiv.org",
        "rfc-editor.org",
        "facebook.com",
        "x.com",
        "twitter.com",
    )
    return bool(host) and not any(token in host for token in blocked_hosts)


def _public_pdf_url_looks_like_pdf(url: str) -> bool:
    lowered = url.lower()
    if ".pdf" in lowered or "/pdf/" in lowered or "arxiv.org/pdf" in lowered:
        return True
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "/bitstream" in path or "/bitstreams/" in path:
        return True
    if path.endswith("/content") and any(token in host for token in ("repository", "openresearch", "scholar")):
        return True
    if path.endswith("/download") and any(token in host for token in ("repository", "pure.", "research.", "scholar")):
        return True
    return False


def _title_token_coverage(title: object, text: object) -> float:
    title_tokens = set(_title_key(str(title or "")).split())
    text_tokens = set(_title_key(str(text or "")).split())
    if not title_tokens or not text_tokens:
        return 0.0
    return len(title_tokens & text_tokens) / max(1, len(title_tokens))


def _public_pdf_url_title_coverage(url: str, title: str) -> float:
    try:
        parsed = urlparse(url)
        text = unquote(f"{parsed.netloc} {parsed.path}")
    except Exception:
        text = url
    text = re.sub(r"[^A-Za-z0-9]+", " ", text)
    return _title_token_coverage(title, text)


def _public_pdf_search_queries(paper: dict) -> list[str]:
    title = _clean_text(paper.get("title"))
    if not title:
        return []
    doi = _paper_doi(paper)
    labels = _acm_venue_search_labels_for_paper(paper)
    queries = [f"\"{title}\""]
    if doi:
        queries.extend([
            f"\"{title}\" \"{doi}\" pdf",
            f"\"{doi}\" pdf",
        ])
    for label in labels[:2]:
        queries.append(f"\"{title}\" \"{label}\"")
    queries.extend([
        f"\"{title}\" pdf",
    ])
    if doi:
        queries.extend([
            f"\"{doi}\" filetype:pdf",
            f"\"{doi}\"",
        ])
    for label in labels:
        queries.append(f"\"{title}\" \"{label}\" pdf")
    queries.extend([
        f"{title} pdf",
        f"{title} filetype:pdf",
        f"\"{title}\" filetype:pdf",
    ])
    unique: list[str] = []
    for query in queries:
        if query not in unique:
            unique.append(query)
    return unique


def _public_pdf_candidate_urls_from_text(text: str, *, title: str = "", doi: str = "") -> list[str]:
    urls: list[str] = []
    title = _clean_text(title)
    doi = _paper_doi({"doi": doi}) if doi else ""
    markdown_pattern = r"\[([^\]]{0,300})\]\((https?://[^)\s]+)\)"
    for match in re.finditer(markdown_pattern, text):
        url = _public_pdf_decode_search_url(match.group(2))
        if not url or not _public_pdf_url_allowed(url):
            continue
        if title:
            block = text[max(0, match.start() - 300): min(len(text), match.end() + 500)]
            link_text = _clean_text(match.group(1))
            block_coverage = max(
                _title_token_coverage(title, link_text),
                _title_token_coverage(title, block),
                _public_pdf_url_title_coverage(url, title),
            )
            if block_coverage < 0.45 and not _doi_evidence_in_text(doi, f"{url} {block}"):
                continue
        if url not in urls:
            urls.append(url)
    for match in re.finditer(r"https?://[^\s\]<>\"']+", text):
        url = _public_pdf_decode_search_url(match.group(0))
        if title and _public_pdf_url_title_coverage(url, title) < 0.35 and not _doi_evidence_in_text(doi, url):
            continue
        if url and _public_pdf_url_allowed(url) and url not in urls:
            urls.append(url)
    return urls


def _public_pdf_sort_candidate_urls(urls: list[str], paper: dict) -> list[str]:
    title = _clean_text(paper.get("title"))
    doi = _paper_doi(paper)

    def score(url: str) -> tuple[float, int, str]:
        coverage = _public_pdf_url_title_coverage(url, title) if title else 0.0
        doi_bonus = 0.35 if _doi_evidence_in_text(doi, url) else 0.0
        pdf_bonus = 0.25 if _public_pdf_url_looks_like_pdf(url) else 0.0
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            host = ""
        personal_or_institutional_bonus = 0.15 if any(
            token in host for token in (".edu", ".ac.", ".edu.", "github.io", "githubusercontent.com")
        ) else 0.0
        return (coverage + doi_bonus + pdf_bonus + personal_or_institutional_bonus, -len(url), url)

    return sorted(urls, key=score, reverse=True)


def _public_pdf_links_from_page(url: str, timeout: int, *, title: str = "", max_links: int = 6) -> list[str]:
    try:
        response = _public_pdf_request_once(url, timeout=timeout)
    except Exception:
        return []
    content_type = str(response.headers.get("content-type") or "").lower()
    if "html" not in content_type and "<html" not in (response.text or "")[:1000].lower():
        return []
    soup = BeautifulSoup(response.text or "", "html.parser")
    page_text = soup.get_text(" ", strip=True)
    if title and _title_token_coverage(title, page_text) < 0.80:
        return []
    links: list[str] = []
    for node in soup.find_all("a", href=True):
        href = urljoin(url, str(node.get("href") or ""))
        href = _public_pdf_decode_search_url(href)
        if _public_pdf_url_allowed(href) and _public_pdf_url_looks_like_pdf(href) and href not in links:
            links.append(href)
        if len(links) >= max_links:
            break
    return links


def _parse_public_pdf_search_publication(url: str, paper: dict | None = None) -> dict[str, Any]:
    timeout = _positive_int_env("ACM_PUBLIC_PDF_SEARCH_PDF_TIMEOUT_SEC", _metadata_timeout(16))
    max_pages = _positive_int_env("ACM_PUBLIC_PDF_SEARCH_PDF_MAX_PAGES", 2)
    pdf_text, metadata_title, error = _fetch_author_pdf_text(url, timeout, max_pages=max_pages)
    if not pdf_text:
        return {
            "url": url,
            "pdf_url": url,
            "miss": True,
            "source": "public_pdf_search_for_acm",
            "error": error or "pdf_text_unavailable",
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    title = _author_pdf_text_title(pdf_text, metadata_title)
    abstract = _extract_acm_pdf_abstract_from_text(pdf_text)
    doi_match = re.search(r"\b10\.1145/[0-9]+\.[0-9]+\b", pdf_text[:12000])
    venue_evidence = _author_pdf_venue_evidence(pdf_text, paper)
    venue_verified = bool(venue_evidence.get("verified"))
    if not abstract:
        return {
            "url": url,
            "pdf_url": url,
            "title": title,
            "doi": doi_match.group(0).lower() if doi_match else "",
            "venue": str(venue_evidence.get("venue") or "") if venue_verified else "",
            "venue_verified": venue_verified,
            "miss": True,
            "source": "public_pdf_search_for_acm",
            "miss_reason": "missing_or_short_abstract",
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    return {
        "url": url,
        "pdf_url": url,
        "title": title,
        "doi": doi_match.group(0).lower() if doi_match else "",
        "abstract": abstract,
        "venue": str(venue_evidence.get("venue") or "") if venue_verified else "",
        "venue_verified": venue_verified,
        "year": _author_pdf_publication_year(pdf_text, venue_verified, paper),
        "source": "public_pdf_search_for_acm",
        "miss": False,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }


def enrich_acm_doi_with_author_pdf(papers: list[dict], limit: int = 0) -> tuple[list[dict], dict[str, Any]]:
    targets = [
        paper for paper in papers
        if isinstance(paper, dict) and not _clean_text(paper.get("abstract")) and _paper_doi(paper).startswith("10.1145/")
    ]
    if limit and limit > 0:
        targets = targets[:limit]
    stats: dict[str, Any] = {
        "source": "external_pdf_title_for_acm",
        "enabled": True,
        "attempted": len(targets),
        "url_count": 0,
        "parsed": 0,
        "cache_hits": 0,
        "abstracts_filled": 0,
        "mismatches": 0,
    }
    if not targets:
        return papers, stats
    cache = _load_author_pdf_abstract_cache()
    cache.setdefault("schema_version", 1)
    cache_changed = False
    for url in _author_pdf_urls_from_env():
        url_key = f"url:{url}"
        if not isinstance(cache.get(url_key), dict):
            parsed = _parse_author_pdf_publication(url)
            cache[url_key] = parsed
            cache_changed = True
            stats["parsed"] = int(stats.get("parsed") or 0) + 1
            doi = _doi_from_url(str(parsed.get("doi") or ""))
            title = _clean_text(parsed.get("title"))
            if doi:
                cache[f"doi:{doi.lower()}"] = parsed
            if title:
                cache[_semantic_scholar_cache_key(title)] = parsed
        stats["url_count"] = int(stats.get("url_count") or 0) + 1
    before = sum(1 for paper in targets if _clean_text(paper.get("abstract")))
    for paper in targets:
        doi = _paper_doi(paper)
        item = cache.get(f"doi:{doi}") if doi else None
        if not isinstance(item, dict):
            item = cache.get(_semantic_scholar_cache_key(paper.get("title", "")))
        if not isinstance(item, dict) or item.get("miss"):
            continue
        if _apply_author_pdf_item(paper, item):
            stats["cache_hits"] = int(stats.get("cache_hits") or 0) + 1
        else:
            stats["mismatches"] = int(stats.get("mismatches") or 0) + 1
    if cache_changed:
        _save_author_pdf_abstract_cache(cache)
    stats["abstracts_filled"] = max(0, sum(1 for paper in targets if _clean_text(paper.get("abstract"))) - before)
    _refresh_acm_doi_abstract_enrichment_audit(papers, stats)
    return papers, stats


def enrich_acm_doi_with_public_pdf_search(papers: list[dict], limit: int = 0) -> tuple[list[dict], dict[str, Any]]:
    targets = [
        paper for paper in papers
        if isinstance(paper, dict) and not _clean_text(paper.get("abstract")) and _paper_doi(paper).startswith("10.1145/")
    ]
    if limit and limit > 0:
        targets = targets[:limit]
    stats: dict[str, Any] = {
        "source": "public_pdf_search_for_acm",
        "enabled": True,
        "attempted": len(targets),
        "search_requests": 0,
        "candidate_urls": 0,
        "pdf_requests": 0,
        "cache_hits": 0,
        "abstracts_filled": 0,
        "mismatches": 0,
        "misses": 0,
        "errors": [],
    }
    if not targets:
        return papers, stats
    cache = _load_public_pdf_search_cache()
    cache.setdefault("schema_version", 1)
    cache_changed = False
    retry_misses = str(os.environ.get("ACM_PUBLIC_PDF_SEARCH_RETRY_MISSES") or "").strip().lower() in {"1", "true", "yes", "on"}
    timeout = _positive_int_env("ACM_PUBLIC_PDF_SEARCH_TIMEOUT_SEC", _metadata_timeout(25))
    query_limit = _positive_int_env("ACM_PUBLIC_PDF_SEARCH_QUERY_LIMIT", 2)
    max_results = _positive_int_env("ACM_PUBLIC_PDF_SEARCH_MAX_RESULTS", 12)
    discovery_limit = max_results * max(1, query_limit)
    max_pages = _positive_int_env("ACM_PUBLIC_PDF_SEARCH_PAGE_LINKS", 3)
    max_pdf_requests_per_paper = _positive_int_env("ACM_PUBLIC_PDF_SEARCH_MAX_PDF_REQUESTS_PER_PAPER", max_results)
    max_seconds_per_paper = _positive_float_env("ACM_PUBLIC_PDF_SEARCH_MAX_SECONDS_PER_PAPER", 45.0)
    spacing = _positive_float_env("ACM_PUBLIC_PDF_SEARCH_SPACING_SEC", 0.8)

    for paper in targets:
        paper_started = time.time()
        doi = _paper_doi(paper)
        doi_key = f"doi:{doi}"
        title_key = _semantic_scholar_cache_key(paper.get("title", ""))
        cached = cache.get(doi_key)
        if not isinstance(cached, dict):
            cached = cache.get(title_key)
        if isinstance(cached, dict):
            if cached.get("abstract") and _apply_public_pdf_search_item(paper, cached):
                stats["cache_hits"] += 1
                stats["abstracts_filled"] += 1
                continue
            cached_version = str(cached.get("query_version") or "")
            if cached.get("miss") and cached_version == PUBLIC_PDF_SEARCH_QUERY_VERSION and not retry_misses and not cached.get("retryable"):
                stats["cache_hits"] += 1
                continue

        candidate_urls: list[str] = []
        search_errors: list[str] = []
        for query in _public_pdf_search_queries(paper)[:query_limit]:
            if max_seconds_per_paper > 0 and time.time() - paper_started >= max_seconds_per_paper:
                search_errors.append("paper_time_budget_exceeded_before_search")
                break
            for reader_url in _public_search_reader_urls(query):
                if max_seconds_per_paper > 0 and time.time() - paper_started >= max_seconds_per_paper:
                    search_errors.append("paper_time_budget_exceeded_before_search_reader")
                    break
                try:
                    stats["search_requests"] += 1
                    response = _request(reader_url, timeout=timeout)
                    for url in _public_pdf_candidate_urls_from_text(response.text or "", title=_clean_text(paper.get("title")), doi=doi):
                        if url not in candidate_urls:
                            candidate_urls.append(url)
                        if len(candidate_urls) >= discovery_limit:
                            break
                except Exception as exc:
                    search_errors.append(str(exc)[:180])
                if len(candidate_urls) >= discovery_limit:
                    break
            if len(candidate_urls) >= discovery_limit:
                break
            if spacing > 0:
                time.sleep(spacing)

        candidate_urls = _public_pdf_sort_candidate_urls(candidate_urls, paper)
        page_pdf_urls: list[str] = []
        direct_pdf_urls: list[str] = []
        for url in candidate_urls:
            if max_seconds_per_paper > 0 and time.time() - paper_started >= max_seconds_per_paper:
                search_errors.append("paper_time_budget_exceeded_before_page_expansion")
                break
            if _public_pdf_url_looks_like_pdf(url):
                if url not in direct_pdf_urls:
                    direct_pdf_urls.append(url)
                continue
            if max_pages > 0:
                for pdf_url in _public_pdf_links_from_page(url, timeout, title=_clean_text(paper.get("title")), max_links=max_pages):
                    if pdf_url not in page_pdf_urls:
                        page_pdf_urls.append(pdf_url)
                    if len(page_pdf_urls) >= max_results:
                        break
            if len(page_pdf_urls) >= max_results:
                break
        candidate_urls = []
        for url in page_pdf_urls + direct_pdf_urls:
            if url not in candidate_urls:
                candidate_urls.append(url)
            if len(candidate_urls) >= max_results:
                break
        stats["candidate_urls"] += len(candidate_urls)
        search_incomplete = (
            len(_public_pdf_search_queries(paper)) > query_limit
            or any("time_budget_exceeded" in str(error) for error in search_errors)
        )

        accepted: dict[str, Any] = {}
        candidate_errors: list[str] = []
        paper_pdf_requests = 0
        for url in candidate_urls:
            if max_pdf_requests_per_paper > 0 and paper_pdf_requests >= max_pdf_requests_per_paper:
                candidate_errors.append("paper_pdf_request_budget_exceeded")
                break
            if max_seconds_per_paper > 0 and time.time() - paper_started >= max_seconds_per_paper:
                candidate_errors.append("paper_time_budget_exceeded_before_pdf")
                break
            stats["pdf_requests"] += 1
            paper_pdf_requests += 1
            parsed = _parse_public_pdf_search_publication(url, paper)
            if parsed.get("miss"):
                candidate_errors.append(f"{url}:{parsed.get('miss_reason') or parsed.get('error') or 'miss'}")
            elif _apply_public_pdf_search_item(paper, parsed):
                accepted = parsed
                break
            else:
                stats["mismatches"] += 1
                candidate_errors.append(f"{url}:identity_mismatch")
            if spacing > 0:
                time.sleep(spacing)

        if accepted:
            cache_item = dict(accepted)
            cache_item["miss"] = False
            cache_item["source"] = "public_pdf_search_for_acm"
            cache_item["query_version"] = PUBLIC_PDF_SEARCH_QUERY_VERSION
            cache_item["updated_at"] = datetime.utcnow().isoformat() + "Z"
            cache[doi_key] = cache_item
            cache[title_key] = cache_item
            stats["abstracts_filled"] += 1
            cache_changed = True
        else:
            retryable = (
                not candidate_urls
                or search_incomplete
                or any("time_budget_exceeded" in str(error) for error in candidate_errors + search_errors)
                or any(_acm_pdf_error_retryable([error]) for error in candidate_errors + search_errors)
            )
            cache[doi_key] = {
                "title": paper.get("title", ""),
                "source": "public_pdf_search_for_acm",
                "query_version": PUBLIC_PDF_SEARCH_QUERY_VERSION,
                "miss": True,
                "miss_reason": "no_verified_public_pdf_abstract" if candidate_urls else "no_candidate_public_pdf_under_budget",
                "candidate_count": len(candidate_urls),
                "errors": (candidate_errors + search_errors)[:6],
                "retryable": retryable,
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
            stats["misses"] += 1
            if candidate_errors or search_errors:
                stats["errors"].append({"doi": doi, "errors": (candidate_errors + search_errors)[:4]})
            cache_changed = True

    if cache_changed:
        _save_public_pdf_search_cache(cache)
    _refresh_acm_doi_abstract_enrichment_audit(papers, stats)
    return papers, stats


def _refresh_acm_doi_abstract_enrichment_audit(papers: list[dict], stats: dict[str, Any]) -> None:
    if not papers:
        return
    acm_doi_count = 0
    missing_abstracts = 0
    source_counts: dict[str, int] = {}
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        doi = _paper_doi(paper)
        if doi.startswith("10.1145/"):
            acm_doi_count += 1
        if not _clean_text(paper.get("abstract")):
            missing_abstracts += 1
        metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
        source = str(metadata.get("abstract_source") or "")
        if source in {"acm_dl_detail", "acm_dl_detail_cache", "acm_dl_pdf", "openalex_doi_for_acm", "openalex_title_for_acm", "openalex_oa_pdf_for_acm", "semantic_scholar_doi_for_acm", "semantic_scholar_title_for_acm", "arxiv_title_match_for_acm", "chatpaper_title_for_acm", "hal_title_for_acm", "public_publication_page_for_acm", "public_pdf_search_for_acm"}:
            source_counts[source] = source_counts.get(source, 0) + 1
    if not acm_doi_count:
        return
    audit = venue_metadata_audit_from_papers(papers)
    if not audit:
        return
    title_complete = bool(audit.get("title_index_complete") if audit.get("title_index_complete") is not None else audit.get("complete"))
    all_have_acm_doi = acm_doi_count == len([paper for paper in papers if isinstance(paper, dict)])
    enrichment_complete = bool(title_complete and all_have_acm_doi and missing_abstracts == 0)
    audit.update({
        "missing_abstract_count": missing_abstracts,
        "has_abstracts": bool(papers) and missing_abstracts == 0,
        "any_abstracts": bool(papers) and missing_abstracts < len(papers),
        "publisher_doi_seed_verified": bool(title_complete and all_have_acm_doi),
        "publisher_doi_prefix": "10.1145",
        "publisher_doi_seed_count": acm_doi_count,
        "abstract_enrichment_source": "+".join(sorted(source_counts)) if source_counts else audit.get("abstract_enrichment_source") or "",
        "abstract_enrichment_source_counts": source_counts,
        "abstract_enrichment_complete": enrichment_complete,
        "abstract_enrichment_stats": stats,
    })
    if enrichment_complete:
        audit["source_scope"] = "acm_doi_seed_with_indexed_abstracts"
        basis = str(audit.get("completeness_basis") or "").strip()
        enrichment_basis = (
            "All rows have ACM DOI seeds and real abstracts after DOI-exact indexed metadata enrichment. "
            "OpenAlex/Semantic Scholar abstracts are provenance-marked and are not treated as ACM DL HTML crawls."
        )
        audit["completeness_basis"] = " ".join(part for part in [basis, enrichment_basis] if part).strip()
    _attach_venue_metadata_audit(papers, audit)


def enrich_acm_doi_with_openalex(papers: list[dict], limit: int = 0) -> tuple[list[dict], dict[str, Any]]:
    targets: list[tuple[dict, str, str]] = []
    skipped_existing = 0
    skipped_non_acm = 0
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        if paper.get("abstract"):
            skipped_existing += 1
            continue
        doi = _paper_doi(paper)
        if not doi.startswith("10.1145/"):
            skipped_non_acm += 1
            continue
        targets.append((paper, doi, f"doi:{doi}"))
    if limit and limit > 0:
        targets = targets[:limit]
    stats: dict[str, Any] = {
        "source": "openalex_doi_for_acm",
        "attempted": len(targets),
        "skipped_existing_abstract": skipped_existing,
        "skipped_non_acm_doi": skipped_non_acm,
        "cache_hits": 0,
        "requested": 0,
        "batches": 0,
        "openalex_records": 0,
        "abstracts_filled": 0,
        "records_without_abstract": 0,
        "missing_openalex_records": 0,
        "errors": [],
    }
    if not targets:
        return papers, stats

    cache = _load_openalex_cache()
    cache_changed = False
    retry_misses = str(os.environ.get("OPENALEX_ACM_RETRY_MISSES") or "").strip().lower() in {"1", "true", "yes", "on"}
    pending: list[tuple[dict, str, str]] = []
    for paper, doi, cache_key in targets:
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            if cached.get("abstract"):
                if not paper.get("abstract"):
                    paper["abstract"] = cached.get("abstract") or ""
                    metadata = paper.setdefault("metadata", {})
                    metadata["abstract_source"] = cached.get("source") or "openalex_doi_for_acm"
                    metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "openalex_doi_for_acm"
                    metadata["abstract_enrichment_provenance"] = "ACM DOI exact match enriched from cached OpenAlex metadata; not ACM DL HTML."
                    stats["abstracts_filled"] += 1
                stats["cache_hits"] += 1
                continue
            if cached.get("miss") and cached.get("acm_doi_batch_attempted") and not retry_misses:
                stats["cache_hits"] += 1
                continue
        pending.append((paper, doi, cache_key))
    if not pending:
        _refresh_acm_doi_abstract_enrichment_audit(papers, stats)
        return papers, stats

    batch_size = max(1, min(50, _positive_int_env("OPENALEX_ACM_BATCH_SIZE", 50)))
    timeout = _positive_int_env("OPENALEX_REQUEST_TIMEOUT_SEC", 25)
    retries = _positive_int_env("OPENALEX_REQUEST_RETRIES", 4)
    spacing = _positive_float_env("OPENALEX_REQUEST_SPACING_SEC", 0.2)
    select = "id,doi,display_name,abstract_inverted_index,primary_location,open_access,locations,authorships"
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        stats["batches"] += 1
        stats["requested"] += len(batch)
        params = {
            "filter": "doi:" + "|".join(f"https://doi.org/{doi}" for _paper, doi, _cache_key in batch),
            "per-page": str(len(batch)),
            "select": select,
            "mailto": _contact_mailto(),
        }
        url = "https://api.openalex.org/works?" + urlencode(params, safe=":/|,")
        payload: dict[str, Any] = {}
        last_error = ""
        for attempt in range(1, retries + 1):
            try:
                response = requests.get(url, headers=HEADERS, timeout=timeout)
                if response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
                    raise RuntimeError(f"HTTP {response.status_code}")
                response.raise_for_status()
                payload = response.json()
                last_error = ""
                break
            except Exception as exc:
                last_error = str(exc)[:240]
                if attempt < retries:
                    time.sleep(min(30.0, max(1.0, spacing) * attempt))
        if last_error:
            stats["errors"].append({"batch_start": start, "error": last_error})
            continue
        items_by_doi: dict[str, dict] = {}
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            doi = _doi_from_url(str(item.get("doi") or "")).lower()
            if doi:
                items_by_doi[doi] = item
        stats["openalex_records"] += len(items_by_doi)
        for paper, doi, cache_key in batch:
            item = items_by_doi.get(doi)
            if not item:
                cache[cache_key] = {
                    "title": paper.get("title", ""),
                    "miss": True,
                    "source": "openalex_doi_for_acm",
                    "miss_reason": "openalex_record_not_found",
                    "acm_doi_batch_attempted": True,
                    "updated_at": datetime.utcnow().isoformat() + "Z",
                }
                cache_changed = True
                stats["missing_openalex_records"] += 1
                continue
            abstract = _openalex_abstract_from_inverted_index(item.get("abstract_inverted_index") or {})
            if _apply_openalex_acm_item(paper, item):
                stats["abstracts_filled"] += 1
            elif not abstract:
                stats["records_without_abstract"] += 1
            cache[cache_key] = {
                "title": paper.get("title", ""),
                "abstract": abstract,
                "url": _openalex_landing_url(item),
                "pdf_url": _openalex_pdf_url(item),
                "openalex_id": item.get("id") or "",
                "openalex_doi": item.get("doi") or "",
                "source": "openalex_doi_for_acm",
                "miss": not bool(abstract),
                "miss_reason": "" if abstract else "openalex_record_without_abstract",
                "acm_doi_batch_attempted": True,
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
            cache_changed = True
        if spacing > 0:
            time.sleep(spacing)
    if cache_changed:
        _save_openalex_cache(cache)
    _refresh_acm_doi_abstract_enrichment_audit(papers, stats)
    return papers, stats


def enrich_acm_doi_with_openalex_oa_pdf(papers: list[dict], limit: int = 0) -> tuple[list[dict], dict[str, Any]]:
    targets = [
        paper
        for paper in papers
        if isinstance(paper, dict) and not _clean_text(paper.get("abstract")) and _paper_doi(paper).startswith("10.1145/")
    ]
    if limit and limit > 0:
        targets = targets[:limit]
    stats: dict[str, Any] = {
        "source": "openalex_oa_pdf_for_acm",
        "enabled": True,
        "attempted": len(targets),
        "cache_hits": 0,
        "pdf_requests": 0,
        "abstracts_filled": 0,
        "records_without_pdf_url": 0,
        "mismatches": 0,
        "misses": 0,
        "errors": [],
    }
    if not targets:
        return papers, stats

    openalex_cache = _load_openalex_cache()
    cache = _load_openalex_oa_pdf_abstract_cache()
    cache_changed = False
    retry_misses = str(os.environ.get("OPENALEX_OA_PDF_RETRY_MISSES") or "").strip().lower() in {"1", "true", "yes", "on"}
    cache_only = str(os.environ.get("OPENALEX_OA_PDF_CACHE_ONLY") or "").strip().lower() in {"1", "true", "yes", "on"}
    timeout = _positive_int_env("OPENALEX_OA_PDF_REQUEST_TIMEOUT_SEC", _metadata_timeout(20))
    max_pages = _positive_int_env("OPENALEX_OA_PDF_MAX_PAGES", 2)
    spacing = _positive_float_env("OPENALEX_OA_PDF_REQUEST_SPACING_SEC", 0.3)

    for paper in targets:
        doi = _paper_doi(paper)
        cache_key = f"doi:{doi}"
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            if cached.get("abstract"):
                abstract = _clean_text(cached.get("abstract"))
                title = _clean_text(paper.get("title"))
                cached_title = _clean_text(cached.get("title"))
                source_doi = _doi_from_url(str(cached.get("source_doi") or cached.get("openalex_doi") or ""))
                item_doi = _doi_from_url(str(cached.get("doi") or cached.get("doi_url") or ""))
                if (
                    abstract
                    and title
                    and cached_title
                    and _title_token_similarity(title, cached_title) >= 0.96
                    and (not source_doi or source_doi.lower() == doi)
                    and (not item_doi or item_doi.lower() == doi)
                ):
                    paper["abstract"] = abstract
                    if cached.get("pdf_url") and not paper.get("pdf_url"):
                        paper["pdf_url"] = str(cached.get("pdf_url") or "")
                    metadata = paper.setdefault("metadata", {})
                    metadata["abstract_source"] = "openalex_oa_pdf_for_acm"
                    metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "openalex_oa_pdf_for_acm"
                    metadata["abstract_enrichment_provenance"] = (
                        "ACM DOI row enriched by extracting the abstract from an OpenAlex DOI-exact open-access PDF after strict title and DOI checks; not ACM DL HTML."
                    )
                    metadata["openalex_oa_pdf_url"] = cached.get("pdf_url") or ""
                    metadata["openalex_oa_pdf_title_similarity"] = round(_title_token_similarity(title, cached_title), 4)
                    stats["cache_hits"] += 1
                    stats["abstracts_filled"] += 1
                    continue
                stats["mismatches"] += 1
                continue
            if cached.get("miss") and not retry_misses and not cached.get("retryable"):
                stats["cache_hits"] += 1
                continue
        if cache_only:
            continue

        openalex_item = openalex_cache.get(cache_key)
        if not isinstance(openalex_item, dict):
            openalex_item = {}
        source_doi = _doi_from_url(str(openalex_item.get("openalex_doi") or openalex_item.get("doi") or f"https://doi.org/{doi}"))
        pdf_url = _clean_text(openalex_item.get("pdf_url"))
        if not pdf_url:
            metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
            pdf_url = _clean_text(metadata.get("openalex_pdf_url") or paper.get("pdf_url"))
        if not pdf_url:
            cache[cache_key] = {
                "title": paper.get("title", ""),
                "source_doi": doi,
                "miss": True,
                "source": "openalex_oa_pdf_for_acm",
                "miss_reason": "openalex_record_without_pdf_url",
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
            cache_changed = True
            stats["records_without_pdf_url"] += 1
            continue

        errors: list[str] = []
        stats["pdf_requests"] += 1
        pdf_text, metadata_title, error = _fetch_author_pdf_text(pdf_url, timeout, max_pages=max_pages)
        if error:
            errors.append(f"{pdf_url}:{error}")
        title = _author_pdf_text_title(pdf_text, metadata_title) if pdf_text else ""
        abstract = _extract_acm_pdf_abstract_from_text(pdf_text) if pdf_text else ""
        pdf_doi_match = re.search(r"\b10\.1145/[0-9]+\.[0-9]+\b", pdf_text[:12000]) if pdf_text else None
        item = {
            "title": title,
            "abstract": abstract,
            "pdf_url": pdf_url,
            "doi": pdf_doi_match.group(0).lower() if pdf_doi_match else "",
            "source_doi": source_doi.lower() if source_doi else doi,
            "openalex_doi": openalex_item.get("openalex_doi") or openalex_item.get("doi") or "",
            "source": "openalex_oa_pdf_for_acm",
            "miss": not bool(abstract),
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        applied = False
        if abstract:
            cached_title = _clean_text(item.get("title"))
            paper_title = _clean_text(paper.get("title"))
            item_doi = _doi_from_url(str(item.get("doi") or ""))
            source_doi_clean = _doi_from_url(str(item.get("source_doi") or ""))
            if (
                paper_title
                and cached_title
                and _title_token_similarity(paper_title, cached_title) >= 0.96
                and (not item_doi or item_doi.lower() == doi)
                and (not source_doi_clean or source_doi_clean.lower() == doi)
            ):
                paper["abstract"] = abstract
                if pdf_url and not paper.get("pdf_url"):
                    paper["pdf_url"] = pdf_url
                metadata = paper.setdefault("metadata", {})
                metadata["abstract_source"] = "openalex_oa_pdf_for_acm"
                metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "openalex_oa_pdf_for_acm"
                metadata["abstract_enrichment_provenance"] = (
                    "ACM DOI row enriched by extracting the abstract from an OpenAlex DOI-exact open-access PDF after strict title and DOI checks; not ACM DL HTML."
                )
                metadata["openalex_oa_pdf_url"] = pdf_url
                metadata["openalex_oa_pdf_title_similarity"] = round(_title_token_similarity(paper_title, cached_title), 4)
                stats["abstracts_filled"] += 1
                applied = True
            else:
                stats["mismatches"] += 1
        else:
            if not errors:
                errors.append(f"{pdf_url}:abstract_not_found")
        item["miss"] = not applied
        if not applied:
            item.pop("abstract", None)
            item["miss_reason"] = "identity_mismatch_or_missing_abstract" if abstract else "missing_or_short_abstract"
            if errors:
                item["errors"] = errors[:4]
                item["retryable"] = _acm_pdf_error_retryable(errors)
                stats["errors"].append({"doi": doi, "errors": errors[:3]})
            stats["misses"] += 1
        cache[cache_key] = item
        cache_changed = True
        if spacing > 0:
            time.sleep(spacing)

    if cache_changed:
        _save_openalex_oa_pdf_abstract_cache(cache)
    _refresh_acm_doi_abstract_enrichment_audit(papers, stats)
    return papers, stats


def _extract_acm_pdf_abstract_from_text(text: str) -> str:
    if not text:
        return ""
    patterns = [
        r"(?is)\bAbstract\b\s*(.*?)(?:\n\s*(?:1\s+Introduction|Introduction|Keywords|CCS Concepts|ACM Reference Format|Index Terms)\b)",
        r"(?is)\bABSTRACT\b\s*(.*?)(?:\n\s*(?:1\s+INTRODUCTION|INTRODUCTION|KEYWORDS|CCS CONCEPTS|ACM REFERENCE FORMAT|INDEX TERMS)\b)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        abstract = _clean_pdf_abstract_text(match.group(1))
        if len(abstract) >= 80:
            return abstract
    return ""


def _fetch_acm_pdf_text(url: str, timeout: int, *, max_pages: int = 2) -> tuple[str, str]:
    if not url:
        return "", "missing_url"
    try:
        import fitz  # type: ignore
    except Exception:
        return "", "pymupdf_unavailable"
    headers = dict(HEADERS)
    headers["Accept"] = "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8"
    doi = _doi_from_url(url)
    if doi and "dl.acm.org" in urlparse(url).netloc.lower():
        headers.setdefault("Referer", f"https://dl.acm.org/doi/abs/{doi}")
    try:
        response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    except Exception as exc:
        return "", "request_error:" + str(exc)[:160]
    status = int(getattr(response, "status_code", 0) or 0)
    content_type = str(response.headers.get("content-type") or "").lower()
    data = response.content or b""
    if status != 200:
        return "", f"http_{status}:{content_type[:80]}"
    if not data.startswith(b"%PDF"):
        lowered = data[:2048].lower()
        if b"just a moment" in lowered or b"cf-mitigated" in lowered or b"cloudflare" in lowered:
            return "", f"challenge_non_pdf:{content_type[:80]}"
        return "", f"non_pdf:{content_type[:80]}"
    try:
        document = fitz.open(stream=data, filetype="pdf")
        try:
            page_count = min(max_pages, len(document))
            return "\n".join(document[index].get_text() for index in range(page_count)), ""
        finally:
            document.close()
    except Exception as exc:
        return "", "pdf_parse_error:" + str(exc)[:160]


def _acm_pdf_error_retryable(errors: list[str]) -> bool:
    text = " ".join(str(error or "").lower() for error in errors)
    return any(token in text for token in ("http_403", "403 client error", "forbidden", "cloudflare", "http_429", "429", "http_500", "http_502", "http_503", "http_504", "challenge_non_pdf", "timeout", "timed out", "connection reset"))


def _acm_pdf_urls_for_paper(paper: dict) -> list[str]:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    doi = _paper_doi(paper)
    if doi and not metadata.get("acm_pdf_url"):
        metadata.update(_acm_metadata_from_doi(doi))
        paper["metadata"] = metadata
    candidates = [
        f"https://dlnext.acm.org/doi/pdf/{doi}?download=true" if doi else "",
        f"https://dl.acm.org/doi/pdf/{doi}?download=true" if doi else "",
        metadata.get("acm_pdf_url"),
        metadata.get("acm_legacy_pdf_url"),
        metadata.get("acm_epdf_url"),
        paper.get("pdf_url"),
    ]
    urls: list[str] = []
    for value in candidates:
        text = str(value or "").strip()
        if text and text not in urls:
            urls.append(text)
    return urls


def enrich_acm_doi_with_official_pdf(papers: list[dict], limit: int = 0) -> tuple[list[dict], dict[str, Any]]:
    targets = [
        paper
        for paper in papers
        if isinstance(paper, dict) and not paper.get("abstract") and _paper_doi(paper).startswith("10.1145/")
    ]
    if limit and limit > 0:
        targets = targets[:limit]
    stats: dict[str, Any] = {
        "source": "acm_dl_pdf",
        "attempted": len(targets),
        "cache_hits": 0,
        "pdf_requests": 0,
        "abstracts_filled": 0,
        "misses": 0,
        "errors": [],
    }
    if not targets:
        return papers, stats
    cache = _load_acm_pdf_abstract_cache()
    cache_changed = False
    retry_misses = str(os.environ.get("ACM_PDF_RETRY_MISSES") or "").strip().lower() in {"1", "true", "yes", "on"}
    cache_only = str(os.environ.get("ACM_PDF_CACHE_ONLY") or "").strip().lower() in {"1", "true", "yes", "on"}
    timeout = _positive_int_env("ACM_PDF_REQUEST_TIMEOUT_SEC", _metadata_timeout(20))
    max_pages = _positive_int_env("ACM_PDF_MAX_PAGES", 2)
    spacing = _positive_float_env("ACM_PDF_REQUEST_SPACING_SEC", 0.3)
    blocked_threshold = _positive_int_env("ACM_PDF_ABORT_AFTER_RETRYABLE_MISSES", 8)
    abort_on_blocked = str(os.environ.get("ACM_PDF_ABORT_ON_BLOCKED", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}
    consecutive_retryable = 0
    for paper in targets:
        if abort_on_blocked and blocked_threshold > 0 and consecutive_retryable >= blocked_threshold:
            stats["aborted"] = True
            stats["abort_reason"] = f"consecutive_retryable_pdf_failures_{consecutive_retryable}"
            break
        doi = _paper_doi(paper)
        cache_key = f"doi:{doi}"
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            if cached.get("abstract"):
                paper["abstract"] = cached.get("abstract") or ""
                metadata = paper.setdefault("metadata", {})
                metadata["abstract_source"] = "acm_dl_pdf"
                metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "acm_dl_pdf"
                metadata["abstract_enrichment_provenance"] = "Abstract extracted from ACM Digital Library PDF or ACM PDF gateway."
                if cached.get("pdf_url") and not paper.get("pdf_url"):
                    paper["pdf_url"] = cached.get("pdf_url") or ""
                stats["cache_hits"] += 1
                stats["abstracts_filled"] += 1
                continue
            if cached.get("miss") and not retry_misses and not cached.get("retryable"):
                stats["cache_hits"] += 1
                continue
        if cache_only:
            continue
        urls = _acm_pdf_urls_for_paper(paper)
        abstract = ""
        used_url = ""
        errors: list[str] = []
        for url in urls:
            try:
                stats["pdf_requests"] += 1
                text, error = _fetch_acm_pdf_text(url, timeout, max_pages=max_pages)
                if error:
                    errors.append(f"{url}:{error}")
                if not text:
                    continue
                abstract = _extract_acm_pdf_abstract_from_text(text)
                if abstract:
                    used_url = url
                    break
                errors.append(f"{url}:abstract_not_found")
            except Exception as exc:
                errors.append(f"{url}:{str(exc)[:120]}")
            if spacing > 0:
                time.sleep(spacing)
        if abstract:
            consecutive_retryable = 0
            paper["abstract"] = abstract
            if used_url and not paper.get("pdf_url"):
                paper["pdf_url"] = used_url
            metadata = paper.setdefault("metadata", {})
            metadata["abstract_source"] = "acm_dl_pdf"
            metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "acm_dl_pdf"
            metadata["abstract_enrichment_provenance"] = "Abstract extracted from ACM Digital Library PDF or ACM PDF gateway."
            cache[cache_key] = {
                "title": paper.get("title", ""),
                "abstract": abstract,
                "pdf_url": used_url,
                "source": "acm_dl_pdf",
                "miss": False,
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
            stats["abstracts_filled"] += 1
            cache_changed = True
        else:
            retryable = _acm_pdf_error_retryable(errors)
            if retryable:
                consecutive_retryable += 1
            else:
                consecutive_retryable = 0
            cache[cache_key] = {
                "title": paper.get("title", ""),
                "miss": True,
                "source": "acm_dl_pdf",
                "errors": errors[:4],
                "retryable": retryable,
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
            if errors:
                paper.setdefault("metadata", {})["acm_pdf_abstract_error"] = "; ".join(errors[:3])
            stats["misses"] += 1
            if retryable:
                stats["retryable_misses"] = int(stats.get("retryable_misses") or 0) + 1
            if errors:
                stats["errors"].append({"doi": doi, "errors": errors[:3]})
            cache_changed = True
    if cache_changed:
        _save_acm_pdf_abstract_cache(cache)
    _refresh_acm_doi_abstract_enrichment_audit(papers, stats)
    return papers, stats


def enrich_acm_doi_with_indexed_abstracts(papers: list[dict], limit: int = 0) -> tuple[list[dict], dict[str, Any]]:
    papers, local_cache_stats = _apply_cached_acm_abstract_sources(papers)
    pure_stats: dict[str, Any] = {
        "enabled": False,
        "attempted": 0,
        "abstracts_filled": 0,
        "disabled_reason": "manual_url_fallback_not_used_for_generic_metadata_completion",
    }
    hal_enabled = str(os.environ.get("ACM_HAL_FALLBACK", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}
    hal_stats: dict[str, Any] = {"enabled": False, "attempted": 0, "abstracts_filled": 0}
    if hal_enabled:
        papers, hal_stats = enrich_acm_doi_with_hal(papers, limit=limit)
    public_page_search_enabled = str(os.environ.get("ACM_PUBLIC_PAGE_SEARCH_FALLBACK") or "").strip().lower() in {"1", "true", "yes", "on"}
    public_page_search_stats: dict[str, Any] = {"enabled": False, "attempted": 0, "abstracts_filled": 0}
    if public_page_search_enabled:
        public_page_search_limit = _positive_int_env("ACM_PUBLIC_PAGE_SEARCH_MAX_ITEMS", limit if limit and limit > 0 else 0)
        papers, public_page_search_stats = enrich_acm_doi_with_public_publication_page_search(papers, limit=public_page_search_limit)
    author_pdf_stats: dict[str, Any] = {
        "enabled": False,
        "attempted": 0,
        "abstracts_filled": 0,
        "disabled_reason": "manual_pdf_url_fallback_not_used_for_generic_metadata_completion",
    }
    public_pdf_search_enabled = str(os.environ.get("ACM_PUBLIC_PDF_SEARCH_FALLBACK") or "").strip().lower() in {"1", "true", "yes", "on"}
    public_pdf_search_stats: dict[str, Any] = {"enabled": False, "attempted": 0, "abstracts_filled": 0}
    if public_pdf_search_enabled:
        public_pdf_search_limit = _positive_int_env("ACM_PUBLIC_PDF_SEARCH_MAX_ITEMS", limit if limit and limit > 0 else 0)
        papers, public_pdf_search_stats = enrich_acm_doi_with_public_pdf_search(papers, limit=public_pdf_search_limit)
    chatpaper_enabled = str(os.environ.get("ACM_CHATPAPER_FALLBACK", "0") or "0").strip().lower() not in {"0", "false", "no", "off"}
    chatpaper_stats: dict[str, Any] = {"enabled": False, "attempted": 0, "abstracts_filled": 0}
    if chatpaper_enabled:
        papers, chatpaper_stats = enrich_acm_doi_with_chatpaper(papers, limit=limit)
    papers, openalex_stats = enrich_acm_doi_with_openalex(papers, limit=limit)
    openalex_oa_pdf_enabled = str(os.environ.get("ACM_OPENALEX_OA_PDF_FALLBACK", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}
    openalex_oa_pdf_stats: dict[str, Any] = {"enabled": False, "attempted": 0, "abstracts_filled": 0}
    if openalex_oa_pdf_enabled:
        papers, openalex_oa_pdf_stats = enrich_acm_doi_with_openalex_oa_pdf(papers, limit=limit)
    pdf_enabled = str(os.environ.get("ACM_PDF_ABSTRACT_FALLBACK", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}
    pdf_stats: dict[str, Any] = {"enabled": False, "attempted": 0, "abstracts_filled": 0}
    stats: dict[str, Any] = {
        "local_cache": local_cache_stats,
        "pure_portal": pure_stats,
        "hal": hal_stats,
        "public_publication_page": public_page_search_stats,
        "author_pdf": author_pdf_stats,
        "public_pdf_search": public_pdf_search_stats,
        "chatpaper": chatpaper_stats,
        "openalex": openalex_stats,
        "openalex_oa_pdf": openalex_oa_pdf_stats,
        "openalex_title_match": {"enabled": False, "attempted": 0, "abstracts_filled": 0},
        "acm_pdf": pdf_stats,
        "semantic_scholar": {"enabled": False, "attempted": 0, "abstracts_filled": 0},
        "arxiv_title_match": {"enabled": False, "attempted": 0, "abstracts_filled": 0},
    }
    openalex_title_enabled = str(os.environ.get("ACM_OPENALEX_TITLE_FALLBACK", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
    if openalex_title_enabled:
        openalex_title_missing = [
            paper
            for paper in papers
            if isinstance(paper, dict) and not paper.get("abstract") and _paper_doi(paper).startswith("10.1145/")
        ]
        openalex_title_limit = _positive_int_env("ACM_OPENALEX_TITLE_MAX_ITEMS", 0)
        if openalex_title_limit > 0:
            openalex_title_missing = openalex_title_missing[:openalex_title_limit]
        before_openalex_title = sum(1 for paper in openalex_title_missing if paper.get("abstract"))
        if openalex_title_missing:
            enrich_with_openalex(openalex_title_missing, limit=len(openalex_title_missing))
        openalex_title_marked = 0
        for paper in openalex_title_missing:
            if not paper.get("abstract"):
                continue
            metadata = paper.setdefault("metadata", {})
            if metadata.get("openalex_id"):
                metadata["abstract_source"] = "openalex_title_for_acm"
                metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "openalex_title_for_acm"
                metadata["abstract_enrichment_provenance"] = "ACM DOI row enriched from a strict OpenAlex title match; not ACM DL HTML."
                openalex_title_marked += 1
        stats["openalex_title_match"] = {
            "enabled": True,
            "attempted": len(openalex_title_missing),
            "abstracts_filled": max(0, sum(1 for paper in openalex_title_missing if paper.get("abstract")) - before_openalex_title),
            "marked_filled": openalex_title_marked,
        }
    enabled_text = str(os.environ.get("ACM_SEMANTIC_SCHOLAR_FALLBACK", "1") or "1").strip().lower()
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    semantic_enabled = bool(api_key) or enabled_text in {"1", "true", "yes", "on"}
    if semantic_enabled:
        missing = [
            paper
            for paper in papers
            if isinstance(paper, dict) and not paper.get("abstract") and _paper_doi(paper).startswith("10.1145/")
        ]
        semantic_limit = _positive_int_env("ACM_SEMANTIC_SCHOLAR_MAX_ITEMS", 0)
        if semantic_limit > 0:
            missing = missing[:semantic_limit]
        before_ids = {id(paper) for paper in missing if not paper.get("abstract")}
        before_filled = sum(1 for paper in missing if paper.get("abstract"))
        if missing:
            enrich_with_semantic_scholar(missing, limit=len(missing), api_key=api_key)
        filled = 0
        for paper in missing:
            if id(paper) not in before_ids or not paper.get("abstract"):
                continue
            metadata = paper.setdefault("metadata", {})
            source = str(metadata.get("abstract_source") or "")
            if source == "semantic_scholar_doi":
                metadata["abstract_source"] = "semantic_scholar_doi_for_acm"
                provenance = "ACM DOI exact match enriched from Semantic Scholar indexed metadata; not ACM DL HTML."
            elif source.startswith("semantic_scholar"):
                metadata["abstract_source"] = "semantic_scholar_title_for_acm"
                provenance = "ACM DOI row enriched from Semantic Scholar title-matched indexed metadata; not ACM DL HTML."
            else:
                provenance = "ACM DOI row enriched from Semantic Scholar indexed metadata; not ACM DL HTML."
            metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or metadata.get("abstract_source") or "semantic_scholar_doi_for_acm"
            metadata["abstract_enrichment_provenance"] = provenance
            filled += 1
        stats["semantic_scholar"] = {
            "enabled": True,
            "attempted": len(missing),
            "abstracts_filled": max(0, sum(1 for paper in missing if paper.get("abstract")) - before_filled),
            "marked_filled": filled,
        }
    arxiv_enabled = str(os.environ.get("ACM_ARXIV_TITLE_FALLBACK") or "").strip().lower() in {"1", "true", "yes", "on"}
    if arxiv_enabled:
        arxiv_missing = [
            paper
            for paper in papers
            if isinstance(paper, dict) and not paper.get("abstract") and _paper_doi(paper).startswith("10.1145/")
        ]
        arxiv_limit = _positive_int_env("ACM_ARXIV_TITLE_MAX_ITEMS", 0)
        if arxiv_limit > 0:
            arxiv_missing = arxiv_missing[:arxiv_limit]
        before_arxiv = sum(1 for paper in arxiv_missing if paper.get("abstract"))
        if arxiv_missing:
            enrich_with_arxiv_title_match(arxiv_missing, limit=len(arxiv_missing))
        arxiv_marked = 0
        for paper in arxiv_missing:
            if not paper.get("abstract"):
                continue
            metadata = paper.setdefault("metadata", {})
            if str(metadata.get("abstract_source") or "") == "arxiv_title_match":
                metadata["abstract_source"] = "arxiv_title_match_for_acm"
                metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "arxiv_title_match_for_acm"
                metadata["abstract_enrichment_provenance"] = "ACM DOI row enriched from an arXiv title/author match; not ACM DL HTML."
                arxiv_marked += 1
        stats["arxiv_title_match"] = {
            "enabled": True,
            "attempted": len(arxiv_missing),
            "abstracts_filled": max(0, sum(1 for paper in arxiv_missing if paper.get("abstract")) - before_arxiv),
            "marked_filled": arxiv_marked,
        }
    arxiv_web_enabled = str(os.environ.get("ACM_ARXIV_WEB_TITLE_FALLBACK") or "").strip().lower() in {"1", "true", "yes", "on"}
    if arxiv_web_enabled:
        arxiv_web_missing = [
            paper
            for paper in papers
            if isinstance(paper, dict) and not paper.get("abstract") and _paper_doi(paper).startswith("10.1145/")
        ]
        arxiv_web_limit = _positive_int_env("ACM_ARXIV_WEB_TITLE_MAX_ITEMS", 0)
        if arxiv_web_limit > 0:
            arxiv_web_missing = arxiv_web_missing[:arxiv_web_limit]
        before_web = sum(1 for paper in arxiv_web_missing if paper.get("abstract"))
        if arxiv_web_missing:
            enrich_with_arxiv_web_title_match(arxiv_web_missing, limit=len(arxiv_web_missing))
        web_marked = 0
        for paper in arxiv_web_missing:
            if not paper.get("abstract"):
                continue
            metadata = paper.setdefault("metadata", {})
            if str(metadata.get("abstract_source") or "") == "arxiv_web_title_match":
                metadata["abstract_source"] = "arxiv_title_match_for_acm"
                metadata["abstract_enrichment_source"] = metadata.get("abstract_enrichment_source") or "arxiv_title_match_for_acm"
                metadata["abstract_enrichment_provenance"] = "ACM DOI row enriched from an arXiv web title match; not ACM DL HTML."
                web_marked += 1
        stats["arxiv_web_title_match"] = {
            "enabled": True,
            "attempted": len(arxiv_web_missing),
            "abstracts_filled": max(0, sum(1 for paper in arxiv_web_missing if paper.get("abstract")) - before_web),
            "marked_filled": web_marked,
        }
    if pdf_enabled:
        papers, pdf_stats = enrich_acm_doi_with_official_pdf(papers, limit=limit)
        pdf_stats["enabled"] = True
        stats["acm_pdf"] = pdf_stats
    _refresh_acm_doi_abstract_enrichment_audit(papers, stats)
    return papers, stats


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


from sources import (
    _conference_virtual_detail_label,
    _conference_virtual_detail_source,
    _conference_virtual_detail_url,
    _extract_between_markers,
    _extract_conference_virtual_abstract,
    _extract_conference_virtual_authors,
    _jsonld_nodes,
    _looks_like_paper_title,
    _mark_detail_fetch_deferred,
    _name_from_jsonld_author,
    _openreview_pdf_url,
    _parse_neurips_detail,
    _parse_neurips_list,
    _parse_neurips_official_papers_list,
    _positive_float_env,
)


from sources import (
    ARXIV_DEFAULT_RECENT_DAYS,
    _append_arxiv_entry,
    _arxiv_date_window,
    _arxiv_entry_id,
    _arxiv_search_queries,
    _biorxiv_category_matches,
    _biorxiv_content_url,
    _title_match_queries,
)


from sources import (
    _icml_verified_download_cache,
    _recent_verified_venue_yecache,
    _venue_cache_candidate_paths,
    _verified_venue_yecache_from_paths,
)



def _http_status_code(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    try:
        return int(getattr(response, "status_code", 0) or 0) or None
    except (TypeError, ValueError):
        return None


def _request(url: str, timeout: int = 12) -> requests.Response:
    retries = max(1, int(os.environ.get("FIND_HTTP_RETRIES", "3") or 3))
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_exc = exc
            status = _http_status_code(exc)
            if attempt + 1 >= retries or status in {400, 401, 403, 404, 429}:
                raise
            time.sleep(min(12.0, 2.0 * (attempt + 1)))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"request failed without response: {url}")


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



def _openreview_probe_timeout(default: int = 8) -> int:
    try:
        value = int(float(os.environ.get("OPENREVIEW_PROBE_TIMEOUT_SEC", "") or default))
    except (TypeError, ValueError):
        value = default
    return max(2, min(30, value))


def _openreview_probe_notes(url: str, base_params: dict[str, object], *, route: str, venue_id: str) -> dict[str, Any]:
    params = dict(base_params)
    params["limit"] = 1
    params["offset"] = 0
    started = time.monotonic()
    audit: dict[str, Any] = {
        "route": route,
        "url": url,
        "openreview_venueid": venue_id,
        "params": {key: value for key, value in params.items() if key in {"content.venueid", "invitation", "limit", "offset"}},
        "ok": False,
    }
    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=_openreview_probe_timeout())
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


def _openreview_probe_summary(probe_audits: list[dict[str, Any]]) -> dict[str, Any]:
    if not probe_audits:
        return {
            "openreview_probe_status": "not_attempted",
            "openreview_probe_errors": [],
            "openreview_probe_audits": [],
        }
    passed = [audit for audit in probe_audits if audit.get("ok")]
    errors = [
        str(audit.get("skip_reason") or audit.get("error") or audit.get("status_code") or "unknown")
        for audit in probe_audits
        if not audit.get("ok")
    ]
    return {
        "openreview_probe_status": "passed" if passed else "failed_or_skipped",
        "openreview_probe_errors": list(dict.fromkeys(errors)),
        "openreview_probe_audits": probe_audits,
    }


def _attach_openreview_fetch_audit(papers: list[dict], venue_ids: list[str], years: list[int], probe_audits: list[dict[str, Any]]) -> list[dict]:
    if not papers:
        return papers
    attached = _attach_openreview_metadata_audit(papers, list(venue_ids), years)
    summary = _openreview_probe_summary(probe_audits)
    for paper in attached:
        metadata = paper.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            continue
        audit = metadata.get(VENUE_METADATA_AUDIT_KEY)
        if not isinstance(audit, dict):
            audit = {}
        audit.update(summary)
        audit.setdefault("adapter", "openreview")
        audit.setdefault("source_adapter", "openreview")
        metadata[VENUE_METADATA_AUDIT_KEY] = audit
    return attached


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
    probe_audits: list[dict[str, Any]] = []
    for year in years:
        venue_ids = _openreview_venue_ids(venue, year)
        for venue_id in venue_ids:
            if venue_id in queried_venue_ids:
                continue
            queried_venue_ids.add(venue_id)
            notes = []
            api2_params = {"content.venueid": venue_id, "details": "replyCount,invitation,original"}
            api2_probe = _openreview_probe_notes("https://api2.openreview.net/notes", api2_params, route="api2_content_venueid", venue_id=venue_id)
            probe_audits.append(api2_probe)
            if api2_probe.get("ok"):
                try:
                    notes = _openreview_notes_paginated(
                        "https://api2.openreview.net/notes",
                        api2_params,
                        max_items,
                    )
                except Exception as exc:
                    api2_probe["fetch_error"] = str(exc)[:240]
                    notes = []
            if not notes:
                for invitation in [f"{venue_id}/-/Blind_Submission", f"{venue_id}/-/Submission"]:
                    api1_params = {"invitation": invitation}
                    api1_probe = _openreview_probe_notes("https://api.openreview.net/notes", api1_params, route="api1_invitation", venue_id=venue_id)
                    probe_audits.append(api1_probe)
                    if not api1_probe.get("ok"):
                        continue
                    try:
                        notes = _openreview_notes_paginated(
                            "https://api.openreview.net/notes",
                            api1_params,
                            max_items,
                        )
                    except Exception as exc:
                        api1_probe["fetch_error"] = str(exc)[:240]
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
                    return _attach_openreview_fetch_audit(papers, list(queried_venue_ids), years, probe_audits)
    return _attach_openreview_fetch_audit(papers, list(queried_venue_ids), years, probe_audits) if papers else papers

def fetch_cvf_openaccess(venue: dict, years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    name = (venue.get("name") or "").upper()
    yeaudits: list[dict[str, Any]] = []
    limit = int(max_items or 0)
    truncated = False
    for year in years:
        url = f"https://openaccess.thecvf.com/{name}{year}?day=all"
        report: dict[str, Any] = {"year": int(year), "source_url": url, "paper_count": 0}
        try:
            soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception as exc:
            report["error"] = str(exc)[:240]
            yeaudits.append(report)
            continue
        for title_node in soup.select("dt.ptitle a[href], dt a[href]"):
            title = _clean_text(title_node.get_text(" ", strip=True))
            if not _looks_like_paper_title(title):
                continue
            paper_url = requests.compat.urljoin(url, title_node["href"])
            authors = ""
            pdf_url = ""
            for sibling in title_node.find_parent("dt").find_next_siblings(["dd", "dt"]):
                if getattr(sibling, "name", "") == "dt":
                    break
                if not authors:
                    names = [
                        _clean_text(anchor.get_text(" ", strip=True))
                        for anchor in sibling.select("form.authsearch a, a[onclick*='authsearch']")
                        if _clean_text(anchor.get_text(" ", strip=True))
                    ]
                    if names:
                        authors = ", ".join(names)
                if not pdf_url:
                    pdf_link = sibling.find("a", href=re.compile(r"\.pdf(?:$|\?)", re.I), string=re.compile(r"pdf", re.I))
                    if not pdf_link:
                        pdf_link = sibling.find("a", href=re.compile(r"/papers/.*\.pdf(?:$|\?)", re.I))
                    if pdf_link and pdf_link.get("href"):
                        pdf_url = requests.compat.urljoin(url, str(pdf_link.get("href") or ""))
            papers.append({
                "id": stable_id("paper", paper_url),
                "source": "cvf_openaccess",
                "title": title,
                "authors": authors,
                "abstract": "",
                "url": paper_url,
                "pdf_url": pdf_url,
                "venue": venue.get("name", ""),
                "year": year,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": {"venue_id": venue.get("id"), "cvf_url": url, "detail_url": paper_url, "title_index_only": True},
            })
            report["paper_count"] = int(report.get("paper_count") or 0) + 1
            if limit > 0 and len(papers) >= limit:
                truncated = True
                break
        report["complete"] = int(report.get("paper_count") or 0) > 0 and not truncated
        yeaudits.append(report)
        if truncated:
            break
        time.sleep(0.2)
    complete = bool(papers) and bool(yeaudits) and all(bool(item.get("complete")) for item in yeaudits) and not truncated
    audit = _venue_metadata_audit(
        status="complete" if complete else "partial",
        title_index_completeness_status="complete" if complete else "partial",
        source_verified=bool(papers and not any(item.get("error") for item in yeaudits)),
        complete=complete,
        title_index_complete=complete,
        official_metadata_complete=False,
        adapter="cvf_openaccess",
        source_adapter="cvf_openaccess",
        source_url=";".join(str(item.get("source_url") or "") for item in yeaudits if item.get("source_url")),
        requested_years=[int(year) for year in years if str(year).isdigit()],
        source_yeaudits=yeaudits,
        paper_count=len(papers),
        missing_abstract_count=len(papers),
        has_abstracts=False,
        any_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="official_cvf_openaccess",
        official_title_index_verified=complete,
        official_accepted_list_verified=complete,
        truncated=truncated,
        completeness_basis="CVF Open Access official venue page was parsed for the complete title index, authors, detail URLs, and PDF links; abstracts are fetched later from selected detail pages.",
    )
    return _attach_venue_metadata_audit(papers, audit) if papers else papers


def fetch_eccv_virtual(years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    yeaudits: list[dict[str, Any]] = []
    limit = int(max_items or 0)
    truncated = False
    for year in years:
        if year % 2 == 1:
            continue
        list_url = f"https://eccv.ecva.net/virtual/{year}/papers.html"
        report: dict[str, Any] = {"year": int(year), "source_url": list_url, "paper_count": 0}
        try:
            soup = BeautifulSoup(_request(list_url).text, "html.parser")
        except Exception as exc:
            report["error"] = str(exc)[:240]
            yeaudits.append(report)
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
            report["paper_count"] = int(report.get("paper_count") or 0) + 1
            if limit > 0 and len(papers) >= limit:
                truncated = True
                break
        report["complete"] = int(report.get("paper_count") or 0) > 0 and not truncated
        yeaudits.append(report)
        if truncated:
            break
    complete = bool(papers) and bool(yeaudits) and all(bool(item.get("complete")) for item in yeaudits) and not truncated
    audit = _venue_metadata_audit(
        status="complete" if complete else "partial",
        title_index_completeness_status="complete" if complete else "partial",
        source_verified=bool(papers and not any(item.get("error") for item in yeaudits)),
        complete=complete,
        title_index_complete=complete,
        official_metadata_complete=False,
        adapter="eccv_virtual",
        source_adapter="eccv_virtual",
        source_url=";".join(str(item.get("source_url") or "") for item in yeaudits if item.get("source_url")),
        requested_years=[int(year) for year in years if str(year).isdigit()],
        source_yeaudits=yeaudits,
        paper_count=len(papers),
        missing_abstract_count=len(papers),
        has_abstracts=False,
        any_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="official_eccv_virtual",
        official_title_index_verified=complete,
        official_accepted_list_verified=complete,
        truncated=truncated,
        completeness_basis="ECCV official ECVA virtual paper list was parsed for the complete title index and paper detail URLs; abstracts are fetched later from selected detail pages.",
    )
    return _attach_venue_metadata_audit(papers, audit) if papers else papers


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
    requested = int(max_items or 0)
    limit = requested if requested > 0 else 100000
    official_url = f"https://papers.nips.cc/paper_files/paper/{year}"
    official_error = ""
    try:
        papers = _parse_neurips_official_papers_list(_request(official_url, timeout=30).text, official_url, limit)
        if papers:
            for paper in papers:
                paper["year"] = year
            if requested != 1:
                papers = _enrich_neurips_official_with_virtual_presentations(
                    papers,
                    year,
                    raise_errors=False,
                )
            presentation_counts: dict[str, int] = {}
            for paper in papers:
                metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
                presentation = str(paper.get("presentation_type") or metadata.get("presentation_type") or "").strip().lower()
                if presentation:
                    presentation_counts[presentation] = presentation_counts.get(presentation, 0) + 1
            complete = requested <= 0 or len(papers) < requested
            audit = _venue_metadata_audit(
                status="complete" if complete else "partial",
                title_index_completeness_status="complete" if complete else "partial",
                source_verified=True,
                complete=complete,
                title_index_complete=complete,
                official_metadata_complete=False,
                adapter="neurips_official_papers",
                source_url=official_url,
                requested_years=[year],
                paper_count=len(papers),
                source_total_count=len(papers),
                missing_abstract_count=len(papers),
                has_abstracts=False,
                any_abstracts=False,
                has_official_categories=False,
                category_status="no_official_categories",
                source_scope="official_neurips_papers_index",
                official_title_index_verified=complete,
                official_accepted_list_verified=complete,
                presentation_metadata_count=sum(presentation_counts.values()) if presentation_counts else 0,
                presentation_type_counts=presentation_counts,
                presentation_metadata_source="neurips.cc_virtual_pages" if presentation_counts else "",
                completeness_basis="NeurIPS official papers.nips.cc yearly paper index was parsed from paper_files/paper/{year}; it exposes accepted paper titles, authors, URLs, and presentation/acceptance tracks, but no topical category taxonomy or abstracts in the index. NeurIPS virtual pages are merged by title to attach item-level oral/spotlight/poster presentation metadata when available.",
            )
            return _attach_venue_metadata_audit(papers, audit)
    except Exception as exc:
        official_error = str(exc)[:240]
        if raise_errors:
            raise

    list_url = f"https://neurips.cc/virtual/{year}/papers.html"
    try:
        candidates = _parse_neurips_list(_request(list_url).text, list_url, limit)
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
            "metadata": {"venue_url": detail_url, "detail_url": detail_url, "title_index_only": True, "official_papers_error": official_error},
        }
        _set_presentation_metadata(paper, _presentation_type_from_url(detail_url), source="neurips_virtual_url")
        papers.append(paper)
    return papers


_NEURIPS_PRESENTATION_PRIORITY = {
    "best paper/award": 4,
    "oral": 3,
    "spotlight": 2,
    "highlight": 2,
    "poster": 1,
}
_NEURIPS_VIRTUAL_PRESENTATION_INDEX_CACHE: dict[int, dict[tuple[str, int], dict]] = {}


def _neurips_virtual_presentation_index(year: int, *, raise_errors: bool = False) -> dict[tuple[str, int], dict]:
    year_int = int(year or 0)
    if year_int in _NEURIPS_VIRTUAL_PRESENTATION_INDEX_CACHE:
        return _NEURIPS_VIRTUAL_PRESENTATION_INDEX_CACHE[year_int]
    pages = [
        (f"https://neurips.cc/virtual/{year}/events/oral", "oral"),
        (f"https://neurips.cc/virtual/{year}/events/spotlight", "spotlight"),
        (f"https://neurips.cc/virtual/{year}/papers.html", "poster"),
    ]
    index: dict[tuple[str, int], dict] = {}
    for page_url, fallback_type in pages:
        try:
            records = _parse_neurips_list(_request(page_url, timeout=30).text, page_url, 100000)
        except Exception:
            if raise_errors:
                raise
            continue
        for detail_url, title in records:
            key = (_title_key(title), int(year or 0))
            if not key[0]:
                continue
            if fallback_type in {"best paper/award", "oral", "spotlight", "highlight"}:
                presentation = fallback_type
            else:
                presentation = _presentation_type_from_url(detail_url) or fallback_type
            old = index.get(key)
            if old:
                old_type = str(old.get("presentation_type") or "").strip().lower()
                if _NEURIPS_PRESENTATION_PRIORITY.get(old_type, 0) >= _NEURIPS_PRESENTATION_PRIORITY.get(presentation, 0):
                    continue
            paper = {
                "id": stable_id("paper", detail_url),
                "source": "neurips_virtual",
                "title": title,
                "url": detail_url,
                "venue": "NeurIPS",
                "year": year,
                "track": _presentation_display_label("NeurIPS", year, presentation),
                "classification_source": "official",
                "metadata": {
                    "venue_url": page_url,
                    "detail_url": detail_url,
                    "title_index_only": True,
                    "source_page": "neurips.cc_virtual",
                },
            }
            _set_presentation_metadata(paper, presentation, source="neurips_virtual_url")
            index[key] = paper
    _NEURIPS_VIRTUAL_PRESENTATION_INDEX_CACHE[year_int] = index
    return index


def _enrich_neurips_official_with_virtual_presentations(
    papers: list[dict], year: int, *, raise_errors: bool = False
) -> list[dict]:
    if not papers:
        return papers
    presentation_index = _neurips_virtual_presentation_index(year, raise_errors=raise_errors)
    if not presentation_index:
        return papers
    enriched: list[dict] = []
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        key = (_title_key(paper.get("title", "")), int(paper.get("year") or year or 0))
        enrichment = presentation_index.get(key)
        enriched.append(_merge_enrichment(paper, enrichment, "neurips_virtual") if enrichment else paper)
    return enriched


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
    if venue.get("id") == "dblp_www":
        existing_urls = {url for _year, url in links}
        for year in years:
            try:
                year_int = int(year)
            except (TypeError, ValueError):
                continue
            companion_url = f"https://dblp.uni-trier.de/db/conf/www/www{year_int}c.html"
            if companion_url in existing_urls:
                continue
            companion_xml = re.sub(r"\.html?$", ".xml", companion_url)
            try:
                companion_text = _request(companion_xml, timeout=_metadata_timeout(8)).text
            except Exception:
                continue
            if re.search(r"<(?:article|inproceedings)[^>]*>.*?</(?:article|inproceedings)>", companion_text, flags=re.S):
                links.append((year_int, companion_url))
                existing_urls.add(companion_url)
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
    if max_items == 1 and stream_papers:
        return stream_papers[:1]
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
    yeaudits: list[dict[str, Any]] = []
    limit = int(max_items or 0)
    truncated = False
    name = (venue.get("name") or "").lower()
    collection = "emnlp" if "emnlp" in name else ("naacl" if "naacl" in name else "acl")

    # ACL Anthology's event HTML can exceed 10 MB and arrive too slowly for a
    # fresh-clone year probe. Its official source repository exposes the same
    # records as compact XML, including authors and abstracts.
    raw_checked = False
    for year in years:
        year_count = 0
        source_reports: list[dict[str, Any]] = []
        raw_sources = [
            (f"https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/{year}.{collection}.xml", ""),
            (f"https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/{year}.findings.xml", collection),
        ]
        for source_url, findings_volume in raw_sources:
            report: dict[str, Any] = {"source_url": source_url, "paper_count": 0}
            try:
                root = ET.fromstring(_request(source_url).text)
                raw_checked = True
            except Exception as exc:
                if _http_status_code(exc) == 404:
                    raw_checked = True
                report["error"] = str(exc)[:240]
                source_reports.append(report)
                continue
            for volume in root.findall("volume"):
                if findings_volume and str(volume.get("id") or "").lower() != findings_volume:
                    continue
                meta = volume.find("meta")
                booktitle = _clean_text("".join(meta.find("booktitle").itertext())) if meta is not None and meta.find("booktitle") is not None else ""
                for node in volume.findall("paper"):
                    title_node = node.find("title")
                    title = _clean_text("".join(title_node.itertext())) if title_node is not None else ""
                    if not _looks_like_paper_title(title):
                        continue
                    anthology_id = _clean_text(node.findtext("url") or "")
                    if not anthology_id:
                        anthology_id = f"{year}.{collection}-{volume.get('id')}.{node.get('id')}"
                    paper_url = f"https://aclanthology.org/{anthology_id.strip('/')}/"
                    if paper_url in seen:
                        continue
                    seen.add(paper_url)
                    authors: list[str] = []
                    for author_node in node.findall("author"):
                        parts = [
                            _clean_text("".join(part.itertext()))
                            for key in ("first", "middle", "last")
                            for part in author_node.findall(key)
                            if _clean_text("".join(part.itertext()))
                        ]
                        if parts:
                            authors.append(" ".join(parts))
                    abstract_node = node.find("abstract")
                    abstract = _clean_text("".join(abstract_node.itertext())) if abstract_node is not None else ""
                    doi = _clean_text(node.findtext("doi") or "")
                    papers.append({
                        "id": stable_id("paper", paper_url),
                        "source": "acl_anthology",
                        "title": title,
                        "authors": ", ".join(authors),
                        "abstract": abstract,
                        "url": paper_url,
                        "pdf_url": paper_url.rstrip("/") + ".pdf",
                        "venue": venue.get("name", "ACL Anthology"),
                        "year": year,
                        "category": "",
                        "classification_source": "llm_inferred",
                        "metadata": {
                            "venue_id": venue.get("id"),
                            "anthology_id": anthology_id,
                            "anthology_xml_url": source_url,
                            "detail_url": paper_url,
                            "booktitle": booktitle,
                            "doi": doi,
                            "abstract_source": "acl_anthology_xml" if abstract else "",
                            "title_index_only": not bool(abstract),
                        },
                    })
                    report["paper_count"] = int(report.get("paper_count") or 0) + 1
                    year_count += 1
                    if limit > 0 and len(papers) >= limit:
                        truncated = True
                        break
                if truncated:
                    break
            source_reports.append(report)
            if truncated:
                break
        yeaudits.append({
            "year": int(year),
            "sources": source_reports,
            "paper_count": year_count,
            "complete": year_count > 0 and not truncated,
        })
        if truncated:
            break

    if papers or raw_checked:
        complete = bool(papers) and bool(yeaudits) and all(bool(item.get("complete")) for item in yeaudits) and not truncated
        missing_abstracts = sum(1 for paper in papers if not _clean_text(paper.get("abstract")))
        audit = _venue_metadata_audit(
            status="complete" if complete and not missing_abstracts else "partial",
            title_index_completeness_status="complete" if complete else "partial",
            source_verified=bool(papers),
            complete=complete,
            title_index_complete=complete,
            official_metadata_complete=bool(complete and not missing_abstracts),
            adapter="acl_anthology",
            source_adapter="acl_anthology",
            source_url=";".join(str(item.get("source_url") or "") for year_audit in yeaudits for item in (year_audit.get("sources") or []) if item.get("source_url")),
            requested_years=[int(year) for year in years if str(year).isdigit()],
            source_yeaudits=yeaudits,
            paper_count=len(papers),
            missing_abstract_count=missing_abstracts,
            has_abstracts=bool(papers and not missing_abstracts),
            any_abstracts=any(bool(_clean_text(paper.get("abstract"))) for paper in papers),
            has_official_categories=False,
            category_status="no_official_categories",
            source_scope="official_acl_anthology_xml",
            official_title_index_verified=complete,
            official_accepted_list_verified=complete,
            truncated=truncated,
            completeness_basis="ACL Anthology official source XML was parsed for titles, authors, abstracts, PDF links, Anthology IDs, and DOI metadata.",
        )
        return _attach_venue_metadata_audit(papers, audit) if papers else papers

    # Preserve the public event-page fallback when raw.githubusercontent.com is
    # unreachable, rather than making that mirror a new hard dependency.
    yeaudits = []
    for year in years:
        year_count = 0
        event_reports: list[dict[str, Any]] = []
        for url in _acl_event_urls(venue, year):
            report: dict[str, Any] = {"event_url": url, "paper_count": 0}
            try:
                soup = BeautifulSoup(_request(url).text, "html.parser")
            except Exception as exc:
                report["error"] = str(exc)[:240]
                event_reports.append(report)
                continue
            for anchor in soup.find_all("a", href=True):
                title = _clean_text(anchor.get_text(" ", strip=True))
                href = anchor["href"]
                if not _looks_like_paper_title(title):
                    continue
                match = re.search(rf"/{year}\.[a-z0-9-]+\.(\d+)/?$", href)
                if not match:
                    continue
                if int(match.group(1)) == 0:
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
                    "metadata": {"venue_id": venue.get("id"), "anthology_url": url, "detail_url": paper_url, "title_index_only": True},
                })
                report["paper_count"] = int(report.get("paper_count") or 0) + 1
                year_count += 1
                if limit > 0 and len(papers) >= limit:
                    truncated = True
                    break
            event_reports.append(report)
            if truncated:
                break
            time.sleep(0.2)
        yeaudits.append({
            "year": int(year),
            "events": event_reports,
            "paper_count": year_count,
            "complete": year_count > 0 and not truncated and not any(item.get("error") for item in event_reports),
        })
        if truncated:
            break
    complete = bool(papers) and bool(yeaudits) and all(bool(item.get("complete")) for item in yeaudits) and not truncated
    audit = _venue_metadata_audit(
        status="complete" if complete else "partial",
        title_index_completeness_status="complete" if complete else "partial",
        source_verified=bool(papers and not any(item.get("error") for year in yeaudits for item in (year.get("events") or []))),
        complete=complete,
        title_index_complete=complete,
        official_metadata_complete=False,
        adapter="acl_anthology",
        source_adapter="acl_anthology",
        source_url=";".join(str(item.get("event_url") or "") for year in yeaudits for item in (year.get("events") or []) if item.get("event_url")),
        requested_years=[int(year) for year in years if str(year).isdigit()],
        source_yeaudits=yeaudits,
        paper_count=len(papers),
        missing_abstract_count=len(papers),
        has_abstracts=False,
        any_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="official_acl_anthology",
        official_title_index_verified=complete,
        official_accepted_list_verified=complete,
        truncated=truncated,
        completeness_basis="ACL Anthology official event pages were parsed for the complete title index, authors/PDF links when present, and paper detail URLs; abstracts are fetched later from selected detail pages.",
    )
    return _attach_venue_metadata_audit(papers, audit) if papers else papers


def _is_cikm_venue(venue: dict) -> bool:
    text = f"{venue.get('id', '')} {venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return "cikm" in text or "information and knowledge management" in text


def _is_www_venue(venue: dict) -> bool:
    text = f"{venue.get('id', '')} {venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return (
        "dblp_www" in text
        or re.search(r"\bwww\b", text) is not None
        or "web conference" in text
        or "world wide web" in text
    )


def _is_sigir_venue(venue: dict) -> bool:
    text = f"{venue.get('id', '')} {venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return "sigir" in text or "information retrieval" in text


def _track_from_heading(node: Any) -> str:
    current = node
    for _ in range(20):
        current = current.find_previous(["h1", "h2", "h3", "h4", "h5", "strong"])
        if current is None:
            break
        text = _clean_text(current.get_text(" ", strip=True))
        if text and len(text) <= 160:
            return text
    return ""


def fetch_www_official_accepted(venue: dict, years: list[int], max_items: int) -> list[dict]:
    if 2026 not in {int(year) for year in years if str(year).isdigit()}:
        return []
    pages = [
        ("Research Tracks", "https://www2026.thewebconf.org/accepted/research-tracks.html"),
        ("Short Papers", "https://www2026.thewebconf.org/accepted/short-papers.html"),
        ("Workshops", "https://www2026.thewebconf.org/accepted/workshops.html"),
        ("E-Posters", "https://www2026.thewebconf.org/accepted/e-posters.html"),
    ]
    limit = int(max_items or 0)
    papers: list[dict] = []
    seen: set[str] = set()
    page_reports: list[dict[str, Any]] = []
    truncated = False
    pattern = re.compile(r"^\s*\(([^)]+)\)\s+(.+?)\s+[—-]\s+(.+?)\s*$")
    for page_label, source_url in pages:
        report: dict[str, Any] = {"track": page_label, "source_url": source_url, "paper_count": 0}
        try:
            response = _request(source_url, timeout=_metadata_timeout(20))
            if response.status_code == 404 or "Page not found" in response.text[:2000]:
                report["missing"] = True
                page_reports.append(report)
                continue
            soup = BeautifulSoup(response.text, "html.parser")
        except Exception as exc:
            report["error"] = str(exc)[:240]
            page_reports.append(report)
            continue
        for node in soup.find_all("li"):
            code_node = node.select_one(".paper-id")
            authors_node = node.select_one(".paper-authors")
            paper_code = _clean_text(code_node.get_text(" ", strip=True)) if code_node else ""
            authors = _clean_text(authors_node.get_text(" ", strip=True)) if authors_node else ""
            title = ""
            if code_node or authors_node:
                node_copy = BeautifulSoup(str(node), "html.parser")
                for remove in node_copy.select(".paper-id, .paper-authors"):
                    remove.decompose()
                title = _clean_text(node_copy.get_text(" ", strip=True)).strip("—- ").strip()
            else:
                text = _clean_text(node.get_text(" ", strip=True))
                match = pattern.match(text)
                if not match:
                    continue
                paper_code, title, authors = (part.strip() for part in match.groups())
            paper_code = paper_code.strip("()")
            if not _looks_like_paper_title(title):
                continue
            key = _title_key(title)
            if not key or key in seen:
                continue
            seen.add(key)
            track = _track_from_heading(node) or page_label
            paper = {
                "id": stable_id("paper", f"www2026:{paper_code}:{title}"),
                "source": "www_official_accepted",
                "title": title,
                "authors": authors,
                "abstract": "",
                "url": source_url,
                "pdf_url": "",
                "venue": "WWW",
                "year": 2026,
                "category": "",
                "track": track,
                "classification_source": "official_track" if track else "unavailable",
                "metadata": {
                    "venue_id": venue.get("id"),
                    "source_url": source_url,
                    "accepted_page": page_label,
                    "paper_code": paper_code,
                    "category_semantics": "track_or_paper_type_not_verified_topic_taxonomy",
                    "title_index_only": True,
                    "abstract_unavailable_reason": "www2026_official_accepted_page_has_titles_authors_no_abstracts",
                },
            }
            papers.append(paper)
            report["paper_count"] = int(report.get("paper_count") or 0) + 1
            if limit > 0 and len(papers) >= limit:
                truncated = True
                break
        page_reports.append(report)
        if truncated:
            break
    complete_title_index = bool(papers) and not truncated and not any(item.get("error") for item in page_reports)
    audit = _venue_metadata_audit(
        status="complete" if complete_title_index else "partial",
        title_index_completeness_status="complete" if complete_title_index else "partial",
        source_verified=bool(papers and not any(item.get("error") for item in page_reports)),
        complete=complete_title_index,
        title_index_complete=complete_title_index,
        official_metadata_complete=False,
        adapter="www_official_accepted",
        source_adapter="www_official_accepted",
        source_url=";".join(url for _label, url in pages),
        requested_years=[2026],
        source_yeaudits=[{"year": 2026, "pages": page_reports, "paper_count": len(papers), "complete": complete_title_index}],
        paper_count=len(papers),
        missing_abstract_count=len(papers),
        has_abstracts=False,
        any_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="official_www_accepted_title_index",
        official_title_index_verified=complete_title_index,
        official_accepted_list_verified=complete_title_index,
        truncated=truncated,
        completeness_basis="The Web Conference 2026 official accepted-paper pages were parsed for title, authors, track/page, and accepted-paper codes. These pages do not expose abstracts, so this is an official accepted title seed only and cannot be a complete reusable metadata cache.",
    )
    return _attach_venue_metadata_audit(papers, audit) if papers else papers


def _google_redirect_target(url: str) -> str:
    try:
        from urllib.parse import parse_qs, urlparse
    except Exception:
        return url
    parsed = urlparse(str(url or ""))
    if "google.com" not in parsed.netloc:
        return url
    value = (parse_qs(parsed.query).get("q") or [""])[0]
    return value or url


def fetch_sigir_official_proceedings(venue: dict, years: list[int], max_items: int) -> list[dict]:
    if 2025 not in {int(year) for year in years if str(year).isdigit()}:
        return []
    source_url = "https://sigir2025.dei.unipd.it/proceedings.html"
    try:
        soup = BeautifulSoup(_request(source_url, timeout=_metadata_timeout(25)).text, "html.parser")
    except Exception:
        return []
    limit = int(max_items or 0)
    papers: list[dict] = []
    seen: set[str] = set()
    for heading in soup.find_all("h3"):
        title = _clean_text(heading.get_text(" ", strip=True))
        if not _looks_like_paper_title(title):
            continue
        key = _title_key(title)
        if not key or key in seen:
            continue
        seen.add(key)
        href = ""
        current = heading
        for _ in range(10):
            current = current.find_next_sibling() if current is not None else None
            if current is None or getattr(current, "name", "") in {"h2", "h3"}:
                break
            link = current.find("a", href=True)
            if link and re.search(r"dl\.acm\.org/doi/|doi\.org/10\.1145/", str(link.get("href") or ""), re.I):
                href = _google_redirect_target(requests.compat.urljoin(source_url, str(link.get("href") or "")))
                break
        doi = _doi_from_url(href)
        abstract = ""
        current = heading
        for _ in range(8):
            current = current.find_next_sibling() if current is not None else None
            if current is None or getattr(current, "name", "") in {"h2", "h3"}:
                break
            if current is None:
                break
            text = _clean_text(current.get_text(" ", strip=True))
            if not text:
                continue
            lowered = text.lower()
            if lowered.startswith("abstract"):
                text = _strip_abstract_ui_controls(text[len("abstract"):].strip())
            if len(text) >= 80 and "abstract" not in lowered[:20]:
                abstract = text
                break
            if len(text) >= 100:
                abstract = _strip_abstract_ui_controls(text)
                break
        track = _track_from_heading(heading) or "SIGIR 2025 Proceedings"
        papers.append({
            "id": stable_id("paper", doi or href or title),
            "source": "sigir_official_proceedings",
            "title": title,
            "authors": "",
            "abstract": abstract,
            "url": href,
            "pdf_url": "",
            "doi": doi,
            "venue": "SIGIR",
            "year": 2025,
            "category": "",
            "track": track,
            "classification_source": "official_track" if track else "unavailable",
            "metadata": {
                "venue_id": venue.get("id"),
                "source_url": source_url,
                "detail_url": href,
                "doi": doi,
                "category_semantics": "track_or_paper_type_not_verified_topic_taxonomy",
                "abstract_source": "sigir2025_official_proceedings_html" if abstract else "",
            },
        })
        if limit > 0 and len(papers) >= limit:
            break
    missing_abstracts = sum(1 for paper in papers if not _clean_text(paper.get("abstract")))
    truncated = bool(limit > 0 and len(papers) >= limit)
    complete = False
    audit = _venue_metadata_audit(
        status="partial",
        title_index_completeness_status="partial",
        source_verified=bool(papers),
        complete=complete,
        title_index_complete=complete,
        official_metadata_complete=complete,
        adapter="sigir_official_proceedings",
        source_adapter="sigir_official_proceedings",
        source_url=source_url,
        requested_years=[2025],
        paper_count=len(papers),
        source_total_count=len(papers),
        missing_abstract_count=missing_abstracts,
        has_abstracts=bool(papers) and missing_abstracts == 0,
        any_abstracts=bool(papers) and missing_abstracts < len(papers),
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="official_sigir_proceedings_partial_html",
        official_title_index_verified=False,
        official_accepted_list_verified=False,
        truncated=truncated,
        completeness_basis="SIGIR 2025 official proceedings page exposes a partial proceedings mirror with DOI links and inline abstracts for about 100 items, not the full accepted/proceedings corpus. It may enrich matching DBLP/title candidates, but cannot be treated as a complete SIGIR metadata cache.",
    )
    return _attach_venue_metadata_audit(papers, audit) if papers else papers


def fetch_sigir_official_accepted(venue: dict, years: list[int], max_items: int) -> list[dict]:
    if 2025 not in {int(year) for year in years if str(year).isdigit()}:
        return []
    source_url = "https://sigir2025.dei.unipd.it/accepted-papers.html"
    try:
        soup = BeautifulSoup(_request(source_url, timeout=_metadata_timeout(20)).text, "html.parser")
    except Exception:
        return []
    limit = int(max_items or 0)
    papers: list[dict] = []
    seen: set[str] = set()
    ignored_tracks = {"accepted papers"}
    for heading in soup.find_all("h2"):
        track = _clean_text(heading.get_text(" ", strip=True))
        if not track or track.lower() in ignored_tracks:
            continue
        list_node = heading.find_next_sibling("ul")
        if not list_node:
            continue
        for node in list_node.find_all("li", recursive=False):
            text = _clean_text(node.get_text(" ", strip=True))
            if not text or len(text) < 20:
                continue
            if re.search(r"^(Call for|Sponsors|Organizers|Attend|Program|Home|Submit|Poster Instructions|SIGIR Student Grants|Accomodation|Recommended Hotels|Other Hotels)\b", text, re.I):
                continue
            title_node = node.select_one(".accepted-paper-title")
            author_node = node.select_one(".accepted-paper-author")
            if title_node:
                title = _clean_text(title_node.get_text(" ", strip=True))
                authors = _clean_text(author_node.get_text(" ", strip=True)) if author_node else ""
            elif " - " in text:
                title, authors = text.split(" - ", 1)
            elif " — " in text:
                title, authors = text.split(" — ", 1)
            else:
                title, authors = text, ""
            title = _clean_text(title)
            authors = _clean_text(authors)
            if not _looks_like_paper_title(title):
                continue
            key = _title_key(title)
            if not key or key in seen:
                continue
            seen.add(key)
            papers.append({
                "id": stable_id("paper", f"sigir2025:{title}"),
                "source": "sigir_official_accepted",
                "title": title,
                "authors": authors,
                "abstract": "",
                "url": source_url,
                "pdf_url": "",
                "venue": "SIGIR",
                "year": 2025,
                "category": "",
                "track": track,
                "classification_source": "official_track" if track else "unavailable",
                "metadata": {
                    "venue_id": venue.get("id"),
                    "source_url": source_url,
                    "accepted_track": track,
                    "category_semantics": "track_or_paper_type_not_verified_topic_taxonomy",
                    "title_index_only": True,
                    "abstract_unavailable_reason": "sigir2025_official_accepted_page_has_titles_no_abstracts",
                },
            })
            if limit > 0 and len(papers) >= limit:
                break
        if limit > 0 and len(papers) >= limit:
            break
    truncated = bool(limit > 0 and len(papers) >= limit)
    complete_title_index = bool(papers) and not truncated
    audit = _venue_metadata_audit(
        status="complete" if complete_title_index else "partial",
        title_index_completeness_status="complete" if complete_title_index else "partial",
        source_verified=bool(papers),
        complete=complete_title_index,
        title_index_complete=complete_title_index,
        official_metadata_complete=False,
        adapter="sigir_official_accepted",
        source_adapter="sigir_official_accepted",
        source_url=source_url,
        requested_years=[2025],
        paper_count=len(papers),
        source_total_count=len(papers),
        missing_abstract_count=len(papers),
        has_abstracts=False,
        any_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="official_sigir_accepted_title_index",
        official_title_index_verified=complete_title_index,
        official_accepted_list_verified=complete_title_index,
        truncated=truncated,
        completeness_basis="SIGIR 2025 official accepted-paper page was parsed for accepted paper titles and tracks, but it exposes no abstracts. This is an official title seed only and cannot be a complete reusable metadata cache.",
    )
    return _attach_venue_metadata_audit(papers, audit) if papers else papers


def fetch_cikm_official_proceedings(venue: dict, years: list[int], max_items: int) -> list[dict]:
    if 2025 not in {int(year) for year in years if str(year).isdigit()}:
        return []
    source_url = "https://cikm2025.org/program/proceedings"
    try:
        soup = BeautifulSoup(_request(source_url, timeout=_metadata_timeout(20)).text, "html.parser")
    except Exception:
        return []
    limit = int(max_items or 0)
    papers: list[dict] = []
    seen: set[str] = set()
    for title_node in soup.select("a.sub_title_3"):
        title = _clean_text(title_node.get_text(" ", strip=True))
        if not _looks_like_paper_title(title):
            continue
        href = requests.compat.urljoin(source_url, str(title_node.get("href") or ""))
        doi = _doi_from_url(href)
        key = doi.lower() or title.lower()
        if key in seen:
            continue
        seen.add(key)
        authors = ""
        abstract = ""
        current = title_node
        for _ in range(8):
            current = current.find_next_sibling()
            if current is None:
                break
            classes = set(current.get("class") or [])
            if "sub_title_3" in classes or "box_blue2" in classes:
                break
            if current.name == "ul" and "proceed_name_list_han" in classes:
                authors = _clean_text(current.get_text(", ", strip=True))
                continue
            text = _clean_text(current.get_text(" ", strip=True))
            if text and len(text) >= 80 and current.name == "div" and not classes:
                abstract = text
                break
        session = ""
        section = title_node.find_previous(class_="box_blue2")
        if section:
            session = _clean_text(section.get_text(" ", strip=True))
        papers.append({
            "id": stable_id("paper", doi or href or title),
            "source": "cikm_official_proceedings",
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "url": href,
            "pdf_url": "",
            "doi": doi,
            "venue": "CIKM",
            "year": 2025,
            "category": "",
            "track": session,
            "classification_source": "official_track" if session else "unavailable",
            "metadata": {
                "venue_id": venue.get("id"),
                "source_url": source_url,
                "doi": doi,
                "detail_url": href,
                "session": session,
                "category_semantics": "program_session_or_paper_type_not_topic",
                "abstract_source": "cikm_official_proceedings_html" if abstract else "",
            },
        })
        if limit > 0 and len(papers) >= limit:
            break
    missing_abstracts = sum(1 for paper in papers if not _clean_text(paper.get("abstract")))
    truncated = bool(limit > 0 and len(papers) >= limit)
    complete = bool(papers) and missing_abstracts == 0 and not truncated
    audit = _venue_metadata_audit(
        status="complete" if complete else "partial",
        title_index_completeness_status="complete" if complete else "partial",
        source_verified=bool(papers),
        complete=complete,
        title_index_complete=complete,
        official_metadata_complete=complete,
        adapter="cikm_official_proceedings",
        source_adapter="cikm_official_proceedings",
        source_url=source_url,
        requested_years=[2025],
        paper_count=len(papers),
        source_total_count=len(papers),
        missing_abstract_count=missing_abstracts,
        has_abstracts=bool(papers) and missing_abstracts == 0,
        any_abstracts=bool(papers) and missing_abstracts < len(papers),
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="official_cikm_proceedings_html",
        official_title_index_verified=complete,
        official_accepted_list_verified=complete,
        truncated=truncated,
        completeness_basis="CIKM 2025 official proceedings page was parsed for the full ACM proceedings title list, authors, sessions, DOI links, and inline abstracts.",
    )
    return _attach_venue_metadata_audit(papers, audit) if papers else papers


def _aaai_issue_links(year: int) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    archive_url = "https://ojs.aaai.org/index.php/AAAI/issue/archive"
    target = f"AAAI-{str(year)[-2:]}"
    audit: dict[str, Any] = {"archive_url": archive_url, "target": target, "issue_count": 0}
    try:
        soup = BeautifulSoup(_request(archive_url, timeout=30).text, "html.parser")
    except Exception as exc:
        audit["error"] = str(exc)[:240]
        return [], audit
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        label = _clean_text(anchor.get_text(" ", strip=True))
        if target not in label or "Technical Tracks" not in label:
            continue
        href = requests.compat.urljoin(archive_url, str(anchor.get("href") or ""))
        if href in seen:
            continue
        seen.add(href)
        links.append((label, href))
    audit["issue_count"] = len(links)
    return links, audit


def fetch_aaai_ojs(venue: dict, years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    seen: set[str] = set()
    year_audits: list[dict[str, Any]] = []
    limit = int(max_items or 0)
    truncated = False
    for year in years:
        issue_links, issue_audit = _aaai_issue_links(int(year))
        issue_reports: list[dict[str, Any]] = []

        def parse_issue(issue_label: str, issue_url: str, *, retry_pass: bool = False) -> dict[str, Any]:
            nonlocal truncated
            before = len(papers)
            report: dict[str, Any] = {"issue": issue_label, "url": issue_url, "paper_count": 0}
            if retry_pass:
                report["retry_pass"] = True
            issue_html = ""
            last_error = ""
            issue_retries = max(1, int(os.environ.get("AAAI_OJS_ISSUE_RETRIES", "6") or 6))
            issue_spacing = max(0.0, float(os.environ.get("AAAI_OJS_ISSUE_SPACING_SEC", "0.5") or 0.5))
            if issue_spacing:
                time.sleep(issue_spacing)
            for attempt in range(issue_retries):
                if attempt:
                    time.sleep(min(30.0, 5.0 * attempt))
                try:
                    issue_html = _request(issue_url, timeout=45).text
                    last_error = ""
                    break
                except Exception as exc:
                    last_error = str(exc)[:240]
            try:
                if not issue_html:
                    raise RuntimeError(last_error or "empty_issue_html")
            except Exception as exc:
                report["error"] = str(exc)[:240]
                return report
            section = ""
            heading_match = re.search(r"<h[12][^>]*>(.*?)</h[12]>", issue_html, flags=re.I | re.S)
            if heading_match:
                section = _clean_text(BeautifulSoup(heading_match.group(1), "html.parser").get_text(" ", strip=True))
            blocks = re.split(r'<div[^>]*class=["\'][^"\']*\bobj_article_summary\b[^"\']*["\'][^>]*>', issue_html, flags=re.I)
            for block in blocks[1:]:
                next_block = re.split(r'<div[^>]*class=["\'][^"\']*\bobj_article_summary\b[^"\']*["\'][^>]*>', block, maxsplit=1, flags=re.I)[0]
                title_match = re.search(r"<h3[^>]*class=[\"'][^\"']*\btitle\b[^\"']*[\"'][^>]*>.*?<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", next_block, flags=re.I | re.S)
                if not title_match:
                    continue
                article_url = requests.compat.urljoin(issue_url, html.unescape(str(title_match.group(1) or "")))
                title = _clean_text(BeautifulSoup(title_match.group(2), "html.parser").get_text(" ", strip=True))
                if not _looks_like_paper_title(title):
                    continue
                key = _title_key(title) or article_url
                if not key or key in seen:
                    continue
                seen.add(key)
                authors = ""
                authors_match = re.search(r'<div[^>]*class=["\'][^"\']*\bauthors\b[^"\']*["\'][^>]*>(.*?)</div>', next_block, flags=re.I | re.S)
                if authors_match:
                    authors = _clean_text(BeautifulSoup(authors_match.group(1), "html.parser").get_text(" ", strip=True))
                pages = ""
                pages_match = re.search(r'<div[^>]*class=["\'][^"\']*\bpages\b[^"\']*["\'][^>]*>(.*?)</div>', next_block, flags=re.I | re.S)
                if pages_match:
                    pages = _clean_text(BeautifulSoup(pages_match.group(1), "html.parser").get_text(" ", strip=True))
                pdf_url = ""
                for href in re.findall(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>', next_block, flags=re.I | re.S):
                    href_text = html.unescape(str(href or ""))
                    if "/article/view/" in href_text:
                        pdf_url = requests.compat.urljoin(issue_url, href_text)
                        break
                paper = {
                    "id": stable_id("paper", article_url or f"aaai:{year}:{title}"),
                    "source": "aaai_ojs",
                    "title": title,
                    "authors": authors,
                    "abstract": "",
                    "url": article_url,
                    "pdf_url": pdf_url,
                    "venue": "AAAI",
                    "year": int(year),
                    "category": "",
                    "track": section or issue_label,
                    "classification_source": "official_track" if section or issue_label else "unavailable",
                    "metadata": {
                        "venue_id": venue.get("id"),
                        "aaai_issue_url": issue_url,
                        "aaai_issue": issue_label,
                        "category_semantics": "publication_issue_not_topic",
                        "pages": pages,
                        "detail_url": article_url,
                        "title_index_only": True,
                    },
                }
                papers.append(paper)
                report["paper_count"] = int(report.get("paper_count") or 0) + 1
                if limit > 0 and len(papers) >= limit:
                    truncated = True
                    break
            report["deduped_count_added"] = len(papers) - before
            return report

        for issue_label, issue_url in issue_links:
            report = parse_issue(issue_label, issue_url)
            issue_reports.append(report)
            if truncated:
                break
            time.sleep(0.1)
        failed_reports = [item for item in issue_reports if item.get("error")]
        if failed_reports and not truncated:
            time.sleep(max(0.0, float(os.environ.get("AAAI_OJS_FAILED_ISSUE_RETRY_DELAY_SEC", "20") or 20)))
            for failed in list(failed_reports):
                retry = parse_issue(str(failed.get("issue") or ""), str(failed.get("url") or ""), retry_pass=True)
                if not retry.get("error"):
                    issue_reports.remove(failed)
                    issue_reports.append(retry)
        year_audits.append({
            "year": int(year),
            **issue_audit,
            "issues": issue_reports,
            "paper_count": sum(int(item.get("paper_count") or 0) for item in issue_reports),
            "complete": bool(issue_links) and not any(item.get("error") for item in issue_reports) and not truncated,
        })
        if truncated:
            break
    complete = bool(papers) and bool(year_audits) and all(bool(item.get("complete")) for item in year_audits) and not truncated
    audit = _venue_metadata_audit(
        status="complete" if complete else "partial",
        title_index_completeness_status="complete" if complete else "partial",
        source_verified=bool(papers and not any(item.get("error") for item in year_audits)),
        complete=complete,
        title_index_complete=complete,
        official_metadata_complete=False,
        adapter="aaai_ojs",
        source_adapter="aaai_ojs",
        source_url="https://ojs.aaai.org/index.php/AAAI/issue/archive",
        requested_years=[int(year) for year in years if str(year).isdigit()],
        source_yeaudits=year_audits,
        paper_count=len(papers),
        missing_abstract_count=len(papers),
        has_abstracts=False,
        any_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="official_aaai_ojs_proceedings",
        official_title_index_verified=complete,
        official_accepted_list_verified=complete,
        truncated=truncated,
        completeness_basis="AAAI official OJS proceedings issue pages were parsed for accepted article metadata. Numbered issue labels are publication shards, not topical categories; article abstracts are fetched later from selected detail pages.",
    )
    return _attach_venue_metadata_audit(papers, audit) if papers else papers


def fetch_ijcai_proceedings(venue: dict, years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    seen: set[str] = set()
    yeaudits: list[dict[str, Any]] = []
    limit = int(max_items or 0)
    truncated = False
    for year in years:
        url = f"https://www.ijcai.org/proceedings/{int(year)}/"
        report: dict[str, Any] = {"year": int(year), "source_url": url, "paper_count": 0}
        try:
            response = _request(url, timeout=30)
            if response.status_code == 404 or "404 Not Found" in response.text[:10000]:
                report["error"] = "http_404"
                yeaudits.append(report)
                continue
            soup = BeautifulSoup(response.text, "html.parser")
        except Exception as exc:
            report["error"] = str(exc)[:240]
            yeaudits.append(report)
            continue
        for wrapper in soup.select("div.paper_wrapper"):
            title = _clean_text((wrapper.select_one(".title") or wrapper).get_text(" ", strip=True))
            if not _looks_like_paper_title(title):
                continue
            authors = _clean_text((wrapper.select_one(".authors") or "").get_text(" ", strip=True)) if wrapper.select_one(".authors") else ""
            detail_node = wrapper.select_one("a[href*='/proceedings/'][href]")
            pdf_node = wrapper.find("a", string=re.compile(r"pdf", re.I))
            detail_url = requests.compat.urljoin(url, str(detail_node.get("href") or "")) if detail_node else ""
            pdf_url = requests.compat.urljoin(url, str(pdf_node.get("href") or "")) if pdf_node else ""
            key = _title_key(title) or detail_url or pdf_url
            if not key or key in seen:
                continue
            seen.add(key)
            paper = {
                "id": stable_id("paper", detail_url or f"ijcai:{year}:{title}"),
                "source": "ijcai_proceedings",
                "title": title,
                "authors": authors,
                "abstract": "",
                "url": detail_url,
                "pdf_url": pdf_url,
                "venue": "IJCAI",
                "year": int(year),
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": {
                    "venue_id": venue.get("id"),
                    "ijcai_proceedings_url": url,
                    "detail_url": detail_url,
                    "title_index_only": True,
                },
            }
            papers.append(paper)
            report["paper_count"] = int(report.get("paper_count") or 0) + 1
            if limit > 0 and len(papers) >= limit:
                truncated = True
                break
        report["complete"] = int(report.get("paper_count") or 0) > 0 and not truncated
        yeaudits.append(report)
        if truncated:
            break
        time.sleep(0.15)
    complete = bool(papers) and bool(yeaudits) and all(bool(item.get("complete")) for item in yeaudits) and not truncated
    audit = _venue_metadata_audit(
        status="complete" if complete else "partial",
        title_index_completeness_status="complete" if complete else "partial",
        source_verified=bool(papers and not any(item.get("error") for item in yeaudits)),
        complete=complete,
        title_index_complete=complete,
        official_metadata_complete=False,
        adapter="ijcai_proceedings",
        source_adapter="ijcai_proceedings",
        source_url=";".join(str(item.get("source_url") or "") for item in yeaudits if item.get("source_url")),
        requested_years=[int(year) for year in years if str(year).isdigit()],
        source_yeaudits=yeaudits,
        paper_count=len(papers),
        missing_abstract_count=len(papers),
        has_abstracts=False,
        any_abstracts=False,
        has_official_categories=False,
        category_status="no_official_categories",
        source_scope="official_ijcai_proceedings",
        official_title_index_verified=complete,
        official_accepted_list_verified=complete,
        truncated=truncated,
        completeness_basis="IJCAI official proceedings index was parsed for all paper titles, authors, detail URLs, and PDF links; abstracts are fetched later from selected detail pages.",
    )
    return _attach_venue_metadata_audit(papers, audit) if papers else papers



from sources import (
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

    if is_openreview_supported_venue(venue) and is_iclr_venue(venue) and 2026 in years:
        papers = fetch_openreview_iclr_2026(max_items)
        if papers:
            candidates.append(("openreview_reference", papers))
            if max_items == 1 or _source_is_complete_official_title_index(papers, "openreview_reference") or _source_has_confident_official_categories(papers, "openreview_reference", max_items):
                return papers, "openreview_reference"

    if is_neurips_venue(venue):
        papers: list[dict] = []
        for year in years:
            papers.extend(fetch_neurips_title_index(year, max_items))
            if len(papers) >= max_items:
                break
        if papers:
            candidates.append(("neurips_virtual", papers[:max_items]))
            if max_items == 1 or _source_is_complete_official_title_index(papers, "neurips_virtual"):
                return _choose_best_venue_source(candidates, max_items)

    if is_icml_venue(venue):
        if 2026 in years:
            papers = fetch_icml_official_virtual_2026(max_items)
            if papers:
                candidates.append(("icml_official_virtual", papers))
                if max_items == 1 or _source_is_complete_official_title_index(papers, "icml_official_virtual"):
                    return _choose_best_venue_source(candidates, max_items)
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
            if max_items == 1:
                return _choose_best_venue_source(candidates, max_items)

    if _is_cikm_venue(venue):
        papers = fetch_cikm_official_proceedings(venue, years, max_items)
        if papers:
            candidates.append(("cikm_official_proceedings", papers))
            if max_items == 1 or _source_is_complete_official_title_index(papers, "cikm_official_proceedings"):
                return _choose_best_venue_source(candidates, max_items)

    if _is_www_venue(venue):
        papers = fetch_www_official_accepted(venue, years, max_items)
        if papers:
            candidates.append(("www_official_accepted", papers))
            if max_items == 1:
                return _choose_best_venue_source(candidates, max_items)

    if _is_sigir_venue(venue):
        proceedings = fetch_sigir_official_proceedings(venue, years, max_items)
        if proceedings:
            candidates.append(("sigir_official_proceedings", proceedings))
            if max_items == 1:
                return _choose_best_venue_source(candidates, max_items)
        accepted = fetch_sigir_official_accepted(venue, years, max_items)
        if accepted:
            merged, used_adapters = _merge_enrichments(accepted, [("sigir_official_proceedings", proceedings)] if proceedings else [])
            adapter = "sigir_official_accepted"
            if used_adapters:
                adapter = f"{adapter}+{'+'.join(used_adapters)}"
            candidates.append((adapter, merged))

    if is_aaai_venue(venue):
        papers = fetch_aaai_ojs(venue, years, max_items)
        if papers:
            candidates.append(("aaai_ojs", papers))
            if max_items == 1 or _source_is_complete_official_title_index(papers, "aaai_ojs"):
                return _choose_best_venue_source(candidates, max_items)

    if is_ijcai_venue(venue):
        papers = fetch_ijcai_proceedings(venue, years, max_items)
        if papers:
            candidates.append(("ijcai_proceedings", papers))
            if max_items == 1 or _source_is_complete_official_title_index(papers, "ijcai_proceedings"):
                return _choose_best_venue_source(candidates, max_items)

    if is_cvf_venue(venue) and (venue.get("name") or "").upper() != "ECCV":
        papers = fetch_cvf_openaccess(venue, years, max_items)
        if papers:
            candidates.append(("cvf_openaccess", papers))
            if max_items == 1:
                return _choose_best_venue_source(candidates, max_items)
    if (venue.get("name") or "").upper() == "ECCV":
        papers = fetch_eccv_virtual(years, max_items)
        if papers:
            candidates.append(("eccv_virtual", papers))
            if max_items == 1:
                return _choose_best_venue_source(candidates, max_items)

    if is_pmlr_venue(venue):
        papers = fetch_pmlr_index(venue, years, max_items)
        if papers:
            candidates.append(("pmlr", papers))
            if max_items == 1 or _source_is_complete_official_title_index(papers, "pmlr"):
                return _choose_best_venue_source(candidates, max_items)

    if is_openreview_supported_venue(venue):
        papers = fetch_openreview_venue(venue, years, max_items)
        if papers:
            candidates.append(("openreview", papers))
            if max_items == 1 or (not candidates[:-1] and _source_has_confident_official_categories(papers, "openreview", max_items)):
                return papers, "openreview"

    if venue.get("address"):
        papers = fetch_dblp_venue(venue, years, max_items)
        if papers:
            candidates.append(("dblp", papers))

    return _choose_best_venue_source(candidates, max_items)

def _fetch_enrichment_sources(venue: dict, years: list[int]) -> list[tuple[str, list[dict]]]:
    enrichments: list[tuple[str, list[dict]]] = []
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
    if _is_cikm_venue(venue):
        papers = fetch_cikm_official_proceedings(venue, years, 100000)
        if papers:
            enrichments.append(("cikm_official_proceedings", papers))
    if _is_www_venue(venue):
        papers = fetch_www_official_accepted(venue, years, 100000)
        if papers:
            enrichments.append(("www_official_accepted", papers))
    if _is_sigir_venue(venue):
        papers = fetch_sigir_official_proceedings(venue, years, 100000)
        if papers:
            enrichments.append(("sigir_official_proceedings", papers))
        papers = fetch_sigir_official_accepted(venue, years, 100000)
        if papers:
            enrichments.append(("sigir_official_accepted", papers))
    if is_aaai_venue(venue):
        papers = fetch_aaai_ojs(venue, years, 100000)
        if papers:
            enrichments.append(("aaai_ojs", papers))
    if is_ijcai_venue(venue):
        papers = fetch_ijcai_proceedings(venue, years, 100000)
        if papers:
            enrichments.append(("ijcai_proceedings", papers))
    if is_cvf_venue(venue) and (venue.get("name") or "").upper() != "ECCV":
        papers = fetch_cvf_openaccess(venue, years, 100000)
        if papers:
            enrichments.append(("cvf_openaccess", papers))
    if (venue.get("name") or "").upper() == "ECCV":
        papers = fetch_eccv_virtual(years, 100000)
        if papers:
            enrichments.append(("eccv_virtual", papers))
    if is_icml_venue(venue):
        papers = fetch_icml_official_virtual_2026(100000) if 2026 in years else []
        if papers:
            enrichments.append(("icml_official_virtual", papers))
        papers = fetch_icml_downloads(years, 100000)
        if papers:
            enrichments.append(("icml_downloads", papers))
    if is_pmlr_venue(venue):
        papers = fetch_pmlr_index(venue, years, 100000)
        if papers:
            enrichments.append(("pmlr", papers))
    if is_openreview_supported_venue(venue):
        papers = fetch_openreview_venue(venue, years, 100000)
        if papers:
            enrichments.append(("openreview", papers))
    return enrichments


def fetch_venue_title_index_all(venue: dict, years: list[int]) -> tuple[list[dict], str]:
    """Fetch the complete venue/year corpus with official-category sources preferred globally."""
    requested_limit = 100000
    candidates: list[tuple[str, list[dict]]] = []

    if is_openreview_supported_venue(venue) and is_iclr_venue(venue) and 2026 in years:
        papers = fetch_openreview_iclr_2026(requested_limit)
        if papers:
            candidates.append(("openreview_reference", papers))
            if _source_is_complete_official_title_index(papers, "openreview_reference") or _source_has_confident_official_categories(papers, "openreview_reference", requested_limit):
                return papers, "openreview_reference"

    if is_neurips_venue(venue):
        papers: list[dict] = []
        for year in years:
            papers.extend(fetch_neurips_title_index(year, requested_limit))
        if papers:
            candidates.append(("neurips_virtual", papers))
            if _source_is_complete_official_title_index(papers, "neurips_virtual"):
                return _choose_best_venue_source(candidates, requested_limit)

    if is_acl_family_venue(venue):
        papers = fetch_acl_anthology(venue, years, requested_limit)
        if papers:
            candidates.append(("acl_anthology", papers))

    if _is_cikm_venue(venue):
        papers = fetch_cikm_official_proceedings(venue, years, requested_limit)
        if papers:
            candidates.append(("cikm_official_proceedings", papers))
            if _source_is_complete_official_title_index(papers, "cikm_official_proceedings"):
                return _choose_best_venue_source(candidates, requested_limit)

    if _is_www_venue(venue):
        papers = fetch_www_official_accepted(venue, years, requested_limit)
        if papers:
            candidates.append(("www_official_accepted", papers))

    if _is_sigir_venue(venue):
        proceedings = fetch_sigir_official_proceedings(venue, years, requested_limit)
        if proceedings:
            candidates.append(("sigir_official_proceedings", proceedings))
        accepted = fetch_sigir_official_accepted(venue, years, requested_limit)
        if accepted:
            merged, used_adapters = _merge_enrichments(accepted, [("sigir_official_proceedings", proceedings)] if proceedings else [])
            adapter = "sigir_official_accepted"
            if used_adapters:
                adapter = f"{adapter}+{'+'.join(used_adapters)}"
            candidates.append((adapter, merged))

    if is_aaai_venue(venue):
        papers = fetch_aaai_ojs(venue, years, requested_limit)
        if papers:
            candidates.append(("aaai_ojs", papers))
            if _source_is_complete_official_title_index(papers, "aaai_ojs"):
                return _choose_best_venue_source(candidates, requested_limit)

    if is_ijcai_venue(venue):
        papers = fetch_ijcai_proceedings(venue, years, requested_limit)
        if papers:
            candidates.append(("ijcai_proceedings", papers))
            if _source_is_complete_official_title_index(papers, "ijcai_proceedings"):
                return _choose_best_venue_source(candidates, requested_limit)

    if is_cvf_venue(venue) and (venue.get("name") or "").upper() != "ECCV":
        papers = fetch_cvf_openaccess(venue, years, requested_limit)
        if papers:
            candidates.append(("cvf_openaccess", papers))
    if (venue.get("name") or "").upper() == "ECCV":
        papers = fetch_eccv_virtual(years, requested_limit)
        if papers:
            candidates.append(("eccv_virtual", papers))

    if is_icml_venue(venue):
        if 2026 in years:
            papers = fetch_icml_official_virtual_2026(requested_limit)
            if papers:
                candidates.append(("icml_official_virtual", papers))
                if _source_is_complete_official_title_index(papers, "icml_official_virtual"):
                    return _choose_best_venue_source(candidates, requested_limit)
        papers = fetch_icml_downloads(years, requested_limit)
        if papers:
            candidates.append(("icml_downloads", papers))
            if _source_is_complete_official_title_index(papers, "icml_downloads"):
                return _choose_best_venue_source(candidates, requested_limit)

    if is_pmlr_venue(venue):
        papers = fetch_pmlr_index(venue, years, requested_limit)
        if papers:
            candidates.append(("pmlr", papers))
            if _source_is_complete_official_title_index(papers, "pmlr"):
                return _choose_best_venue_source(candidates, requested_limit)

    if is_openreview_supported_venue(venue):
        papers = fetch_openreview_venue(venue, years, requested_limit)
        if papers:
            candidates.append(("openreview", papers))
            if not candidates[:-1] and _source_has_confident_official_categories(papers, "openreview", requested_limit):
                return papers, "openreview"

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
    if not paper.get("abstract") and paper.get("pdf_url"):
        abstract = _extract_acl_pdf_abstract(str(paper.get("pdf_url") or ""), request_timeout)
        if abstract:
            paper["abstract"] = abstract
            metadata["abstract_source"] = f"{detail_label}_pdf"
            result["abstract_filled"] = True
    if paper.get("abstract") or paper.get("authors") or paper.get("pdf_url"):
        metadata["detail_source"] = detail_label
    if not paper.get("pdf_url"):
        metadata.setdefault("full_text_locator_status", "official_virtual_abstract_page_without_pdf_link")
    return result


def _official_detail_source(paper: dict) -> str:
    source = str(paper.get("source") or "").strip()
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    url = str(metadata.get("detail_url") or paper.get("url") or "").strip()
    if source == "neurips_official_papers" and "papers.nips.cc/paper_files/paper/" in url:
        return "neurips_official_papers"
    acm_url = str(metadata.get("acm_abs_url") or metadata.get("acm_full_html_url") or "").strip()
    doi = str(metadata.get("doi") or paper.get("doi") or "").strip().lower()
    if source == "dblp" and (acm_url or doi.startswith("10.1145/")):
        return "acm_dl"
    if source == "aaai_ojs" and "ojs.aaai.org" in url:
        return "aaai_ojs"
    if source == "ijcai_proceedings" and "ijcai.org/proceedings/" in url:
        return "ijcai_proceedings"
    if source == "cvf_openaccess" and "openaccess.thecvf.com" in url:
        return "cvf_openaccess"
    if source == "acl_anthology" and "aclanthology.org" in url:
        return "acl_anthology"
    return ""


def _official_detail_url(paper: dict) -> str:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    if _official_detail_source(paper) == "acm_dl":
        doi = str(metadata.get("doi") or paper.get("doi") or "").strip()
        if not metadata.get("acm_abs_url") and doi:
            metadata.update(_acm_metadata_from_doi(doi))
        return str(metadata.get("acm_abs_url") or metadata.get("acm_full_html_url") or metadata.get("doi_url") or paper.get("url") or "").strip()
    return str(metadata.get("detail_url") or paper.get("url") or "").strip()


def _meta_values(soup: BeautifulSoup, name: str) -> list[str]:
    values: list[str] = []
    for node in soup.find_all("meta", attrs={"name": name}):
        text = _clean_text(str(node.get("content") or ""))
        if text:
            values.append(text)
    return values


def _extract_aaai_abstract(soup: BeautifulSoup) -> str:
    for name in ["DC.Description", "description"]:
        for value in _meta_values(soup, name):
            if len(value) >= 80:
                return value
    for selector in ["section.item.abstract", ".item.abstract"]:
        node = soup.select_one(selector)
        if not node:
            continue
        text = _clean_text(node.get_text(" ", strip=True))
        if text.lower().startswith("abstract "):
            text = text[len("abstract "):].strip()
        if len(text) >= 80:
            return text
    return ""


def _extract_acm_abstract(soup: BeautifulSoup) -> str:
    for name in ["citation_abstract", "description", "dc.Description", "DC.Description"]:
        for value in _meta_values(soup, name):
            text = _strip_abstract_ui_controls(value)
            if len(text) >= 80:
                return text
    for selector in [
        "div.abstractSection",
        "section.abstract",
        "div.abstract",
        "section[aria-labelledby*='abstract']",
        "[id*='abstract']",
        "[class*='abstract']",
    ]:
        node = soup.select_one(selector)
        if not node:
            continue
        text = _strip_abstract_ui_controls(node.get_text(" ", strip=True))
        lowered = text.lower()
        if lowered.startswith("abstract "):
            text = text[len("abstract "):].strip()
        if len(text) >= 80 and "just a moment" not in lowered:
            return text
    return ""


def _extract_cvf_abstract(soup: BeautifulSoup) -> str:
    node = soup.select_one("#abstract, div#abstract")
    if node:
        text = _strip_abstract_ui_controls(node.get_text(" ", strip=True))
        if len(text) >= 80:
            return text
    return _extract_conference_virtual_abstract(soup)


def _extract_ijcai_abstract(soup: BeautifulSoup) -> str:
    text = soup.get_text("\n", strip=True)
    marker = "\nPDF\nBibTeX\n"
    body = text.split(marker, 1)[1] if marker in text else text
    body = body.split("\nKeywords:", 1)[0]
    body = body.split("\nCopyright", 1)[0]
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    abstract_lines: list[str] = []
    for line in lines:
        lowered = line.lower()
        if lowered in {"pdf", "bibtex"} or lowered.startswith("https://doi.org/"):
            continue
        if re.search(r"^proceedings of .* pages \d", lowered):
            continue
        if len(line) >= 40 or abstract_lines:
            abstract_lines.append(line)
    abstract = _clean_text(" ".join(abstract_lines))
    return abstract if len(abstract) >= 80 else ""


def _extract_acl_anthology_abstract(soup: BeautifulSoup) -> str:
    node = soup.select_one("div.acl-abstract, .acl-abstract")
    if not node:
        return ""
    text = _strip_abstract_ui_controls(node.get_text(" ", strip=True))
    if text.lower().startswith("abstract "):
        text = text[len("abstract "):].strip()
    return text if len(text) >= 80 else ""


def _extract_pdf_text(url: str, request_timeout: int, *, max_pages: int = 2) -> str:
    if not url:
        return ""
    try:
        import fitz  # type: ignore
    except Exception:
        return ""
    try:
        response = _request(url, timeout=request_timeout)
        data = response.content
        if not data or b"%PDF" not in data[:1024]:
            return ""
        document = fitz.open(stream=data, filetype="pdf")
        try:
            page_count = min(max_pages, len(document))
            return "\n".join(document[index].get_text() for index in range(page_count))
        finally:
            document.close()
    except Exception:
        return ""


def _clean_pdf_abstract_text(value: str) -> str:
    text = _clean_text(value)
    text = re.sub(r"([A-Za-z])- ([a-z])", r"\1\2", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"^[\s.,;:]+", "", text)
    return text.strip()


def _extract_acl_pdf_abstract(pdf_url: str, request_timeout: int) -> str:
    return _extract_acl_pdf_abstract_from_text(_extract_pdf_text(pdf_url, request_timeout, max_pages=2))


def _extract_acl_pdf_abstract_from_text(text: str) -> str:
    match = re.search(
        r"(?is)\bAbstract\b\s*(.*?)(?:\n\s*(?:1\s+Introduction|Introduction|Keywords|Index Terms|1\s+[A-Z][^\n]{2,80})\b)",
        text,
    )
    if not match:
        return ""
    abstract = _clean_pdf_abstract_text(match.group(1))
    return abstract if len(abstract) >= 80 else ""


def _official_detail_request(url: str, request_timeout: int, source: str) -> str:
    retries = max(1, _positive_int_env("OFFICIAL_DETAIL_REQUEST_RETRIES", 3))
    if source == "aaai_ojs":
        retries = max(retries, _positive_int_env("AAAI_OJS_DETAIL_RETRIES", 5))
    try:
        spacing = max(0.0, float(os.environ.get("OFFICIAL_DETAIL_RETRY_BACKOFF_SEC", "2.0") or 2.0))
    except (TypeError, ValueError):
        spacing = 2.0
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return _request(url, timeout=request_timeout).text
        except Exception as exc:
            last_error = exc
            lowered = str(exc).lower()
            retryable = any(token in lowered for token in ("429", "500", "502", "503", "504", "temporarily", "timeout", "timed out", "connection reset"))
            if attempt >= retries or not retryable:
                break
            time.sleep(min(30.0, spacing * attempt))
    if last_error:
        raise last_error
    raise RuntimeError(f"Official detail request failed: {url}")


def _fetch_one_official_detail(paper: dict, request_timeout: int) -> dict:
    source = _official_detail_source(paper)
    url = _official_detail_url(paper)
    result = {"abstract_filled": False, "authors_filled": False, "pdf_filled": False, "error": ""}
    if not source or not url:
        return result
    acm_cache_key = ""
    if source == "acm_dl":
        doi = _paper_doi(paper)
        if doi:
            acm_cache_key = f"doi:{doi}"
            with _ACM_DETAIL_CACHE_LOCK:
                cached = _load_acm_detail_abstract_cache().get(acm_cache_key)
            if isinstance(cached, dict) and cached.get("abstract") and not paper.get("abstract"):
                if _apply_acm_detail_cached_abstract(paper, cached):
                    result["abstract_filled"] = True
                if cached.get("pdf_url") and not paper.get("pdf_url"):
                    paper["pdf_url"] = str(cached.get("pdf_url") or "")
                    result["pdf_filled"] = True
                return result
        if str(os.environ.get("ACM_DETAIL_CACHE_ONLY") or "").strip().lower() in {"1", "true", "yes", "on"}:
            metadata = paper.setdefault("metadata", {})
            if not paper.get("pdf_url") and str(metadata.get("acm_pdf_url") or "").strip():
                paper["pdf_url"] = str(metadata.get("acm_pdf_url") or "").strip()
                result["pdf_filled"] = True
            result["error"] = "acm_detail_cache_miss"
            metadata["detail_fetch_error"] = result["error"]
            return result
    try:
        detail_html = _official_detail_request(url, request_timeout, source)
        soup = BeautifulSoup(detail_html, "html.parser")
    except Exception as exc:
        result["error"] = str(exc)[:240]
        metadata = paper.setdefault("metadata", {})
        metadata["detail_fetch_error"] = result["error"]
        if source == "acm_dl" and not paper.get("pdf_url") and str(metadata.get("acm_pdf_url") or "").strip():
            paper["pdf_url"] = str(metadata.get("acm_pdf_url") or "").strip()
            result["pdf_filled"] = True
        if source == "acm_dl" and acm_cache_key:
            with _ACM_DETAIL_CACHE_LOCK:
                cache = _load_acm_detail_abstract_cache()
                existing = cache.get(acm_cache_key)
                if not (isinstance(existing, dict) and existing.get("abstract")):
                    cache[acm_cache_key] = {
                        "title": paper.get("title", ""),
                        "miss": True,
                        "source": "acm_dl_detail",
                        "error": result["error"],
                        "retryable": _acm_pdf_error_retryable([result["error"]]),
                        "updated_at": datetime.utcnow().isoformat() + "Z",
                    }
                    _save_acm_detail_abstract_cache(cache)
        return result
    metadata = paper.setdefault("metadata", {})
    if source == "neurips_official_papers":
        parsed = _parse_neurips_detail(detail_html, url, str(paper.get("title") or ""), int(paper.get("year") or 0))
        if parsed.get("title") and not paper.get("title"):
            paper["title"] = parsed.get("title")
        if parsed.get("authors") and not paper.get("authors"):
            paper["authors"] = parsed.get("authors")
            metadata["authors_source"] = source
            result["authors_filled"] = True
        if parsed.get("abstract") and not paper.get("abstract"):
            paper["abstract"] = parsed.get("abstract")
            metadata["abstract_source"] = source
            result["abstract_filled"] = True
        if parsed.get("pdf_url") and not paper.get("pdf_url"):
            paper["pdf_url"] = parsed.get("pdf_url")
            result["pdf_filled"] = True
        if parsed.get("url") and not paper.get("openreview_url"):
            metadata["openreview_url"] = parsed.get("url")
        if paper.get("abstract") or paper.get("authors") or paper.get("pdf_url"):
            metadata["detail_source"] = source
        return result
    if not paper.get("authors"):
        authors = _meta_values(soup, "citation_author")
        if authors:
            paper["authors"] = ", ".join(authors)
            metadata["authors_source"] = source
            result["authors_filled"] = True
    if not paper.get("abstract"):
        if source == "aaai_ojs":
            abstract = _extract_aaai_abstract(soup)
        elif source == "acm_dl":
            abstract = _extract_acm_abstract(soup)
        elif source == "ijcai_proceedings":
            abstract = _extract_ijcai_abstract(soup)
        elif source == "cvf_openaccess":
            abstract = _extract_cvf_abstract(soup)
        elif source == "acl_anthology":
            abstract = _extract_acl_anthology_abstract(soup)
            if not abstract:
                pdf_url = str(paper.get("pdf_url") or "").strip()
                if not pdf_url:
                    pdf_values = _meta_values(soup, "citation_pdf_url")
                    pdf_url = pdf_values[0] if pdf_values else ""
                pdf_text = _extract_pdf_text(pdf_url, request_timeout, max_pages=2)
                abstract = _extract_acl_pdf_abstract_from_text(pdf_text)
                if abstract:
                    metadata["abstract_source"] = "acl_anthology_pdf"
                elif pdf_text:
                    metadata["abstract_unavailable_verified"] = "acl_anthology_pdf_no_abstract_heading"
        else:
            abstract = ""
        if abstract:
            paper["abstract"] = abstract
            metadata.setdefault("abstract_source", "acm_dl_detail" if source == "acm_dl" else source)
            result["abstract_filled"] = True
            if source == "acm_dl" and acm_cache_key:
                with _ACM_DETAIL_CACHE_LOCK:
                    cache = _load_acm_detail_abstract_cache()
                    cache[acm_cache_key] = {
                        "title": paper.get("title", ""),
                        "abstract": abstract,
                        "url": url,
                        "pdf_url": paper.get("pdf_url") or metadata.get("acm_pdf_url") or "",
                        "source": "acm_dl_detail",
                        "miss": False,
                        "updated_at": datetime.utcnow().isoformat() + "Z",
                    }
                    _save_acm_detail_abstract_cache(cache)
    if not paper.get("pdf_url"):
        if source == "acm_dl" and str(metadata.get("acm_pdf_url") or "").strip():
            paper["pdf_url"] = str(metadata.get("acm_pdf_url") or "").strip()
            result["pdf_filled"] = True
        for meta_name in ["citation_pdf_url"]:
            values = _meta_values(soup, meta_name)
            if values:
                paper["pdf_url"] = values[0]
                result["pdf_filled"] = True
                break
        if not paper.get("pdf_url"):
            pdf_link = soup.find("a", href=re.compile(r"\.pdf(?:$|\?)", re.I))
            if pdf_link and pdf_link.get("href"):
                paper["pdf_url"] = requests.compat.urljoin(url, str(pdf_link.get("href") or ""))
                result["pdf_filled"] = True
    if paper.get("abstract") or paper.get("authors") or paper.get("pdf_url"):
        metadata["detail_source"] = source
    elif source == "acm_dl" and soup.title and "just a moment" in soup.title.get_text(" ", strip=True).lower():
        result["error"] = "acm_dl_blocked_by_cloudflare_or_challenge"
        metadata["detail_fetch_error"] = result["error"]
        if acm_cache_key:
            with _ACM_DETAIL_CACHE_LOCK:
                cache = _load_acm_detail_abstract_cache()
                existing = cache.get(acm_cache_key)
                if not (isinstance(existing, dict) and existing.get("abstract")):
                    cache[acm_cache_key] = {
                        "title": paper.get("title", ""),
                        "miss": True,
                        "source": "acm_dl_detail",
                        "error": result["error"],
                        "retryable": True,
                        "updated_at": datetime.utcnow().isoformat() + "Z",
                    }
                    _save_acm_detail_abstract_cache(cache)
    return result


def enrich_official_details(papers: list[dict], *, wall_timeout_sec: float | None = None, should_cancel: Callable[[], bool] | None = None) -> tuple[list[dict], dict]:
    targets = [paper for paper in papers if _official_detail_source(paper)]
    if not targets:
        return papers, {"attempted": 0, "abstracts_filled": 0, "authors_filled": 0, "pdfs_filled": 0, "deferred": 0, "sources": {}}
    attempted = 0
    abstracts_filled = 0
    authors_filled = 0
    pdfs_filled = 0
    timed_out = False
    cancelled = False
    started = time.monotonic()
    wall_timeout = wall_timeout_sec if wall_timeout_sec is not None else _positive_float_env("OFFICIAL_DETAIL_WALL_TIMEOUT_SEC", _positive_float_env("VENUE_DETAIL_WALL_TIMEOUT_SEC", 180.0))
    request_timeout = int(_positive_float_env("OFFICIAL_DETAIL_REQUEST_TIMEOUT_SEC", _metadata_timeout(8)))
    worker_default = min(16, max(1, len(targets)))
    max_workers = int(_positive_float_env("OFFICIAL_DETAIL_WORKERS", worker_default))
    max_workers = max(1, min(32, max_workers, max(1, len(targets))))
    cancel_check = should_cancel or (lambda: False)
    source_counts: dict[str, int] = {}
    for paper in targets:
        source = _official_detail_source(paper)
        source_counts[source] = source_counts.get(source, 0) + 1
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
        future_to_paper[executor.submit(_fetch_one_official_detail, paper, request_timeout)] = paper
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


def enrich_conference_virtual_details(papers: list[dict], limit: int | None = None, *, wall_timeout_sec: float | None = None, should_cancel: Callable[[], bool] | None = None) -> tuple[list[dict], dict]:
    attempted = 0
    abstracts_filled = 0
    authors_filled = 0
    pdfs_filled = 0
    timed_out = False
    cancelled = False
    candidates = papers if limit is None else papers[:limit]
    targets = [paper for paper in candidates if _conference_virtual_detail_source(paper)]
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


def fetch_selected_venue_details(papers: list[dict], *, should_cancel: Callable[[], bool] | None = None, wall_timeout_sec: float | None = None) -> list[dict]:
    details: list[dict] = []
    neurips_by_year: dict[int, list[dict]] = {}
    conference_virtual: list[dict] = []
    official_detail: list[dict] = []
    cancel_check = should_cancel or (lambda: False)
    for candidate in papers:
        if cancel_check():
            _mark_detail_fetch_deferred(candidate, 'cancel_requested')
            details.append(candidate)
            continue
        metadata = candidate.get('metadata') if isinstance(candidate.get('metadata'), dict) else {}
        if candidate.get('source') == 'neurips_virtual' and metadata.get('title_index_only'):
            neurips_by_year.setdefault(int(candidate.get('year') or date.today().year), []).append(candidate)
        elif _conference_virtual_detail_source(candidate):
            conference_virtual.append(candidate)
        elif _official_detail_source(candidate):
            official_detail.append(candidate)
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
    if official_detail:
        enriched, stats = enrich_official_details(official_detail, wall_timeout_sec=wall_timeout_sec, should_cancel=cancel_check)
        enriched, acm_stats = enrich_acm_doi_with_indexed_abstracts(enriched)
        for item in enriched:
            if isinstance(item.get('metadata'), dict):
                item['metadata'].setdefault('detail_fetch_stats', stats)
                if acm_stats.get("openalex", {}).get("attempted") or acm_stats.get("semantic_scholar", {}).get("attempted"):
                    item['metadata'].setdefault('acm_indexed_abstract_enrichment_stats', acm_stats)
        details.extend(enriched)
    return details

def fetch_venue_sample(venue: dict, year: int, sample_limit: int = 3) -> dict:
    adapter = "dblp"
    try:
        limit = max(1, int(sample_limit or 1))
        papers, adapter = fetch_venue_title_index(venue, [year], limit)
        papers = fetch_selected_venue_details(
            papers[:limit],
            wall_timeout_sec=_positive_float_env("VENUE_HEALTH_DETAIL_WALL_TIMEOUT_SEC", 30.0),
        )
        samples = [
            {
                "title": paper.get("title", ""),
                "url": paper.get("url", ""),
                "abstract": (paper.get("abstract", "") or "")[:300],
            }
            for paper in papers[:limit]
        ]
        missing_abstracts = sum(1 for sample in samples if not _clean_text(sample.get("abstract")))
        metadata_ok = bool(samples) and missing_abstracts == 0
        return {
            "venue_id": venue.get("id", ""),
            "year": year,
            "ok": metadata_ok,
            "sample_count": len(samples),
            "source_adapter": adapter,
            "message": "ok" if metadata_ok else (f"No papers fetched via {adapter}." if not samples else f"{missing_abstracts}/{len(samples)} sampled papers still lack abstracts after live detail enrichment via {adapter}."),
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
    spacing = _positive_float_env("SEMANTIC_SCHOLAR_REQUEST_SPACING_SEC", 0.2 if api_key else 1.0)
    cache_only = str(os.environ.get("SEMANTIC_SCHOLAR_CACHE_ONLY") or "").strip().lower() in {"1", "true", "yes", "on"}
    retry_misses = str(os.environ.get("SEMANTIC_SCHOLAR_RETRY_MISSES") or "").strip().lower() in {"1", "true", "yes", "on"}

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
        if keys and not retry_misses and all(_semantic_scholar_cache_is_permanent_miss(cache.get(key)) for key in keys):
            continue
        if cache_only:
            continue

        doi = ""
        for key in keys:
            if key.startswith("doi:"):
                doi = key.split(":", 1)[1]
                break
        urls: list[tuple[str, str]] = []
        if doi:
            urls.append(("semantic_scholar_doi", f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields={fields}"))
        query = quote_plus(re.sub(r"[():/\-]", " ", paper.get("title", "")))
        title_fallback_enabled = str(os.environ.get("SEMANTIC_SCHOLAR_TITLE_FALLBACK", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}
        if query and (title_fallback_enabled or not doi):
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
        if spacing > 0:
            time.sleep(spacing)
    if cache_changed:
        _save_semantic_scholar_cache(cache)
    return papers


def enrich_with_arxiv_title_match(papers: list[dict], limit: int = 40) -> list[dict]:
    # Metadata enrichment only: original venue rows still need final title+abstract LLM scoring.
    ns = {"a": "http://www.w3.org/2005/Atom"}
    timeout = max(3, int(os.environ.get("ARXIV_TITLE_MATCH_TIMEOUT_SEC", "8") or 8))
    max_results = max(1, min(5, int(os.environ.get("ARXIV_TITLE_MATCH_MAX_RESULTS", "3") or 3)))
    max_queries_per_paper = max(1, min(3, int(os.environ.get("ARXIV_TITLE_MATCH_MAX_QUERIES", "2") or 2)))
    min_similarity = max(0.5, min(0.99, _positive_float_env("ARXIV_TITLE_MATCH_MIN_SIMILARITY", 0.92)))
    max_network_requests = _positive_int_env("ARXIV_TITLE_MATCH_MAX_NETWORK_REQUESTS", 20)
    network_requests = 0
    rate_limited = False
    for paper in papers[:limit]:
        if paper.get("abstract") and paper.get("pdf_url"):
            continue
        title = _clean_text(str(paper.get("title") or ""))
        if not title:
            continue
        if rate_limited:
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
            if max_network_requests > 0 and network_requests >= max_network_requests:
                query_errors.append("network_request_budget_exhausted")
                rate_limited = True
                break
            url = "https://export.arxiv.org/api/query?search_query=" + quote_plus(query_text) + f"&sortBy=submittedDate&sortOrder=descending&start=0&max_results={max_results}"
            try:
                network_requests += 1
                response = requests.get(url, headers=HEADERS, timeout=(min(5, timeout), timeout))
                if response.status_code == 429:
                    query_errors.append("http_429")
                    rate_limited = True
                    break
                response.raise_for_status()
                root = ET.fromstring(response.text)
            except Exception as exc:
                query_errors.append(str(exc)[:120])
                continue
            for entry in root.findall("a:entry", ns):
                candidate_title = " ".join((entry.findtext("a:title", default="", namespaces=ns) or "").split())
                similarity = _title_token_similarity(title, candidate_title)
                if similarity <= best_similarity:
                    continue
                candidate_authors = [node.text or "" for node in entry.findall("a:author/a:name", ns)]
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
        if not best or best_similarity < min_similarity or not (best.get("abstract") or paper.get("abstract")):
            metadata = paper.setdefault("metadata", {})
            if query_errors:
                metadata["arxiv_title_match_error"] = "; ".join(query_errors[:3])
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
        time.sleep(0.35)
    return papers


def _clean_arxiv_web_abstract(value: str) -> str:
    text = _clean_text(value)
    text = re.sub(r"\s*△\s*Less\s*$", "", text).strip()
    text = re.sub(r"^Abstract:\s*", "", text, flags=re.I).strip()
    return text


def enrich_with_arxiv_web_title_match(papers: list[dict], limit: int = 40) -> list[dict]:
    timeout = max(3, int(os.environ.get("ARXIV_WEB_TITLE_MATCH_TIMEOUT_SEC", "12") or 12))
    max_results = max(1, min(25, _positive_int_env("ARXIV_WEB_TITLE_MATCH_MAX_RESULTS", 5)))
    min_similarity = max(0.5, min(0.99, _positive_float_env("ARXIV_WEB_TITLE_MATCH_MIN_SIMILARITY", 0.92)))
    max_network_requests = _positive_int_env("ARXIV_WEB_TITLE_MATCH_MAX_NETWORK_REQUESTS", 10)
    spacing = _positive_float_env("ARXIV_WEB_TITLE_MATCH_SPACING_SEC", 2.0)
    network_requests = 0
    rate_limited = False
    for paper in papers[:limit]:
        if paper.get("abstract"):
            continue
        title = _clean_text(str(paper.get("title") or ""))
        if not title:
            continue
        if rate_limited:
            continue
        if max_network_requests > 0 and network_requests >= max_network_requests:
            rate_limited = True
            continue
        url = (
            "https://arxiv.org/search/?query="
            + quote_plus(title)
            + f"&searchtype=all&abstracts=show&order=-announced_date_first&size={max_results}"
        )
        try:
            network_requests += 1
            response = requests.get(url, headers=HEADERS, timeout=(min(5, timeout), timeout))
            if response.status_code == 429:
                paper.setdefault("metadata", {})["arxiv_web_title_match_error"] = "http_429"
                rate_limited = True
                continue
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
        except Exception as exc:
            paper.setdefault("metadata", {})["arxiv_web_title_match_error"] = str(exc)[:160]
            continue
        best: dict[str, Any] = {}
        best_similarity = 0.0
        expected_authors = _author_family_tokens(paper.get("authors"))
        for item in soup.select("li.arxiv-result")[:max_results]:
            title_node = item.select_one("p.title")
            abstract_node = item.select_one("span.abstract-full")
            link_node = item.select_one("p.list-title a")
            candidate_title = _clean_text(title_node.get_text(" ", strip=True) if title_node else "")
            similarity = _title_token_similarity(title, candidate_title)
            if similarity <= best_similarity:
                continue
            candidate_authors = [node.get_text(" ", strip=True) for node in item.select("p.authors a")]
            candidate_family = _author_family_tokens(candidate_authors)
            if expected_authors and candidate_family and not (expected_authors & candidate_family):
                continue
            abstract = _clean_arxiv_web_abstract(abstract_node.get_text(" ", strip=True) if abstract_node else "")
            href = str(link_node.get("href") or "") if link_node else ""
            best_similarity = similarity
            best = {
                "title": candidate_title,
                "abstract": abstract,
                "url": href,
                "pdf_url": href.replace("/abs/", "/pdf/") if "/abs/" in href else "",
                "arxiv_id": _arxiv_entry_id(href),
                "similarity": similarity,
            }
        if not best or best_similarity < min_similarity or not best.get("abstract"):
            if spacing > 0:
                time.sleep(spacing)
            continue
        paper["abstract"] = best["abstract"]
        paper["url"] = paper.get("url") or best["url"]
        paper["pdf_url"] = paper.get("pdf_url") or best["pdf_url"]
        metadata = paper.setdefault("metadata", {})
        metadata["abstract_source"] = "arxiv_web_title_match"
        metadata["arxiv_title_match_id"] = best["arxiv_id"]
        metadata["arxiv_title_similarity"] = round(best_similarity, 4)
        metadata["arxiv_title_match_title"] = best["title"]
        if spacing > 0:
            time.sleep(spacing)
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


def _journal_search_phrases(search_phrases: list[str] | None, *, max_phrases: int = 12) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for phrase in search_phrases or []:
        text = " ".join(str(phrase or "").split()).strip()
        if len(text) < 3:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= max(1, max_phrases):
            break
    return out


def fetch_nature_portfolio(
    journals: list[str],
    article_types: list[str],
    max_items: int | None = None,
    start_date: str = "",
    end_date: str = "",
    enrich_details: bool = True,
    search_phrases: list[str] | None = None,
) -> tuple[list[dict], dict]:
    start_date = normalize_date(start_date)
    end_date = normalize_date(end_date) or date.today().isoformat()
    journals = [journal.strip().strip("/") for journal in journals if journal.strip()] or ["nature"]
    article_types = [item.strip() for item in article_types if item.strip()] or ["article"]
    search_phrases = _journal_search_phrases(search_phrases)
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
        "targeted_pages": [],
        "targeted_search_phrases": search_phrases,
        "stopped_reason": "",
    }
    by_key: dict[str, dict] = {}
    candidate_limit = max(1, int(max_items)) if max_items is not None else None
    raw_item_limit = _positive_int_env("NATURE_RAW_MAX_ITEMS", 0)
    item_limit = raw_item_limit or None
    date_window_days = _date_window_days(start_date, end_date)
    estimated_pages_for_window = max(10, min(100, (date_window_days // 7 + 1) * 3)) if date_window_days else 60
    default_max_pages = estimated_pages_for_window
    max_pages_per_journal = _positive_int_env("NATURE_MAX_PAGES_PER_JOURNAL", default_max_pages)
    max_pages = max(1, min(250, max_pages_per_journal or default_max_pages))
    request_timeout = _positive_int_env("NATURE_REQUEST_TIMEOUT_SEC", 12)
    status["candidate_limit"] = candidate_limit
    status["raw_item_limit"] = item_limit
    status["max_pages_per_journal"] = max_pages
    status["request_timeout_sec"] = request_timeout
    status["date_window_days"] = date_window_days

    def coverage_reached_start() -> bool:
        if not start_date:
            return True
        dates = [
            normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10])
            for paper in by_key.values()
        ]
        dates = [value for value in dates if value]
        return bool(dates) and not _coverage_misses_start(dates, start_date)

    def reached_limit() -> bool:
        if item_limit is None or len(by_key) < item_limit:
            return False
        return coverage_reached_start()

    def item_limit_stop_reason() -> str:
        return "target count reached after date coverage" if start_date else "item limit"

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

    def openalex_nature_url(phrase: str, page: int, per_page: int, issn: str = "") -> str:
        filters: list[str] = []
        if issn:
            filters.append(f"primary_location.source.issn:{issn}")
        if start_date:
            filters.append(f"from_publication_date:{start_date}")
        if end_date:
            filters.append(f"to_publication_date:{end_date}")
        params = {
            "search": phrase,
            "sort": "publication_date:desc",
            "select": "id,doi,display_name,publication_date,authorships,primary_location,type,abstract_inverted_index,open_access,locations",
            "per-page": max(1, min(200, per_page)),
            "page": max(1, page),
            "mailto": _contact_mailto(),
        }
        if filters:
            params["filter"] = ",".join(filters)
        return "https://api.openalex.org/works?" + urlencode(params)

    def parse_openalex_nature_items(items: list[dict], slug: str, phrase: str, expected_issn: str = "") -> list[dict]:
        journal = _nature_journal_meta(slug)
        journal_name = str(journal.get("name") or slug or "").strip()
        expected_issn = str(expected_issn or "").strip().lower()
        papers: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            primary_location = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
            source = primary_location.get("source") if isinstance(primary_location.get("source"), dict) else {}
            venue = _clean_text(str(source.get("display_name") or ""))
            source_issns = source.get("issn") if isinstance(source.get("issn"), list) else []
            source_issn_values = {str(value).strip().lower() for value in source_issns if str(value).strip()}
            source_issn_l = str(source.get("issn_l") or "").strip().lower()
            if expected_issn:
                if expected_issn not in source_issn_values and expected_issn != source_issn_l:
                    continue
            elif journal_name and venue.lower() != journal_name.lower():
                continue
            title = _clean_text(str(item.get("display_name") or ""))
            if not title or not _looks_like_paper_title(title):
                continue
            published = normalize_date(str(item.get("publication_date") or ""))
            if not _in_date_range(published, start_date, end_date):
                continue
            doi_url = str(item.get("doi") or "").strip()
            doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi_url, flags=re.I)
            authors: list[str] = []
            for authorship in item.get("authorships") or []:
                if not isinstance(authorship, dict):
                    continue
                author = authorship.get("author") if isinstance(authorship.get("author"), dict) else {}
                name = _clean_text(str(author.get("display_name") or ""))
                if name:
                    authors.append(name)
                if len(authors) >= 12:
                    break
            abstract = _openalex_abstract_from_inverted_index(item.get("abstract_inverted_index") or {})
            landing_url = _openalex_landing_url(item)
            pdf_url = _openalex_pdf_url(item)
            year = int(published[:4]) if published[:4].isdigit() else date.today().year
            papers.append({
                "id": stable_id("nature", doi or item.get("id") or title),
                "source": "nature",
                "title": title,
                "authors": ", ".join(authors),
                "abstract": abstract,
                "url": landing_url or (f"https://doi.org/{doi}" if doi else str(item.get("id") or "")),
                "pdf_url": pdf_url,
                "venue": venue or journal_name,
                "year": year,
                "category": str(item.get("type") or "journal-article"),
                "classification_source": "openalex_keyword_search",
                "metadata": {
                    "journal_slug": slug,
                    "journal_tier": journal.get("tier", ""),
                    "journal_group": journal.get("group", ""),
                    "article_type": str(item.get("type") or "journal-article"),
                    "doi": doi,
                    "published": published,
                    "openalex_id": item.get("id") or "",
                    "openalex_landing_url": landing_url,
                    "openalex_pdf_url": pdf_url,
                    "openalex_search_phrase": phrase,
                    "openalex_source_issn": expected_issn,
                    "abstract_source": "openalex" if abstract else "",
                },
            })
        return papers

    def add_openalex_nature_targeted(slug: str) -> None:
        if not search_phrases:
            return
        targeted_setting = os.environ.get("NATURE_TARGETED", "1").strip().lower()
        if targeted_setting in {"0", "false", "no", "off"}:
            return
        per_page = max(1, min(200, _positive_int_env("NATURE_TARGETED_PER_PAGE", 200)))
        default_targeted_pages = max(10, min(100, (date_window_days // 14 + 2) if date_window_days else 30))
        max_pages = _positive_int_env("NATURE_TARGETED_MAX_PAGES_PER_PHRASE", default_targeted_pages)
        timeout = _positive_int_env("OPENALEX_REQUEST_TIMEOUT_SEC", 20)
        retries = _positive_int_env("OPENALEX_REQUEST_RETRIES", 4)
        try:
            spacing_sec = max(0.0, float(os.environ.get("OPENALEX_REQUEST_SPACING_SEC", "3.0") or 3.0))
        except (TypeError, ValueError):
            spacing_sec = 3.0
        pages: list[dict] = status.setdefault("targeted_pages", [])
        journal = _nature_journal_meta(slug)
        issn = str(journal.get("issn") or "").strip()
        if issn:
            status["targeted_search_filter_kind"] = "openalex_issn"
        elif not status.get("targeted_search_filter_kind"):
            status["targeted_search_filter_kind"] = "openalex_source_name"
        for phrase in search_phrases:
            for page in range(1, max_pages + 1):
                if item_limit is not None and len(by_key) >= item_limit:
                    status["raw_item_limit_reached"] = True
                    status["stopped_reason"] = "raw item limit"
                    return
                url = openalex_nature_url(phrase, page, per_page, issn)
                page_report = {"journal": slug, "issn": issn, "phrase": phrase, "page": page, "rows": per_page, "url": url, "count": 0, "added": 0, "ok": False, "message": ""}
                last_error = ""
                payload: dict | None = None
                for attempt in range(1, max(1, retries) + 1):
                    try:
                        payload = _request(url, timeout=timeout).json()
                        if spacing_sec:
                            time.sleep(spacing_sec)
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        if attempt < max(1, retries):
                            time.sleep(min(20.0, max(2.0, spacing_sec) * attempt * 2))
                if payload is None:
                    page_report["message"] = last_error or "request failed"
                    status["errors"].append(f"{slug}/openalex_targeted/{phrase}/{page}: {page_report['message']}")
                    status["targeted_query_error"] = True
                    pages.append(page_report)
                    break
                results = payload.get("results") if isinstance(payload, dict) else []
                if not isinstance(results, list):
                    results = []
                papers = parse_openalex_nature_items(results, slug, phrase, issn)
                added = add_papers(papers)
                page_report.update({"count": len(papers), "added": added, "ok": bool(papers), "message": "ok" if papers else "empty targeted page"})
                pages.append(page_report)
                status["targeted_search_used"] = True
                status["targeted_search_count"] = int(status.get("targeted_search_count") or 0) + len(papers)
                if not results or len(results) < per_page:
                    break
            else:
                status["targeted_page_limit_reached"] = True
                status["limited"] = True

    def older_than_start(papers: list[dict]) -> bool:
        if not start_date:
            return False
        dates = [
            normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10])
            for paper in papers
        ]
        dates = [value for value in dates if value]
        return bool(dates) and max(dates) < start_date

    targeted_only = bool(search_phrases) and os.environ.get("NATURE_TARGETED_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}
    for slug in journals:
        add_openalex_nature_targeted(slug)
    if status.get("targeted_search_used") and not status.get("targeted_page_limit_reached") and not status.get("raw_item_limit_reached") and not status.get("targeted_query_error"):
        status["targeted_query_exhausted"] = True
    if targeted_only:
        status["targeted_only"] = True
        status["stopped_reason"] = status.get("stopped_reason") or "targeted search only"
    for slug in ([] if targeted_only else journals):
        for article_type in article_types:
            status["bulk_fallback_used"] = True
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
                    status["stopped_reason"] = item_limit_stop_reason()
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
    page_reports = status.get("pages") if isinstance(status.get("pages"), list) else []
    status["pages_scanned"] = len(page_reports)
    dates = [
        normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10])
        for paper in papers
    ]
    dates = [value for value in dates if value]
    coverage_limited = _coverage_misses_start(dates, start_date)
    if targeted_only and status.get("targeted_query_exhausted") and not status.get("targeted_page_limit_reached"):
        coverage_limited = False
        status["targeted_query_coverage_basis"] = "OpenAlex keyword query exhausted; absence of older in-window topic hits is not a date-coverage failure."
    if coverage_limited and dates and start_date and status.get("stopped_reason") == "date boundary":
        tolerance_days = _positive_int_env("NATURE_COVERAGE_TOLERANCE_DAYS", 7)
        gap_days = _coverage_gap_days(dates, start_date)
        if 0 < gap_days <= tolerance_days:
            coverage_limited = False
            status["coverage_tolerance_applied"] = True
            status["coverage_tolerance_days"] = tolerance_days
            status["coverage_gap_days"] = gap_days
            status["coverage_tolerance_reason"] = "Nature issue/listing cadence: crawl reached the date boundary and the first in-window article is within tolerance after requested start_date."
    if coverage_limited:
        status["coverage_limited"] = True
        if status.get("stopped_reason") in {"", "item limit"}:
            status["stopped_reason"] = "date coverage incomplete"
    status["raw_item_limit_reached"] = bool(item_limit is not None and len(papers) >= item_limit)
    status["target_count_reached"] = False
    status["limited"] = bool(status.get("raw_item_limit_reached") or coverage_limited or status.get("stopped_reason") == "safety page limit" or status.get("targeted_page_limit_reached"))
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
        if status.get("coverage_tolerance_applied"):
            message += f"; coverage tolerance applied for requested start_date {start_date}"
        if coverage_limited and start_date:
            message += f"; coverage did not reach requested start_date {start_date}"
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
    search_phrases: list[str] | None = None,
) -> tuple[list[dict], dict]:
    start_date = normalize_date(start_date)
    end_date = normalize_date(end_date) or date.today().isoformat()
    journals = [journal.strip() for journal in journals if journal.strip()] or ["science"]
    article_types = [item.strip() for item in article_types if item.strip()] or ["Research Article"]
    allowed_types = {item.lower() for item in article_types if item.lower() not in {"all", "*"}}
    search_phrases = _journal_search_phrases(search_phrases)
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
        "targeted_search_phrases": search_phrases,
        "stopped_reason": "",
    }
    by_key: dict[str, dict] = {}
    candidate_limit = max(1, int(max_items)) if max_items is not None else None
    raw_item_limit = _positive_int_env("SCIENCE_RAW_MAX_ITEMS", 0)
    item_limit = raw_item_limit or None
    rows = 100
    date_window_days = _date_window_days(start_date, end_date)
    estimated_pages_for_window = max(8, min(50, date_window_days // 20 + 2)) if date_window_days else 20
    default_crossref_pages = estimated_pages_for_window
    max_crossref_pages = _positive_int_env("SCIENCE_MAX_CROSSREF_PAGES_PER_JOURNAL", default_crossref_pages)
    request_timeout = _positive_int_env("SCIENCE_REQUEST_TIMEOUT_SEC", 20)
    request_retries = _positive_int_env("SCIENCE_REQUEST_RETRIES", 3)
    status["candidate_limit"] = candidate_limit
    status["raw_item_limit"] = item_limit
    status["max_crossref_pages_per_journal"] = max_crossref_pages or None
    status["request_timeout_sec"] = request_timeout
    status["request_retries"] = request_retries
    status["date_window_days"] = date_window_days

    def request_crossref_payload(url: str) -> dict:
        last_error: Exception | None = None
        for attempt in range(1, max(1, request_retries) + 1):
            try:
                return _request(url, timeout=request_timeout).json()
            except Exception as exc:
                last_error = exc
                if attempt < max(1, request_retries):
                    time.sleep(min(5.0, 0.75 * attempt))
        if last_error:
            raise last_error
        raise RuntimeError("Science Crossref request failed")

    def coverage_reached_start() -> bool:
        if not start_date:
            return True
        dates = [
            normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10])
            for paper in by_key.values()
        ]
        dates = [value for value in dates if value]
        return bool(dates) and not _coverage_misses_start(dates, start_date)

    def reached_limit() -> bool:
        if item_limit is None or len(by_key) < item_limit:
            return False
        return coverage_reached_start()

    def item_limit_stop_reason() -> str:
        return "target count reached after date coverage" if start_date else "item limit"

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

    def openalex_science_url(issn: str, page: int, per_page: int, phrase: str = "") -> str:
        filters = [f"primary_location.source.issn:{issn}"]
        if start_date:
            filters.append(f"from_publication_date:{start_date}")
        if end_date:
            filters.append(f"to_publication_date:{end_date}")
        params = {
            "filter": ",".join(filters),
            "sort": "publication_date:desc",
            "select": "id,doi,display_name,publication_date,authorships,primary_location,type,abstract_inverted_index,open_access,locations",
            "per-page": max(1, min(200, per_page)),
            "page": max(1, page),
            "mailto": _contact_mailto(),
        }
        if phrase:
            params["search"] = phrase
        return "https://api.openalex.org/works?" + urlencode(params)

    def parse_openalex_science_items(items: list[dict], slug: str) -> list[dict]:
        journal = _science_journal_meta(slug)
        papers: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            doi_url = str(item.get("doi") or "").strip()
            doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi_url, flags=re.I)
            title = _clean_text(str(item.get("display_name") or ""))
            if not title or not _looks_like_paper_title(title):
                continue
            published = normalize_date(str(item.get("publication_date") or ""))
            if not _in_date_range(published, start_date, end_date):
                continue
            primary_location = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
            source = primary_location.get("source") if isinstance(primary_location.get("source"), dict) else {}
            venue = str(source.get("display_name") or journal["name"])
            authors: list[str] = []
            for authorship in item.get("authorships") or []:
                if not isinstance(authorship, dict):
                    continue
                author = authorship.get("author") if isinstance(authorship.get("author"), dict) else {}
                name = _clean_text(str(author.get("display_name") or ""))
                if name:
                    authors.append(name)
                if len(authors) >= 12:
                    break
            abstract = _openalex_abstract_from_inverted_index(item.get("abstract_inverted_index") or {})
            pdf_url = _openalex_pdf_url(item)
            landing_url = _openalex_landing_url(item)
            url = _science_abs_url(doi, landing_url or str(item.get("id") or ""))
            year = int(published[:4]) if published[:4].isdigit() else date.today().year
            papers.append({
                "id": stable_id("science", doi or item.get("id") or title),
                "source": "science",
                "title": title,
                "authors": ", ".join(authors),
                "abstract": abstract,
                "url": url,
                "pdf_url": _science_pdf_url(doi) or pdf_url,
                "venue": venue,
                "year": year,
                "category": str(item.get("type") or "journal-article"),
                "classification_source": "openalex",
                "metadata": {
                    "journal_slug": slug,
                    "journal_tier": journal.get("tier", ""),
                    "journal_group": journal.get("group", ""),
                    "article_type": str(item.get("type") or "journal-article"),
                    "doi": doi,
                    "published": published,
                    "openalex_id": item.get("id") or "",
                    "openalex_landing_url": landing_url,
                    "openalex_pdf_url": pdf_url,
                    "abstract_source": "openalex" if abstract else "",
                },
            })
        return papers

    def add_openalex_science_fallback(slug: str, issn: str, phrases: list[str] | None = None, *, targeted: bool = False) -> None:
        openalex_setting = os.environ.get("SCIENCE_OPENALEX_FALLBACK", "").strip().lower()
        if openalex_setting in {"0", "false", "no", "off"}:
            return
        if os.environ.get("PYTEST_CURRENT_TEST") and openalex_setting not in {"1", "true", "yes", "on"}:
            return
        per_page = 200
        default_openalex_pages = max(10, min(100, (date_window_days // 14 + 2) if date_window_days else 30))
        max_pages = _positive_int_env("SCIENCE_OPENALEX_MAX_PAGES_PER_JOURNAL", default_openalex_pages)
        timeout = _positive_int_env("OPENALEX_REQUEST_TIMEOUT_SEC", 20)
        retries = _positive_int_env("OPENALEX_REQUEST_RETRIES", 4)
        try:
            spacing_sec = max(0.0, float(os.environ.get("OPENALEX_REQUEST_SPACING_SEC", "3.0") or 3.0))
        except (TypeError, ValueError):
            spacing_sec = 3.0
        pages: list[dict] = status.setdefault("targeted_pages" if targeted else "openalex_pages", [])

        def request_openalex_payload(url: str) -> dict:
            last_error: Exception | None = None
            for attempt in range(1, max(1, retries) + 1):
                try:
                    payload = _request(url, timeout=timeout).json()
                    if spacing_sec:
                        time.sleep(spacing_sec)
                    return payload
                except Exception as exc:
                    last_error = exc
                    if attempt < max(1, retries):
                        time.sleep(min(20.0, max(2.0, spacing_sec) * attempt * 2))
            if last_error:
                raise last_error
            raise RuntimeError("Science OpenAlex request failed")

        phrase_list = phrases if phrases else [""]
        for phrase in phrase_list:
            for page in range(1, max_pages + 1):
                if item_limit is not None and len(by_key) >= item_limit:
                    status["raw_item_limit_reached"] = True
                    status["stopped_reason"] = "raw item limit"
                    break
                if (not targeted) and reached_limit():
                    status["stopped_reason"] = item_limit_stop_reason()
                    break
                url = openalex_science_url(issn, page, per_page, phrase)
                page_report = {"journal": slug, "phrase": phrase, "page": page, "rows": per_page, "url": url, "count": 0, "added": 0, "ok": False, "message": ""}
                try:
                    payload = request_openalex_payload(url)
                    results = payload.get("results") if isinstance(payload, dict) else []
                    if not isinstance(results, list):
                        results = []
                    papers = parse_openalex_science_items(results, slug)
                    added = add_papers(papers)
                    page_report.update({"count": len(papers), "added": added, "ok": bool(papers), "message": "ok" if papers else "empty openalex page"})
                    if targeted:
                        status["targeted_search_used"] = True
                        status["targeted_search_filter_kind"] = "openalex_issn"
                        status["targeted_search_count"] = int(status.get("targeted_search_count") or 0) + len(papers)
                    else:
                        status["openalex_fallback_used"] = True
                        status["openalex_count"] = int(status.get("openalex_count") or 0) + len(papers)
                except Exception as exc:
                    page_report["message"] = str(exc)
                    status["errors"].append(f"{slug}/openalex/{phrase or 'fallback'}/{page}: {exc}")
                    if targeted:
                        status["targeted_query_error"] = True
                    pages.append(page_report)
                    break
                pages.append(page_report)
                if not results:
                    break
                if len(results) < per_page:
                    break
                time.sleep(0.1)
            else:
                if targeted:
                    status["targeted_page_limit_reached"] = True
                    status["limited"] = True
                else:
                    status["openalex_page_limit_reached"] = True
                    status["limited"] = True
            if item_limit is not None and len(by_key) >= item_limit:
                break

    def science_toc_url(slug: str, volume: str = "", issue: str = "") -> str:
        if volume and issue:
            return f"https://www.science.org/toc/{slug}/{volume}/{issue}"
        return f"https://www.science.org/toc/{slug}/current"

    def science_toc_issue_meta(soup: BeautifulSoup) -> dict:
        for script in soup.find_all("script"):
            text = script.string or script.get_text() or ""
            match = re.search(r"AAASdataLayer=({.*?});if\(AAASdataLayer", text, re.S)
            if not match:
                continue
            try:
                payload = json.loads(match.group(1))
            except Exception:
                continue
            page_info = payload.get("page", {}).get("pageInfo", {})
            return page_info if isinstance(page_info, dict) else {}
        return {}

    def science_toc_date(text: str, fallback: str = "") -> str:
        text = " ".join(str(text or "").split()).strip()
        if not text:
            return normalize_date(fallback)
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                pass
        return normalize_date(text[:10]) or normalize_date(fallback)

    def parse_science_toc_items(soup: BeautifulSoup, slug: str, page_url: str) -> list[dict]:
        issue_meta = science_toc_issue_meta(soup)
        issue_date = normalize_date(str(issue_meta.get("issueDate") or ""))
        journal = _science_journal_meta(slug)
        papers: list[dict] = []
        for card in soup.select("div.card"):
            link = card.select_one("h3.article-title a[href*='/doi/']")
            if not link:
                continue
            href = str(link.get("href") or "")
            if "/doi/" not in href:
                continue
            doi = href.split("/doi/", 1)[1]
            doi = re.sub(r"^(abs|full|epdf)/", "", doi).strip()
            title = _clean_text(link.get_text(" ", strip=True))
            if not doi or not _looks_like_paper_title(title):
                continue
            time_node = card.select_one("time")
            published = science_toc_date(time_node.get_text(" ", strip=True) if time_node else "", issue_date)
            if not _in_date_range(published, start_date, end_date):
                continue
            section_node = card.find_previous("h5", class_=lambda value: value and "to-section" in str(value))
            section = _clean_text(section_node.get_text(" ", strip=True)) if section_node else "Science TOC"
            body = card.select_one(".card-body")
            abstract = _clean_text(body.get_text(" ", strip=True)) if body else ""
            abstract = re.sub(r"^Abstract\s*", "", abstract, flags=re.I).strip()
            authors = [
                _clean_text(node.get_text(" ", strip=True))
                for node in card.select(".card-contribs ul[title='list of authors'] li span")
            ]
            authors = [author for author in authors if author]
            year = int(published[:4]) if published[:4].isdigit() else date.today().year
            papers.append({
                "id": stable_id("science", doi),
                "source": "science",
                "title": title,
                "authors": ", ".join(authors[:12]),
                "abstract": abstract,
                "url": _science_abs_url(doi, "https://www.science.org" + href if href.startswith("/") else href),
                "pdf_url": _science_pdf_url(doi),
                "venue": journal["name"],
                "year": year,
                "category": section,
                "classification_source": "official_science_toc",
                "metadata": {
                    "journal_slug": slug,
                    "journal_tier": journal.get("tier", ""),
                    "journal_group": journal.get("group", ""),
                    "article_type": section,
                    "doi": doi,
                    "published": published,
                    "issue_date": issue_date,
                    "volume": str(issue_meta.get("volume") or ""),
                    "issue": str(issue_meta.get("issue") or ""),
                    "toc_url": page_url,
                    "abstract_source": "science_toc" if abstract else "",
                },
            })
        return papers

    def add_science_toc_fallback(slug: str) -> None:
        toc_setting = os.environ.get("SCIENCE_TOC_FALLBACK", "").strip().lower()
        if toc_setting in {"0", "false", "no", "off"}:
            return
        if os.environ.get("PYTEST_CURRENT_TEST") and toc_setting not in {"1", "true", "yes", "on"}:
            return
        max_issues = _positive_int_env("SCIENCE_TOC_MAX_ISSUES", 70)
        timeout = _positive_int_env("SCIENCE_TOC_REQUEST_TIMEOUT_SEC", 20)
        retries = _positive_int_env("SCIENCE_TOC_REQUEST_RETRIES", 4)
        try:
            spacing_sec = max(0.0, float(os.environ.get("SCIENCE_TOC_REQUEST_SPACING_SEC", "5.0") or 5.0))
        except (TypeError, ValueError):
            spacing_sec = 5.0
        pages: list[dict] = status.setdefault("toc_pages", [])
        next_url = science_toc_url(slug)
        seen_pages: set[str] = set()
        connect_timeout = max(3, min(10, timeout))
        read_timeout = max(5, min(15, timeout))
        for _ in range(max_issues):
            if reached_limit():
                status["stopped_reason"] = item_limit_stop_reason()
                break
            if not next_url or next_url in seen_pages:
                break
            seen_pages.add(next_url)
            page_url = next_url if next_url.startswith("http") else "https://www.science.org" + next_url
            page_report = {"journal": slug, "url": page_url, "count": 0, "added": 0, "ok": False, "message": ""}
            html_text = ""
            last_error = ""
            for attempt in range(1, max(1, retries) + 1):
                try:
                    response = requests.get(page_url, headers=HEADERS, timeout=(connect_timeout, read_timeout), allow_redirects=True)
                    response.raise_for_status()
                    html_text = response.text
                    break
                except Exception as exc:
                    last_error = str(exc)
                    if attempt < max(1, retries):
                        time.sleep(min(30.0, max(5.0, spacing_sec * attempt * 3)))
            if not html_text:
                page_report["message"] = last_error or "empty response"
                status["errors"].append(f"{slug}/toc/{page_url}: {page_report['message']}")
                pages.append(page_report)
                break
            soup = BeautifulSoup(html_text, "html.parser")
            issue_meta = science_toc_issue_meta(soup)
            page_report["volume"] = str(issue_meta.get("volume") or "")
            page_report["issue"] = str(issue_meta.get("issue") or "")
            page_report["issue_date"] = normalize_date(str(issue_meta.get("issueDate") or ""))
            papers = parse_science_toc_items(soup, slug, page_url)
            added = add_papers(papers)
            page_report.update({"count": len(papers), "added": added, "ok": bool(papers), "message": "ok" if papers else "no toc article cards"})
            status["toc_fallback_used"] = True
            status["toc_count"] = int(status.get("toc_count") or 0) + len(papers)
            pages.append(page_report)
            if page_report.get("issue_date") and start_date and str(page_report["issue_date"]) < start_date:
                status["stopped_reason"] = "date boundary"
                break
            previous = ""
            for link in soup.find_all("a", href=True):
                label = _clean_text(link.get_text(" ", strip=True)).lower()
                href = str(link.get("href") or "")
                if f"/toc/{slug}/" in href and ("previous issue" in label or label == "previous"):
                    previous = href
                    break
            if not previous:
                break
            next_url = previous
            if spacing_sec:
                time.sleep(spacing_sec)

    def add_science_boundary_toc_fallback(slug: str) -> None:
        boundary_setting = os.environ.get("SCIENCE_BOUNDARY_TOC_FALLBACK", "").strip().lower()
        if boundary_setting in {"0", "false", "no", "off"}:
            return
        if os.environ.get("PYTEST_CURRENT_TEST") and boundary_setting not in {"1", "true", "yes", "on"}:
            return
        if slug != "science" or not start_date:
            return
        timeout = _positive_int_env("SCIENCE_TOC_REQUEST_TIMEOUT_SEC", 12)
        try:
            start_dt = date.fromisoformat(start_date)
        except ValueError:
            return
        current_payload = ""
        try:
            current_payload = requests.get(science_toc_url(slug), headers=HEADERS, timeout=(5, max(5, min(12, timeout))), allow_redirects=True).text
        except Exception as exc:
            status["errors"].append(f"{slug}/boundary_toc/current: {exc}")
            return
        current_soup = BeautifulSoup(current_payload, "html.parser")
        current_meta = science_toc_issue_meta(current_soup)
        try:
            current_issue = int(str(current_meta.get("issue") or "0"))
            current_volume = int(str(current_meta.get("volume") or "0"))
            current_issue_date = date.fromisoformat(normalize_date(str(current_meta.get("issueDate") or "")))
        except Exception:
            return
        week_delta = max(0, round((current_issue_date - start_dt).days / 7))
        target_issue = current_issue - week_delta
        estimated_volume = max(1, current_volume - round(week_delta / 13))
        candidate_pairs: list[tuple[int, int]] = []
        for issue_number in [target_issue, target_issue - 1, target_issue - 2, target_issue + 1]:
            if issue_number > 0 and estimated_volume > 0:
                candidate_pairs.append((estimated_volume, issue_number))
        candidate_pairs.append((current_volume, target_issue))
        pages: list[dict] = status.setdefault("boundary_toc_pages", [])
        tried: set[tuple[int, int]] = set()
        for volume, issue_number in candidate_pairs:
            if (volume, issue_number) in tried:
                continue
            tried.add((volume, issue_number))
            if not _coverage_misses_start([
                normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10])
                for paper in by_key.values()
            ], start_date):
                return
            url = science_toc_url(slug, str(volume), str(issue_number))
            report = {"journal": slug, "url": url, "volume": str(volume), "issue": str(issue_number), "count": 0, "added": 0, "ok": False, "message": ""}
            soup = None
            last_error = ""
            for attempt in range(1, 4):
                try:
                    response = requests.get(url, headers=HEADERS, timeout=(5, max(5, min(12, timeout))), allow_redirects=True)
                    response.raise_for_status()
                    soup = BeautifulSoup(response.text, "html.parser")
                    break
                except Exception as exc:
                    last_error = str(exc)
                    if attempt < 3:
                        time.sleep(20 * attempt)
            if soup is None:
                report["message"] = last_error or "request failed"
                pages.append(report)
                continue
            issue_meta = science_toc_issue_meta(soup)
            if str(issue_meta.get("issue") or "") != str(issue_number):
                report["message"] = "issue metadata mismatch"
                pages.append(report)
                continue
            issue_date = normalize_date(str(issue_meta.get("issueDate") or ""))
            papers = parse_science_toc_items(soup, slug, url)
            added = add_papers(papers)
            report.update({"count": len(papers), "added": added, "ok": bool(papers), "message": "ok" if papers else "no toc article cards", "issue_date": issue_date})
            pages.append(report)
            if papers:
                status["boundary_toc_fallback_used"] = True
                status["boundary_toc_count"] = int(status.get("boundary_toc_count") or 0) + len(papers)

    targeted_only = bool(search_phrases) and os.environ.get("SCIENCE_TARGETED_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}
    for slug in journals:
        journal = _science_journal_meta(slug)
        issn = journal.get("issn", "")
        if issn and search_phrases:
            add_openalex_science_fallback(slug, issn, search_phrases, targeted=True)
    if status.get("targeted_search_used") and not status.get("targeted_page_limit_reached") and not status.get("raw_item_limit_reached") and not status.get("targeted_query_error"):
        status["targeted_query_exhausted"] = True
    if targeted_only:
        status["targeted_only"] = True
        status["stopped_reason"] = status.get("stopped_reason") or "targeted search only"

    for slug in ([] if targeted_only else journals):
        status["bulk_fallback_used"] = True
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
                    payload = request_crossref_payload(crossref_url)
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
                    status["stopped_reason"] = item_limit_stop_reason()
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
            status["stopped_reason"] = item_limit_stop_reason()
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
            status["stopped_reason"] = item_limit_stop_reason()
            break
    if start_date and not targeted_only and not coverage_reached_start():
        for slug in journals:
            add_science_boundary_toc_fallback(slug)
            if reached_limit():
                status["stopped_reason"] = item_limit_stop_reason()
                break
    if start_date and not targeted_only and not coverage_reached_start() and os.environ.get("SCIENCE_FULL_TOC_FALLBACK", "").lower() in {"1", "true", "yes", "on"}:
        for slug in journals:
            add_science_toc_fallback(slug)
            if reached_limit():
                status["stopped_reason"] = item_limit_stop_reason()
                break
    if start_date and not targeted_only and not coverage_reached_start():
        for slug in journals:
            journal = _science_journal_meta(slug)
            issn = journal.get("issn", "")
            if not issn:
                continue
            add_openalex_science_fallback(slug, issn)
            if reached_limit():
                status["stopped_reason"] = item_limit_stop_reason()
                break
    papers = list(by_key.values())
    status["count"] = len(papers)
    status["ok"] = bool(papers)
    crossref_reports = status.get("crossref_pages") if isinstance(status.get("crossref_pages"), list) else []
    status["pages_scanned"] = len(crossref_reports)
    dates = [
        normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10])
        for paper in papers
    ]
    dates = [value for value in dates if value]
    coverage_limited = _coverage_misses_start(dates, start_date)
    if targeted_only and status.get("targeted_query_exhausted") and not status.get("targeted_page_limit_reached"):
        coverage_limited = False
        status["targeted_query_coverage_basis"] = "OpenAlex keyword query exhausted; absence of older in-window topic hits is not a date-coverage failure."
    if coverage_limited and dates and start_date:
        try:
            oldest_date = date.fromisoformat(min(dates))
            requested_start = date.fromisoformat(start_date)
            tolerance_days = _positive_int_env("SCIENCE_COVERAGE_TOLERANCE_DAYS", 7)
            if 0 < (oldest_date - requested_start).days <= tolerance_days and (
                status.get("boundary_toc_fallback_used")
            ):
                coverage_limited = False
                status["coverage_tolerance_applied"] = True
                status["coverage_tolerance_days"] = tolerance_days
                status["coverage_tolerance_reason"] = "Science is a weekly issue source; first covered issue is within tolerance after requested start_date."
        except ValueError:
            pass
    if coverage_limited:
        status["coverage_limited"] = True
        if status.get("stopped_reason") in {"", "item limit"}:
            status["stopped_reason"] = "date coverage incomplete"
    else:
        status.pop("coverage_limited", None)
        if item_limit is not None and len(papers) >= item_limit and status.get("stopped_reason") in {"empty crossref page", "no new items", "end of crossref results"}:
            status["stopped_reason"] = "target count reached after fallback coverage"
    item_limited_without_coverage_target = bool(status.get("stopped_reason") == "item limit")
    status["raw_item_limit_reached"] = bool(item_limit is not None and len(papers) >= item_limit)
    status["target_count_reached"] = False
    status["candidate_limit"] = candidate_limit
    status["raw_item_limit"] = item_limit
    status["limited"] = bool(item_limited_without_coverage_target or status.get("raw_item_limit_reached") or coverage_limited or status.get("stopped_reason") == "crossref page limit" or status.get("targeted_page_limit_reached") or status.get("openalex_page_limit_reached"))
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
        if status.get("coverage_tolerance_applied"):
            message += f"; weekly issue coverage tolerance applied for requested start_date {start_date}"
        if status.get("toc_fallback_used"):
            message += f"; Science.org TOC fallback added/scanned {status.get('toc_count', 0)} items"
        if status.get("boundary_toc_fallback_used"):
            message += f"; boundary TOC fallback added/scanned {status.get('boundary_toc_count', 0)} items"
        if status.get("openalex_fallback_used"):
            message += f"; OpenAlex fallback added/scanned {status.get('openalex_count', 0)} items"
        if coverage_limited and start_date:
            message += f"; coverage did not reach requested start_date {start_date}"
        if status.get("stopped_reason"):
            message += f"; stopped: {status['stopped_reason']}"
        status["message"] = message
    elif status["errors"]:
        status["message"] = "Science feeds unavailable or failed: " + " | ".join(status["errors"][:3])
    else:
        status["message"] = "No Science items found for selected journals/types/date range."
    return papers, status

# bioRxiv's OpenAlex source id (preprint server). medRxiv is S4306402521.
BIORXIV_OPENALEX_SOURCE_ID = "S4306402567"


def _biorxiv_paper_from_openalex(item: dict, start_date: str, end_date: str) -> dict | None:
    if not isinstance(item, dict):
        return None
    doi_url = str(item.get("doi") or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi_url, flags=re.I).lower()
    title = _clean_text(str(item.get("display_name") or ""))
    if not title:
        return None
    published = normalize_date(str(item.get("publication_date") or ""))
    if not _in_date_range(published, start_date, end_date):
        return None
    authors: list[str] = []
    for authorship in item.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") if isinstance(authorship.get("author"), dict) else {}
        name = _clean_text(str(author.get("display_name") or ""))
        if name:
            authors.append(name)
        if len(authors) >= 12:
            break
    abstract = _openalex_abstract_from_inverted_index(item.get("abstract_inverted_index") or {})
    primary_location = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
    landing = str(primary_location.get("landing_page_url") or "")
    url = landing or (f"https://www.biorxiv.org/content/{doi}" if doi else str(item.get("id") or ""))
    return {
        "id": stable_id("paper", doi or title),
        "source": "biorxiv",
        "biorxiv_doi": doi,
        "title": title,
        "authors": ", ".join(authors),
        "abstract": abstract,
        "url": url,
        "pdf_url": "",
        "venue": "bioRxiv",
        "year": int(published[:4]) if published[:4].isdigit() else date.today().year,
        "category": "",
        "categories": [],
        "classification_source": "unavailable",
        "metadata": {
            "published": published,
            "doi": doi,
            "primary_category": "",
            "all_categories": [],
            "retrieval": "openalex",
            "openalex_id": str(item.get("id") or ""),
        },
    }


def _phrase_or_clause(phrases: list[str], *, fields: tuple[str, ...] = ()) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for phrase in phrases or []:
        text = " ".join(str(phrase or "").split()).strip().strip('"')
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        term = f'"{text}"' if " " in text or "-" in text else text
        parts.append("(" + " OR ".join(f'{field}:{term}' for field in fields) + ")" if fields else term)
    return " OR ".join(parts)


def _fetch_biorxiv_openalex(phrases: list[str], max_items: int, start_date: str, end_date: str, status: dict, log=None, should_cancel=None) -> list[dict]:
    """Search one equal-status OR group, sorted by publication date."""
    timeout = _positive_int_env("OPENALEX_REQUEST_TIMEOUT_SEC", 25)
    retries = _positive_int_env("OPENALEX_REQUEST_RETRIES", 4)
    try:
        spacing = max(0.0, float(os.environ.get("OPENALEX_REQUEST_SPACING_SEC", "1.0") or 1.0))
    except (TypeError, ValueError):
        spacing = 1.0
    per_page = max(20, min(100, _positive_int_env("BIORXIV_OPENALEX_PER_PAGE", 100)))
    item_limit = max(0, int(max_items or 0))
    api_key = str(os.environ.get("OPENALEX_API_KEY") or "").strip()
    max_retry_wait = _positive_int_env("OPENALEX_MAX_RETRY_WAIT_SEC", 30)
    by_key: dict[str, dict] = {}
    phrase_list = [str(value).strip() for value in (phrases or []) if str(value).strip()]
    search_query = _phrase_or_clause(phrase_list)
    cursor = "*"
    while search_query and cursor and (not item_limit or len(by_key) < item_limit):
        if should_cancel and should_cancel():
            status["stopped_reason"] = "cancelled"
            return list(by_key.values())
        filters = [
            f"primary_location.source.id:{BIORXIV_OPENALEX_SOURCE_ID}",
            f"from_publication_date:{start_date}",
            f"to_publication_date:{end_date}",
        ]
        params = {
            "filter": ",".join(filters),
            "search": search_query,
            "per-page": per_page,
            "cursor": cursor,
            "sort": "publication_date:desc",
            "select": "id,doi,display_name,publication_date,authorships,abstract_inverted_index,primary_location,type",
        }
        if api_key:
            params["api_key"] = api_key
        url = "https://api.openalex.org/works?" + urlencode(params)
        payload = None
        last_error = ""
        for attempt in range(1, max(1, retries) + 1):
            if should_cancel and should_cancel():
                status["stopped_reason"] = "cancelled"
                return list(by_key.values())
            try:
                payload = _request(url, timeout=timeout).json()
                break
            except Exception as exc:
                last_error = str(exc).replace(api_key, "[REDACTED]") if api_key else str(exc)
                if _http_status_code(exc) == 429:
                    status["openalex_rate_limited"] = True
                    response = getattr(exc, "response", None)
                    headers = getattr(response, "headers", {}) or {}
                    try:
                        retry_after = max(0, int(float(headers.get("Retry-After") or headers.get("X-RateLimit-Reset") or 0)))
                    except (TypeError, ValueError):
                        retry_after = 0
                    remaining = str(headers.get("X-RateLimit-Remaining") or "")
                    remaining_usd = str(headers.get("X-RateLimit-Remaining-USD") or "").strip()
                    response_text = str(getattr(response, "text", "") or "").lower()
                    status["openalex_retry_after_sec"] = retry_after
                    status["openalex_rate_limit_remaining"] = remaining
                    daily_budget_exhausted = (
                        remaining_usd in {"0", "0.0", "$0", "$0.0"}
                        or "insufficient budget" in response_text
                    )
                    if daily_budget_exhausted:
                        status["stopped_reason"] = "openalex_daily_budget_exhausted"
                        status["limited"] = True
                        budget_message = (
                            "openalex: daily API budget exhausted; "
                            f"retry after {retry_after}s"
                            + ("" if api_key else "; set OPENALEX_API_KEY for the higher free daily budget")
                        )
                        status["errors"].append(budget_message)
                        if log:
                            log(budget_message)
                        return list(by_key.values())
                    if retry_after > max_retry_wait:
                        status["stopped_reason"] = "openalex_rate_limited"
                        status["limited"] = True
                        status["errors"].append(f"openalex: rate limited; retry after {retry_after}s exceeds the configured wait limit")
                        if log:
                            log(f"openalex: rate limited; retry after {retry_after}s exceeds the configured wait limit")
                        return list(by_key.values())
                    if attempt < max(1, retries):
                        if log:
                            log(f"openalex: rate limited; retrying in {max(1, retry_after or 2 ** (attempt - 1))}s")
                        time.sleep(max(1.0, float(retry_after or 2 ** (attempt - 1))))
                        continue
                    status["stopped_reason"] = "openalex_rate_limited"
                    status["limited"] = True
                    status["errors"].append(f"openalex: {last_error}")
                    if log:
                        log("openalex: rate limited after retry budget was exhausted")
                    return list(by_key.values())
                if attempt < max(1, retries):
                    time.sleep(min(8.0, max(1.0, spacing) * attempt))
        if payload is None:
            status["errors"].append(f"openalex: {last_error}")
            break
        results = payload.get("results") if isinstance(payload, dict) else []
        if not isinstance(results, list) or not results:
            break
        added = 0
        for item in results:
            paper = _biorxiv_paper_from_openalex(item, start_date, end_date)
            if not paper:
                continue
            key = paper.get("biorxiv_doi") or paper.get("title", "").lower()
            if key and key not in by_key:
                by_key[key] = paper
                added += 1
            if item_limit and len(by_key) >= item_limit:
                break
        status["pages_fetched"] = status.get("pages_fetched", 0) + 1
        if log:
            log(f"bioRxiv/OpenAlex OR query: +{added} (total {len(by_key)})")
        cursor = (payload.get("meta") or {}).get("next_cursor") if isinstance(payload.get("meta"), dict) else None
        if len(results) < per_page:
            break
        if spacing:
            time.sleep(spacing)
    status["openalex_api_key_configured"] = bool(api_key)
    papers = sorted(
        by_key.values(),
        key=lambda paper: normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10]) or "",
        reverse=True,
    )
    if item_limit:
        papers = papers[:item_limit]
    status["openalex_count"] = len(papers)
    return papers


def _fetch_biorxiv_europepmc(phrases: list[str], max_items: int, start_date: str, end_date: str, status: dict, log=None, should_cancel=None) -> list[dict]:
    timeout = _positive_int_env("EUROPEPMC_REQUEST_TIMEOUT_SEC", 25)
    retries = _positive_int_env("EUROPEPMC_REQUEST_RETRIES", 3)
    try:
        spacing = max(0.0, float(os.environ.get("EUROPEPMC_REQUEST_SPACING_SEC", "0.5") or 0.5))
    except (TypeError, ValueError):
        spacing = 0.5
    page_size = 100
    by_key: dict[str, dict] = {}
    cursor = "*"
    pages = 0
    item_limit = max(0, int(max_items or 0))
    max_pages = _positive_int_env("BIORXIV_EUROPEPMC_MAX_PAGES", 0)
    or_clause = _phrase_or_clause(phrases, fields=("TITLE", "ABSTRACT")) or "preprint"
    query = f'(SRC:PPR) AND (PUBLISHER:"bioRxiv") AND ({or_clause}) AND (FIRST_PDATE:[{start_date} TO {end_date}]) sort_date:y'
    while cursor and (not max_pages or pages < max_pages) and (not item_limit or len(by_key) < item_limit):
        if should_cancel and should_cancel():
            status["stopped_reason"] = "cancelled"
            break
        params = {
            "query": query,
            "format": "json",
            "pageSize": page_size,
            "resultType": "core",
            "cursorMark": cursor,
        }
        url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urlencode(params)
        payload = None
        last_error = ""
        for attempt in range(1, max(1, retries) + 1):
            if should_cancel and should_cancel():
                status["stopped_reason"] = "cancelled"
                return list(by_key.values())
            try:
                payload = _request(url, timeout=timeout).json()
                break
            except Exception as exc:
                last_error = str(exc)
                if _http_status_code(exc) == 429:
                    status["europepmc_rate_limited"] = True
                    status["stopped_reason"] = "europepmc_rate_limited"
                    status["limited"] = True
                    status["errors"].append(f"europepmc page {pages}: {last_error}")
                    return list(by_key.values())
                if attempt < max(1, retries):
                    time.sleep(min(6.0, max(0.5, spacing) * attempt))
        if payload is None:
            status["errors"].append(f"europepmc page {pages}: {last_error}")
            break
        results = (payload.get("resultList") or {}).get("result") if isinstance(payload, dict) else []
        if not isinstance(results, list) or not results:
            break
        added = 0
        for record in results:
            if not isinstance(record, dict):
                continue
            doi = str(record.get("doi") or "").strip().lower()
            report_details = record.get("bookOrReportDetails") if isinstance(record.get("bookOrReportDetails"), dict) else {}
            publisher = str(record.get("publisher") or report_details.get("publisher") or "").strip()
            if publisher.lower() != "biorxiv":
                continue
            title = _clean_text(str(record.get("title") or ""))
            if not title:
                continue
            published = normalize_date(str(record.get("firstPublicationDate") or ""))
            if not _in_date_range(published, start_date, end_date):
                continue
            key = doi or title.lower()
            if not key or key in by_key:
                continue
            abstract = _clean_text(str(record.get("abstractText") or ""))
            authors = _clean_text(str(record.get("authorString") or ""))
            url_landing = f"https://www.biorxiv.org/content/{doi}" if doi else ""
            by_key[key] = {
                "id": stable_id("paper", doi or title),
                "source": "biorxiv",
                "biorxiv_doi": doi,
                "title": title,
                "authors": authors,
                "abstract": abstract,
                "url": url_landing,
                "pdf_url": "",
                "venue": "bioRxiv",
                "year": int(published[:4]) if published[:4].isdigit() else date.today().year,
                "category": "",
                "categories": [],
                "classification_source": "unavailable",
                "metadata": {"published": published, "doi": doi, "retrieval": "europepmc", "publisher": publisher},
            }
            added += 1
            if item_limit and len(by_key) >= item_limit:
                break
        pages += 1
        status["pages_fetched"] = status.get("pages_fetched", 0) + 1
        if log:
            log(f"bioRxiv/EuropePMC page {pages}: +{added} (total {len(by_key)})")
        cursor = payload.get("nextCursorMark") if isinstance(payload, dict) else None
        if spacing:
            time.sleep(spacing)
    if max_pages and pages >= max_pages and cursor:
        status["europepmc_page_limit_reached"] = True
        status["limited"] = True
    papers = sorted(
        by_key.values(),
        key=lambda paper: normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10]) or "",
        reverse=True,
    )
    if item_limit:
        papers = papers[:item_limit]
    status["europepmc_count"] = len(papers)
    return papers


def _filter_biorxiv_targeted_by_official_category(
    papers: list[dict],
    categories: list[str],
    status: dict,
    log=None,
    should_cancel=None,
) -> list[dict]:
    selected = [category.strip().lower() for category in (categories or []) if category.strip() and category.strip().lower() != "all"]
    if not selected or not papers:
        return papers
    timeout = _positive_int_env("BIORXIV_CATEGORY_REQUEST_TIMEOUT_SEC", 15)
    workers = max(1, min(6, _positive_int_env("BIORXIV_CATEGORY_WORKERS", 4), len(papers)))

    def lookup(paper: dict) -> tuple[dict, str, str]:
        if should_cancel and should_cancel():
            return paper, "", "cancelled"
        doi = str(paper.get("biorxiv_doi") or paper.get("metadata", {}).get("doi") or "").strip()
        if not doi:
            return paper, "", "missing DOI"
        try:
            payload = _request(f"https://api.biorxiv.org/details/biorxiv/{quote(doi, safe='/')}", timeout=timeout).json()
        except Exception as exc:
            return paper, "", str(exc)
        records = payload.get("collection") if isinstance(payload, dict) else []
        if not isinstance(records, list):
            records = []
        matching = [
            record for record in records
            if isinstance(record, dict) and str(record.get("doi") or "").strip().lower() == doi.lower()
        ]
        record = matching[-1] if matching else (records[-1] if records and isinstance(records[-1], dict) else {})
        return paper, str(record.get("category") or "").strip(), "" if record else "official DOI lookup returned no record"

    matched: list[dict] = []
    unverified: list[dict] = []
    rejected = 0
    cancelled_early = False
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {
        executor.submit(lookup, paper)
        for paper in papers
        if not (should_cancel and should_cancel())
    }
    pending = set(futures)
    try:
        while pending:
            if should_cancel and should_cancel():
                cancelled_early = True
                status["stopped_reason"] = "cancelled"
                status["limited"] = True
                for future in pending:
                    future.cancel()
                break
            completed, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in completed:
                paper, category, error = future.result()
                if category:
                    paper["category"] = category
                    paper["categories"] = [category]
                    paper["classification_source"] = "official"
                    metadata = paper.setdefault("metadata", {})
                    metadata["biorxiv_category"] = category
                    metadata["primary_category"] = category
                    metadata["all_categories"] = [category]
                    if _biorxiv_category_matches(category, selected):
                        matched.append(paper)
                    else:
                        rejected += 1
                else:
                    paper["classification_source"] = "unavailable"
                    unverified.append(paper)
                    if error:
                        status.setdefault("errors", []).append(f"category lookup {paper.get('biorxiv_doi') or paper.get('title')}: {error}")
    finally:
        executor.shutdown(wait=not cancelled_early, cancel_futures=True)
    status["official_category_matched_count"] = len(matched)
    status["official_category_rejected_count"] = rejected
    status["official_category_unverified_count"] = len(unverified)
    if unverified:
        status["limited"] = True
        status["official_category_unverified_dropped_count"] = len(unverified)
    if log:
        log(
            f"bioRxiv official category filter: matched={len(matched)}; "
            f"rejected={rejected}; unverified_dropped={len(unverified)}"
        )
    return matched


def fetch_biorxiv_targeted(
    phrases: list[str],
    fetch_limit: int,
    start_date: str = "",
    end_date: str = "",
    categories: list[str] | None = None,
    log=None,
    should_cancel=None,
) -> tuple[list[dict], dict]:
    """Keyword-targeted bioRxiv metadata crawl from a list of anchor PHRASES
    (Europe PMC primary OR-of-phrases, OpenAlex only as an unavailable/empty fallback). Returns
    metadata only (title/abstract/authors/doi/date/url)."""
    start_date = normalize_date(start_date) or (date.today() - timedelta(days=BIORXIV_DEFAULT_RECENT_DAYS)).isoformat()
    end_date = normalize_date(end_date) or date.today().isoformat()
    fetch_limit = max(1, int(fetch_limit or 5000))
    raw_item_limit = fetch_limit
    phrases = [str(p).strip() for p in (phrases or []) if str(p).strip()]
    status = {
        "source": "biorxiv",
        "ok": False,
        "limited": False,
        "count": 0,
        "message": "",
        "categories": list(categories or []),
        "start_date": start_date,
        "end_date": end_date,
        "queries": [f"europepmc SRC:PPR OR({len(phrases)} phrases) FIRST_PDATE:{start_date}..{end_date}"],
        "errors": [],
        "pages_fetched": 0,
        "targeted": True,
        "retrieval": "europepmc+openalex",
        "phrases": phrases,
        "fetch_limit": fetch_limit,
        "raw_item_limit": fetch_limit,
    }
    by_key: dict[str, dict] = {}

    def add(papers: list[dict]) -> None:
        for paper in papers:
            key = str(paper.get("biorxiv_doi") or "").lower() or str(paper.get("title") or "").lower()
            if not key:
                continue
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = paper
            elif not existing.get("abstract") and paper.get("abstract"):
                existing["abstract"] = paper["abstract"]

    use_epmc = os.environ.get("BIORXIV_EUROPEPMC", "1").lower() in {"1", "true", "yes", "on"}
    use_openalex = os.environ.get("BIORXIV_OPENALEX", "1").lower() in {"1", "true", "yes", "on"}
    if use_epmc:
        add(_fetch_biorxiv_europepmc(phrases, raw_item_limit, start_date, end_date, status, log, should_cancel))
    raw_limit_reached = bool(raw_item_limit and len(by_key) >= raw_item_limit)
    if (
        use_openalex
        and not by_key
        and not raw_limit_reached
        and not (should_cancel and should_cancel())
    ):
        status["queries"].append(
            f"openalex source:biorxiv OR({len(phrases)} phrases)"
        )
        status["openalex_fallback_requested"] = True
        add(_fetch_biorxiv_openalex(phrases, fetch_limit, start_date, end_date, status, log, should_cancel))
    elif use_openalex:
        status["openalex_skipped_reason"] = (
            "explicit internal raw item limit reached"
            if raw_limit_reached
            else "Europe PMC returned keyword-matched papers"
        )

    raw_targeted_count = len(by_key)
    papers = _filter_biorxiv_targeted_by_official_category(
        list(by_key.values()),
        list(categories or []),
        status,
        log,
        should_cancel,
    )
    papers.sort(key=lambda p: normalize_date(str(p.get("metadata", {}).get("published", ""))[:10]) or "", reverse=True)
    papers = papers[:fetch_limit]
    status["count"] = len(papers)
    status["deduped_count"] = len(papers)
    status["raw_count"] = raw_targeted_count
    status["ok"] = bool(papers)
    status["raw_item_limit_reached"] = bool(raw_item_limit and raw_targeted_count >= raw_item_limit)
    status["fetch_limit_reached"] = raw_targeted_count >= fetch_limit
    if status["raw_item_limit_reached"]:
        status["limited"] = True
        status["stopped_reason"] = "explicit internal raw item limit"
    if papers:
        dates = [normalize_date(str(p.get("metadata", {}).get("published", ""))[:10]) for p in papers]
        dates = [d for d in dates if d]
        if dates:
            status["date_coverage"] = {"newest": max(dates), "oldest": min(dates)}
        status["message"] = (
            f"ok; keyword-targeted; europepmc={status.get('europepmc_count', 0)}; "
            f"openalex={status.get('openalex_count', 0)}; fetch_limit={fetch_limit}; "
            f"queries={'; '.join(status['queries'])}"
        )
    elif status["errors"]:
        status["message"] = "bioRxiv keyword search unavailable: " + " | ".join(status["errors"][:3])
    else:
        status["message"] = f"No bioRxiv papers found for the targeted phrases in {start_date}..{end_date}."
    return papers, status


def fetch_biorxiv(
    categories: list[str],
    fetch_limit: int,
    start_date: str = "",
    end_date: str = "",
    search_phrases: list[str] | None = None,
    log=None,
    should_cancel=None,
) -> tuple[list[dict], dict]:
    # Normal runs require the extracted keyword query. Native date-window scans
    # are reserved for the explicit complete-window audit mode.
    targeted_enabled = os.environ.get("BIORXIV_TARGETED", "1").lower() in {"1", "true", "yes", "on"}
    complete_window_scan = os.environ.get("BIORXIV_COMPLETE_WINDOW", "0").lower() in {"1", "true", "yes", "on"}
    targeted_before_complete_window = os.environ.get("BIORXIV_TARGETED_BEFORE_COMPLETE_WINDOW", "0").lower() in {"1", "true", "yes", "on"}
    fetch_limit = max(1, int(fetch_limit or 5000))
    categories = [category.strip() for category in (categories or []) if category.strip()] or ["all"]
    search_phrases = [p for p in (search_phrases or []) if str(p).strip()]
    skip_targeted_for_complete_window = bool(search_phrases and targeted_enabled and complete_window_scan and not targeted_before_complete_window)
    targeted_seed: list[dict] = []
    targeted_status: dict = {}
    if search_phrases and targeted_enabled and not skip_targeted_for_complete_window:
        papers, status = fetch_biorxiv_targeted(
            search_phrases,
            fetch_limit,
            start_date,
            end_date,
            categories=categories,
            log=log,
            should_cancel=should_cancel,
        )
        if not complete_window_scan:
            return papers, status
        if complete_window_scan:
            targeted_seed = list(papers)
            targeted_status = dict(status)
            if log:
                log(f"bioRxiv targeted recall seeded {len(targeted_seed)} papers; continuing with complete native window scan")
    if targeted_enabled and not search_phrases and not complete_window_scan:
        return [], {
            "source": "biorxiv",
            "ok": False,
            "limited": True,
            "count": 0,
            "message": "bioRxiv keyword extraction returned no valid search keywords; no unfiltered window scan was run.",
            "categories": categories,
            "start_date": normalize_date(start_date),
            "end_date": normalize_date(end_date),
            "queries": [],
            "errors": ["missing_search_keywords"],
            "targeted": True,
            "fetch_limit": fetch_limit,
        }
    papers: list[dict] = []
    by_key: dict[str, dict] = {}
    start_date = normalize_date(start_date) or (date.today() - timedelta(days=BIORXIV_DEFAULT_RECENT_DAYS)).isoformat()
    end_date = normalize_date(end_date) or date.today().isoformat()
    request_timeout = _positive_int_env("BIORXIV_REQUEST_TIMEOUT_SEC", 20)
    request_retries = _positive_int_env("BIORXIV_REQUEST_RETRIES", 3)
    max_pages = _positive_int_env("BIORXIV_MAX_PAGES", 0)
    window_days = max(1, min(60, _positive_int_env("BIORXIV_WINDOW_DAYS", 30)))
    parallel_pages = max(1, min(8, _positive_int_env("BIORXIV_PARALLEL_PAGES", 4 if complete_window_scan else 1)))
    try:
        request_spacing_sec = max(0.0, float(os.environ.get("BIORXIV_REQUEST_SPACING_SEC", "0.35") or 0.35))
    except (TypeError, ValueError):
        request_spacing_sec = 0.35
    raw_item_limit = fetch_limit
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
        "request_retries": request_retries,
        "request_spacing_sec": request_spacing_sec,
        "latest_first": False,
        "complete_window_scan": complete_window_scan,
        "raw_item_limit": fetch_limit,
        "fetch_limit": fetch_limit,
        "max_pages": max_pages or None,
        "window_days": window_days,
        "parallel_pages": parallel_pages,
        "windows": [],
    }
    if skip_targeted_for_complete_window:
        status["targeted_recall_skipped"] = True
        status["targeted_recall_skip_reason"] = "complete_window_scan_uses_native_biorxiv_window_first"
        if log:
            log("bioRxiv complete-window scan: skipping optional EuropePMC/OpenAlex targeted seed so native date-window coverage cannot be blocked by third-party rate limits")
    if targeted_status:
        status["targeted_recall_used"] = True
        status["targeted_recall_count"] = len(targeted_seed)
        status["targeted_recall_message"] = str(targeted_status.get("message") or "")
        status["targeted_recall_queries"] = list(targeted_status.get("queries") or [])
        status["queries"].extend(query for query in status["targeted_recall_queries"] if query not in status["queries"])
        status["errors"].extend(str(error) for error in targeted_status.get("errors") or [] if str(error))

    def cancelled() -> bool:
        if should_cancel and should_cancel():
            status["stopped_reason"] = "cancelled"
            status["limited"] = True
            return True
        return False

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

    def parse_page_count(payload: dict) -> int:
        messages = payload.get("messages") if isinstance(payload, dict) else []
        if isinstance(messages, list) and messages:
            message = messages[0] if isinstance(messages[0], dict) else {}
            try:
                return max(0, int(str(message.get("count") or "0")))
            except (TypeError, ValueError):
                return 0
        return 0

    def request_page(window_start: str, window_end: str, cursor: int, window_report: dict, category: str = "") -> dict | None:
        if cancelled():
            return None
        unsupported_categories = status.setdefault("unsupported_categories", [])
        if category and category.lower() in {str(value).lower() for value in unsupported_categories}:
            return None
        url = f"https://api.biorxiv.org/details/biorxiv/{window_start}/{window_end}/{cursor}/json"
        if category and category.lower() != "all":
            url += "?" + urlencode({"category": category})
        last_error = ""
        for attempt in range(1, max(1, request_retries) + 1):
            if cancelled():
                return None
            try:
                response = _request(url, timeout=request_timeout)
                if request_spacing_sec:
                    time.sleep(request_spacing_sec)
                payload = response.json()
                messages = payload.get("messages") if isinstance(payload, dict) else []
                message = messages[0] if isinstance(messages, list) and messages and isinstance(messages[0], dict) else {}
                reported_category = str(message.get("category") or "").strip()
                if category and reported_category and reported_category.lower() != category.lower():
                    error = (
                        f"bioRxiv category '{category}' is unavailable or unsupported; "
                        f"API reported '{reported_category}'"
                    )
                    if category not in unsupported_categories:
                        unsupported_categories.append(category)
                    status["limited"] = True
                    status["errors"].append(error)
                    window_report.setdefault("errors", []).append(error)
                    return None
                return payload
            except Exception as exc:
                last_error = str(exc)
                if _http_status_code(exc) == 429:
                    status["rate_limited"] = True
                    status["limited"] = True
                    status["stopped_reason"] = "rate limited"
                    break
                if attempt < max(1, request_retries):
                    time.sleep(min(8.0, max(0.5, request_spacing_sec) * attempt))
        message = f"{window_start}..{window_end} cursor={cursor}: {last_error}"
        status["errors"].append(message)
        window_report.setdefault("errors", []).append(message)
        return None

    def raw_limit_reached() -> bool:
        return bool(raw_item_limit and len(papers) >= raw_item_limit)

    def add_existing_paper(paper: dict) -> bool:
        if not isinstance(paper, dict):
            return False
        key = str(paper.get("biorxiv_doi") or "").lower() or str(paper.get("metadata", {}).get("doi") or "").lower() or str(paper.get("title") or "").lower()
        if not key:
            return False
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = paper
            papers.append(paper)
            return True
        if not existing.get("abstract") and paper.get("abstract"):
            existing["abstract"] = paper["abstract"]
        metadata = existing.setdefault("metadata", {})
        for field, value in (paper.get("metadata") or {}).items():
            metadata.setdefault(field, value)
        return False

    def consume_records(
        records: list[dict],
        window_start: str,
        window_end: str,
        *,
        max_new_items: int = 0,
    ) -> int:
        matched_this_page = 0
        added_this_page = 0
        for record in records:
            if raw_limit_reached() or (max_new_items and added_this_page >= max_new_items):
                break
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
                "classification_source": "official",
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
            added_this_page += 1
            if raw_limit_reached():
                status["raw_item_limit_reached"] = True
                status["stopped_reason"] = "explicit internal raw item limit"
                break
        return matched_this_page

    def finalize_biorxiv_status() -> tuple[list[dict], dict]:
        papers.sort(key=lambda paper: normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10]) or "", reverse=True)
        status["count"] = len(papers)
        status["deduped_count"] = len(papers)
        status["ok"] = bool(papers)
        status["fetch_limit_reached"] = bool(raw_item_limit and len(papers) >= raw_item_limit)
        status["raw_item_limit_reached"] = bool(raw_item_limit and len(papers) >= raw_item_limit)
        if status["raw_item_limit_reached"] and status.get("stopped_reason") in {"", None}:
            status["stopped_reason"] = "explicit internal raw item limit"
        if complete_window_scan and status.get("stopped_reason") in {"", None}:
            status["stopped_reason"] = "full window scanned"
        elif status.get("stopped_reason") in {"", None}:
            status["stopped_reason"] = "selected queries exhausted"
        if status.get("raw_item_limit_reached"):
            status["limited"] = True
        if papers:
            dates = [normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10]) for paper in papers]
            dates = [value for value in dates if value]
            if dates:
                status["date_coverage"] = {"newest": max(dates), "oldest": min(dates)}
            if complete_window_scan and not status.get("raw_item_limit_reached") and status.get("stopped_reason") != "page limit":
                limit_message = "complete native date window scanned"
            elif status.get("stopped_reason") == "explicit internal raw item limit":
                limit_message = "reached explicit internal raw item limit"
            elif status.get("stopped_reason") == "page limit":
                limit_message = "limited by page limit"
            else:
                limit_message = "fetched available windows"
            status["message"] = (
                f"ok; {limit_message}; windows={len(status['windows'])}; pages_fetched={status['pages_fetched']}; "
                f"api_records={status['api_raw_count']}; matched_records={status['matched_category_count']}; "
                f"queries={'; '.join(status['queries'])}"
            )
            if status.get("targeted_recall_used"):
                status["message"] += f"; targeted_seed={status.get('targeted_recall_count', 0)}"
        elif status["errors"]:
            status["message"] = "bioRxiv unavailable or query failed: " + " | ".join(status["errors"][:3])
        else:
            status["message"] = f"No bioRxiv papers found; queries={'; '.join(status['queries'])}"
        return papers, status

    for seed_paper in targeted_seed:
        if cancelled():
            break
        if raw_limit_reached():
            status["raw_item_limit_reached"] = True
            status["stopped_reason"] = "explicit internal raw item limit"
            break
        if add_existing_paper(dict(seed_paper)):
            status["targeted_seed_added"] = int(status.get("targeted_seed_added") or 0) + 1

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

    native_categories = [""] if any(category.lower() == "all" for category in categories) else list(categories)

    window_requests = [
        (window_start, window_end, category)
        for window_start, window_end in windows
        for category in native_categories
    ]
    for window_start, window_end, native_category in window_requests:
        if cancelled():
            break
        if raw_limit_reached():
            status["limited"] = True
            status["stopped_reason"] = "explicit internal raw item limit"
            break
        cursor = 0
        window_report = {
            "start_date": window_start,
            "end_date": window_end,
            "category": native_category or "all",
            "pages": 0,
            "api_records": 0,
            "matched_records": 0,
            "total": 0,
            "errors": [],
        }
        seen_page_keys: set[str] = set()
        if complete_window_scan and parallel_pages > 1:
            first_payload = request_page(window_start, window_end, 0, window_report, native_category)
            first_records = first_payload.get("collection") if isinstance(first_payload, dict) else []
            if not isinstance(first_records, list):
                first_records = []
            total_for_window = parse_total(first_payload or {}) or len(first_records)
            page_size = parse_page_count(first_payload or {}) or len(first_records) or 30
            if total_for_window:
                window_report["total"] = total_for_window

            def consume_payload(payload: dict | None) -> bool:
                records = payload.get("collection") if isinstance(payload, dict) else []
                if not isinstance(records, list) or not records:
                    return False
                status["pages_fetched"] += 1
                window_report["pages"] += 1
                window_report["api_records"] += len(records)
                status["api_raw_count"] += len(records)
                status["raw_count"] = status["api_raw_count"]
                matched = consume_records(records, window_start, window_end)
                window_report["matched_records"] += matched
                status["matched_category_count"] += matched
                return True

            consume_payload(first_payload)
            if total_for_window and page_size > 0:
                cursors = list(range(page_size, total_for_window, page_size))
            else:
                cursors = []
            if max_pages:
                remaining = max(0, max_pages - status["pages_fetched"])
                cursors = cursors[:remaining]
            if cursors and not raw_limit_reached() and not cancelled():
                status["parallel_page_fetch_used"] = True
                worker_count = max(1, min(parallel_pages, len(cursors)))
                executor = ThreadPoolExecutor(max_workers=worker_count)
                future_to_cursor = {
                    executor.submit(
                        request_page,
                        window_start,
                        window_end,
                        page_cursor,
                        window_report,
                        native_category,
                    ): page_cursor
                    for page_cursor in cursors
                }
                pending = set(future_to_cursor)
                stopped_early = False
                try:
                    while pending:
                        if cancelled():
                            stopped_early = True
                            for future in pending:
                                future.cancel()
                            break
                        completed, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                        for future in completed:
                            if max_pages and status["pages_fetched"] >= max_pages:
                                status["stopped_reason"] = "page limit"
                                status["limited"] = True
                                stopped_early = True
                                break
                            payload = future.result()
                            consume_payload(payload)
                            if raw_limit_reached():
                                status["limited"] = True
                                status["stopped_reason"] = "explicit internal raw item limit"
                                stopped_early = True
                                break
                        if stopped_early:
                            for future in pending:
                                future.cancel()
                            break
                finally:
                    executor.shutdown(wait=not stopped_early, cancel_futures=True)
            status["windows"].append(window_report)
            if max_pages and status["pages_fetched"] >= max_pages:
                status["stopped_reason"] = "page limit"
                status["limited"] = True
                break
            if raw_limit_reached():
                break
            continue
        while not raw_limit_reached():
            if cancelled():
                break
            if max_pages and status["pages_fetched"] >= max_pages:
                status["limited"] = True
                status["stopped_reason"] = "page limit"
                break
            data = request_page(window_start, window_end, cursor, window_report, native_category)
            if not isinstance(data, dict):
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
            matched_this_page = consume_records(records, window_start, window_end)
            window_report["matched_records"] += matched_this_page
            status["matched_category_count"] += matched_this_page
            if raw_limit_reached():
                break
            cursor += len(records)
            if total_for_window and cursor >= total_for_window:
                break
            time.sleep(0.25)
        status["windows"].append(window_report)
        if status.get("stopped_reason") in {"explicit internal raw item limit", "page limit", "rate limited", "cancelled"}:
            break
    return finalize_biorxiv_status()

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
            if _http_status_code(exc) == 429:
                break
            if attempt < attempts:
                time.sleep(0.8 * attempt)
    if last_error:
        raise last_error
    raise RuntimeError("arXiv request failed")


_ARXIV_PAGE_SIZE = 100


def fetch_arxiv(categories: list[str], fetch_limit: int, start_date: str = "", end_date: str = "", topic_queries: list[str] | None = None, log=None, progress=None, should_cancel=None, max_queries: int | None = None, timeout_sec: int | None = None, targeted_queries: list[tuple[str, str]] | None = None) -> tuple[list[dict], dict]:
    papers: list[dict] = []
    by_key: dict[str, dict] = {}
    start_date, end_date, date_window_source = _arxiv_date_window(start_date, end_date)
    categories = [category.strip() for category in (categories or []) if category.strip()]
    fetch_limit = max(1, int(fetch_limit or 5000))
    full_scan = os.environ.get("ARXIV_FULL_SCAN", "0").lower() in {"1", "true", "yes", "on"}
    env_max_queries = int(os.environ.get("ARXIV_MAX_QUERIES", "0") or 0)
    env_timeout = int(os.environ.get("ARXIV_TIMEOUT_SEC", "0") or 0)
    try:
        request_spacing_sec = max(0.0, float(os.environ.get("ARXIV_REQUEST_SPACING_SEC", "3.0") or 3.0))
    except (TypeError, ValueError):
        request_spacing_sec = 3.0
    use_targeted = bool(targeted_queries)
    max_queries = max(1, env_max_queries or int(max_queries or (len(categories) + len(topic_queries or []) if full_scan else 3)))
    arxiv_timeout = max(20, env_timeout or int(timeout_sec or 45))
    total_limit = fetch_limit
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
        "page_size": _ARXIV_PAGE_SIZE,
        "full_scan": full_scan,
        "pages_fetched": 0,
        "deduped_count": 0,
        "total_limit": total_limit,
        "fetch_limit": fetch_limit,
        "raw_item_limit": fetch_limit,
        "request_spacing_sec": request_spacing_sec,
    }
    if use_targeted:
        queries = list(targeted_queries or [])[:max_queries]
        primary_sort = "submittedDate"
    else:
        queries = _arxiv_search_queries(categories, topic_queries or [], start_date, end_date)[:max_queries]
        primary_sort = "submittedDate"
    status["targeted"] = use_targeted
    status["primary_sort"] = primary_sort
    ns = {"a": "http://www.w3.org/2005/Atom"}

    def run_query(query_index: int, total_queries: int, query_label: str, query_text: str, *, sort_by: str = "submittedDate") -> None:
        query = quote_plus(query_text)
        status["queries"].append(query_text)
        start = 0
        while True:
            if should_cancel and should_cancel():
                status["errors"].append("cancelled")
                status["limited"] = True
                status["stopped_reason"] = "cancelled"
                return
            if log:
                log(f"arXiv query {query_index}/{total_queries} [{query_label}] page_start={start}: {query_text[:180]}")
            if progress:
                progress("arxiv", query_index - 1, max(1, total_queries), f"arXiv query {query_index}/{total_queries}: {query_label}, page start {start}")
            url = f"https://export.arxiv.org/api/query?search_query={query}&sortBy={sort_by}&sortOrder=descending&start={start}&max_results={_ARXIV_PAGE_SIZE}"
            try:
                root = ET.fromstring(_request_arxiv_page(url, arxiv_timeout).text)
            except Exception as exc:
                error_text = str(exc)
                status["errors"].append(f"{query_label} start={start}: {error_text}")
                if "429" in error_text or "Too Many Requests" in error_text:
                    status["limited"] = True
                    status["stopped_reason"] = "rate limited"
                    status["message"] = f"arXiv rate limited after {status['pages_fetched']} pages; kept {len(papers)} papers."
                else:
                    status["limited"] = True
                    status["stopped_reason"] = "request failed"
                if log:
                    log(f"arXiv query {query_index}/{total_queries} failed at start={start}: {error_text[:240]}")
                return
            status["pages_fetched"] += 1
            entries = root.findall("a:entry", ns)
            if not entries:
                return
            before = len(papers)
            for entry in entries:
                _append_arxiv_entry(papers, by_key, entry, ns, query_label, query_text, start_date, end_date)
            if total_limit and len(papers) >= total_limit:
                papers.sort(
                    key=lambda paper: normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10]) or "",
                    reverse=True,
                )
                del papers[total_limit:]
                status["raw_item_limit_reached"] = True
                status["limited"] = True
                status["stopped_reason"] = "explicit internal raw item limit"
                status["message"] = (
                    f"Reached configured arXiv fetch_limit={fetch_limit}; "
                    f"kept the {len(papers)} most recently submitted matching papers."
                )
                return
            if log:
                log(f"arXiv query {query_index}/{total_queries} collected {len(papers) - before} new papers on this page; total {len(papers)}")
            if len(entries) < _ARXIV_PAGE_SIZE:
                return
            start += _ARXIV_PAGE_SIZE
            if request_spacing_sec:
                time.sleep(request_spacing_sec)

    for query_index, (query_label, query_text) in enumerate(queries, 1):
        run_query(query_index, len(queries), query_label, query_text, sort_by=primary_sort)
        if status.get("stopped_reason") in {"cancelled", "rate limited", "request failed", "explicit internal raw item limit"}:
            break
    papers.sort(
        key=lambda paper: normalize_date(str(paper.get("metadata", {}).get("published", ""))[:10]) or "",
        reverse=True,
    )
    papers = papers[:fetch_limit]
    status["topic_result_count"] = len(papers)
    status["count"] = len(papers)
    status["deduped_count"] = len(by_key)
    status["ok"] = bool(papers)
    status["fetch_limit_reached"] = len(papers) >= fetch_limit
    if papers:
        status["message"] = status.get("message") or f"ok; pages_fetched={status['pages_fetched']}; queries={'; '.join(status['queries'])}"
        if total_limit and len(papers) >= total_limit:
            status["raw_item_limit_reached"] = True
    elif status["limited"]:
        status["message"] = status.get("message") or "arXiv rate limited before returning papers. Retry later or reduce query volume."
    elif status["errors"]:
        status["message"] = "arXiv unavailable or query failed: " + " | ".join(status["errors"][:3])
    else:
        status["message"] = f"No arXiv papers found; queries={'; '.join(status['queries'])}"
    return papers, status

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


# Backward-compatible dotted imports for callers that still use the old package layout.
def _register_compat_aliases(*aliases: str) -> None:
    import sys as _sys
    _module = _sys.modules.get(__name__)
    if _module is None:
        return
    globals().setdefault("__path__", [])
    for _alias in aliases:
        _sys.modules.setdefault(_alias, _module)

_register_compat_aliases('support.find_support')
