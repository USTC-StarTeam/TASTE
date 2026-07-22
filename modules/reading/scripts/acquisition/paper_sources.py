from __future__ import annotations

import re
import os
import sys
import time
import importlib
import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

import requests

try:
    from bs4 import BeautifulSoup
except Exception:  # beautifulsoup4 是 HTML 正文抽取增强依赖；缺失时使用保守降级解析。
    BeautifulSoup = None  # type: ignore[assignment]

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

from core.common import DEFAULT_USER_AGENT, FULL_TEXT_MIN_CHARS as CONFIG_FULL_TEXT_MIN_CHARS, batch_cooldown_wait_cap, best_full_text_title, config_bool, config_value, env_bool, jina_api_key_configured, jina_request_headers, mark_process_http_blocker, process_backend_slot, process_blocker, response_receipt, service_cooldown_remaining, service_from_url, service_get
from core.common import coerce_str_list, read_json, safe_slug, write_text
from core.common import OUTPUT_ROOT, relative_to_reading, resolve_reading_path
from acquisition.semantic_scholar import semantic_scholar_enrich_paper


LogFn = Callable[[str], None]
AcquisitionServices = dict[str, Callable[..., Any]]

PMC_ID_RE = re.compile(r"\b(PMC\d{5,})\b", re.I)


