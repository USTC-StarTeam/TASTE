from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core.common import (
    normalized_paper_title,
    paper_author_family_tokens,
    service_get,
)


RequestGet = Callable[..., Any]
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
_DECLARED_PDF_SELECTORS = (
    'meta[name="citation_pdf_url"]',
    'meta[name="eprints.document_url"]',
    'a[href$=".pdf"]',
    'a[href*="/bitstream/"]',
    'a[href*="/bitstreams/"]',
)


def _text(value: object) -> str:
    if isinstance(value, list):
        return next((str(item or "").strip() for item in value if str(item or "").strip()), "")
    return str(value or "").strip()


def _values(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _doi(value: object) -> str:
    match = _DOI_RE.search(str(value or ""))
    return match.group(0).rstrip(".,;)").lower() if match else ""


def _header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", {}) or {}
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return str(value or "")
    return ""


def _miss(kind: str, url: str, reason: str, response: Any | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": kind, "accepted": False, "url": url, "reason": reason}
    if response is not None:
        item["status_code"] = int(getattr(response, "status_code", 0) or 0)
    return item


def _is_declared_pdf_url(value: object) -> bool:
    url = str(value or "").strip()
    return bool(
        re.search(r"\.pdf(?:$|[?#])", url, re.I)
        or re.search(r"/bitstreams?/[^/?#]+/(?:download|content)(?:$|[?#])", url, re.I)
    )


def _declared_pdf_urls(response: Any) -> list[str]:
    content_type = _header(response, "content-type").lower()
    if "html" not in content_type:
        return []
    soup = BeautifulSoup(str(getattr(response, "text", "") or ""), "html.parser")
    urls: list[str] = []
    for node in soup.select(",".join(_DECLARED_PDF_SELECTORS)):
        value = str(node.get("content") or node.get("href") or "").strip()
        resolved = urljoin(str(getattr(response, "url", "") or ""), value)
        if resolved.startswith(("http://", "https://")) and _is_declared_pdf_url(resolved) and resolved not in urls:
            urls.append(resolved)
    return urls


def _repository_landing_candidates(
    url: str,
    *,
    kind: str,
    request_get: RequestGet,
) -> list[dict[str, Any]]:
    if _is_declared_pdf_url(url):
        return [{
            "kind": kind,
            "accepted": True,
            "pdf_url": url,
            "source_url": url,
            "requires_pdf_text_identity_check": True,
        }]
    try:
        response = request_get(url, timeout=20)
    except Exception as exc:
        return [_miss(kind.replace("_pdf", "_landing"), url, "request_error:" + type(exc).__name__)]
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code == 429:
        return [_miss(kind.replace("_pdf", "_landing"), url, "http_429", response)]
    if not bool(getattr(response, "ok", False)):
        return [_miss(kind.replace("_pdf", "_landing"), url, f"http_{status_code or 'error'}", response)]
    pdf_urls = _declared_pdf_urls(response)
    if not pdf_urls:
        return [_miss(kind.replace("_pdf", "_landing"), url, "no_declared_pdf", response)]
    return [
        {
            "kind": kind,
            "accepted": True,
            "pdf_url": pdf_url,
            "source_url": url,
            "requires_pdf_text_identity_check": True,
        }
        for pdf_url in pdf_urls
    ]


def _openaire_repository_candidates(paper: dict[str, Any], request_get: RequestGet) -> list[dict[str, Any]]:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    raw_urls = metadata.get("openaire_repository_urls")
    urls = _values(raw_urls)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for url in urls[:3]:
        if url in seen or not url.startswith(("http://", "https://")):
            continue
        seen.add(url)
        candidates.extend(
            _repository_landing_candidates(
                url,
                kind="openaire_repository_pdf",
                request_get=request_get,
            )
        )
    return candidates


def _hal_candidates(paper: dict[str, Any], request_get: RequestGet) -> list[dict[str, Any]]:
    title = _text(paper.get("title"))
    normalized_title = normalized_paper_title(title)
    if not normalized_title:
        return []
    escaped_title = title.replace('"', " ")
    try:
        response = request_get(
            "https://api.archives-ouvertes.fr/search/",
            params={
                "q": f'title_s:"{escaped_title}"',
                "fl": "title_s,doiId_s,authFullName_s,fileMain_s,uri_s",
                "wt": "json",
                "rows": "5",
            },
            timeout=20,
        )
    except Exception as exc:
        return [_miss("hal_exact_title_lookup", "https://api.archives-ouvertes.fr/search/", "request_error:" + type(exc).__name__)]
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code == 429:
        return [_miss("hal_exact_title_lookup", str(getattr(response, "url", "") or ""), "http_429", response)]
    if not bool(getattr(response, "ok", False)):
        return [_miss("hal_exact_title_lookup", str(getattr(response, "url", "") or ""), f"http_{status_code or 'error'}", response)]
    try:
        payload = response.json()
    except Exception:
        return [_miss("hal_exact_title_lookup", str(getattr(response, "url", "") or ""), "invalid_json", response)]
    docs = payload.get("response", {}).get("docs", []) if isinstance(payload, dict) else []
    expected_doi = _doi(paper.get("doi") or (paper.get("metadata") or {}).get("doi"))
    expected_authors = paper_author_family_tokens(paper.get("authors"))
    candidates: list[dict[str, Any]] = []
    for doc in docs if isinstance(docs, list) else []:
        if not isinstance(doc, dict):
            continue
        record_title = _text(doc.get("title_s"))
        if normalized_paper_title(record_title) != normalized_title:
            continue
        record_doi = _doi(_text(doc.get("doiId_s")))
        if expected_doi and record_doi and expected_doi != record_doi:
            continue
        record_authors = paper_author_family_tokens(_values(doc.get("authFullName_s")))
        if expected_authors and record_authors and not (expected_authors & record_authors):
            continue
        if expected_doi and not record_doi and expected_authors and not (expected_authors & record_authors):
            continue
        file_url = _text(doc.get("fileMain_s"))
        record_url = _text(doc.get("uri_s"))
        if file_url:
            candidates.append({
                "kind": "hal_exact_title_pdf",
                "accepted": True,
                "pdf_url": file_url,
                "source_url": record_url or str(getattr(response, "url", "") or ""),
                "requires_pdf_text_identity_check": True,
            })
        elif record_url:
            candidates.extend(
                _repository_landing_candidates(
                    record_url,
                    kind="hal_exact_title_pdf",
                    request_get=request_get,
                )
            )
    if not candidates:
        return [_miss("hal_exact_title_lookup", str(getattr(response, "url", "") or ""), "no_verified_full_text")]
    return candidates


def _hal_lookup_relevant(paper: dict[str, Any]) -> bool:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    if _doi(paper.get("doi") or metadata.get("doi")).startswith("10.1145/"):
        return True
    if str(metadata.get("hal_url") or "").strip():
        return True
    venue_blob = " ".join(
        str(value or "").lower()
        for value in [paper.get("source"), paper.get("venue"), metadata.get("conference_channel")]
    )
    return any(marker in venue_blob for marker in ("acm", "kdd", "sigir", "cikm", "web conference"))


def structured_repository_pdf_candidates(
    paper: dict[str, Any],
    *,
    request_get: RequestGet = service_get,
) -> list[dict[str, Any]]:
    candidates = _openaire_repository_candidates(paper, request_get)
    if _hal_lookup_relevant(paper):
        candidates.extend(_hal_candidates(paper, request_get))
    accepted_seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        pdf_url = str(candidate.get("pdf_url") or "")
        if candidate.get("accepted") and pdf_url:
            if pdf_url in accepted_seen:
                continue
            accepted_seen.add(pdf_url)
        result.append(candidate)
    return result
