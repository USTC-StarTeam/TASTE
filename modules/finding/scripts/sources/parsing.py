from __future__ import annotations

import json
import os
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from sources.common import (
    _clean_text,
    _presentation_type_from_text,
    _presentation_type_from_url,
    _set_presentation_metadata,
    _strip_abstract_ui_controls,
    stable_id,
)


def _looks_like_paper_title(value: str) -> bool:
    text = _clean_text(value)
    lowered = text.lower()
    if len(text) < 8:
        return False
    blocked = [
        "main navigation",
        "skip to",
        "successful page load",
        "openreview",
        "neurips 2025",
        "papers",
        "proceedings of",
        "companion proceedings of",
        "front matter",
        "preface",
        "table of contents",
    ]
    return not any(item == lowered or lowered.startswith(item) for item in blocked)


def _openreview_pdf_url(url: str) -> str:
    match = re.search(r"openreview\.net/forum\?id=([^&#]+)", url or "")
    if not match:
        return ""
    return f"https://openreview.net/pdf?id={match.group(1)}"

def _extract_between_markers(text: str, start: str, markers: list[str]) -> str:
    index = text.lower().find(start.lower())
    if index < 0:
        return ""
    body = text[index + len(start):]
    end_positions = [body.lower().find(marker.lower()) for marker in markers]
    end_positions = [pos for pos in end_positions if pos >= 0]
    if end_positions:
        body = body[: min(end_positions)]
    return "\n".join(line.strip() for line in body.splitlines() if line.strip()).strip()


def _parse_neurips_detail(html: str, url: str, fallback_title: str, year: int) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title_candidates = [fallback_title]
    for selector in [
        "meta[property='og:title']",
        "meta[name='twitter:title']",
    ]:
        node = soup.select_one(selector)
        if node and node.get("content"):
            title_candidates.append(str(node["content"]))
    title_candidates.extend(node.get_text(" ", strip=True) for node in soup.find_all(["h1", "h2", "h3"]))
    title = next((_clean_text(candidate) for candidate in title_candidates if _looks_like_paper_title(candidate)), fallback_title)
    text = soup.get_text("\n", strip=True)
    abstract = _extract_between_markers(text, "Abstract", ["Show more", "Video", "Chat is not available", "Successful Page Load"])

    authors = ""
    if title and title in text:
        after_title = text.split(title, 1)[1]
        before_abstract = after_title.split("Abstract", 1)[0]
        author_lines = [line.strip(" ·") for line in before_abstract.splitlines() if line.strip(" ·")]
        if author_lines:
            authors = author_lines[0]

    openreview_url = ""
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        label = anchor.get_text(" ", strip=True).lower()
        if "openreview.net" in href or "openreview" in label:
            openreview_url = href
            break

    paper_url = openreview_url or url
    paper = {
        "id": stable_id("paper", paper_url or f"neurips:{year}:{title}"),
        "source": "neurips_virtual",
        "title": title or fallback_title,
        "authors": authors,
        "abstract": abstract,
        "url": paper_url,
        "pdf_url": _openreview_pdf_url(openreview_url),
        "venue": "NeurIPS",
        "year": year,
        "category": "",
        "classification_source": "llm_inferred",
        "metadata": {"venue_url": url, "openreview_url": openreview_url},
    }
    presentation = _presentation_type_from_url(url)
    if not presentation:
        for candidate in title_candidates:
            presentation = _presentation_type_from_text(candidate)
            if presentation:
                break
    _set_presentation_metadata(paper, presentation, source="neurips_virtual_url_or_title")
    return paper