ARXIV_ID_RE = re.compile(r"([0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.I)
ARXIV_LINK_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)\s*([0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.I)
READ_USER_AGENT = DEFAULT_USER_AGENT
MIN_FULL_TEXT_CHARS = CONFIG_FULL_TEXT_MIN_CHARS
_FULL_TEXT_CACHE_INDEX: dict[str, list[dict[str, Any]]] | None = None


def _download_first_readable_pdf(paper: dict[str, Any], pdf_dir: Path, log: LogFn) -> tuple[bool, Path, str, dict[str, Any]]:
    raise RuntimeError("PDF acquisition service was not supplied by the Reading orchestrator")


def _pdf_text_identity_ok(paper: dict[str, Any], text: str) -> bool:
    return bool(best_full_text_title(paper, text))


def _extract_pdf_text(path: Path) -> str:
    try:
        import fitz
    except Exception:
        return ""
    try:
        document = fitz.open(path)
        return "\n".join(page.get_text("text") for page in document)
    except Exception:
        return ""


def arxiv_id_from_text(value: Any) -> str:
    text = str(value or "").strip()
    match = ARXIV_LINK_RE.search(text)
    if match:
        return match.group(1)
    match = ARXIV_ID_RE.fullmatch(text.removeprefix("arXiv:").strip())
    return match.group(1) if match else ""


def pmc_id_from_text(value: Any) -> str:
    match = PMC_ID_RE.search(str(value or ""))
    return match.group(1).upper() if match else ""


def pmc_id_from_paper(paper: dict[str, Any], acquisition: dict[str, Any] | None = None) -> str:
    blobs = [paper.get(key) for key in ["pmc_id", "pmcid", "url", "html_url", "pdf_url", "doi", "input_article"]]
    if isinstance(acquisition, dict):
        blobs.append(str(acquisition))
    for blob in blobs:
        pmc_id = pmc_id_from_text(blob)
        if pmc_id:
            return pmc_id
    return ""


def doi_from_paper(paper: dict[str, Any]) -> str:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    for value in [
        paper.get("doi"), paper.get("published_doi"), paper.get("url"), paper.get("abs_url"),
        paper.get("pdf_url"), paper.get("input_article"), metadata.get("doi"),
        metadata.get("published_doi"), metadata.get("crossref_url"), metadata.get("url"), metadata.get("pdf_url"),
    ]:
        text = str(value or "")
        match = re.search(r"\b(10\.\d{4,9}/[^\s\"<>]+)", text, re.I)
        if match:
            return match.group(1).strip().rstrip(".,;:)]}").lower()
    return ""


def _atom_text(node: ET.Element, path: str, ns: dict[str, str]) -> str:
    return " ".join((node.findtext(path, default="", namespaces=ns) or "").split())


def fetch_arxiv_metadata(arxiv_id: str) -> dict[str, Any]:
    if not arxiv_id:
        return {}
    url = "https://export.arxiv.org/api/query?id_list=" + quote_plus(arxiv_id)
    try:
        response = service_get(url, timeout=30)
        if response.status_code != 200:
            return {"metadata_status": "arxiv_metadata_http_error", "status_code": response.status_code, "metadata_url": url}
        root = ET.fromstring(response.content)
    except Exception as exc:
        return {"metadata_status": "arxiv_metadata_fetch_failed", "error": exc.__class__.__name__, "metadata_url": url}
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = root.find("a:entry", ns)
    if entry is None:
        return {"metadata_status": "arxiv_metadata_not_found", "metadata_url": url}
    entry_id = _atom_text(entry, "a:id", ns)
    pdf_url = ""
    for link in entry.findall("a:link", ns):
        if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
            pdf_url = link.attrib.get("href", "")
            break
    if not pdf_url and "/abs/" in entry_id:
        pdf_url = entry_id.replace("/abs/", "/pdf/")
    return {
        "metadata_status": "arxiv_metadata_ready",
        "source": "arxiv",
        "paper_id": arxiv_id,
        "entry_id": entry_id or f"https://arxiv.org/abs/{arxiv_id}",
        "url": entry_id or f"https://arxiv.org/abs/{arxiv_id}",
        "abs_url": entry_id or f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": pdf_url,
        "title": _atom_text(entry, "a:title", ns),
        "abstract": _atom_text(entry, "a:summary", ns),
        "published": _atom_text(entry, "a:published", ns),
        "updated": _atom_text(entry, "a:updated", ns),
        "authors": [_atom_text(author, "a:name", ns) for author in entry.findall("a:author", ns)],
        "categories": [node.attrib.get("term", "") for node in entry.findall("a:category", ns) if node.attrib.get("term")],
        "metadata_url": url,
    }


def _looks_like_pdf_url(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(text.startswith("http") and (text.endswith(".pdf") or "/pdf/" in text or "pdf?" in text))


def build_paper_record(
    *,
    article: str,
    title: str = "",
    authors: Any = None,
    abstract: str = "",
    paper_id: str = "",
    pdf_url: str = "",
    url: str = "",
    source: str = "standalone_input",
) -> dict[str, Any]:
    article_text = str(article or "").strip()
    arxiv_id = arxiv_id_from_text(article_text) or arxiv_id_from_text(url) or arxiv_id_from_text(pdf_url)
    arxiv = fetch_arxiv_metadata(arxiv_id) if arxiv_id else {}
    record: dict[str, Any] = {}
    if arxiv.get("metadata_status") == "arxiv_metadata_ready":
        record.update(arxiv)
    record.update({
        "source": record.get("source") or source,
        "paper_id": paper_id or record.get("paper_id") or safe_slug(title or article_text),
        "id": paper_id or record.get("paper_id") or safe_slug(title or article_text),
        "title": title or record.get("title") or article_text,
        "authors": coerce_str_list(authors) or record.get("authors") or [],
        "abstract": abstract or record.get("abstract") or "",
        "url": url or record.get("url") or (article_text if article_text.startswith("http") and not _looks_like_pdf_url(article_text) else ""),
        "abs_url": record.get("abs_url") or (article_text if "arxiv.org/abs" in article_text else ""),
        "pdf_url": pdf_url or record.get("pdf_url") or (article_text if _looks_like_pdf_url(article_text) else ""),
        "input_article": article_text,
    })
    if article_text.startswith("10.") and "/" in article_text:
        record["doi"] = article_text
        record.setdefault("url", f"https://doi.org/{article_text}")
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    if metadata.get("doi") and not record.get("doi"):
        record["doi"] = str(metadata.get("doi"))
    record, semantic_receipt = semantic_scholar_enrich_paper(record)
    if semantic_receipt.get("status") != "skipped_disabled":
        record["semantic_scholar_acquisition"] = semantic_receipt
    return record




def _html_to_text(html: str) -> str:
    if BeautifulSoup is None:
        cleaned = re.sub(r"(?is)<(script|style|noscript|nav|footer|header|aside)\b.*?</\1>", " ", html or "")
        cleaned = re.sub(r"(?s)<[^>]+>", "\n", cleaned)
        text = re.sub(r"&nbsp;", " ", cleaned)
    else:
        soup = BeautifulSoup(html or "", "html.parser")
        for node in soup(["script", "style", "noscript", "nav", "footer", "header", "aside"]):
            node.decompose()
        candidates = []
        for selector in ["article", "main", "div.c-article-body", "section.article__body", "div.article__body", "body"]:
            node = soup.select_one(selector)
            if node is not None:
                candidate = "\n".join(part.strip() for part in node.get_text("\n", strip=True).splitlines() if part.strip())
                if len(candidate) > 500:
                    candidates.append(candidate)
        text = max(candidates, key=len) if candidates else soup.get_text("\n", strip=True)
    lines = []
    seen = set()
    for line in text.splitlines():
        item = " ".join(line.split())
        if len(item) < 3 or item in seen:
            continue
        seen.add(item)
        lines.append(item)
    return "\n".join(lines)




def _looks_like_paper_body(text: str) -> bool:
    value = str(text or "")
    lowered = value.lower()
    markers = [
        "introduction", "background", "methods", "materials and methods", "results",
        "discussion", "conclusion", "references", "experiment", "evaluation",
    ]
    marker_count = sum(1 for marker in markers if marker in lowered)
    return len(value) >= 8000 or (len(value) >= 5000 and marker_count >= 2)



def _xml_to_text(xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except Exception:
        return ""
    body = root.find(".//body")
    nodes = [body] if body is not None else [root]
    chunks: list[str] = []
    for node in nodes:
        for text in node.itertext():
            item = " ".join(str(text or "").split())
            if len(item) >= 3:
                chunks.append(item)
    lines: list[str] = []
    seen: set[str] = set()
    for item in chunks:
        if item in seen:
            continue
        seen.add(item)
        lines.append(item)
    return "\n".join(lines)


def _fetch_pmc_xml_text(pmc_id: str, timeout: int = 30) -> tuple[str, dict[str, Any]]:
    if not pmc_id:
        return "", {"accepted": False, "reason": "missing_pmc_id"}
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmc_id}/fullTextXML"
    try:
        response = service_get(url, timeout=timeout, headers={"Accept": "application/xml,text/xml,*/*"}, service="europepmc")
    except Exception as exc:
        return "", {"accepted": False, "url": url, "error": exc.__class__.__name__, "pmc_id": pmc_id}
    content_type = str(response.headers.get("content-type") or "").lower()
    if response.status_code != 200:
        return "", {"accepted": False, "pmc_id": pmc_id, **response_receipt(response, service="europepmc"), "content_type": content_type}
    text = _xml_to_text(response.text)
    return text, {"accepted": len(text) >= MIN_FULL_TEXT_CHARS and _looks_like_paper_body(text), **response_receipt(response, service="europepmc"), "content_type": content_type, "text_chars": len(text), "pmc_id": pmc_id, "source": "europepmc_fullTextXML"}


def _is_biorxiv_like_paper(paper: dict[str, Any]) -> bool:
    doi = doi_from_paper(paper)
    blob = " ".join(str(paper.get(key) or "") for key in ["source", "venue", "url", "html_url", "pdf_url", "doi"]).lower()
    return doi.startswith("10.1101/") or doi.startswith("10.64898/") or "biorxiv" in blob or "biorxiv.org" in blob


def _config_float(path: str, default: float) -> float:
    try:
        return float(config_value(path, default))
    except Exception:
        return default


def _wait_for_biorxiv_challenge_cooldown(doi: str, stage: str) -> dict[str, Any]:
    remaining = service_cooldown_remaining("biorxiv")
    if remaining <= 0:
        return {}
    cap = batch_cooldown_wait_cap("biorxiv")
    if cap <= 0 or remaining > cap:
        return {
            "kind": "biorxiv_challenge_cooldown_wait",
            "accepted": False,
            "reason": "skipped_due_to_active_challenge_cooldown",
            "service": "biorxiv",
            "doi": doi,
            "stage": stage,
            "cooldown_remaining_sec": remaining,
            "wait_cap_sec": cap,
            "message_zh": "bioRxiv 服务仍处于 Cloudflare challenge 冷却期；等待上限不足，本轮跳过该官方请求，避免继续触发站点防护。",
        }
    time.sleep(remaining)
    return {
        "kind": "biorxiv_challenge_cooldown_wait",
        "accepted": True,
        "service": "biorxiv",
        "doi": doi,
        "stage": stage,
        "waited_sec": remaining,
        "cooldown_remaining_after_wait_sec": service_cooldown_remaining("biorxiv"),
        "policy": "wait_before_biorxiv_official_api_or_xml_after_cloudflare_challenge",
    }


def _normalize_biorxiv_jatsxml_url(url: str) -> str:
    if not str(url or "").startswith("http"):
        return str(url or "")
    parsed = urlsplit(str(url))
    if "biorxiv.org" not in parsed.netloc.lower():
        return str(url)
    return urlunsplit((parsed.scheme, parsed.netloc, re.sub(r"/{2,}", "/", parsed.path), parsed.query, parsed.fragment))


def _fetch_biorxiv_jats_xml_text(paper: dict[str, Any], timeout: int = 30) -> tuple[str, dict[str, Any]]:
    doi = doi_from_paper(paper)
    if not doi or not _is_biorxiv_like_paper(paper):
        return "", {"kind": "biorxiv_api_jatsxml", "accepted": False, "reason": "missing_biorxiv_doi"}
    cooldown_waits: list[dict[str, Any]] = []
    wait_attempt = _wait_for_biorxiv_challenge_cooldown(doi, "before_api_details")
    if wait_attempt:
        cooldown_waits.append(wait_attempt)
    if wait_attempt and wait_attempt.get("accepted") is not True:
        return "", {
            "kind": "biorxiv_api_jatsxml",
            "accepted": False,
            "doi": doi,
            "reason": str(wait_attempt.get("reason") or "skipped_due_to_active_challenge_cooldown"),
            "cooldown_waits": cooldown_waits,
            "message_zh": "bioRxiv 服务仍处于 Cloudflare challenge 冷却期；本轮跳过官方 API/XML 请求，避免继续触发站点防护。",
        }
    api_url = "https://api.biorxiv.org/details/biorxiv/" + quote(doi, safe="/")
    try:
        response = service_get(api_url, timeout=timeout, headers={"Accept": "application/json,*/*"}, service="biorxiv")
    except Exception as exc:
        return "", {"kind": "biorxiv_api_jatsxml", "accepted": False, "url": api_url, "doi": doi, "error": exc.__class__.__name__}
    api_receipt = {"kind": "biorxiv_api_details", "doi": doi, **response_receipt(response, service="biorxiv")}
    if response.status_code != 200:
        return "", {"kind": "biorxiv_api_jatsxml", "accepted": False, "doi": doi, "api_attempt": api_receipt, "cooldown_waits": cooldown_waits, "reason": f"http_{response.status_code}"}
    try:
        payload = response.json()
    except Exception as exc:
        return "", {"kind": "biorxiv_api_jatsxml", "accepted": False, "doi": doi, "api_attempt": api_receipt, "cooldown_waits": cooldown_waits, "error": exc.__class__.__name__}
    item = next(
        (
            candidate
            for candidate in payload.get("collection") or []
            if isinstance(candidate, dict) and str(candidate.get("doi") or "").lower() == doi.lower()
        ),
        {},
    )
    jats_url = _normalize_biorxiv_jatsxml_url(str(item.get("jatsxml") or "").strip() if isinstance(item, dict) else "")
    if not jats_url.startswith("http"):
        return "", {"kind": "biorxiv_api_jatsxml", "accepted": False, "doi": doi, "api_attempt": api_receipt, "cooldown_waits": cooldown_waits, "reason": "missing_jatsxml_url"}
    wait_attempt = _wait_for_biorxiv_challenge_cooldown(doi, "before_jatsxml")
    if wait_attempt:
        cooldown_waits.append(wait_attempt)
    if wait_attempt and wait_attempt.get("accepted") is not True:
        return "", {
            "kind": "biorxiv_api_jatsxml",
            "accepted": False,
            "doi": doi,
            "api_attempt": api_receipt,
            "jatsxml_url": jats_url,
            "reason": str(wait_attempt.get("reason") or "skipped_due_to_active_challenge_cooldown"),
            "cooldown_waits": cooldown_waits,
            "message_zh": "bioRxiv API 已返回 JATS XML 地址，但服务处于 challenge 冷却期；本轮不继续请求 XML。",
        }
    try:
        xml_response = service_get(jats_url, timeout=timeout, headers={"Accept": "application/xml,text/xml,*/*"}, service="biorxiv")
    except Exception as exc:
        return "", {"kind": "biorxiv_api_jatsxml", "accepted": False, "doi": doi, "api_attempt": api_receipt, "jatsxml_url": jats_url, "cooldown_waits": cooldown_waits, "error": exc.__class__.__name__}
    xml_receipt = {"kind": "biorxiv_jatsxml", "doi": doi, **response_receipt(xml_response, service="biorxiv")}
    if xml_response.status_code != 200:
        return "", {"kind": "biorxiv_api_jatsxml", "accepted": False, "doi": doi, "api_attempt": api_receipt, "xml_attempt": xml_receipt, "cooldown_waits": cooldown_waits, "reason": f"http_{xml_response.status_code}"}
    text = _xml_to_text(xml_response.text)
    body_ok = len(text) >= MIN_FULL_TEXT_CHARS and _looks_like_paper_body(text)
    identity_ok = _pdf_text_identity_ok(paper, text)
    accepted = body_ok and identity_ok
    return text if accepted else "", {
        "kind": "biorxiv_api_jatsxml",
        "accepted": accepted,
        "doi": doi,
        "api_attempt": api_receipt,
        "xml_attempt": xml_receipt,
        "jatsxml_url": jats_url,
        "cooldown_waits": cooldown_waits,
        "text_chars": len(text),
        "paper_body_markers": body_ok,
        "pdf_text_identity_check": identity_ok,
        "source": "biorxiv_api_jatsxml",
        **({"reason": "jatsxml_identity_or_body_mismatch"} if not accepted else {}),
    }


def _pmc_xml_candidates_from_europepmc(doi: str, title: str = "", timeout: int = 30) -> tuple[list[str], dict[str, Any]]:
    query = ""
    if doi:
        query = f'DOI:"{doi}"'
    elif title:
        query = f'TITLE:"{title}"'
    if not query:
        return [], {"accepted": False, "reason": "missing_doi_or_title"}
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    params = {"query": query, "format": "json", "pageSize": "5"}
    try:
        response = service_get(url, params=params, timeout=timeout, headers={"Accept": "application/json"}, service="europepmc")
    except Exception as exc:
        return [], {"accepted": False, "url": url, "query": query, "error": exc.__class__.__name__}
    receipt: dict[str, Any] = {"accepted": response.status_code == 200, "query": query, **response_receipt(response, service="europepmc")}
    if response.status_code != 200:
        return [], receipt
    try:
        payload = response.json()
    except Exception as exc:
        receipt.update({"accepted": False, "error": exc.__class__.__name__})
        return [], receipt
    result_list = payload.get("resultList") if isinstance(payload.get("resultList"), dict) else {}
    ids: list[str] = []
    for item in result_list.get("result") or []:
        if not isinstance(item, dict):
            continue
        pmcid = str(item.get("pmcid") or item.get("pmcId") or "").strip()
        has_full_text = str(item.get("hasTextMinedTerms") or item.get("inEPMC") or item.get("hasFullText") or "").lower()
        if pmcid and pmcid.upper().startswith("PMC"):
            ids.append(pmcid.upper())
        elif pmcid and has_full_text in {"y", "true", "1"}:
            ids.append(("PMC" + pmcid).upper() if pmcid.isdigit() else pmcid.upper())
    deduped: list[str] = []
    for pmcid in ids:
        if pmcid not in deduped:
            deduped.append(pmcid)
    receipt["pmc_ids"] = deduped
    return deduped, receipt


def _openalex_full_text_hints(
    paper: dict[str, Any],
    candidate_provider: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    doi = doi_from_paper(paper)
    if not doi:
        return {"status": "skipped_missing_doi", "doi": ""}
    hints: list[dict[str, Any]] = []
    if candidate_provider is None:
        return {"status": "unavailable_missing_acquisition_service", "doi": doi, "hints": hints}
    try:
        candidates = candidate_provider({**paper, "doi": doi})
    except Exception as exc:
        return {"status": "failed", "doi": doi, "error": exc.__class__.__name__, "hints": hints}
    for candidate in candidates:
        landing = str(candidate.get("landing_page_url") or "").strip()
        pdf_url = str(candidate.get("pdf_url") or "").strip()
        text = " ".join([landing, pdf_url, str(candidate)])
        pmc_id = pmc_id_from_text(text)
        if landing or pdf_url or pmc_id:
            hints.append({
                "kind": candidate.get("kind"),
                "landing_page_url": landing,
                "pdf_url": pdf_url,
                "pmc_id": pmc_id,
                "openalex_id": candidate.get("openalex_id"),
            })
    return {"status": "ok" if hints else "no_openalex_full_text_hints", "doi": doi, "hints": hints}


def _same_paper_html_hints(
    paper: dict[str, Any],
    candidate_provider: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    hints: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    if candidate_provider is None:
        return {"status": "unavailable_missing_acquisition_service", "hints": hints, "attempts": attempts}
    try:
        candidates = candidate_provider(paper)
    except Exception as exc:
        return {"status": "failed", "error": exc.__class__.__name__, "hints": hints, "attempts": attempts}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("accepted") and candidate.get("landing_page_url"):
            hints.append({
                "kind": candidate.get("kind"),
                "landing_page_url": candidate.get("landing_page_url"),
                "source_url": candidate.get("source_url"),
            })
        else:
            attempts.append(candidate)
    return {"status": "ok" if hints else "no_same_paper_html_hints", "hints": hints, "attempts": attempts}

def _fetch_html_text(url: str, timeout: int = 30) -> tuple[str, dict[str, Any]]:
    if not url or not str(url).startswith("http"):
        return "", {"accepted": False, "reason": "missing_html_url"}
    lowered_url = str(url).lower()
    openreview_anonymous_enabled = env_bool(
        "READING_OPENREVIEW_ALLOW_ANONYMOUS_HTTP",
        config_bool("openreview.allow_anonymous_http", True),
    )
    if "openreview.net" in lowered_url and not openreview_anonymous_enabled:
        return "", {
            "accepted": False,
            "url": url,
            "service": "openreview",
            "reason": "anonymous_openreview_http_disabled",
            "message_zh": "当前显式禁用匿名 OpenReview HTML/PDF/API 兜底；请配置官方 openreview-py 凭据，或移除 READING_OPENREVIEW_ALLOW_ANONYMOUS_HTTP=0。",
        }
    if "arxiv.org/abs/" in lowered_url:
        return "", {"accepted": False, "url": url, "reason": "arxiv_abs_page_is_metadata_not_paper_full_text"}
    if ("papers.nips.cc" in lowered_url or "proceedings.neurips.cc" in lowered_url) and "-abstract-" in lowered_url:
        return "", {"accepted": False, "url": url, "reason": "conference_abstract_page_is_not_paper_full_text"}
    service_name = service_from_url(url)
    cooldown_remaining = service_cooldown_remaining(service_name)
    if cooldown_remaining > 0:
        return "", {
            "accepted": False,
            "url": url,
            "service": service_name,
            "reason": "skipped_due_to_active_challenge_cooldown",
            "cooldown_remaining_sec": cooldown_remaining,
            "message_zh": "该服务仍处于本进程记录的 Cloudflare challenge 冷却期；本轮跳过 HTML 请求，避免继续触发站点防护。",
        }
    try:
        response = service_get(url, timeout=timeout, headers={"Accept": "text/html,application/xhtml+xml,*/*"})
    except Exception as exc:
        return "", {"accepted": False, "url": url, "error": exc.__class__.__name__}
    content_type = str(response.headers.get("content-type") or "").lower()
    if response.status_code != 200:
        return "", {"accepted": False, **response_receipt(response), "content_type": content_type}
    if "html" not in content_type and not response.text.lstrip().lower().startswith("<!doctype") and "<html" not in response.text[:500].lower():
        return "", {"accepted": False, **response_receipt(response), "content_type": content_type, "reason": "not_html"}
    text = _html_to_text(response.text)
    accepted = len(text) >= MIN_FULL_TEXT_CHARS and _looks_like_paper_body(text)
    return text, {"accepted": accepted, **response_receipt(response), "content_type": content_type, "text_chars": len(text), "paper_body_markers": _looks_like_paper_body(text)}


def _is_science_like_paper(paper: dict[str, Any]) -> bool:
    doi = doi_from_paper(paper).lower()
    blob = " ".join(
        str(paper.get(key) or "")
        for key in ["source", "venue", "journal", "published_journal", "url", "abs_url", "html_url", "pdf_url"]
    ).lower()
    return doi.startswith("10.1126/") or "science.org" in blob or "science" in {str(paper.get("source") or "").lower(), str(paper.get("venue") or "").lower()}


def _science_html_candidate_urls(paper: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    doi = doi_from_paper(paper)
    if doi.startswith("10.1126/"):
        urls.extend([
            f"https://www.science.org/doi/full/{doi}",
            f"https://www.science.org/doi/{doi}",
            f"https://www.science.org/doi/abs/{doi}",
        ])
    for value in [paper.get("html_url"), paper.get("url"), paper.get("abs_url")]:
        item = str(value or "").strip()
        if item:
            urls.append(item)
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _try_science_official_html_first(paper: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not _is_science_like_paper(paper):
        return "", {}
    attempts: list[dict[str, Any]] = []
    for html_url in _science_html_candidate_urls(paper)[:6]:
        html_text, attempt = _fetch_html_text(html_url)
        attempts.append({**attempt, "kind": "science_official_html_before_pdf"})
        if attempt.get("accepted") and len(html_text) >= MIN_FULL_TEXT_CHARS and _looks_like_paper_body(html_text):
            return html_text, {
                "attempts": attempts,
                "selected": attempts[-1],
                "policy": "science_official_html_is_tried_before_pdf_to_reduce_pdf_endpoint_challenge; official PDF remains fallback if HTML is not accepted",
            }
    return "", {
        "attempts": attempts,
        "selected": {},
        "policy": "science_official_html_first_failed; fallback_to_pdf_html_xml_acquisition",
    }


def _reader_pdf_text_url(pdf_url: str) -> str:
    target = re.sub(r"^https?://", "", str(pdf_url or "").strip(), flags=re.I)
    return "https://r.jina.ai/http://" + quote(target, safe="/?&=%:~")


def _reader_backend_name() -> str:
    return "jina_reader_authenticated" if jina_api_key_configured() else "jina_reader_anonymous"


def _reader_process_blocker_receipt(*, kind: str, source_url: str, reader_url: str) -> dict[str, Any]:
    blocker = process_blocker(_reader_backend_name())
    if not blocker:
        return {}
    return {
        "kind": kind,
        "accepted": False,
        "source_url": source_url,
        "reader_url": reader_url,
        "reason": "skipped_after_prior_backend_access_failure",
        "prior_reason": blocker.get("reason"),
    }


def _openreview_reader_pdf_text_enabled() -> bool:
    return env_bool(
        "READING_OPENREVIEW_READER_PDF_TEXT",
        config_bool("openreview.reader_pdf_text", True),
    )


def _openreview_reader_blocker_reason(text: str) -> str:
    lowered = str(text or "")[:5000].lower()
    if "verifying your browser" in lowered or "complete the check below" in lowered:
        return "openreview_reader_challenge"
    if "challenge required" in lowered or "challenge-required" in lowered:
        return "openreview_reader_challenge"
    return ""


def _fetch_reader_pdf_text(paper: dict[str, Any], pdf_url: str, timeout: int = 45) -> tuple[str, dict[str, Any]]:
    pdf_url = str(pdf_url or "").strip()
    if not pdf_url.startswith("http"):
        return "", {"kind": "reader_pdf_text", "accepted": False, "pdf_url": pdf_url, "reason": "missing_pdf_url"}
    lowered = pdf_url.lower()
    if "openreview.net" in lowered and not _openreview_reader_pdf_text_enabled():
        return "", {"kind": "reader_pdf_text", "accepted": False, "pdf_url": pdf_url, "reason": "openreview_reader_pdf_skipped"}
    reader_url = _reader_pdf_text_url(pdf_url)
    backend = _reader_backend_name()
    with process_backend_slot(backend) as blocker:
        if blocker:
            blocker_receipt = _reader_process_blocker_receipt(kind="reader_pdf_text", source_url=pdf_url, reader_url=reader_url)
            blocker_receipt["pdf_url"] = pdf_url
            return "", blocker_receipt
        try:
            response = service_get(reader_url, timeout=timeout, headers=jina_request_headers())
        except Exception as exc:
            return "", {"kind": "reader_pdf_text", "accepted": False, "pdf_url": pdf_url, "reader_url": reader_url, "error": exc.__class__.__name__}
        receipt: dict[str, Any] = {"kind": "reader_pdf_text", "pdf_url": pdf_url, "reader_url": reader_url, **response_receipt(response)}
        if response.status_code != 200:
            if response.status_code in {401, 403, 429}:
                mark_process_http_blocker(backend, response, f"http_{response.status_code}")
            receipt.update({"accepted": False})
            return "", receipt
    raw_text = response.text or ""
    if "Warning: Target URL returned error 404" in raw_text[:1500] or "Warning: Target URL returned error 403" in raw_text[:1500]:
        receipt.update({"accepted": False, "reason": "reader_target_error_page", "text_chars": len(raw_text)})
        return "", receipt
    openreview_blocker = _openreview_reader_blocker_reason(raw_text) if "openreview.net" in lowered else ""
    if openreview_blocker:
        receipt.update({"accepted": False, "reason": openreview_blocker, "text_chars": len(raw_text)})
        return "", receipt
    text = raw_text.split("Markdown Content:", 1)[-1].strip() if "Markdown Content:" in raw_text else raw_text.strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    paper_body_markers = _looks_like_paper_body(text)
    identity_ok = _pdf_text_identity_ok(paper, text)
    accepted = len(text) >= MIN_FULL_TEXT_CHARS and paper_body_markers and identity_ok
    receipt.update({
        "accepted": accepted,
        "text_chars": len(text),
        "paper_body_markers": paper_body_markers,
        "pdf_text_identity_check": identity_ok,
    })
    if not accepted:
        receipt["reason"] = "reader_pdf_text_identity_or_body_mismatch"
        return "", receipt
    return text, receipt


def _fetch_biorxiv_reader_full_text(paper: dict[str, Any], timeout: int = 45) -> tuple[str, dict[str, Any]]:
    doi = doi_from_paper(paper)
    if not doi or not _is_biorxiv_like_paper(paper):
        return "", {"kind": "biorxiv_reader_full_html", "accepted": False, "reason": "missing_biorxiv_doi"}
    attempts: list[dict[str, Any]] = []
    backend = _reader_backend_name()
    with process_backend_slot(backend) as blocker:
        if blocker:
            source_url = f"https://www.biorxiv.org/content/{doi}.full"
            reader_url = _reader_pdf_text_url(source_url)
            attempts.append(_reader_process_blocker_receipt(kind="biorxiv_reader_full_html", source_url=source_url, reader_url=reader_url))
        else:
            for suffix in [".full", ".full.txt"]:
                source_url = f"https://www.biorxiv.org/content/{doi}{suffix}"
                reader_url = _reader_pdf_text_url(source_url)
                try:
                    response = service_get(reader_url, timeout=timeout, headers=jina_request_headers())
                except Exception as exc:
                    attempts.append({
                        "kind": "biorxiv_reader_full_html",
                        "accepted": False,
                        "source_url": source_url,
                        "reader_url": reader_url,
                        "error": exc.__class__.__name__,
                    })
                    continue
                raw_text = response.text or ""
                receipt: dict[str, Any] = {
                    "kind": "biorxiv_reader_full_html",
                    "source_url": source_url,
                    "reader_url": reader_url,
                    **response_receipt(response),
                }
                if response.status_code != 200:
                    if response.status_code in {401, 403, 429}:
                        mark_process_http_blocker(backend, response, f"http_{response.status_code}")
                    receipt.update({"accepted": False, "reason": f"http_{response.status_code}"})
                    attempts.append(receipt)
                    if response.status_code in {401, 403, 429}:
                        break
                    continue
                if "Warning: Target URL returned error 404" in raw_text[:1500] or "Warning: Target URL returned error 403" in raw_text[:1500]:
                    receipt.update({"accepted": False, "reason": "reader_target_error_page", "text_chars": len(raw_text)})
                    attempts.append(receipt)
                    continue
                text = raw_text.split("Markdown Content:", 1)[-1].strip() if "Markdown Content:" in raw_text else raw_text.strip()
                text = re.sub(r"\n{3,}", "\n\n", text)
                paper_body_markers = _looks_like_paper_body(text)
                identity_ok = _pdf_text_identity_ok(paper, text)
                # Jina's markdown view of bioRxiv full HTML can omit the title line while
                # preserving the full article body. For the official /content/<doi>.full
                # route constructed from the input DOI, the exact DOI URL is a stronger
                # same-paper signal than a title found in the first few rendered lines.
                exact_official_doi_source = doi in source_url and "www.biorxiv.org/content/" in source_url
                accepted = len(text) >= MIN_FULL_TEXT_CHARS and paper_body_markers and (identity_ok or exact_official_doi_source)
                receipt.update({
                    "accepted": accepted,
                    "text_chars": len(text),
                    "paper_body_markers": paper_body_markers,
                    "pdf_text_identity_check": identity_ok,
                    "biorxiv_exact_official_doi_source_check": exact_official_doi_source,
                })
                attempts.append(receipt)
                if accepted:
                    return text, {"attempts": attempts, "selected": receipt}
                receipt["reason"] = "biorxiv_reader_full_html_identity_or_body_mismatch"
    return "", {"attempts": attempts, "selected": {}}


def _reader_pdf_text_from_failed_pdf_attempts(paper: dict[str, Any], acquisition: dict[str, Any], limit: int = 4) -> tuple[str, dict[str, Any]]:
    attempts = acquisition.get("attempts") if isinstance(acquisition.get("attempts"), list) else []
    reader_attempts: list[dict[str, Any]] = []
    seen: set[str] = set()

    def openreview_blocker_seen() -> bool:
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            kind = str(attempt.get("kind") or "").lower()
            pdf_url = str(attempt.get("pdf_url") or "").lower()
            receipt = attempt.get("download_receipt") if isinstance(attempt.get("download_receipt"), dict) else {}
            selected = receipt.get("selected") if isinstance(receipt.get("selected"), dict) else {}
            if "openreview" not in kind and "openreview.net" not in pdf_url:
                continue
            reasons = " ".join(
                str(value or "").lower()
                for value in [
                    attempt.get("download_failure_reason"),
                    attempt.get("reason"),
                    attempt.get("error"),
                    receipt.get("reason"),
                    receipt.get("error"),
                    selected.get("reason"),
                    selected.get("error"),
                ]
            )
            try:
                has_403 = any(int(value or 0) == 403 for value in [attempt.get("status_code"), receipt.get("status_code"), selected.get("status_code")])
            except (TypeError, ValueError):
                has_403 = False
            if has_403 or "forbidden" in reasons or "challenge" in reasons or "403" in reasons:
                return True
        return False

    def priority(attempt: dict[str, Any]) -> int:
        kind = str(attempt.get("kind") or "").lower()
        pdf_url = str(attempt.get("pdf_url") or "").lower()
        if "mlanthology" in kind and "openreview.net/pdf/" in pdf_url:
            return 0
        if "openreview.net/pdf/" in pdf_url and pdf_url.endswith(".pdf"):
            return 1
        if "openreview_pdf_from_forum_url" in kind or "openreview.net/pdf?id=" in pdf_url:
            return 2
        if "openreview" in kind or "openreview.net/attachment" in pdf_url or "openreview.net" in pdf_url:
            return 3
        return 4

    eligible_attempts: list[tuple[int, int, dict[str, Any]]] = []
    for attempt_index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict):
            continue
        pdf_url = str(attempt.get("pdf_url") or "").strip()
        if not pdf_url or not pdf_url.startswith("http") or pdf_url in seen:
            continue
        seen.add(pdf_url)
        kind = str(attempt.get("kind") or "").lower()
        lowered_url = pdf_url.lower()
        is_biorxiv_official_pdf = "biorxiv.org/content/" in lowered_url and kind == "doi_direct_biorxiv_full_pdf"
        if not (
            attempt.get("requires_pdf_text_identity_check")
            or "search_result" in kind
            or "conference" in kind
            or "mlanthology" in kind
            or "openreview" in kind
            or "openreview.net" in lowered_url
            or is_biorxiv_official_pdf
        ):
            continue
        eligible_attempts.append((priority(attempt), attempt_index, attempt))

    effective_limit = 1 if openreview_blocker_seen() else limit
    for _, _, attempt in sorted(eligible_attempts, key=lambda item: (item[0], item[1])):
        pdf_url = str(attempt.get("pdf_url") or "").strip()
        text, receipt = _fetch_reader_pdf_text(paper, pdf_url)
        receipt["source_pdf_attempt"] = {
            "kind": attempt.get("kind"),
            "download_failure_reason": attempt.get("download_failure_reason"),
            "rejected_reason": attempt.get("rejected_reason"),
        }
        reader_attempts.append(receipt)
        if receipt.get("accepted") and text:
            return text, {"attempts": reader_attempts, "selected": receipt}
        if len(reader_attempts) >= effective_limit:
            break
    return "", {"attempts": reader_attempts, "selected": {}}


def _runtime_cache_title_key(value: object) -> str:
    stop = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "towards", "toward", "with"}
    normalized = re.sub(r"[\u2010-\u2015]", "-", str(value or ""))
    tokens = [
        token.lower()
        for token in re.findall(r"[A-Za-z0-9]+", normalized)
        if len(token) >= 2 and token.lower() not in stop
    ]
    return " ".join(sorted(set(tokens)))


def _runtime_cached_full_text(paper: dict[str, Any], limit: int = 4) -> tuple[str, dict[str, Any]]:
    if env_bool("READING_DISABLE_RUNTIME_FULL_TEXT_CACHE", env_bool("READING_DISABLE_RUNTIME_CACHE", False)):
        return "", {"attempts": [], "selected": {}, "status": "disabled_by_READING_DISABLE_RUNTIME_CACHE"}

    def build_index() -> dict[str, list[dict[str, Any]]]:
        cache: dict[str, list[dict[str, Any]]] = {}
        if not OUTPUT_ROOT.exists():
            return cache
        for result_path in OUTPUT_ROOT.glob("**/read_results.json"):
            payload = read_json(result_path, {})
            if not isinstance(payload, dict):
                continue
            packet = payload.get("full_text_packet") if isinstance(payload.get("full_text_packet"), dict) else {}
            cached_paper = payload.get("paper") if isinstance(payload.get("paper"), dict) else {}
            if not packet.get("full_text_available"):
                continue
            text_path_value = str(packet.get("text_path") or "").strip()
            if not text_path_value:
                continue
            try:
                text_path = resolve_reading_path(text_path_value)
            except Exception:
                continue
            if not text_path.is_file():
                continue
            title_key = _runtime_cache_title_key(cached_paper.get("title") or packet.get("title"))
            if not title_key:
                continue
            cache.setdefault(title_key, []).append({
                "kind": "reading_runtime_cached_full_text",
                "cached_text_path": str(text_path),
                "cached_read_results": str(result_path),
                "cached_full_text_chars": packet.get("full_text_chars") or packet.get("text_chars") or 0,
                "cached_full_text_evidence_kind": packet.get("full_text_evidence_kind") or packet.get("text_kind") or "",
                "cached_text_kind": packet.get("text_kind") or "",
                "pdf_url": packet.get("pdf_url") or "",
                "accepted": True,
            })
        return cache

    global _FULL_TEXT_CACHE_INDEX
    if _FULL_TEXT_CACHE_INDEX is None:
        _FULL_TEXT_CACHE_INDEX = build_index()
    title_key = _runtime_cache_title_key(paper.get("title"))
    attempts: list[dict[str, Any]] = []
    if not title_key:
        return "", {"attempts": [], "selected": {}}
    seen_paths: set[str] = set()
    for candidate in _FULL_TEXT_CACHE_INDEX.get(title_key, []):
        text_path_value = str(candidate.get("cached_text_path") or "")
        if not text_path_value or text_path_value in seen_paths:
            continue
        seen_paths.add(text_path_value)
        attempt = dict(candidate)
        try:
            text = Path(text_path_value).read_text(encoding="utf-8")
        except Exception as exc:
            attempt.update({"accepted": False, "error": exc.__class__.__name__})
            attempts.append(attempt)
            continue
        paper_body_markers = _looks_like_paper_body(text)
        identity_ok = _pdf_text_identity_ok(paper, text)
        accepted = len(text) >= MIN_FULL_TEXT_CHARS and paper_body_markers and identity_ok
        attempt.update({
            "accepted": accepted,
            "text_chars": len(text),
            "paper_body_markers": paper_body_markers,
            "pdf_text_identity_check": identity_ok,
        })
        if not accepted:
            attempt["reason"] = "runtime_cached_full_text_identity_or_body_mismatch"
            attempts.append(attempt)
            if len(attempts) >= limit:
                break
            continue
        attempts.append(attempt)
        return text, {"attempts": attempts, "selected": attempt}
    return "", {"attempts": attempts[:limit], "selected": {}}


def _flatten_route_items(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if any(key in value for key in ["kind", "reason", "status_code", "error", "url", "message_zh", "accepted"]):
            out.append(value)
        for key in ["attempts", "hints", "fetches", "download_receipt", "selected", "openreview_browser_login"]:
            out.extend(_flatten_route_items(value.get(key)))
    elif isinstance(value, list):
        for item in value:
            out.extend(_flatten_route_items(item))
    return out


def _cloudflare_challenged_services(value: Any) -> set[str]:
    services: set[str] = set()
    for item in _flatten_route_items(value):
        if not isinstance(item, dict):
            continue
        headers_subset = item.get("headers_subset") if isinstance(item.get("headers_subset"), dict) else {}
        if item.get("challenge_type") != "cloudflare" and str(headers_subset.get("cf-mitigated") or "").lower() != "challenge":
            continue
        service = str(item.get("service") or "").strip()
        if not service:
            url = str(item.get("url") or item.get("source_url") or item.get("pdf_url") or "").strip()
            service = service_from_url(url) if url else ""
        if service:
            services.add(service)
    return services


def _route_summary(items: list[dict[str, Any]], *, limit: int = 28) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        reason = item.get("reason") or item.get("download_failure_reason") or item.get("rejected_reason") or item.get("error")
        status_code = item.get("status_code")
        if not (reason or status_code or item.get("message_zh")):
            continue
        if not (item.get("kind") or item.get("service") or item.get("url") or item.get("source_url")):
            continue
        row = {
            "service": item.get("service"),
            "kind": item.get("kind"),
            "reason": reason,
            "status_code": status_code,
            "content_type": item.get("content_type"),
            "url": item.get("url") or item.get("source_url"),
            "challenge_type": item.get("challenge_type"),
            "cf_mitigated": item.get("headers_subset", {}).get("cf-mitigated") if isinstance(item.get("headers_subset"), dict) else None,
            "message_zh": item.get("message_zh"),
        }
        key = tuple(row.get(key) for key in ["service", "kind", "reason", "status_code", "url"])
        if key in seen:
            continue
        seen.add(key)
        summary.append({key: value for key, value in row.items() if value not in (None, "", [])})
        if len(summary) >= limit:
            break
    return summary


def _route_is_openreview(item: dict[str, Any]) -> bool:
    return (
        item.get("service") == "openreview"
        or "openreview" in str(item.get("kind") or "").lower()
        or "openreview.net" in str(item.get("url") or item.get("source_url") or item.get("pdf_url") or "").lower()
    )


def _route_is_acm(item: dict[str, Any]) -> bool:
    return (
        item.get("service") == "acm"
        or "acm" in str(item.get("kind") or "").lower()
        or "dl.acm.org" in str(item.get("url") or item.get("source_url") or item.get("pdf_url") or "").lower()
    )


def _route_is_biorxiv(item: dict[str, Any]) -> bool:
    return (
        item.get("service") == "biorxiv"
        or "biorxiv" in str(item.get("kind") or "").lower()
        or "biorxiv.org" in str(item.get("url") or item.get("source_url") or item.get("pdf_url") or "").lower()
    )


def _route_is_science(item: dict[str, Any]) -> bool:
    return (
        item.get("service") == "science"
        or "science.org" in str(item.get("url") or item.get("source_url") or item.get("pdf_url") or "").lower()
        or str(item.get("kind") or "").lower().startswith("doi_direct_science")
    )


def _route_is_cvf(item: dict[str, Any]) -> bool:
    return "openaccess.thecvf.com" in str(item.get("url") or item.get("source_url") or item.get("pdf_url") or "").lower()


_COOLDOWN_DEFERRED_REASONS = {
    "openreview_service_cooldown_active",
    "service_cooldown_active",
    "skipped_due_to_active_challenge_cooldown",
    "skipped_due_to_openreview_service_cooldown",
    "skipped_due_to_prior_cloudflare_challenge",
    "skipped_due_to_prior_service_access_blocker",
}


def _route_failure_reason(item: dict[str, Any]) -> str:
    return str(
        item.get("reason")
        or item.get("download_failure_reason")
        or item.get("rejected_reason")
        or item.get("error")
        or ""
    ).strip()


def _route_is_cooldown_deferred(item: dict[str, Any]) -> bool:
    return _route_failure_reason(item) in _COOLDOWN_DEFERRED_REASONS


def _route_service(item: dict[str, Any]) -> str:
    service = str(item.get("service") or "").strip()
    if service:
        return service
    kind = str(item.get("kind") or "").lower()
    for known_service in ("openreview", "biorxiv", "arxiv", "science", "iclr", "icml", "acm"):
        if known_service in kind:
            return known_service
    url = str(item.get("url") or item.get("source_url") or item.get("pdf_url") or "").strip()
    if url.startswith("openreview://"):
        return "openreview"
    return service_from_url(url) if url else ""


def _pdf_request_was_made(item: dict[str, Any]) -> bool:
    reason = _route_failure_reason(item)
    download_receipt = item.get("download_receipt") if isinstance(item.get("download_receipt"), dict) else {}
    receipt_reason = _route_failure_reason(download_receipt)
    if reason in _COOLDOWN_DEFERRED_REASONS or reason in {
        "conference_presentation_pdf_not_article_body",
        "supplementary_material_pdf_not_article_body",
    } or receipt_reason in _COOLDOWN_DEFERRED_REASONS:
        return False
    return bool(
        item.get("downloaded") is True
        or item.get("status_code")
        or item.get("download_receipt")
        or item.get("downloaded") is False
    )


def _blocked_full_text_reason(
    paper: dict[str, Any],
    acquisition: dict[str, Any],
    html_attempt: dict[str, Any],
    pmc_xml_attempt: dict[str, Any],
    openalex_hints: dict[str, Any] | None = None,
    same_paper_html_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    title = str(paper.get("title") or "")
    urls = " ".join(str(paper.get(key) or "") for key in ["url", "abs_url", "html_url", "pdf_url"]).lower()
    attempts = acquisition.get("attempts") if isinstance(acquisition.get("attempts"), list) else []
    discovery = acquisition.get("candidate_discovery") if isinstance(acquisition.get("candidate_discovery"), list) else []
    html_attempts = html_attempt.get("attempts") if isinstance(html_attempt.get("attempts"), list) else []
    pmc_attempts = _flatten_route_items(pmc_xml_attempt)
    openalex_attempts = _flatten_route_items(openalex_hints or {})
    html_hint_attempts = _flatten_route_items(same_paper_html_hints or {})
    acquisition_route_items = _flatten_route_items(acquisition)
    all_route_items = [*acquisition_route_items, *discovery, *attempts, *html_attempts, *pmc_attempts, *openalex_attempts, *html_hint_attempts]
    statuses = [
        int(item.get("status_code") or 0)
        for item in all_route_items
        if isinstance(item, dict) and int(item.get("status_code") or 0) > 0
    ]
    pdf_attempt_count = sum(1 for item in attempts if isinstance(item, dict) and _pdf_request_was_made(item))
    cooldown_deferred_items = [
        item
        for item in all_route_items
        if isinstance(item, dict) and _route_is_cooldown_deferred(item)
    ]
    cooldown_services = sorted({service for item in cooldown_deferred_items for service in [_route_service(item)] if service})
    cooldown_remaining_sec = max(
        (float(item.get("cooldown_remaining_sec") or 0.0) for item in cooldown_deferred_items),
        default=0.0,
    )
    discovery_reasons = _route_summary(all_route_items)
    openreview_related = [
        item for item in all_route_items
        if isinstance(item, dict) and _route_is_openreview(item)
    ]
    acm_related = [
        item for item in all_route_items
        if isinstance(item, dict) and _route_is_acm(item)
    ]
    biorxiv_related = [
        item for item in all_route_items
        if isinstance(item, dict) and _route_is_biorxiv(item)
    ]
    science_related = [
        item for item in all_route_items
        if isinstance(item, dict) and _route_is_science(item)
    ]
    cvf_related = [
        item for item in all_route_items
        if isinstance(item, dict) and _route_is_cvf(item)
    ]
    openreview_reasons = [
        {
            "service": item.get("service"),
            "kind": item.get("kind"),
            "reason": item.get("reason") or item.get("download_failure_reason") or item.get("error"),
            "cooldown_reason": item.get("cooldown_reason"),
            "message_zh": item.get("message_zh"),
            "status_code": item.get("status_code"),
        }
        for item in openreview_related
        if item.get("reason") or item.get("download_failure_reason") or item.get("error") or item.get("cooldown_reason") or item.get("status_code") or item.get("message_zh")
    ][:12]
    if cooldown_deferred_items and not any(
        int(item.get("status_code") or 0) in {403, 429}
        or item.get("challenge_type") == "cloudflare"
        for item in all_route_items
        if isinstance(item, dict)
    ):
        return {
            "code": "deferred_service_cooldown_before_full_text_request",
            "message_zh": "全文来源仍处于共享访问冷却期，本轮未向这些 PDF/HTML 来源发起请求；这不是 PDF 不可读。系统将在冷却结束后把本论文重新入队一次。",
            "retryable_after_cooldown": True,
            "cooldown_services": cooldown_services,
            "cooldown_remaining_sec": round(cooldown_remaining_sec, 3),
            "cooldown_deferred_route_count": len(cooldown_deferred_items),
            "cooldown_origins": [
                {
                    key: value
                    for key, value in {
                        "service": _route_service(item),
                        "kind": item.get("kind"),
                        "reason": _route_failure_reason(item),
                        "cooldown_reason": item.get("cooldown_reason"),
                    }.items()
                    if value not in (None, "")
                }
                for item in cooldown_deferred_items[:12]
            ],
            "pdf_request_count": pdf_attempt_count,
            "failed_route_summary": discovery_reasons,
            "next_research_action": "等待共享冷却结束后由 Reading 批处理协调器自动重新入队；不要把未请求的候选诊断为 PDF 损坏。",
            "title": title,
        }
    if acm_related and any(int(item.get("status_code") or 0) == 403 or item.get("reason") == "http_403" or item.get("download_failure_reason") == "http_403" for item in acm_related):
        return {
            "code": "blocked_acm_official_pdf_403_no_verified_open_full_text",
            "message_zh": "ACM DL 官方 DOI/PDF/HTML 路线在当前网络返回 403；系统已继续尝试同篇 arXiv/OpenAlex/Unpaywall/Semantic Scholar/EuropePMC/PMC 等公开路线，仍未取得可验证论文正文。",
            "site_status_codes": sorted(set(statuses)),
            "failed_route_summary": discovery_reasons,
            "next_research_action": "降低 ACM 并发和频率后重试官方 PDF/HTML；若仍 403，需要使用可访问 ACM 的网络/机构权限，或只接受经标题/作者/DOI 验证的同篇 arXiv/开放 PDF，不能用其它论文补位。",
            "title": title,
        }
    if biorxiv_related and any(
        int(item.get("status_code") or 0) == 403
        or item.get("reason") in {
            "http_403",
            "biorxiv_cloudflare_challenge",
            "skipped_due_to_active_challenge_cooldown",
            "skipped_due_to_prior_cloudflare_challenge",
        }
        or item.get("download_failure_reason") in {
            "http_403",
            "biorxiv_cloudflare_challenge",
            "skipped_due_to_active_challenge_cooldown",
            "skipped_due_to_prior_cloudflare_challenge",
        }
        or (
            isinstance(item.get("headers_subset"), dict)
            and str(item["headers_subset"].get("cf-mitigated") or "").lower() == "challenge"
        )
        or item.get("challenge_type") == "cloudflare"
        for item in biorxiv_related
    ):
        return {
            "code": "blocked_biorxiv_official_challenge_no_verified_open_full_text",
            "message_zh": "bioRxiv 官方 PDF/HTML 在当前网络返回 403 或 Cloudflare challenge；系统已继续尝试 DOI、Crossref、Unpaywall、EuropePMC/PMC 等同篇开放路线，仍未取得可验证正文。",
            "site_status_codes": sorted(set(statuses)),
            "failed_route_summary": discovery_reasons,
            "next_research_action": "按 bioRxiv 独立限频桶低频重试官方 PDF/HTML；如果仍为 challenge，保持阻塞或在可正常访问 bioRxiv 的网络环境重跑，不能绕过 challenge 或用其它论文替代。",
            "title": title,
        }
    if science_related and any(
        int(item.get("status_code") or 0) == 403
        or item.get("reason") in {
            "http_403",
            "science_cloudflare_challenge",
            "skipped_due_to_active_challenge_cooldown",
            "skipped_due_to_prior_cloudflare_challenge",
        }
        or item.get("download_failure_reason") in {
            "http_403",
            "science_cloudflare_challenge",
            "skipped_due_to_active_challenge_cooldown",
            "skipped_due_to_prior_cloudflare_challenge",
        }
        or (
            isinstance(item.get("headers_subset"), dict)
            and str(item["headers_subset"].get("cf-mitigated") or "").lower() == "challenge"
        )
        or item.get("challenge_type") == "cloudflare"
        for item in science_related
    ):
        return {
            "code": "blocked_science_official_challenge_no_open_xml_or_pdf",
            "message_zh": "Science.org 官方 PDF/HTML 在当前网络返回 403 或 Cloudflare challenge；系统已继续尝试 DOI、Crossref/OpenAlex、Unpaywall、PubMed 和 EuropePMC/PMC 等同篇路线，仍未取得可验证正文。",
            "site_status_codes": sorted(set(statuses)),
            "failed_route_summary": discovery_reasons,
            "next_research_action": "按 Science.org 独立限频桶低频重试 article HTML/PDF；若仍为 challenge，保持阻塞或在可正常访问 Science.org 的网络环境重跑，不使用补充材料 PDF 或题录页代替正文。",
            "title": title,
        }
    if openreview_related and any(
        int(item.get("status_code") or 0) == 403
        or item.get("reason") in {
            "openreview_official_client_forbidden",
            "openreview_official_pdf_forbidden",
            "openreview_official_title_search_forbidden",
            "openreview_login_page_network_error",
            "openreview_login_page_challenge",
            "openreview_browser_login_failed",
        }
        or item.get("download_failure_reason") in {
            "openreview_official_client_forbidden",
            "openreview_official_pdf_forbidden",
            "openreview_official_title_search_forbidden",
            "openreview_login_page_network_error",
            "openreview_login_page_challenge",
            "openreview_browser_login_failed",
        }
        or any(
            marker in str(item.get("cooldown_reason") or "").lower()
            for marker in ["forbidden", "network_error", "challenge"]
        )
        or "challengerequired" in str(item.get("error") or "").lower()
        for item in openreview_related
    ):
        return {
            "code": "blocked_openreview_403_no_verified_open_full_text",
            "message_zh": "OpenReview 官方 client/API/PDF 或带凭据浏览器在当前网络遇到 403、challenge 或连接层阻断；系统已尝试同篇 arXiv/OpenAlex/Semantic Scholar/EuropePMC/PMC 等公开全文路线，未找到可验证正文。",
            "site_status_codes": sorted(set(statuses)),
            "openreview_reasons": openreview_reasons,
            "failed_route_summary": discovery_reasons,
            "next_research_action": "等待共享冷却结束后低频重试；若同一网络仍阻断 OpenReview，改用可正常访问 OpenReview 的网络，或只接受可通过标题/作者或 DOI 验证的同篇 arXiv、会议 proceedings、开放索引或作者主页 PDF。",
            "title": title,
        }
    if openreview_related and any(str(item.get("reason") or item.get("download_failure_reason") or "").startswith("missing_openreview") or item.get("reason") in {"anonymous_openreview_api_disabled", "anonymous_openreview_http_disabled"} for item in openreview_related):
        return {
            "code": "blocked_openreview_official_access_not_configured",
            "message_zh": "该论文需要 OpenReview 官方 PDF/附件路线，但当前未配置官方 openreview-py 凭据，或匿名 OpenReview 请求被显式禁用；系统已继续尝试 arXiv/OpenAlex/Semantic Scholar/EuropePMC/PMC 等同篇公开路线，仍未取得可验证全文。",
            "openreview_reasons": openreview_reasons,
            "failed_route_summary": discovery_reasons,
            "next_research_action": "配置 OPENREVIEW_USERNAME/OPENREVIEW_PASSWORD 并安装 openreview-py 后重试；若仍失败，查询该 venue 的官方 submissions invitation 或作者主页/PMLR/arXiv。",
            "title": title,
        }
    if cvf_related and any(item.get("error") == "SSLError" or item.get("reason") == "SSLError" for item in cvf_related):
        return {
            "code": "blocked_cvf_transient_ssl_no_verified_open_full_text",
            "message_zh": "CVF Open Access 官方 PDF/HTML 在当前网络多次出现 SSL EOF/连接中断；系统已保留同篇固定输入阻塞，未用其它论文补位。",
            "site_status_codes": sorted(set(statuses)),
            "failed_route_summary": discovery_reasons,
            "next_research_action": "稍后低频重试该 CVF 官方 PDF/HTML；若仍失败，可在可访问 CVF 的网络环境中重跑同一固定输入。",
            "title": title,
        }
    if any(item.get("reason") == "http_429_rate_limited" or int(item.get("status_code") or 0) == 429 for item in all_route_items if isinstance(item, dict)):
        return {
            "code": "blocked_rate_limited_before_same_paper_full_text",
            "message_zh": "官方或开放索引路线返回 429 限流；系统已停止继续高频请求并进入共享冷却，冷却结束后会将本论文重新入队一次。",
            "retryable_after_cooldown": True,
            "site_status_codes": sorted(set(statuses)),
            "failed_route_summary": discovery_reasons,
            "next_research_action": "先等待 Reading 批处理协调器完成冷却后自动重试；若仍限流，再检查共享出口上的其它请求。arXiv 至少 3 秒间隔，OpenReview 使用官方登录 client，OpenAlex 配置 API key。",
            "title": title,
        }
    if any(item.get("reason") == "missing_springer_nature_api_key" for item in all_route_items if isinstance(item, dict)):
        return {
            "code": "blocked_springer_nature_official_api_not_configured",
            "message_zh": "Nature/Springer 同篇官方开放全文 API 需要 API key；当前只尝试了公开 article PDF/HTML/开放索引，未取得可验证正文。",
            "failed_route_summary": discovery_reasons,
            "next_research_action": "配置 SPRINGER_API_KEY/SPRINGER_NATURE_API_KEY 后重试 Springer Nature Open Access/TDM 路线；同时保留 Nature article HTML/PDF 低频兜底。",
            "title": title,
        }
    if "openreview.net" in urls and 403 in statuses:
        return {
            "code": "blocked_openreview_403_no_verified_open_full_text",
            "message_zh": "OpenReview 官方页面/PDF/API 在当前网络返回 403；系统已尝试同篇 arXiv/OpenAlex/Semantic Scholar/EuropePMC/PMC 等公开全文路线，未找到可验证正文。",
            "site_status_codes": sorted(set(statuses)),
            "openreview_reasons": openreview_reasons,
            "failed_route_summary": discovery_reasons,
            "next_research_action": "停止匿名 OpenReview 直连；改用官方 openreview-py 登录 client、低频 venue 批量索引，或等待/查找 arXiv/PMLR/作者主页公开 PDF。",
            "title": title,
        }
    if "science.org" in urls and 403 in statuses:
        return {
            "code": "blocked_science_403_no_open_xml_or_pdf",
            "message_zh": "Science 正文/PDF 在当前网络返回 403；OpenAlex/Crossref/PubMed 只提供题录、摘要或补充材料，EuropePMC/PMC 未提供全文 XML，不能冒充精读正文。",
            "site_status_codes": sorted(set(statuses)),
            "failed_route_summary": discovery_reasons,
            "next_research_action": "优先使用 Science article HTML 正文；若必须 PDF，低频尝试 DOI PDF 并记录 403，不使用补充材料 PDF 代替正文。",
            "title": title,
        }
    if pdf_attempt_count == 0 and not html_attempts:
        return {
            "code": "blocked_no_same_paper_full_text_locator",
            "message_zh": "未找到同篇论文的可下载 PDF、正文 HTML 或全文 XML 定位信息。",
            "failed_route_summary": discovery_reasons,
            "next_research_action": "继续调研 DOI、OpenAlex、Semantic Scholar、Crossref、Unpaywall、EuropePMC/PMC、作者主页和会议 proceedings。",
            "title": title,
        }
    if pdf_attempt_count > 0:
        return {
            "code": "blocked_same_paper_pdf_unreadable_and_no_html_xml",
            "message_zh": "同篇论文 PDF 候选无法下载或无法抽取足够正文，HTML/XML 兜底也未取得正文。",
            "pdf_attempt_count": pdf_attempt_count,
            "failed_route_summary": discovery_reasons,
            "next_research_action": "检查 PDF 是否为补充材料、登录墙、Cloudflare 页面或扫描件；若是站点限制，低频重试官方 PDF、再转 HTML/XML/作者主页。",
            "title": title,
        }
    return {
        "code": "blocked_same_paper_full_text_unavailable",
        "message_zh": "同篇论文全文证据不可用；不能使用摘要、题录或其它论文替代。",
        "failed_route_summary": discovery_reasons,
        "next_research_action": "按 DOI、arXiv、OpenReview official、OpenAlex、Semantic Scholar、Crossref、Unpaywall、EuropePMC/PMC、作者主页顺序重新调研。",
        "title": title,
    }

def acquire_full_text(
    paper: dict[str, Any],
    run_path: Path,
    log: LogFn = print,
    *,
    services: AcquisitionServices | None = None,
) -> dict[str, Any]:
    downloads = run_path / "downloads"
    texts = run_path / "extracted"
    downloads.mkdir(parents=True, exist_ok=True)
    texts.mkdir(parents=True, exist_ok=True)
    started = time.time()
    science_html_first_text, science_html_first_attempt = _try_science_official_html_first(paper)
    if science_html_first_text:
        downloaded = False
        pdf_path = downloads / f"{safe_slug(paper.get('paper_id') or paper.get('title'), fallback='paper')}.pdf"
        resolved_pdf_url = ""
        acquisition = {
            "attempts": [],
            "selected": {},
            "skipped": "science_official_html_ready_before_pdf",
            "html_first": science_html_first_attempt,
        }
        text = science_html_first_text
        text_kind = "html"
        html_attempt: dict[str, Any] = science_html_first_attempt
    elif paper.get("skip_pdf_acquisition"):
        downloaded = False
        pdf_path = downloads / f"{safe_slug(paper.get('paper_id') or paper.get('title'), fallback='paper')}.pdf"
        resolved_pdf_url = ""
        acquisition = {"attempts": [], "selected": {}, "skipped": "skip_pdf_acquisition"}
        text = ""
        text_kind = "pdf"
        html_attempt = {}
    else:
        download_pdf = (services or {}).get("download_first_readable_pdf") or _download_first_readable_pdf
        downloaded, pdf_path, resolved_pdf_url, acquisition = download_pdf(paper, downloads, log)
        if science_html_first_attempt:
            acquisition["html_first"] = science_html_first_attempt
        text = _extract_pdf_text(pdf_path) if downloaded else ""
        text_kind = "pdf"
        html_attempt = {}
    pmc_xml_attempt: dict[str, Any] = {}
    if len(text) < MIN_FULL_TEXT_CHARS:
        openalex_provider = (services or {}).get("openalex_pdf_candidates")
        landing_page_provider = (services or {}).get("same_paper_landing_page_candidates")
        openalex_hints = (
            _openalex_full_text_hints(paper, openalex_provider)
            if openalex_provider
            else _openalex_full_text_hints(paper)
        )
        same_paper_html_hints = (
            _same_paper_html_hints(paper, landing_page_provider)
            if landing_page_provider
            else _same_paper_html_hints(paper)
        )
    else:
        openalex_hints = {"status": "skipped_pdf_text_ready"}
        same_paper_html_hints = {"status": "skipped_pdf_text_ready"}
    reader_pdf_attempt: dict[str, Any] = {}
    if len(text) < MIN_FULL_TEXT_CHARS:
        reader_text, reader_pdf_attempt = _reader_pdf_text_from_failed_pdf_attempts(paper, acquisition)
        if reader_text:
            text = reader_text
            text_kind = "html"
            html_attempt = reader_pdf_attempt
    biorxiv_reader_full_attempt: dict[str, Any] = {}
    if len(text) < MIN_FULL_TEXT_CHARS and _is_biorxiv_like_paper(paper):
        biorxiv_reader_text, biorxiv_reader_full_attempt = _fetch_biorxiv_reader_full_text(paper)
        if biorxiv_reader_text:
            text = biorxiv_reader_text
            text_kind = "html"
            html_attempt = biorxiv_reader_full_attempt
    runtime_cached_text_attempt: dict[str, Any] = {}
    if len(text) < MIN_FULL_TEXT_CHARS:
        cached_text, runtime_cached_text_attempt = _runtime_cached_full_text(paper)
        if cached_text:
            text = cached_text
            text_kind = "html"
            html_attempt = runtime_cached_text_attempt
    if len(text) < MIN_FULL_TEXT_CHARS:
        html_urls: list[str] = []
        challenged_services = _cloudflare_challenged_services(acquisition)

        def add_html_url(value: object) -> None:
            item = str(value or "").strip()
            lowered_item = item.lower()
            if not item or item in html_urls:
                return
            if "/virtual/" in lowered_item and ("/poster/" in lowered_item or "/oral/" in lowered_item):
                return
            html_urls.append(item)

        for value in [paper.get("html_url"), paper.get("url"), paper.get("abs_url")]:
            add_html_url(value)
        if isinstance(openalex_hints.get("hints"), list):
            for hint in openalex_hints.get("hints") or []:
                if isinstance(hint, dict):
                    add_html_url(hint.get("landing_page_url"))
        if isinstance(same_paper_html_hints.get("hints"), list):
            for hint in same_paper_html_hints.get("hints") or []:
                if isinstance(hint, dict):
                    add_html_url(hint.get("landing_page_url"))
        html_attempts: list[dict[str, Any]] = []
        attempted_html_urls: set[str] = set()
        if science_html_first_attempt:
            prior_attempts = science_html_first_attempt.get("attempts") if isinstance(science_html_first_attempt.get("attempts"), list) else []
            html_attempts.extend(prior_attempts)
            attempted_html_urls.update(str(item.get("url") or "") for item in prior_attempts if isinstance(item, dict))
        if reader_pdf_attempt:
            html_attempts.extend(reader_pdf_attempt.get("attempts") if isinstance(reader_pdf_attempt.get("attempts"), list) else [])
        if runtime_cached_text_attempt:
            html_attempts.extend(runtime_cached_text_attempt.get("attempts") if isinstance(runtime_cached_text_attempt.get("attempts"), list) else [])
        for html_url in html_urls[:10]:
            if html_url in attempted_html_urls:
                continue
            html_service = service_from_url(html_url)
            if html_service in challenged_services:
                html_attempts.append({
                    "accepted": False,
                    "url": html_url,
                    "service": html_service,
                    "reason": "skipped_due_to_prior_cloudflare_challenge",
                    "message_zh": "本篇前序请求已触发该服务 Cloudflare challenge；本轮跳过同服务 HTML 兜底，避免连续请求扩大封禁风险。",
                })
                continue
            html_text, attempt = _fetch_html_text(html_url)
            html_attempts.append(attempt)
            if attempt.get("accepted") and len(html_text) > len(text):
                text = html_text
                text_kind = "html"
            if len(text) >= MIN_FULL_TEXT_CHARS and _looks_like_paper_body(text):
                break
        html_attempt = {"attempts": html_attempts, "selected": next((item for item in html_attempts if item.get("accepted")), {})}
    if len(text) < MIN_FULL_TEXT_CHARS or (text_kind == "html" and not _looks_like_paper_body(text)):
        pmc_id = pmc_id_from_paper(paper, acquisition)
        if not pmc_id and isinstance(openalex_hints.get("hints"), list):
            for hint in openalex_hints.get("hints") or []:
                if isinstance(hint, dict) and hint.get("pmc_id"):
                    pmc_id = str(hint.get("pmc_id"))
                    break
        pmc_attempts: list[dict[str, Any]] = []
        pmc_ids: list[str] = []
        if pmc_id:
            pmc_ids.append(pmc_id)
        doi = doi_from_paper(paper)
        if _is_biorxiv_like_paper(paper):
            biorxiv_xml_text, biorxiv_xml_attempt = _fetch_biorxiv_jats_xml_text(paper)
            pmc_attempts.append(biorxiv_xml_attempt)
            if len(biorxiv_xml_text) > len(text):
                text = biorxiv_xml_text
                text_kind = "full_text_xml"
        europepmc_ids, europepmc_receipt = _pmc_xml_candidates_from_europepmc(doi, str(paper.get("title") or ""))
        pmc_attempts.append({"kind": "europepmc_search", **europepmc_receipt})
        for item in europepmc_ids:
            if item not in pmc_ids:
                pmc_ids.append(item)
        for next_pmc_id in pmc_ids[:5]:
            pmc_text, attempt = _fetch_pmc_xml_text(next_pmc_id)
            pmc_attempts.append({"kind": "europepmc_fullTextXML", **attempt})
            if len(pmc_text) > len(text):
                text = pmc_text
                text_kind = "full_text_xml"
            if len(text) >= MIN_FULL_TEXT_CHARS and _looks_like_paper_body(text):
                break
        pmc_xml_attempt = {"attempts": pmc_attempts, "selected": next((item for item in pmc_attempts if item.get("accepted")), {})}
    selected_html = html_attempt.get("selected") if isinstance(html_attempt.get("selected"), dict) else {}
    reader_pdf_selected = selected_html if selected_html.get("kind") == "reader_pdf_text" else {}
    runtime_cache_selected = selected_html if selected_html.get("kind") == "reading_runtime_cached_full_text" else {}
    text_path = texts / ("full_text.txt" if text_kind == "pdf" else "html_text.txt" if text_kind == "html" else "full_text_xml.txt")
    if text:
        write_text(text_path, text.rstrip() + "\n")
    html_body_ok = text_kind not in {"html", "full_text_xml"} or _looks_like_paper_body(text)
    full_text_available = len(text) >= MIN_FULL_TEXT_CHARS and html_body_ok
    blocked_full_text_reason = None if full_text_available else _blocked_full_text_reason(
        paper,
        acquisition,
        html_attempt,
        pmc_xml_attempt,
        openalex_hints,
        same_paper_html_hints,
    )
    if full_text_available:
        status = "pdf_text_read" if text_kind == "pdf" else "html_text_read" if text_kind == "html" else "full_text_read"
    elif blocked_full_text_reason.get("retryable_after_cooldown") is True:
        status = "deferred_service_cooldown_retry"
    elif text_kind == "html" and text:
        status = "html_metadata_or_abstract_only"
    elif downloaded:
        status = "pdf_text_too_short"
    elif html_attempt:
        status = "html_text_too_short_or_unavailable"
    else:
        status = "pdf_unavailable"
    packet = {
        "paper_id": paper.get("paper_id") or paper.get("id") or safe_slug(paper.get("title")),
        "title": paper.get("title") or "",
        "authors": paper.get("authors") or [],
        "url": paper.get("url") or paper.get("abs_url") or "",
        "pdf_url": resolved_pdf_url or reader_pdf_selected.get("pdf_url") or runtime_cache_selected.get("pdf_url") or paper.get("pdf_url") or "",
        "pdf_path": relative_to_reading(pdf_path) if downloaded else "",
        "text_path": relative_to_reading(text_path) if text else "",
        "pdf_downloaded": bool(downloaded),
        "text_kind": text_kind if text else "",
        "full_text_evidence_kind": "pdf" if downloaded and text_kind == "pdf" else "reader_pdf_text" if reader_pdf_selected else "runtime_cached_full_text" if runtime_cache_selected else "html" if text_kind == "html" else "xml" if text_kind == "full_text_xml" else "none",
        "true_pdf_full_text": bool(downloaded and text_kind == "pdf" and len(text) >= MIN_FULL_TEXT_CHARS),
        "non_pdf_full_text_note_zh": "" if downloaded and text_kind == "pdf" else ("已通过 reader 镜像提取公开 PDF 文本并通过正文 identity 检查；这是可精读正文文本，但不是本地下载的 PDF 文件。" if reader_pdf_selected else "已复用 Reading runtime 中已验证的同篇全文文本，并重新通过正文 identity 检查；这是 cache-assisted 正文，不是本次重新下载的 PDF。" if runtime_cache_selected else "已取得 HTML/XML 正文，可用于精读；但这不是论文 PDF。" if len(text) >= MIN_FULL_TEXT_CHARS else ""),
        "text_chars": len(text),
        "full_text_chars": len(text),
        "full_text_available": full_text_available,
        "full_text_status": status,
        "source": "reading_standalone_acquisition",
        "acquisition_seconds": round(time.time() - started, 3),
        "pdf_acquisition": acquisition,
        "html_acquisition": html_attempt,
        "biorxiv_reader_full_acquisition": biorxiv_reader_full_attempt,
        "pmc_xml_acquisition": pmc_xml_attempt,
        "openalex_full_text_hints": openalex_hints,
        "same_paper_html_hints": same_paper_html_hints,
        "same_paper_repair_policy": "PDF/HTML/XML full-text fallback may use DOI, publisher metadata, OpenAlex/Unpaywall, EuropePMC/PMC, OpenReview, or arXiv title-author verification only for the same paper; article replacement is forbidden.",
    }
    if blocked_full_text_reason is not None:
        packet["blocked_full_text_reason"] = blocked_full_text_reason
    return packet
