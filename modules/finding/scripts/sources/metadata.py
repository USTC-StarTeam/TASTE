from __future__ import annotations

import hashlib
import re

from sources.common import _clean_text, _title_key


def _semantic_scholar_cache_key(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", (title or "").strip().lower())
    return hashlib.sha1(cleaned.encode("utf-8", errors="ignore")).hexdigest()

def _same_metadata_text(left: object, right: object) -> bool:
    return " ".join(str(left or "").split()).casefold() == " ".join(str(right or "").split()).casefold()


def _semantic_scholar_cache_real_abstract(cached: dict) -> str:
    abstract = str(cached.get("abstract") or "").strip()
    if not abstract:
        return ""
    tldr = str(cached.get("tldr") or "").strip()
    source = str(cached.get("source") or cached.get("abstract_source") or "").lower()
    if "tldr" in source:
        return ""
    if tldr and _same_metadata_text(abstract, tldr):
        return ""
    return abstract


def _apply_semantic_scholar_cache(paper: dict, cached: dict) -> None:
    abstract = _semantic_scholar_cache_real_abstract(cached)
    if abstract:
        paper["abstract"] = abstract
        paper.setdefault("metadata", {})["abstract_source"] = cached.get("source") or "semantic_scholar_cache"
    if cached.get("url"):
        paper["url"] = paper.get("url") or cached.get("url") or ""
    if cached.get("pdf_url"):
        paper["pdf_url"] = paper.get("pdf_url") or cached.get("pdf_url") or ""
    if cached.get("tldr"):
        paper.setdefault("metadata", {})["tldr"] = cached.get("tldr") or ""


_SEMANTIC_SCHOLAR_RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


def _semantic_scholar_errors_retryable(errors: object) -> bool:
    if not isinstance(errors, list):
        return False
    for error in errors:
        lowered = str(error or "").lower()
        if any(f"http_{code}" in lowered for code in _SEMANTIC_SCHOLAR_RETRYABLE_STATUS_CODES):
            return True
        if any(token in lowered for token in ("timeout", "timed out", "connection reset", "temporarily", "rate limit")):
            return True
    return False


def _semantic_scholar_cache_miss_is_retryable(cached: object) -> bool:
    if not isinstance(cached, dict) or not cached.get("miss"):
        return False
    return bool(cached.get("retryable") or cached.get("temporary_failure") or _semantic_scholar_errors_retryable(cached.get("lookup_errors")))


def _semantic_scholar_cache_is_permanent_miss(cached: object) -> bool:
    return isinstance(cached, dict) and bool(cached.get("miss")) and not _semantic_scholar_cache_miss_is_retryable(cached)


def _doi_from_url(value: str) -> str:
    text = (value or "").strip()
    match = re.search(r"10\.\d{4,9}/[^\s\"<>]+", text)
    if not match:
        return ""
    return match.group(0).rstrip(".,);]")




def _doi_url(doi: str) -> str:
    doi = (doi or "").strip()
    return f"https://doi.org/{doi}" if doi else ""


def _acm_ids_from_doi(doi: str) -> tuple[str, str]:
    match = re.match(r"10\.1145/(\d+)(?:\.(\d+))?", (doi or "").strip(), flags=re.I)
    if not match:
        return "", ""
    proceedings_id = match.group(1) or ""
    article_id = match.group(2) or proceedings_id
    return proceedings_id, article_id


def _acm_metadata_from_doi(doi: str) -> dict[str, str]:
    proceedings_id, article_id = _acm_ids_from_doi(doi)
    if not article_id:
        return {}
    return {
        "doi": doi,
        "doi_url": _doi_url(doi),
        "acm_proceedings_id": proceedings_id,
        "acm_article_id": article_id,
        "acm_abs_url": f"https://dl.acm.org/doi/abs/{doi}",
        "acm_pdf_url": f"https://dl.acm.org/doi/pdf/{doi}",
        "acm_epdf_url": f"https://dl.acm.org/doi/epdf/{doi}",
        "acm_full_html_url": f"https://dl.acm.org/doi/fullHtml/{doi}",
        "acm_legacy_pdf_url": f"https://dl.acm.org/ft_gateway.cfm?id={article_id}&type=pdf",
    }


def _dblp_record_metadata(
    venue_id: object,
    *,
    stream_id: str = "",
    dblp_url: str = "",
    dblp_xml_url: str = "",
    dblp_record_url: str = "",
    dblp_key: str = "",
    ee: str = "",
    doi: str = "",
) -> dict:
    doi = (doi or _doi_from_url(ee)).strip()
    metadata: dict[str, object] = {"venue_id": venue_id}
    if stream_id:
        metadata["dblp_stream"] = stream_id
    if dblp_url:
        metadata["dblp_url"] = dblp_url
    if dblp_xml_url:
        metadata["dblp_xml_url"] = dblp_xml_url
    if dblp_record_url:
        metadata["dblp_record_url"] = dblp_record_url
    if dblp_key:
        metadata["dblp_key"] = dblp_key
    if ee:
        metadata["publisher_url"] = ee
    if doi:
        if doi.lower().startswith("10.1145/"):
            metadata.update(_acm_metadata_from_doi(doi))
        else:
            metadata.update({"doi": doi, "doi_url": _doi_url(doi)})
    return metadata


def _openalex_pdf_url(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    primary = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
    candidates = [primary.get("pdf_url") or ""]
    open_access = item.get("open_access") if isinstance(item.get("open_access"), dict) else {}
    candidates.append(open_access.get("oa_url") or "")
    for loc in item.get("locations") or []:
        if isinstance(loc, dict):
            candidates.append(loc.get("pdf_url") or "")
    for url in candidates:
        text = str(url or "").strip()
        if text and (".pdf" in text.lower() or "/pdf/" in text.lower()):
            return text
    return ""


def _openalex_landing_url(item: dict) -> str:
    primary = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
    return str(primary.get("landing_page_url") or item.get("doi") or item.get("id") or "")


def _title_token_similarity(a: object, b: object) -> float:
    left = set(_title_key(str(a or "")).split())
    right = set(_title_key(str(b or "")).split())
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def _author_family_tokens(value: object) -> set[str]:
    if isinstance(value, str):
        raw_names = re.split(r",|;| and ", value)
    elif isinstance(value, list):
        raw_names = [str(item) for item in value]
    else:
        raw_names = []
    tokens: set[str] = set()
    for name in raw_names:
        parts = re.findall(r"[A-Za-z][A-Za-z'-]+", name.lower())
        if parts:
            tokens.add(parts[-1])
    return tokens


def _openalex_author_family_tokens(item: dict) -> set[str]:
    tokens: set[str] = set()
    for authorship in item.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") if isinstance(authorship.get("author"), dict) else {}
        name = str(author.get("display_name") or "")
        tokens.update(_author_family_tokens(name))
    return tokens


def _openalex_candidate_matches(paper: dict, item: dict) -> bool:
    item_title = item.get("display_name") or item.get("title")
    similarity = _title_token_similarity(paper.get("title"), item_title)
    expected_authors = _author_family_tokens(paper.get("authors"))
    candidate_authors = _openalex_author_family_tokens(item)
    if expected_authors:
        return similarity >= 0.82 and bool(expected_authors & candidate_authors)
    return similarity >= 0.95


def _openalex_item_from_payload(payload: dict, paper: dict, *, from_search: bool) -> dict:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("results"), list):
        for item in payload.get("results") or []:
            if isinstance(item, dict) and (not from_search or _openalex_candidate_matches(paper, item)):
                return item
        return {}
    return payload if not from_search or _openalex_candidate_matches(paper, payload) else {}

def _openalex_abstract_from_inverted_index(index: dict) -> str:
    if not isinstance(index, dict) or not index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, offsets in index.items():
        if not isinstance(offsets, list):
            continue
        for offset in offsets:
            try:
                positions.append((int(offset), str(word)))
            except Exception:
                continue
    if not positions:
        return ""
    return _clean_text(" ".join(word for _offset, word in sorted(positions)))


def _openalex_cache_key(paper: dict) -> str:
    doi = _doi_from_url(str(paper.get("doi") or paper.get("url") or paper.get("pdf_url") or ""))
    if doi:
        return f"doi:{doi.lower()}"
    return f"title:{_semantic_scholar_cache_key(paper.get('title', ''))}"
