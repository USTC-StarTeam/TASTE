from __future__ import annotations

import importlib
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

_SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))
importlib.invalidate_caches()
_core_common_module = sys.modules.get("core.common")
if _core_common_module is not None:
    _core_common_path = Path(str(getattr(_core_common_module, "__file__", ""))).resolve(strict=False)
    if _core_common_path != (_SCRIPTS_ROOT / "core" / "common.py").resolve(strict=False):
        sys.modules.pop("core.common", None)
_core_module = sys.modules.get("core")
if _core_module is not None:
    _core_path = str((_SCRIPTS_ROOT / "core").resolve(strict=False))
    _core_package_paths = getattr(_core_module, "__path__", None)
    if _core_package_paths is None:
        sys.modules.pop("core", None)
    else:
        _core_paths = [str(Path(str(path)).resolve(strict=False)) for path in _core_package_paths]
        if _core_path not in _core_paths:
            _core_package_paths.insert(0, _core_path)
try:
    _core_common_spec = importlib.util.find_spec("core.common")
except ModuleNotFoundError:
    _core_common_spec = None
if _core_common_spec is None:
    import types

    _core_path = str((_SCRIPTS_ROOT / "core").resolve(strict=False))
    _core_package = sys.modules.get("core")
    if _core_package is None or getattr(_core_package, "__path__", None) is None:
        _core_package = types.ModuleType("core")
        sys.modules["core"] = _core_package
    _core_package_paths = [
        str(Path(str(path)).resolve(strict=False))
        for path in getattr(_core_package, "__path__", [])
    ]
    _core_package.__path__ = [_core_path, *[path for path in _core_package_paths if path != _core_path]]
    _core_common_spec = importlib.util.spec_from_file_location("core.common", _SCRIPTS_ROOT / "core" / "common.py")
    if _core_common_spec is None or _core_common_spec.loader is None:
        raise ModuleNotFoundError("core.common")
    _core_common_module = importlib.util.module_from_spec(_core_common_spec)
    sys.modules["core.common"] = _core_common_module
    _core_common_spec.loader.exec_module(_core_common_module)

from core.common import DEFAULT_USER_AGENT, config_bool, env_bool, response_receipt, service_get

