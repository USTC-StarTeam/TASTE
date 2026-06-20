from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import quote, quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from acquisition.paper_sources import MIN_FULL_TEXT_CHARS, acquire_full_text, build_paper_record
from common.io import safe_slug, write_json, write_text
from common.paths import READING_ROOT, WORKSPACE_ROOT, ensure_inside_reading
from orchestration.claude_subagent import build_deep_read_prompt, run_claude_deep_read

USER_AGENT = "TASTE-reading-channel-batch-test/1.0"
DEFAULT_CHANNELS = [
    "nips2025",
    "iclr2026",
    "icml2026",
    "sigkdd2026",
    "arxiv",
    "biorxiv",
    "nature",
    "science_family",
]

LogFn = Callable[[str], None]


def now_compact() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def get_url(url: str, timeout: int = 30) -> tuple[requests.Response | None, dict[str, Any]]:
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        return response, {
            "url": url,
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "bytes": len(response.content),
        }
    except Exception as exc:
        return None, {"url": url, "error": exc.__class__.__name__, "message": str(exc)[:300]}


def clean_text(value: Any, max_len: int = 5000) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max_len].rstrip()


def unique_papers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        title = clean_text(row.get("title"), 500)
        key = str(row.get("paper_id") or row.get("doi") or row.get("url") or title).lower()
        if not title or key in seen:
            continue
        seen.add(key)
        row["title"] = title
        out.append(row)
    return out



def strip_markup(value: Any, max_len: int = 5000) -> str:
    raw = str(value or "")
    if "<" in raw and ">" in raw:
        raw = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return clean_text(raw, max_len)


def _openalex_abstract(work: dict[str, Any]) -> str:
    index = work.get("abstract_inverted_index")
    if not isinstance(index, dict):
        return ""
    positioned: list[tuple[int, str]] = []
    for word, positions in index.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int):
                positioned.append((pos, str(word)))
    return clean_text(" ".join(word for _, word in sorted(positioned)), 3000)


def _openalex_json(url: str, timeout: int = 45) -> tuple[dict[str, Any], dict[str, Any]]:
    response, receipt = get_url(url, timeout=timeout)
    if not response or response.status_code != 200:
        return {}, receipt
    try:
        payload = response.json()
    except Exception as exc:
        receipt = {**receipt, "error": exc.__class__.__name__, "message": str(exc)[:300]}
        return {}, receipt
    return payload if isinstance(payload, dict) else {}, receipt


def _openalex_work_by_doi(doi: str) -> tuple[dict[str, Any], dict[str, Any]]:
    cleaned = str(doi or "").strip()
    if not cleaned:
        return {}, {"error": "missing_doi"}
    url = "https://api.openalex.org/works/https://doi.org/" + quote(cleaned, safe="")
    return _openalex_json(url)


def _openalex_pdf_location(work: dict[str, Any]) -> tuple[str, str, str]:
    locations: list[dict[str, Any]] = []
    best = work.get("best_oa_location")
    if isinstance(best, dict):
        locations.append(best)
    for location in work.get("locations") or []:
        if isinstance(location, dict):
            locations.append(location)
    for location in locations:
        pdf_url = str(location.get("pdf_url") or "").strip()
        if not pdf_url:
            continue
        landing = str(location.get("landing_page_url") or work.get("doi") or "").strip()
        raw_source = str(location.get("raw_source_name") or "").strip()
        source = location.get("source") if isinstance(location.get("source"), dict) else {}
        source_name = raw_source or str(source.get("display_name") or "").strip()
        return pdf_url, landing, source_name
    return "", str(work.get("doi") or "").strip(), ""


def _title_is_research_article(title: str) -> bool:
    lowered = strip_markup(title, 500).lower()
    if len(lowered) < 12:
        return False
    blocked_prefixes = (
        "erratum", "correction", "corrigendum", "editorial", "retraction",
        "publisher correction", "expression of concern", "comment on",
        "reply to", "summer reading", "in other journals",
    )
    return not lowered.startswith(blocked_prefixes)



def _openalex_arxiv_pdf_location(work: dict[str, Any]) -> tuple[str, str, str]:
    locations: list[dict[str, Any]] = []
    best = work.get("best_oa_location")
    if isinstance(best, dict):
        locations.append(best)
    for location in work.get("locations") or []:
        if isinstance(location, dict):
            locations.append(location)
    for location in locations:
        pdf_url = str(location.get("pdf_url") or "").strip()
        landing = str(location.get("landing_page_url") or "").strip()
        if "arxiv.org" in pdf_url.lower() or "arxiv.org" in landing.lower():
            return pdf_url or landing.replace("/abs/", "/pdf/"), landing, "ArXiv.org"
    return _openalex_pdf_location(work)

def atom_entries(response_text: str) -> list[ET.Element]:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(response_text.encode("utf-8") if isinstance(response_text, str) else response_text)
    return root.findall("a:entry", ns)


def atom_text(entry: ET.Element, path: str) -> str:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    return clean_text(entry.findtext(path, default="", namespaces=ns), 5000)