def _parse_neurips_official_papers_list(html: str, list_url: str, max_items: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    papers: list[dict] = []
    seen: set[str] = set()
    limit = int(max_items or 0)
    for anchor in soup.select('a[href*="/paper_files/paper/"][href*="/hash/"]'):
        href = str(anchor.get("href") or "")
        title = _clean_text(anchor.get_text(" ", strip=True))
        if not _looks_like_paper_title(title):
            continue
        detail_url = urljoin(list_url, href)
        if detail_url in seen:
            continue
        seen.add(detail_url)
        parent = anchor.find_parent("li")
        track_node = parent.select_one("span") if parent else None
        track = _clean_text(track_node.get_text(" ", strip=True)) if track_node else ""
        parent_text = _clean_text(parent.get_text(" ", strip=True)) if parent else ""
        authors = parent_text
        if authors.startswith(title):
            authors = authors[len(title):].strip()
        if track and authors.endswith(track):
            authors = authors[: -len(track)].strip()
        paper = {
            "id": stable_id("paper", detail_url),
            "source": "neurips_official_papers",
            "title": title,
            "authors": authors,
            "abstract": "",
            "url": detail_url,
            "pdf_url": detail_url.replace("-Abstract-", "-Paper-").replace(".html", ".pdf") if "-Abstract-" in detail_url else "",
            "venue": "NeurIPS",
            "year": 0,
            "category": track,
            "track": track,
            "classification_source": "official" if track else "llm_inferred",
            "metadata": {"venue_url": list_url, "detail_url": detail_url, "title_index_only": True, "source_page": "papers.nips.cc"},
        }
        _set_presentation_metadata(paper, _presentation_type_from_text(track), source="neurips_official_papers_track")
        papers.append(paper)
        if limit > 0 and len(papers) >= limit:
            break
    return papers


def _parse_neurips_list(html: str, list_url: str, max_items: int) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        title = _clean_text(anchor.get_text(" ", strip=True))
        if not any(part in href for part in ["/poster/", "/oral/", "/spotlight/", "/highlight/", "/paper/"]):
            continue
        if not _looks_like_paper_title(title):
            continue
        detail_url = urljoin(list_url, href)
        if detail_url in seen:
            continue
        seen.add(detail_url)
        candidates.append((detail_url, title))
        if len(candidates) >= max_items:
            break
    return candidates

def _extract_conference_virtual_abstract(soup: BeautifulSoup) -> str:
    selectors = [
        "div[class*='abstract']",
        "section[class*='abstract']",
        "#abstract",
        "meta[name='citation_abstract']",
        "meta[name='description']",
        "meta[property='og:description']",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        text = str(node.get("content") or "") if node.name == "meta" else node.get_text(" ", strip=True)
        text = _clean_text(text)
        if text.lower().startswith("abstract "):
            text = text[len("abstract "):].strip()
        text = _strip_abstract_ui_controls(text)
        if len(text) >= 80:
            return text
    page_text = soup.get_text("\n", strip=True)
    text = _extract_between_markers(
        page_text,
        "Abstract",
        ["Show more", "Show less", "Video", "Chat is not available", "Successful Page Load", "BibTeX", "Supplementary"],
    )
    text = _strip_abstract_ui_controls(text)
    return text if len(text) >= 80 else ""


def _extract_icml_virtual_abstract(soup: BeautifulSoup) -> str:
    return _extract_conference_virtual_abstract(soup)


def _jsonld_nodes(value: object) -> list[dict]:
    nodes: list[dict] = []
    if isinstance(value, dict):
        nodes.append(value)
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict):
                    nodes.extend(_jsonld_nodes(item))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                nodes.extend(_jsonld_nodes(item))
    return nodes


def _name_from_jsonld_author(value: object) -> str:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, dict):
        for key in ["name", "givenName", "familyName"]:
            text = _clean_text(str(value.get(key) or ""))
            if text:
                if key == "givenName" and value.get("familyName"):
                    return _clean_text(f"{text} {value.get('familyName')}")
                return text
    return ""


def _extract_conference_virtual_authors(soup: BeautifulSoup) -> list[str]:
    authors: list[str] = []
    for node in soup.find_all("meta", attrs={"name": "citation_author"}):
        name = _clean_text(str(node.get("content") or ""))
        if name and name not in authors:
            authors.append(name)
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        for node in _jsonld_nodes(payload):
            raw_authors = node.get("author") or node.get("authors") or node.get("creator")
            if not raw_authors:
                continue
            values = raw_authors if isinstance(raw_authors, list) else [raw_authors]
            for value in values:
                name = _name_from_jsonld_author(value)
                if name and name not in authors:
                    authors.append(name)
    return authors


def _extract_icml_virtual_authors(soup: BeautifulSoup) -> list[str]:
    return _extract_conference_virtual_authors(soup)


def _positive_float_env(name: str, default: float = 0.0) -> float:
    try:
        value = float(str(os.environ.get(name, '') or default).strip())
    except Exception:
        value = default
    return value if value > 0 else default


def _mark_detail_fetch_deferred(paper: dict, reason: str) -> None:
    metadata = paper.setdefault('metadata', {})
    metadata['detail_fetch_deferred'] = True
    metadata['detail_fetch_deferred_reason'] = reason
    paper['detail_fetch_deferred'] = True
    paper['detail_fetch_deferred_reason'] = reason


def _conference_virtual_detail_url(paper: dict) -> str:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    return str(metadata.get("detail_url") or metadata.get("venue_url") or paper.get("url") or "").strip()


def _conference_virtual_detail_source(paper: dict) -> str:
    source = str(paper.get("source") or "").strip()
    url = _conference_virtual_detail_url(paper)
    if not url or "/virtual/" not in url:
        return ""
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    if source in {"icml_downloads", "icml_downloads_cache", "eccv_virtual", "neurips_virtual"}:
        if metadata.get("title_index_only") or not (paper.get("abstract") and paper.get("pdf_url")):
            return source
    lowered = url.lower()
    if any(domain in lowered for domain in ["icml.cc/virtual/", "neurips.cc/virtual/", "eccv.ecva.net/virtual/"]):
        if metadata.get("title_index_only") or not (paper.get("abstract") and paper.get("pdf_url")):
            return source or "conference_virtual"
    return ""


def _conference_virtual_detail_target(paper: dict) -> bool:
    return bool(_conference_virtual_detail_source(paper))


def _icml_virtual_detail_target(paper: dict) -> bool:
    return _conference_virtual_detail_source(paper) in {"icml_downloads", "icml_downloads_cache"}


def _conference_virtual_detail_label(paper: dict) -> str:
    source = _conference_virtual_detail_source(paper)
    if source in {"icml_downloads", "icml_downloads_cache"}:
        return "icml_virtual"
    if source and source.endswith("_virtual"):
        return f"{source}_detail"
    return "conference_virtual_detail"