READ_USER_AGENT = DEFAULT_USER_AGENT
API_BASE = "https://api.semanticscholar.org/graph/v1"
DEFAULT_TIMEOUT = 20
DEFAULT_FIELDS = ",".join([
    "paperId",
    "corpusId",
    "externalIds",
    "url",
    "title",
    "abstract",
    "year",
    "venue",
    "publicationVenue",
    "publicationTypes",
    "authors",
    "citationCount",
    "influentialCitationCount",
    "isOpenAccess",
    "openAccessPdf",
    "fieldsOfStudy",
    "s2FieldsOfStudy",
    "tldr",
])

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)
ARXIV_ID_RE = re.compile(r"([0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.I)
ARXIV_LINK_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)\s*([0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.I)


def semantic_scholar_enabled() -> bool:
    """Semantic Scholar 是可选增强源；无 key 时默认不请求，避免共享限流拖慢主流程。"""
    if str(os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or os.environ.get("S2_API_KEY") or "").strip():
        return True
    return env_bool(
        "READING_ENABLE_SEMANTIC_SCHOLAR",
        config_bool("semantic_scholar.enabled_without_key", False),
    )


def _headers() -> dict[str, str]:
    headers = {"User-Agent": READ_USER_AGENT, "Accept": "application/json"}
    api_key = str(os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or os.environ.get("S2_API_KEY") or "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _clean_doi(value: object) -> str:
    text = str(value or "").strip()
    match = DOI_RE.search(text)
    doi = match.group(1) if match else text if text.lower().startswith("10.") and "/" in text else ""
    return doi.strip().rstrip(".,;:)]}").removeprefix("doi:").removeprefix("https://doi.org/").lower()


def _doi_from_paper(paper: dict[str, Any]) -> str:
    for key in ["doi", "published_doi", "url", "abs_url", "pdf_url", "input_article"]:
        doi = _clean_doi(paper.get(key))
        if doi:
            return doi
    return ""


def _arxiv_from_text(value: object) -> str:
    text = str(value or "").strip()
    match = ARXIV_LINK_RE.search(text)
    if match:
        return match.group(1)
    match = ARXIV_ID_RE.fullmatch(text.removeprefix("arXiv:").strip())
    return match.group(1) if match else ""


def _arxiv_from_paper(paper: dict[str, Any]) -> str:
    for key in ["arxiv_id", "paper_id", "id", "url", "abs_url", "pdf_url", "input_article"]:
        arxiv_id = _arxiv_from_text(paper.get(key))
        if arxiv_id:
            return arxiv_id
    return ""


def _title_tokens(value: object) -> set[str]:
    stop = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "towards", "toward", "with"}
    normalized = re.sub(r"[\u2010-\u2015]", "-", str(value or ""))
    return {token.lower() for token in re.findall(r"[A-Za-z0-9]+", normalized) if len(token) >= 2 and token.lower() not in stop}


def _title_similarity(left: object, right: object) -> float:
    left_tokens = _title_tokens(left)
    right_tokens = _title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def _paper_lookup_id(paper: dict[str, Any]) -> str:
    doi = _doi_from_paper(paper)
    if doi:
        return "DOI:" + doi
    arxiv_id = _arxiv_from_paper(paper)
    if arxiv_id:
        return "ARXIV:" + arxiv_id
    paper_id = str(paper.get("semantic_scholar_paper_id") or paper.get("s2_paper_id") or "").strip()
    if paper_id:
        return paper_id
    corpus_id = str(paper.get("semantic_scholar_corpus_id") or paper.get("corpus_id") or "").strip()
    return "CorpusId:" + corpus_id if corpus_id else ""


def _request_json(url: str, *, params: dict[str, str], timeout: int = DEFAULT_TIMEOUT) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.time()
    try:
        response = service_get(url, params=params, headers=_headers(), timeout=timeout, service="semanticscholar")
    except Exception as exc:
        return {}, {"status": "fetch_failed", "error": exc.__class__.__name__, "url": url, "seconds": round(time.time() - started, 3)}
    receipt: dict[str, Any] = {
        **response_receipt(response, service="semanticscholar"),
        "seconds": round(time.time() - started, 3),
    }
    if response.status_code == 429:
        receipt.update({"status": "rate_limited", "retry_after": response.headers.get("retry-after")})
        return {}, receipt
    if response.status_code != 200:
        receipt.update({"status": "http_error", "content_type": response.headers.get("content-type", "")})
        return {}, receipt
    try:
        payload = response.json()
    except Exception as exc:
        receipt.update({"status": "parse_failed", "error": exc.__class__.__name__})
        return {}, receipt
    receipt["status"] = "ok"
    return payload if isinstance(payload, dict) else {}, receipt


def _external_id(external_ids: dict[str, Any], *names: str) -> str:
    lowered = {str(key).lower(): str(value).strip() for key, value in external_ids.items() if str(value or "").strip()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return value
    return ""


def semantic_scholar_context_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    external_ids = payload.get("externalIds") if isinstance(payload.get("externalIds"), dict) else {}
    pdf = payload.get("openAccessPdf") if isinstance(payload.get("openAccessPdf"), dict) else {}
    venue = payload.get("publicationVenue") if isinstance(payload.get("publicationVenue"), dict) else {}
    tldr = payload.get("tldr") if isinstance(payload.get("tldr"), dict) else {}
    return {
        "paper_id": payload.get("paperId") or "",
        "corpus_id": payload.get("corpusId") or "",
        "url": payload.get("url") or "",
        "title": payload.get("title") or "",
        "year": payload.get("year") or "",
        "venue": payload.get("venue") or venue.get("name") or "",
        "publication_types": payload.get("publicationTypes") or [],
        "citation_count": payload.get("citationCount"),
        "influential_citation_count": payload.get("influentialCitationCount"),
        "fields_of_study": payload.get("fieldsOfStudy") or [],
        "s2_fields_of_study": payload.get("s2FieldsOfStudy") or [],
        "is_open_access": payload.get("isOpenAccess"),
        "open_access_pdf_url": pdf.get("url") or "",
        "open_access_pdf_status": pdf.get("status") or "",
        "tldr": tldr.get("text") or "",
        "external_ids": external_ids,
    }


def _apply_semantic_scholar_payload(paper: dict[str, Any], payload: dict[str, Any], *, source: str) -> dict[str, Any]:
    out = dict(paper)
    context = semantic_scholar_context_from_payload(payload)
    external_ids = context.get("external_ids") if isinstance(context.get("external_ids"), dict) else {}
    doi = _external_id(external_ids, "DOI")
    arxiv_id = _external_id(external_ids, "ArXiv")
    pmid = _external_id(external_ids, "PubMed", "PMID")
    pmcid = _external_id(external_ids, "PubMedCentral", "PMCID", "PMC")
    authors = payload.get("authors") if isinstance(payload.get("authors"), list) else []

    out.setdefault("title", context.get("title") or "")
    out.setdefault("abstract", payload.get("abstract") or "")
    if not out.get("authors") and authors:
        out["authors"] = [str(item.get("name") or "").strip() for item in authors if isinstance(item, dict) and str(item.get("name") or "").strip()]
    if doi and not out.get("doi"):
        out["doi"] = doi.lower()
    if arxiv_id and not out.get("arxiv_id"):
        out["arxiv_id"] = arxiv_id
    if pmid and not out.get("pmid"):
        out["pmid"] = pmid
    if pmcid and not out.get("pmcid"):
        out["pmcid"] = pmcid if pmcid.upper().startswith("PMC") else "PMC" + pmcid
    if context.get("paper_id"):
        out["semantic_scholar_paper_id"] = context.get("paper_id")
    if context.get("corpus_id"):
        out["semantic_scholar_corpus_id"] = context.get("corpus_id")
    for src_key, dst_key in [("year", "year"), ("venue", "venue"), ("tldr", "semantic_scholar_tldr")]:
        if context.get(src_key) and not out.get(dst_key):
            out[dst_key] = context.get(src_key)
    if context.get("citation_count") is not None and not out.get("citation_count"):
        out["citation_count"] = context.get("citation_count")
    if context.get("influential_citation_count") is not None and not out.get("influential_citation_count"):
        out["influential_citation_count"] = context.get("influential_citation_count")

    pdf_url = str(context.get("open_access_pdf_url") or "").strip()
    if pdf_url.startswith("http"):
        out["semantic_scholar_open_access_pdf_url"] = pdf_url
        urls = out.get("candidate_pdf_urls") if isinstance(out.get("candidate_pdf_urls"), list) else []
        if pdf_url not in [str(item) for item in urls] and pdf_url != out.get("pdf_url"):
            out["candidate_pdf_urls"] = [*urls, pdf_url]

    out["semantic_scholar_context"] = {**context, "source": source}
    return out


def semantic_scholar_enrich_paper(paper: dict[str, Any], *, enabled: bool | None = None, timeout: int = DEFAULT_TIMEOUT) -> tuple[dict[str, Any], dict[str, Any]]:
    if enabled is None:
        enabled = semantic_scholar_enabled()
    if not enabled:
        return dict(paper), {"status": "skipped_disabled", "reason": "设置 SEMANTIC_SCHOLAR_API_KEY/S2_API_KEY 或 READING_ENABLE_SEMANTIC_SCHOLAR=1 后启用。"}
    lookup_id = _paper_lookup_id(paper)
    if lookup_id:
        url = API_BASE + "/paper/" + quote(lookup_id, safe=":")
        payload, receipt = _request_json(url, params={"fields": DEFAULT_FIELDS}, timeout=timeout)
        receipt["lookup_mode"] = "paper_id"
        receipt["lookup_id"] = lookup_id
        if receipt.get("status") == "ok" and payload:
            return _apply_semantic_scholar_payload(paper, payload, source="semantic_scholar_graph_lookup"), receipt
        return dict(paper), receipt

    title = str(paper.get("title") or "").strip()
    if len(title.split()) < 3:
        return dict(paper), {"status": "skipped_no_lookup_key", "reason": "缺少 DOI、arXiv、S2 paperId/corpusId，且标题过短。"}
    url = API_BASE + "/paper/search"
    payload, receipt = _request_json(url, params={"query": title, "limit": "5", "fields": DEFAULT_FIELDS}, timeout=timeout)
    receipt["lookup_mode"] = "title_search"
    if receipt.get("status") != "ok":
        return dict(paper), receipt
    best: dict[str, Any] = {}
    best_similarity = 0.0
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        similarity = _title_similarity(title, item.get("title"))
        if similarity > best_similarity:
            best = item
            best_similarity = similarity
    receipt["best_title_similarity"] = round(best_similarity, 4)
    if best and best_similarity >= 0.82:
        return _apply_semantic_scholar_payload(paper, best, source="semantic_scholar_title_search"), receipt
    receipt["status"] = "no_confident_match"
    return dict(paper), receipt


def semantic_scholar_pdf_candidates(paper: dict[str, Any], *, enabled: bool | None = None, timeout: int = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    enriched, receipt = semantic_scholar_enrich_paper(paper, enabled=enabled, timeout=timeout)
    pdf_url = str(enriched.get("semantic_scholar_open_access_pdf_url") or "").strip()
    if not pdf_url.startswith("http"):
        if receipt.get("status") == "skipped_disabled":
            return []
        reason = "http_429_rate_limited" if int(receipt.get("status_code") or 0) == 429 else receipt.get("status") or "no_open_access_pdf"
        return [{
            "kind": "semantic_scholar_open_access_pdf",
            "accepted": False,
            "reason": reason,
            "semantic_scholar_receipt": receipt,
            "service": receipt.get("service") or "semanticscholar",
            "status_code": receipt.get("status_code"),
            "content_type": receipt.get("content_type"),
            "url": receipt.get("url"),
            "retry_after": receipt.get("retry_after"),
        }]
    if receipt.get("status") != "ok":
        return [{
            "kind": "semantic_scholar_open_access_pdf",
            "pdf_url": pdf_url,
            "accepted": False,
            "reason": receipt.get("status") or "semantic_scholar_receipt_not_ok",
            "semantic_scholar_receipt": receipt,
        }]
    expected_title = str(paper.get("title") or "").strip()
    found_title = str(enriched.get("semantic_scholar_context", {}).get("title") or enriched.get("title") or "").strip()
    if expected_title and found_title and _title_similarity(expected_title, found_title) < 0.82:
        return [{
            "kind": "semantic_scholar_open_access_pdf",
            "pdf_url": pdf_url,
            "accepted": False,
            "reason": "semantic_scholar_title_mismatch",
            "semantic_scholar_context": enriched.get("semantic_scholar_context") or {},
            "semantic_scholar_receipt": receipt,
        }]
    if pdf_url.startswith("https://openreview.net/"):
        return [{
            "kind": "semantic_scholar_open_access_pdf",
            "pdf_url": pdf_url,
            "accepted": False,
            "reason": "semantic_scholar_openreview_pdf_deferred_to_official_route",
            "semantic_scholar_context": enriched.get("semantic_scholar_context") or {},
            "semantic_scholar_receipt": receipt,
        }]
    if not pdf_url.startswith("http"):
        return []
    return [{
        "kind": "semantic_scholar_open_access_pdf",
        "pdf_url": pdf_url,
        "accepted": True,
        "semantic_scholar_context": enriched.get("semantic_scholar_context") or {},
        "semantic_scholar_receipt": receipt,
    }]