def crawl_arxiv_openalex_fallback(limit: int, reason: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    url = (
        "https://api.openalex.org/works?"
        "filter=from_publication_date:2026-01-01,has_pdf_url:true,primary_location.source.id:S4393918464"
        f"&search={quote('machine learning')}&per-page={min(max(limit, 20), 100)}"
    )
    payload, receipt = _openalex_json(url, timeout=45)
    receipts.append({"kind": "openalex_arxiv_fallback", **receipt})
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = strip_markup(item.get("display_name"), 500)
        if not _title_is_research_article(title):
            continue
        pdf_url, landing_url, source_name = _openalex_arxiv_pdf_location(item)
        if not pdf_url:
            continue
        paper_id = str(item.get("doi") or item.get("id") or safe_slug(title)).replace("https://doi.org/", "")
        rows.append({
            "source": "arxiv",
            "paper_id": paper_id,
            "title": title,
            "authors": [],
            "abstract": _openalex_abstract(item),
            "url": landing_url or pdf_url,
            "abs_url": landing_url if "/abs/" in landing_url else "",
            "pdf_url": pdf_url,
            "published_journal": source_name or "ArXiv.org",
            "source_note_zh": "arXiv 官方 API 限流或不可用时，使用 OpenAlex 的 ArXiv 源开放 PDF 兜底。",
        })
        if len(rows) >= limit:
            break
    return unique_papers(rows), {"source_url": url, "status": "ok" if rows else "blocked_no_candidates", "primary_arxiv_failure": reason, "fetches": receipts, "candidate_count": len(rows)}


def crawl_arxiv(limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    url = "https://export.arxiv.org/api/query?search_query=cat:cs.LG&start=0&max_results=" + str(limit) + "&sortBy=submittedDate&sortOrder=descending"
    response, receipt = get_url(url, timeout=45)
    rows: list[dict[str, Any]] = []
    if not response or response.status_code != 200:
        return crawl_arxiv_openalex_fallback(limit, {"source_url": url, "fetch": receipt, "status": "blocked_fetch_failed"})
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(response.content)
    except Exception as exc:
        return crawl_arxiv_openalex_fallback(limit, {"source_url": url, "fetch": receipt, "status": "blocked_parse_failed", "error": exc.__class__.__name__})
    for entry in root.findall("a:entry", ns):
        entry_id = atom_text(entry, "a:id")
        pdf_url = ""
        for link in entry.findall("a:link", ns):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href", "")
                break
        if not pdf_url and "/abs/" in entry_id:
            pdf_url = entry_id.replace("/abs/", "/pdf/")
        rows.append({
            "source": "arxiv",
            "paper_id": entry_id.rsplit("/", 1)[-1] if entry_id else "",
            "title": atom_text(entry, "a:title"),
            "authors": [atom_text(author, "a:name") for author in entry.findall("a:author", ns)],
            "abstract": atom_text(entry, "a:summary"),
            "url": entry_id,
            "abs_url": entry_id,
            "pdf_url": pdf_url,
            "published": atom_text(entry, "a:published"),
        })
    rows = unique_papers(rows)
    if not rows:
        return crawl_arxiv_openalex_fallback(limit, {"source_url": url, "fetch": receipt, "status": "blocked_no_primary_candidates"})
    return rows, {"source_url": url, "fetch": receipt, "status": "ok", "candidate_count": len(rows)}

def crawl_biorxiv(limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    end = dt.datetime.now(dt.timezone.utc).date().isoformat()
    start = "2026-01-01"
    target_rows = min(max(limit, 30), 60)
    cursor = 0
    scanned = 0
    scan_cap = max(target_rows * 10, 120)
    while len(rows) < target_rows and scanned < scan_cap:
        url = f"https://api.biorxiv.org/pubs/biorxiv/{start}/{end}/{cursor}"
        payload, receipt = _openalex_json(url, timeout=45)
        receipts.append(receipt)
        collection = payload.get("collection") if isinstance(payload, dict) else []
        if not isinstance(collection, list) or not collection:
            break
        for item in collection:
            scanned += 1
            if not isinstance(item, dict):
                continue
            preprint_doi = str(item.get("preprint_doi") or item.get("biorxiv_doi") or "").strip()
            published_doi = str(item.get("published_doi") or "").strip()
            title = strip_markup(item.get("preprint_title"), 500)
            if not preprint_doi or not published_doi or not _title_is_research_article(title):
                continue
            work, work_receipt = _openalex_work_by_doi(published_doi)
            receipts.append({"kind": "openalex_published_version", **work_receipt})
            pdf_url, landing_url, source_name = _openalex_pdf_location(work)
            if not pdf_url:
                continue
            rows.append({
                "source": "biorxiv",
                "paper_id": preprint_doi,
                "doi": preprint_doi,
                "published_doi": published_doi,
                "title": title,
                "authors": item.get("preprint_authors") or "",
                "abstract": clean_text(item.get("preprint_abstract") or _openalex_abstract(work), 3000),
                "url": landing_url or f"https://doi.org/{published_doi}",
                "html_url": landing_url or f"https://doi.org/{published_doi}",
                "pdf_url": pdf_url,
                "published_journal": item.get("published_journal") or source_name,
                "source_note_zh": "候选来自 bioRxiv 官方 published mapping；bioRxiv 直连 PDF/HTML 在当前环境受 Cloudflare 403 限制时，使用对应已发表公开全文作为精读材料。",
                "published": item.get("published_date") or "",
            })
            if len(rows) >= target_rows:
                break
        cursor += len(collection)
    return unique_papers(rows), {
        "status": "ok" if rows else "blocked_no_public_full_text_candidates",
        "fetches": receipts,
        "candidate_count": len(rows),
        "scanned_mapping_count": scanned,
        "note_zh": "bioRxiv API 只稳定提供元数据；直连 preprint PDF/HTML 当前环境 403。本 crawler 使用 bioRxiv 官方 published mapping 找对应已发表开放全文，不把摘要当全文。",
    }

def crawl_nature(limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    url = "https://www.nature.com/search?article_type=research&order=date_desc"
    response, receipt = get_url(url, timeout=30)
    if not response or response.status_code != 200:
        return [], {"source_url": url, "fetch": receipt, "status": "blocked_fetch_failed"}
    soup = BeautifulSoup(response.text, "html.parser")
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href") or ""
        title = clean_text(anchor.get_text(" ", strip=True), 500)
        if "/articles/" not in href or len(title) < 20:
            continue
        absolute = urljoin("https://www.nature.com", href)
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append((title, absolute))
        if len(links) >= limit:
            break
    rows: list[dict[str, Any]] = []
    for title, article_url in links:
        rows.append({
            "source": "nature",
            "paper_id": safe_slug(article_url.rsplit("/", 1)[-1]),
            "title": title,
            "authors": [],
            "abstract": "",
            "url": article_url,
            "html_url": article_url,
            "pdf_url": article_url.rstrip("/") + "_reference.pdf",
            "skip_pdf_acquisition": False,
        })
    return unique_papers(rows), {"source_url": url, "fetch": receipt, "status": "ok", "candidate_count": len(rows), "note_zh": "Nature 搜索页只用于候选列表；正文在单篇处理阶段直接从 article HTML 抽取。"}


def crawl_neurips2025(limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    url = "https://proceedings.neurips.cc/paper_files/paper/2025"
    response, receipt = get_url(url, timeout=45)
    if not response or response.status_code != 200:
        return [], {"source_url": url, "fetch": receipt, "status": "blocked_fetch_failed"}
    soup = BeautifulSoup(response.text, "html.parser")
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href") or ""
        title = clean_text(anchor.get_text(" ", strip=True), 500)
        if "/paper_files/paper/2025/hash/" not in href or "Abstract" not in href or len(title) < 10:
            continue
        absolute = urljoin("https://proceedings.neurips.cc", href)
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append((title, absolute))
        if len(links) >= limit:
            break
    rows: list[dict[str, Any]] = []
    detail_receipts: list[dict[str, Any]] = []
    for title, abstract_url in links:
        detail, detail_receipt = get_url(abstract_url, timeout=30)
        detail_receipts.append(detail_receipt)
        authors: list[str] = []
        abstract = ""
        pdf_url = ""
        if detail and detail.status_code == 200:
            dsoup = BeautifulSoup(detail.text, "html.parser")
            meta_title = dsoup.find("meta", attrs={"name": "citation_title"})
            if meta_title and meta_title.get("content"):
                title = clean_text(meta_title.get("content"), 500)
            authors = [m.get("content", "") for m in dsoup.find_all("meta", attrs={"name": "citation_author"})]
            meta_abs = dsoup.find("meta", attrs={"name": "description"})
            abstract = clean_text(meta_abs.get("content") if meta_abs else "", 2000)
            for anchor in dsoup.find_all("a", href=True):
                if clean_text(anchor.get_text(" ", strip=True)).lower() == "paper" or anchor.get("href", "").lower().endswith(".pdf"):
                    pdf_url = urljoin("https://proceedings.neurips.cc", anchor.get("href"))
                    break
        rows.append({
            "source": "nips2025",
            "paper_id": safe_slug(abstract_url.rsplit("/", 1)[-1].replace("-Abstract-", "-")),
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "url": abstract_url,
            "pdf_url": pdf_url,
        })
    return unique_papers(rows), {"source_url": url, "fetch": receipt, "detail_fetches": detail_receipts, "status": "ok", "candidate_count": len(rows)}


def poster_abstract_and_links(url: str) -> tuple[str, list[str], dict[str, Any]]:
    response, receipt = get_url(url, timeout=30)
    if not response or response.status_code != 200:
        return "", [], receipt
    soup = BeautifulSoup(response.text, "html.parser")
    abstract = ""
    for selector in [".abstract-text-inner", ".abstract-content", "#abstractText", ".schedule-abstract"]:
        node = soup.select_one(selector)
        if node:
            abstract = clean_text(node.get_text(" ", strip=True), 2500)
            if abstract:
                break
    if not abstract:
        meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        abstract = clean_text(meta.get("content") if meta else "", 2500)
    links = [a.get("href") or "" for a in soup.find_all("a", href=True)]
    return abstract, links, receipt



def crawl_virtual(channel: str, base_url: str, page_url: str, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    response, receipt = get_url(page_url, timeout=45)
    if not response or response.status_code != 200:
        return [], {"source_url": page_url, "fetch": receipt, "status": "blocked_fetch_failed"}
    soup = BeautifulSoup(response.text, "html.parser")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href") or ""
        title = clean_text(anchor.get_text(" ", strip=True), 500)
        if "/virtual/" not in href or "/poster/" not in href or len(title) < 15:
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        rows.append({
            "source": channel,
            "paper_id": safe_slug(absolute.rsplit("/", 1)[-1]),
            "title": title,
            "authors": [],
            "abstract": "",
            "url": absolute,
            "pdf_url": "",
            "source_note_zh": "会议 virtual poster 页只作为候选索引；poster/slide 不算论文全文，全文获取交给 arXiv 标题验证等公开 PDF 候选。",
        })
        if len(rows) >= limit:
            break
    return unique_papers(rows), {"source_url": page_url, "fetch": receipt, "status": "ok", "candidate_count": len(rows), "note_zh": "为避免 OpenReview 403 和 poster 摘要页误判，ICLR/ICML 候选阶段只抓标题与 poster URL，不把 poster 页面当全文。"}


def crawl_iclr2026(limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    query = '"Published as a conference paper at ICLR 2026"'
    per_page = min(max(limit * 3, 50), 100)
    url = (
        "https://api.openalex.org/works?"
        "filter=from_publication_date:2025-01-01,has_pdf_url:true"
        f"&search={quote(query)}&per-page={per_page}"
    )
    payload, receipt = _openalex_json(url, timeout=45)
    receipts.append({"kind": "openalex_iclr2026_published_phrase", "query": query, **receipt})
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = strip_markup(item.get("display_name"), 500)
        if not _title_is_research_article(title):
            continue
        pdf_url, landing_url, source_name = _openalex_pdf_location(item)
        if not pdf_url or "biorxiv.org" in pdf_url.lower() or "openreview.net" in pdf_url.lower():
            continue
        doi_url = str(item.get("doi") or "").strip()
        paper_id = doi_url.replace("https://doi.org/", "") or str(item.get("id") or safe_slug(title))
        rows.append({
            "source": "iclr2026",
            "paper_id": paper_id,
            "doi": doi_url.replace("https://doi.org/", "") if doi_url else "",
            "title": title,
            "authors": [],
            "abstract": _openalex_abstract(item),
            "url": landing_url or doi_url or pdf_url,
            "html_url": landing_url or doi_url or "",
            "pdf_url": pdf_url,
            "published_journal": source_name or "ICLR 2026",
            "source_note_zh": "候选来自 OpenAlex 对 arXiv/开放全文元数据的检索，匹配短语为 Published as a conference paper at ICLR 2026；OpenReview 直连 403 时优先使用这些开放 PDF。",
        })
        if len(rows) >= limit:
            break
    if len(rows) < limit:
        virtual_rows, virtual_receipt = crawl_virtual("iclr2026", "https://iclr.cc", "https://iclr.cc/virtual/2026/papers.html", limit - len(rows))
        receipts.append({"kind": "iclr_virtual_fallback", **virtual_receipt})
        rows.extend(virtual_rows)
    rows = unique_papers(rows)
    return rows, {"status": "ok" if rows else "blocked_no_candidates", "fetches": receipts, "candidate_count": len(rows), "note_zh": "ICLR2026 优先用 OpenAlex/arXiv 的会议论文公开 PDF，virtual 页面只作兜底索引；OpenReview PDF 当前环境 403。"}



def crawl_icml2026(limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    query = '"Forty-third International Conference on Machine Learning"'
    per_page = min(max(limit * 3, 50), 100)
    url = (
        "https://api.openalex.org/works?"
        "filter=from_publication_date:2025-01-01,has_pdf_url:true"
        f"&search={quote(query)}&per-page={per_page}"
    )
    payload, receipt = _openalex_json(url, timeout=45)
    receipts.append({"kind": "openalex_icml2026_conference_name", "query": query, **receipt})
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = strip_markup(item.get("display_name"), 500)
        if not _title_is_research_article(title):
            continue
        pdf_url, landing_url, source_name = _openalex_pdf_location(item)
        if not pdf_url or "biorxiv.org" in pdf_url.lower() or "openreview.net" in pdf_url.lower():
            continue
        doi_url = str(item.get("doi") or "").strip()
        paper_id = doi_url.replace("https://doi.org/", "") or str(item.get("id") or safe_slug(title))
        rows.append({
            "source": "icml2026",
            "paper_id": paper_id,
            "doi": doi_url.replace("https://doi.org/", "") if doi_url else "",
            "title": title,
            "authors": [],
            "abstract": _openalex_abstract(item),
            "url": landing_url or doi_url or pdf_url,
            "html_url": landing_url or doi_url or "",
            "pdf_url": pdf_url,
            "published_journal": source_name or "ICML 2026",
            "source_note_zh": "候选来自 OpenAlex 对 ICML 2026 正式会议名的检索；virtual 页面仅作兜底索引。",
        })
        if len(rows) >= limit:
            break
    if len(rows) < limit:
        virtual_rows, virtual_receipt = crawl_virtual("icml2026", "https://icml.cc", "https://icml.cc/virtual/2026/papers.html", limit - len(rows))
        receipts.append({"kind": "icml_virtual_fallback", **virtual_receipt})
        rows.extend(virtual_rows)
    rows = unique_papers(rows)
    return rows, {"status": "ok" if rows else "blocked_no_candidates", "fetches": receipts, "candidate_count": len(rows), "note_zh": "ICML2026 优先使用 OpenAlex/arXiv 的会议论文开放 PDF，virtual 页面只作兜底索引。"}



def crawl_sigkdd2026(limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    probe_urls = [
        "https://kdd2026.kdd.org/",
        "https://kdd2026.kdd.org/research-track-call-for-papers/",
        "https://dl.acm.org/doi/proceedings/10.1145/3770854",
    ]
    receipts: list[dict[str, Any]] = []
    for url in probe_urls:
        _, receipt = get_url(url, timeout=30)
        receipts.append(receipt)
    rows: list[dict[str, Any]] = []
    prefixes = ["10.1145/3770854", "10.1145/3770855"]
    per_page = min(max(limit * 2, 25), 100)
    for prefix in prefixes:
        url = f"https://api.openalex.org/works?filter=from_publication_date:2026-01-01,doi_starts_with:{quote(prefix)}&per-page={per_page}"
        payload, receipt = _openalex_json(url, timeout=45)
        receipts.append({"kind": "openalex_kdd_doi_prefix", "doi_prefix": prefix, **receipt})
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            title = strip_markup(item.get("display_name"), 500)
            doi_url = str(item.get("doi") or "").strip()
            doi = doi_url.replace("https://doi.org/", "")
            if not doi.startswith(prefix + ".") or not _title_is_research_article(title):
                continue
            pdf_url, landing_url, source_name = _openalex_pdf_location(item)
            if not pdf_url:
                continue
            rows.append({
                "source": "sigkdd2026",
                "paper_id": doi,
                "doi": doi,
                "title": title,
                "authors": [],
                "abstract": _openalex_abstract(item),
                "url": landing_url or doi_url,
                "html_url": landing_url or doi_url,
                "pdf_url": pdf_url,
                "published_journal": source_name or "Proceedings of the 32nd ACM SIGKDD Conference on Knowledge Discovery and Data Mining",
                "source_note_zh": "候选来自 OpenAlex 中 ACM KDD 2026 proceedings DOI 前缀；ACM DL 页面在当前环境 403 时使用 OpenAlex 指向的开放 PDF。",
            })
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    status = "ok" if rows else "blocked_no_public_paper_list"
    return unique_papers(rows)[:limit], {"status": status, "probe_urls": probe_urls, "fetches": receipts, "candidate_count": len(rows), "note_zh": "SIGKDD 2026 官网未提供 accepted 列表；ACM proceedings 与 OpenAlex DOI 前缀提供单篇候选和开放 PDF。"}

def _rss_items(response_text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    try:
        root = ET.fromstring(response_text.encode("utf-8"))
    except Exception:
        return rows
    for item in root.findall(".//{http://purl.org/rss/1.0/}item") + root.findall(".//item"):
        title = clean_text(item.findtext("{http://purl.org/rss/1.0/}title") or item.findtext("title") or "", 500)
        link = clean_text(item.findtext("{http://purl.org/rss/1.0/}link") or item.findtext("link") or "", 1000)
        desc = clean_text(item.findtext("{http://purl.org/rss/1.0/}description") or item.findtext("description") or "", 2000)
        if title and link:
            rows.append({"title": title, "link": link, "description": re.sub(r"<[^>]+>", " ", desc)})
    return rows




def crawl_science_family(limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    journals = [
        ("science_advances", "2375-2548", "sciadv", "10.1126/sciadv"),
        ("science_robotics", "2470-9476", "scirobotics", "10.1126/scirobotics"),
        ("science", "0036-8075", "science", "10.1126/science"),
    ]
    rows_per_query = min(max(limit * 4, 60), 100)

    for journal, _issn, _doi_prefix, openalex_prefix in journals:
        url = f"https://api.openalex.org/works?filter=from_publication_date:2026-01-01,doi_starts_with:{quote(openalex_prefix)}&per-page={rows_per_query}"
        payload, receipt = _openalex_json(url, timeout=45)
        receipts.append({"kind": "openalex_science_family", "journal": journal, "doi_prefix": openalex_prefix, **receipt})
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            title = strip_markup(item.get("display_name"), 500)
            doi_url = str(item.get("doi") or "").strip()
            doi = doi_url.replace("https://doi.org/", "")
            if not doi.startswith(openalex_prefix + ".") or not _title_is_research_article(title):
                continue
            pdf_url, landing_url, source_name = _openalex_pdf_location(item)
            if not pdf_url:
                continue
            rows.append({
                "source": "science_family",
                "paper_id": doi,
                "doi": doi,
                "title": title,
                "authors": [],
                "abstract": _openalex_abstract(item),
                "url": landing_url or doi_url or f"https://www.science.org/doi/{doi}",
                "html_url": f"https://www.science.org/doi/full/{doi}",
                "pdf_url": pdf_url,
                "journal": journal,
                "published_journal": source_name or journal,
                "source_note_zh": "候选来自 OpenAlex/Science DOI 前缀；只保留研究论文标题和开放 PDF 线索。",
            })
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break

    if len(rows) < limit:
        for journal, issn, doi_prefix, _openalex_prefix in journals:
            url = (
                f"https://api.crossref.org/journals/{issn}/works?"
                f"filter=from-pub-date:2026-01-01,type:journal-article&rows={rows_per_query}"
                "&select=DOI,title,URL,abstract,container-title,type,published-print,published-online,link"
            )
            response, receipt = get_url(url, timeout=45)
            receipts.append({"kind": "crossref_science_family", "journal": journal, **receipt})
            if not response or response.status_code != 200:
                continue
            try:
                payload = response.json()
            except Exception:
                continue
            for item in payload.get("message", {}).get("items", []):
                if not isinstance(item, dict):
                    continue
                doi = str(item.get("DOI") or "").strip()
                title_values = item.get("title") if isinstance(item.get("title"), list) else []
                title = strip_markup(title_values[0] if title_values else "", 500)
                if not doi.startswith(f"10.1126/{doi_prefix}.") or not _title_is_research_article(title):
                    continue
                abstract = strip_markup(item.get("abstract") or "", 2000)
                rows.append({
                    "source": "science_family",
                    "paper_id": doi,
                    "doi": doi,
                    "title": title,
                    "authors": [],
                    "abstract": abstract,
                    "url": f"https://www.science.org/doi/{doi}",
                    "html_url": f"https://www.science.org/doi/full/{doi}",
                    "pdf_url": f"https://www.science.org/doi/pdf/{doi}?download=true",
                    "journal": journal,
                    "source_note_zh": "候选来自 Science 系列 Crossref 研究论文；更正、新闻、社论等标题被过滤。",
                })
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break
    rows = unique_papers(rows)
    return rows, {"status": "ok" if rows else "blocked_no_candidates", "fetches": receipts, "candidate_count": len(rows), "note_zh": "优先使用 OpenAlex 的 Science DOI 前缀开放 PDF；Crossref 仅作兜底。不把 RSS 新闻、erratum 或 Crossref 摘要当全文。"}

CRAWLERS: dict[str, Callable[[int], tuple[list[dict[str, Any]], dict[str, Any]]]] = {
    "nips2025": crawl_neurips2025,
    "iclr2026": crawl_iclr2026,
    "icml2026": crawl_icml2026,
    "sigkdd2026": crawl_sigkdd2026,
    "arxiv": crawl_arxiv,
    "biorxiv": crawl_biorxiv,
    "nature": crawl_nature,
    "science_family": crawl_science_family,
}


def render_item_md(payload: dict[str, Any]) -> str:
    paper = payload.get("paper") if isinstance(payload.get("paper"), dict) else {}
    packet = payload.get("full_text_packet") if isinstance(payload.get("full_text_packet"), dict) else {}
    lines = [
        f"# 渠道批量测试阅读产物：{paper.get('title') or packet.get('title') or '未命名论文'}",
        "",
        f"- 渠道：`{payload.get('channel')}`",
        f"- 状态：`{payload.get('status')}`",
        f"- 正文状态：`{packet.get('full_text_status')}`",
        f"- 正文字数：{packet.get('full_text_chars') or packet.get('text_chars') or 0}",
        f"- PDF：{packet.get('pdf_url') or paper.get('pdf_url') or '未获取'}",
        f"- 正文路径：`{packet.get('text_path') or '未生成'}`",
        f"- Claude/subagent prompt：`{payload.get('artifacts', {}).get('prompt') or ''}`",
        "",
    ]
    if payload.get("status") == "prepared_for_main_claude_subagent":
        lines.extend([
            "## 验收结论",
            "",
            "已生成全文证据包和主控 Claude Code 精读提示；提示要求主控必须调用 Task/subagent，并要求输出 `subagent_deep_read` 与 `deep_read_audit`。本批量测试未实际消费 Claude 额度执行深度精读。",
            "",
        ])
    else:
        lines.extend([
            "## 阻塞说明",
            "",
            "该条未达到全文精读输入要求；不能用摘要或题录冒充论文精读。请查看 `full_text_packet.json` 中的 PDF/HTML 获取证据。",
            "",
        ])
    return "\n".join(lines)


def process_candidate(channel: str, row: dict[str, Any], channel_dir: Path, index: int, claude_mode: str, timeout_sec: int) -> dict[str, Any]:
    paper_id = safe_slug(row.get("paper_id") or row.get("title") or f"paper_{index}", fallback=f"paper_{index}")
    item_dir = ensure_inside_reading(channel_dir / f"{index:03d}_{paper_id}", label="批量测试单篇目录")
    item_dir.mkdir(parents=True, exist_ok=True)
    paper = build_paper_record(
        article=str(row.get("url") or row.get("abs_url") or row.get("pdf_url") or row.get("doi") or row.get("title") or ""),
        title=str(row.get("title") or ""),
        authors=row.get("authors") or "",
        abstract=str(row.get("abstract") or ""),
        paper_id=str(row.get("paper_id") or ""),
        pdf_url=str(row.get("pdf_url") or ""),
        url=str(row.get("url") or row.get("abs_url") or ""),
        source=channel,
    )
    if row.get("html_url"):
        paper["html_url"] = row.get("html_url")
    if row.get("doi"):
        paper["doi"] = row.get("doi")
    for key in ["published_doi", "published_journal", "journal", "source_note_zh"]:
        if row.get(key):
            paper[key] = row.get(key)
    if row.get("skip_pdf_acquisition"):
        paper["skip_pdf_acquisition"] = True
    write_json(item_dir / "paper.json", paper)
    packet = acquire_full_text(paper, item_dir, log=lambda message: None)
    full_packet = {"channel": channel, "source": "reading_channel_batch_test", "papers": [packet], "generated_at": utc_iso()}
    write_json(item_dir / "full_text_packet.json", full_packet)
    output_path = ensure_inside_reading(item_dir / "outputs" / "reading_result.json", label="批量测试 Claude 输出")
    prompt_path = ensure_inside_reading(item_dir / "prompts" / "deep_read_prompt.md", label="批量测试 prompt")
    prompt = build_deep_read_prompt(paper=paper, packet=packet, run_path=item_dir, output_path=output_path)
    write_text(prompt_path, prompt)
    full_text_ok = bool(packet.get("full_text_available")) and int(packet.get("full_text_chars") or packet.get("text_chars") or 0) >= MIN_FULL_TEXT_CHARS
    prompt_text = prompt_path.read_text(encoding="utf-8")
    prompt_ok = all(marker in prompt_text for marker in ["Task/subagent", "subagent_deep_read", str(packet.get("text_path") or "")]) if packet.get("text_path") else False
    claude_receipt: dict[str, Any] = {"status": "not_run_batch_prepare", "run_executed": False}
    if claude_mode == "run" and full_text_ok:
        claude_receipt = run_claude_deep_read(prompt_path=prompt_path, run_path=item_dir, expected_output_path=output_path, timeout_sec=timeout_sec, mode="run")
    elif claude_mode == "prepare" or not full_text_ok:
        claude_receipt = run_claude_deep_read(prompt_path=prompt_path, run_path=item_dir, expected_output_path=output_path, timeout_sec=timeout_sec, mode="prepare")
    status = "prepared_for_main_claude_subagent" if full_text_ok and prompt_ok else "blocked_full_text_unavailable"
    if claude_mode == "run" and full_text_ok:
        result_payload = claude_receipt.get("result_payload") if isinstance(claude_receipt.get("result_payload"), dict) else {}
        audit = result_payload.get("deep_read_audit") if isinstance(result_payload.get("deep_read_audit"), dict) else {}
        subagent_flag = result_payload.get("subagent_deep_read") is True or audit.get("subagent_used") is True
        if subagent_flag and isinstance(result_payload.get("reading"), dict):
            status = "complete"
        else:
            status = str(claude_receipt.get("status") or status)
    payload = {
        "channel": channel,
        "index": index,
        "status": status,
        "generated_at": utc_iso(),
        "paper": paper,
        "full_text_packet": packet,
        "claude": claude_receipt,
        "validation": {
            "required_files_exist": all((item_dir / name).exists() for name in ["paper.json", "full_text_packet.json", "read_results.json", "read.md"]),
            "full_text_ok": full_text_ok,
            "prompt_ok": prompt_ok,
            "text_path_inside_reading": str(packet.get("text_path") or "").startswith(str(READING_ROOT)),
            "pdf_or_html_evidence": bool(packet.get("pdf_path") or packet.get("html_acquisition")),
        },
        "artifacts": {
            "item_dir": str(item_dir),
            "paper": str(item_dir / "paper.json"),
            "full_text_packet": str(item_dir / "full_text_packet.json"),
            "prompt": str(prompt_path),
            "read_results": str(item_dir / "read_results.json"),
            "read_md": str(item_dir / "read.md"),
        },
    }
    write_json(item_dir / "read_results.json", payload)
    write_text(item_dir / "read.md", render_item_md(payload))
    payload["validation"]["required_files_exist"] = all((item_dir / name).exists() for name in ["paper.json", "full_text_packet.json", "read_results.json", "read.md"])
    write_json(item_dir / "read_results.json", payload)
    return payload


def process_channel(channel: str, args: argparse.Namespace, root: Path) -> dict[str, Any]:
    channel_dir = ensure_inside_reading(root / channel, label="批量测试渠道目录")
    channel_dir.mkdir(parents=True, exist_ok=True)
    crawler = CRAWLERS[channel]
    crawl_started = time.time()
    candidates, crawl_receipt = crawler(max(args.candidate_limit, args.per_channel))
    crawl_receipt = {**crawl_receipt, "duration_seconds": round(time.time() - crawl_started, 3), "candidate_count": len(candidates)}
    write_json(channel_dir / "crawl_receipt.json", crawl_receipt)
    results: list[dict[str, Any]] = []
    ready_count = 0
    attempted = 0
    for row in candidates[: args.candidate_limit]:
        attempted += 1
        result = process_candidate(channel, row, channel_dir, attempted, args.claude_mode, args.timeout_sec)
        results.append(result)
        if result.get("status") == "prepared_for_main_claude_subagent" or result.get("status") == "complete":
            ready_count += 1
        if ready_count >= args.per_channel:
            break
    blocked = [row for row in results if row.get("status") not in {"prepared_for_main_claude_subagent", "complete"}]
    summary = {
        "channel": channel,
        "status": "passed_minimum_ready_count" if ready_count >= args.per_channel else "blocked_minimum_ready_count_not_met",
        "target_per_channel": args.per_channel,
        "candidate_count": len(candidates),
        "attempted_count": attempted,
        "artifact_count": len(results),
        "ready_count": ready_count,
        "blocked_count": len(blocked),
        "full_text_count": sum(1 for row in results if row.get("validation", {}).get("full_text_ok")),
        "prompt_ok_count": sum(1 for row in results if row.get("validation", {}).get("prompt_ok")),
        "crawl_receipt": str(channel_dir / "crawl_receipt.json"),
        "items": [{
            "status": row.get("status"),
            "title": row.get("paper", {}).get("title"),
            "full_text_status": row.get("full_text_packet", {}).get("full_text_status"),
            "full_text_chars": row.get("full_text_packet", {}).get("full_text_chars"),
            "read_results": row.get("artifacts", {}).get("read_results"),
            "read_md": row.get("artifacts", {}).get("read_md"),
        } for row in results],
    }
    write_json(channel_dir / "channel_summary.json", summary)
    return summary


def render_batch_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Reading 多渠道批量抓取与阅读产物验收报告",
        "",
        f"- 运行 ID：`{payload.get('run_id')}`",
        f"- 运行目录：`{payload.get('run_dir')}`",
        f"- 每渠道目标：{payload.get('target_per_channel')} 篇",
        f"- Claude 模式：`{payload.get('claude_mode')}`",
        "",
        "## 渠道汇总",
        "",
        "| 渠道 | 状态 | 候选 | 尝试 | 可读产物 | 阻塞 |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for item in payload.get("channels", []):
        lines.append(f"| {item.get('channel')} | {item.get('status')} | {item.get('candidate_count')} | {item.get('attempted_count')} | {item.get('ready_count')} | {item.get('blocked_count')} |")
    lines.extend(["", "## 核对规则", "", "- 每篇必须有 `paper.json`、`full_text_packet.json`、`prompts/deep_read_prompt.md`、`read_results.json`、`read.md`。", "- `full_text_chars >= 1200` 且正文路径在 `modules/reading` 内，才算可进入主控 Claude/subagent 精读。", "- prompt 必须包含 `Task/subagent`、`subagent_deep_read` 和实际正文路径。", "- 本报告不把摘要或题录当作全文精读。", ""])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reading 多渠道批量抓取、下载和阅读产物验收。")
    parser.add_argument("--run-id", default="", help="批量测试运行 ID。")
    parser.add_argument("--per-channel", type=int, default=10)
    parser.add_argument("--candidate-limit", type=int, default=100)
    parser.add_argument("--channels", default=",".join(DEFAULT_CHANNELS), help="逗号分隔渠道。")
    parser.add_argument("--claude-mode", choices=["prepare", "run"], default="prepare")
    parser.add_argument("--timeout-sec", type=int, default=1800)
    parser.add_argument("--workers", type=int, default=2)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_id = safe_slug(args.run_id or f"channel_batch_{now_compact()}", fallback="channel_batch")
    root = ensure_inside_reading(WORKSPACE_ROOT / "batch_tests" / run_id, label="批量测试目录")
    root.mkdir(parents=True, exist_ok=True)
    channels = [item.strip() for item in args.channels.split(",") if item.strip()]
    unknown = [item for item in channels if item not in CRAWLERS]
    if unknown:
        raise SystemExit(f"未知渠道：{', '.join(unknown)}")
    started = time.time()
    summaries: list[dict[str, Any]] = []
    with futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        future_map = {pool.submit(process_channel, channel, args, root): channel for channel in channels}
        for future in futures.as_completed(future_map):
            channel = future_map[future]
            try:
                summaries.append(future.result())
            except Exception as exc:
                error_dir = root / channel
                error_dir.mkdir(parents=True, exist_ok=True)
                summary = {"channel": channel, "status": "channel_exception", "error": exc.__class__.__name__, "message": str(exc)[:800], "target_per_channel": args.per_channel, "candidate_count": 0, "attempted_count": 0, "ready_count": 0, "blocked_count": 0}
                write_json(error_dir / "channel_summary.json", summary)
                summaries.append(summary)
    summaries = sorted(summaries, key=lambda item: channels.index(item.get("channel")) if item.get("channel") in channels else 999)
    report = {
        "run_id": run_id,
        "status": "passed_all_channels" if all(item.get("ready_count", 0) >= args.per_channel for item in summaries) else "blocked_some_channels",
        "generated_at": utc_iso(),
        "duration_seconds": round(time.time() - started, 3),
        "run_dir": str(root),
        "target_per_channel": args.per_channel,
        "candidate_limit": args.candidate_limit,
        "claude_mode": args.claude_mode,
        "channels": summaries,
    }
    write_json(root / "batch_report.json", report)
    write_text(root / "batch_report.md", render_batch_report(report))
    write_json(WORKSPACE_ROOT / "latest_channel_batch_test.json", {"run_id": run_id, "status": report["status"], "run_dir": str(root), "batch_report": str(root / "batch_report.json")})
    print(json.dumps({"status": report["status"], "run_id": run_id, "run_dir": str(root), "batch_report": str(root / "batch_report.json"), "channels": [{"channel": item.get("channel"), "status": item.get("status"), "ready_count": item.get("ready_count"), "candidate_count": item.get("candidate_count")} for item in summaries]}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed_all_channels" else 2


if __name__ == "__main__":
    raise SystemExit(main())
