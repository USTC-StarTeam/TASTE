from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from common.io import coerce_str_list, safe_slug, write_text
from acquisition.semantic_scholar import semantic_scholar_enrich_paper
from pipeline.read_pipeline import _download_first_readable_pdf, _extract_pdf_text


LogFn = Callable[[str], None]

PMC_ID_RE = re.compile(r"\b(PMC\d{5,})\b", re.I)


ARXIV_ID_RE = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)?([0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?",
    re.I,
)
READ_USER_AGENT = "TASTE-reading-standalone/1.0"
MIN_FULL_TEXT_CHARS = 1200


def arxiv_id_from_text(value: Any) -> str:
    match = ARXIV_ID_RE.search(str(value or ""))
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


def _atom_text(node: ET.Element, path: str, ns: dict[str, str]) -> str:
    return " ".join((node.findtext(path, default="", namespaces=ns) or "").split())


def fetch_arxiv_metadata(arxiv_id: str) -> dict[str, Any]:
    if not arxiv_id:
        return {}
    url = "https://export.arxiv.org/api/query?id_list=" + quote_plus(arxiv_id)
    try:
        response = requests.get(url, timeout=30, headers={"User-Agent": READ_USER_AGENT})
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
    record, semantic_receipt = semantic_scholar_enrich_paper(record)
    if semantic_receipt.get("status") != "skipped_disabled":
        record["semantic_scholar_acquisition"] = semantic_receipt
    return record




def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for node in soup(["script", "style", "noscript", "nav", "footer", "header", "aside"]):
        node.decompose()
    candidates = []
    for selector in ["article", "main", "div.c-article-body", "section.article__body", "div.article__body", "body"]:
        node = soup.select_one(selector)
        if node is not None:
            text = "\n".join(part.strip() for part in node.get_text("\n", strip=True).splitlines() if part.strip())
            if len(text) > 500:
                candidates.append(text)
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
    return len(value) >= 8000 or marker_count >= 2



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
        response = requests.get(url, timeout=timeout, headers={"User-Agent": READ_USER_AGENT, "Accept": "application/xml,text/xml,*/*"})
    except Exception as exc:
        return "", {"accepted": False, "url": url, "error": exc.__class__.__name__, "pmc_id": pmc_id}
    content_type = str(response.headers.get("content-type") or "").lower()
    if response.status_code != 200:
        return "", {"accepted": False, "url": url, "status_code": response.status_code, "content_type": content_type, "pmc_id": pmc_id}
    text = _xml_to_text(response.text)
    return text, {"accepted": len(text) >= MIN_FULL_TEXT_CHARS and _looks_like_paper_body(text), "url": url, "status_code": response.status_code, "content_type": content_type, "text_chars": len(text), "pmc_id": pmc_id, "source": "europepmc_fullTextXML"}

def _fetch_html_text(url: str, timeout: int = 30) -> tuple[str, dict[str, Any]]:
    if not url or not str(url).startswith("http"):
        return "", {"accepted": False, "reason": "missing_html_url"}
    lowered_url = str(url).lower()
    if "/virtual/" in lowered_url and "/poster/" in lowered_url:
        return "", {"accepted": False, "url": url, "reason": "conference_poster_page_is_not_paper_full_text"}
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": READ_USER_AGENT})
    except Exception as exc:
        return "", {"accepted": False, "url": url, "error": exc.__class__.__name__}
    content_type = str(response.headers.get("content-type") or "").lower()
    if response.status_code != 200:
        return "", {"accepted": False, "url": url, "status_code": response.status_code, "content_type": content_type}
    if "html" not in content_type and not response.text.lstrip().lower().startswith("<!doctype") and "<html" not in response.text[:500].lower():
        return "", {"accepted": False, "url": url, "status_code": response.status_code, "content_type": content_type, "reason": "not_html"}
    text = _html_to_text(response.text)
    return text, {"accepted": len(text) >= MIN_FULL_TEXT_CHARS, "url": url, "status_code": response.status_code, "content_type": content_type, "text_chars": len(text)}

def acquire_full_text(paper: dict[str, Any], run_path: Path, log: LogFn = print) -> dict[str, Any]:
    downloads = run_path / "downloads"
    texts = run_path / "extracted"
    downloads.mkdir(parents=True, exist_ok=True)
    texts.mkdir(parents=True, exist_ok=True)
    started = time.time()
    if paper.get("skip_pdf_acquisition"):
        downloaded = False
        pdf_path = downloads / f"{safe_slug(paper.get('paper_id') or paper.get('title'), fallback='paper')}.pdf"
        resolved_pdf_url = ""
        acquisition = {"attempts": [], "selected": {}, "skipped": "skip_pdf_acquisition"}
    else:
        downloaded, pdf_path, resolved_pdf_url, acquisition = _download_first_readable_pdf(paper, downloads, log)
    text = _extract_pdf_text(pdf_path) if downloaded else ""
    text_kind = "pdf"
    html_attempt: dict[str, Any] = {}
    pmc_xml_attempt: dict[str, Any] = {}
    if len(text) < MIN_FULL_TEXT_CHARS:
        html_url = str(paper.get("html_url") or paper.get("url") or paper.get("abs_url") or "").strip()
        html_text, html_attempt = _fetch_html_text(html_url)
        if len(html_text) > len(text):
            text = html_text
            text_kind = "html"
    if len(text) < MIN_FULL_TEXT_CHARS or (text_kind == "html" and not _looks_like_paper_body(text)):
        pmc_id = pmc_id_from_paper(paper, acquisition)
        pmc_text, pmc_xml_attempt = _fetch_pmc_xml_text(pmc_id)
        if len(pmc_text) > len(text):
            text = pmc_text
            text_kind = "full_text_xml"
    text_path = texts / ("full_text.txt" if text_kind == "pdf" else "html_text.txt" if text_kind == "html" else "full_text_xml.txt")
    if text:
        write_text(text_path, text.rstrip() + "\n")
    html_body_ok = text_kind not in {"html", "full_text_xml"} or _looks_like_paper_body(text)
    if len(text) >= MIN_FULL_TEXT_CHARS and html_body_ok:
        status = "pdf_text_read" if text_kind == "pdf" else "html_text_read" if text_kind == "html" else "full_text_read"
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
        "pdf_url": resolved_pdf_url or paper.get("pdf_url") or "",
        "pdf_path": str(pdf_path) if downloaded else "",
        "text_path": str(text_path) if text else "",
        "pdf_downloaded": bool(downloaded),
        "text_kind": text_kind if text else "",
        "text_chars": len(text),
        "full_text_chars": len(text),
        "full_text_available": len(text) >= MIN_FULL_TEXT_CHARS and html_body_ok,
        "full_text_status": status,
        "source": "reading_standalone_acquisition",
        "acquisition_seconds": round(time.time() - started, 3),
        "pdf_acquisition": acquisition,
        "html_acquisition": html_attempt,
        "pmc_xml_acquisition": pmc_xml_attempt,
    }
    return packet
