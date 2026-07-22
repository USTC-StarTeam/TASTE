from __future__ import annotations


# ---- source common ----

import hashlib
import html
import re
from datetime import date, datetime

from finding_runtime import normalize_metadata_text


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _clean_text(value: str) -> str:
    return normalize_metadata_text(value)


_ABSTRACT_UI_CONTROL_RE = re.compile(
    r"(?:\s*(?:show\s+(?:more|less)|read\s+(?:more|less)|显示更多|显示较少|展开|收起)\s*[。.]?\s*)+$",
    re.IGNORECASE,
)


def _strip_abstract_ui_controls(value: object) -> str:
    return _ABSTRACT_UI_CONTROL_RE.sub("", _clean_text(str(value or ""))).strip()


def _presentation_type_from_url(url: object) -> str:
    text = str(url or "").lower()
    if re.search(r"/(?:best[-_]?paper|award)(?:/|$)", text):
        return "best paper/award"
    if re.search(r"/oral(?:/|$)", text):
        return "oral"
    if re.search(r"/(?:spotlight|highlight)(?:/|$)", text):
        return "spotlight"
    if re.search(r"/poster(?:/|$)", text):
        return "poster"
    return ""


def _presentation_type_from_text(value: object) -> str:
    text = _clean_text(str(value or "")).lower()
    if not text:
        return ""
    if re.search(r"\b(best|award|outstanding|distinguished)[-\s]+paper\b", text):
        return "best paper/award"
    if re.search(r"\boral\b", text):
        return "oral"
    if re.search(r"\bspotlight\b|\bhighlight\b", text):
        return "spotlight"
    if re.search(r"\bposter\b", text):
        return "poster"
    return ""


def _presentation_display_label(venue: object, year: object, presentation_type: str) -> str:
    label = str(presentation_type or "").strip()
    if not label:
        return ""
    display = " ".join(part for part in [str(venue or "").strip(), str(year or "").strip(), label.title()] if part).strip()
    return display or label


def _set_presentation_metadata(paper: dict, presentation_type: str, *, source: str) -> None:
    label = str(presentation_type or "").strip().lower()
    if not label:
        return
    metadata = paper.setdefault("metadata", {})
    display = _presentation_display_label(paper.get("venue"), paper.get("year"), label)
    paper.setdefault("track", display)
    paper.setdefault("presentation_type", label)
    paper.setdefault("presentation_label", display)
    if isinstance(metadata, dict):
        metadata.setdefault("presentation_type", label)
        metadata.setdefault("presentation_label", display)
        metadata.setdefault("presentation_source", source)


def _title_key(value: str) -> str:
    text = html.unescape(_clean_text(value)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()

def normalize_date(value: str = "") -> str:
    text = (value or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    match = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return date(year, month, day).isoformat()
    return text


def _in_date_range(value: str, start_date: str = "", end_date: str = "") -> bool:
    current = normalize_date((value or "")[:10])
    start = normalize_date(start_date)
    end = normalize_date(end_date)
    if not current:
        return True
    if start and current < start:
        return False
    if end and current > end:
        return False
    return True

def is_neurips_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return "neurips" in text or "neural information processing systems" in text


def is_acl_family_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return any(key in text for key in ["acl", "emnlp", "naacl", "association for computational linguistics"])


def is_iclr_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')} {venue.get('address', '')}".lower()
    return "iclr" in text or "learning representations" in text


def is_cvf_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return any(key in text for key in ["cvpr", "iccv", "eccv"])


def is_pmlr_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return any(key in text for key in ["icml", "aistats", "colt", "uai"])


def is_icml_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return "icml" in text or "international conference on machine learning" in text


def is_aaai_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return "aaai" in text and "conference on artificial intelligence" in text


def is_ijcai_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return "ijcai" in text or "international joint conference on artificial intelligence" in text




OPENREVIEW_VENUE_PATTERNS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("neurips", "neural information processing systems"), ("NeurIPS.cc/{year}/Conference",)),
    (("iclr", "learning representations"), ("ICLR.cc/{year}/Conference",)),
    (("icml", "international conference on machine learning"), ("ICML.cc/{year}/Conference",)),
    (("aistats", "artificial intelligence and statistics"), ("aistats.org/AISTATS/{year}/Conference",)),
    (("uai", "uncertainty in artificial intelligence"), ("auai.org/UAI/{year}/Conference",)),
    (("colt", "conference on learning theory"), ("learningtheory.org/COLT/{year}/Conference",)),
    (("corl", "conference on robot learning"), ("robot-learning.org/CoRL/{year}/Conference",)),
    (("colm", "conference on language modeling"), ("colmweb.org/COLM/{year}/Conference",)),
    (("rlc", "reinforcement learning conference"), ("rl-conference.cc/RLC/{year}/Conference",)),
    (("log", "learning on graphs"), ("logconference.io/LOG/{year}/Conference",)),
    (("midl", "medical imaging with deep learning"), ("MIDL.io/{year}/Conference",)),
    (("tmlr", "transactions on machine learning research"), ("TMLR",)),
]


def _venue_text(venue: dict) -> str:
    return f"{venue.get('name', '')} {venue.get('full_name', '')} {venue.get('address', '')}".lower()


def _matches_venue_keyword(text: str, keyword: str) -> bool:
    keyword = keyword.lower()
    if " " in keyword:
        return keyword in text
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None


def _openreview_patterns_for_venue(venue: dict) -> list[str]:
    text = _venue_text(venue)
    patterns: list[str] = []
    for keywords, venue_patterns in OPENREVIEW_VENUE_PATTERNS:
        if any(_matches_venue_keyword(text, keyword) for keyword in keywords):
            patterns.extend(venue_patterns)
    return patterns


def is_openreview_supported_venue(venue: dict) -> bool:
    return bool(_openreview_patterns_for_venue(venue))


def _openreview_venue_ids(venue: dict, year: int) -> list[str]:
    venue_ids = []
    for pattern in _openreview_patterns_for_venue(venue):
        venue_ids.append(pattern.format(year=year) if "{year}" in pattern else pattern)
    return list(dict.fromkeys(venue_ids))


# ---- source audit ----

import os
from datetime import datetime
from typing import Any


VENUE_METADATA_AUDIT_KEY = "venue_metadata_audit"


def _venue_metadata_audit(**kwargs: Any) -> dict[str, Any]:
    audit = {
        "schema_version": 1,
        "status": kwargs.pop("status", "unknown"),
        "source_verified": bool(kwargs.pop("source_verified", False)),
        "complete": bool(kwargs.pop("complete", False)),
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }
    audit.update({key: value for key, value in kwargs.items() if value is not None})
    return audit


def _attach_venue_metadata_audit(papers: list[dict], audit: dict[str, Any]) -> list[dict]:
    for paper in papers:
        metadata = paper.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata[VENUE_METADATA_AUDIT_KEY] = dict(audit)
    return papers


def _attach_openreview_metadata_audit(papers: list[dict], venue_ids: list[str], years: list[int]) -> list[dict]:
    missing_abstracts = sum(1 for paper in papers if not str(paper.get("abstract") or "").strip())
    has_categories = any(str(paper.get("classification_source") or "").lower() == "official" and (paper.get("primary_area") or paper.get("category")) for paper in papers)
    audit = _venue_metadata_audit(
        status="partial",
        title_index_completeness_status="partial",
        source_verified=bool(papers),
        complete=False,
        title_index_complete=False,
        official_metadata_complete=bool(papers),
        adapter="openreview",
        openreview_venueids=list(dict.fromkeys(venue_ids)),
        requested_years=list(dict.fromkeys(int(year) for year in years)),
        paper_count=len(papers),
        missing_abstract_count=missing_abstracts,
        has_abstracts=bool(papers) and missing_abstracts == 0,
        any_abstracts=bool(papers) and missing_abstracts < len(papers),
        has_official_categories=has_categories,
        category_status="official_or_cached_categories" if has_categories else "no_official_categories",
        source_scope="openreview_official_venue_notes",
        official_title_index_verified=True,
        official_accepted_list_verified=True,
        completeness_basis="OpenReview official venue notes were fetched and title/abstract/category metadata was parsed; source remains partial until an adapter-level total-count audit verifies every record.",
    )
    return _attach_venue_metadata_audit(papers, audit)


def venue_metadata_audit_from_papers(papers: list[dict]) -> dict[str, Any]:
    audits: list[dict[str, Any]] = []
    for paper in papers:
        metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
        audit = metadata.get(VENUE_METADATA_AUDIT_KEY) if isinstance(metadata, dict) else None
        if isinstance(audit, dict):
            audits.append(audit)
    if not audits:
        return {}
    merged = dict(audits[0])
    merged["paper_count"] = len(papers)
    merged["complete"] = all(bool(audit.get("complete")) for audit in audits)
    statuses = list(dict.fromkeys(str(audit.get("status") or "unknown") for audit in audits))
    merged["status"] = statuses[0] if len(statuses) == 1 else "mixed"
    def abstract_unavailable_verified(paper: object) -> bool:
        if not isinstance(paper, dict):
            return False
        metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
        return bool(metadata.get("abstract_unavailable_verified"))

    missing_abstracts = sum(1 for paper in papers if not str((paper if isinstance(paper, dict) else {}).get("abstract") or "").strip() and not abstract_unavailable_verified(paper))
    unavailable_abstracts = sum(1 for paper in papers if not str((paper if isinstance(paper, dict) else {}).get("abstract") or "").strip() and abstract_unavailable_verified(paper))
    merged["missing_abstract_count"] = missing_abstracts
    merged["official_abstract_unavailable_count"] = unavailable_abstracts
    merged["has_abstracts"] = bool(papers) and missing_abstracts == 0
    merged["any_abstracts"] = bool(papers) and missing_abstracts < len(papers)
    if any("has_official_categories" in audit for audit in audits):
        merged["has_official_categories"] = all(bool(audit.get("has_official_categories")) for audit in audits)
    if any("official_title_index_verified" in audit for audit in audits):
        merged["official_title_index_verified"] = all(bool(audit.get("official_title_index_verified")) for audit in audits)
    if any("official_accepted_list_verified" in audit for audit in audits):
        merged["official_accepted_list_verified"] = all(bool(audit.get("official_accepted_list_verified")) for audit in audits)
    presentation_counts: dict[str, int] = {}
    presentation_sources: set[str] = set()
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
        label = str(paper.get("presentation_type") or metadata.get("presentation_type") or "").strip().lower()
        if label:
            presentation_counts[label] = presentation_counts.get(label, 0) + 1
            source = str(metadata.get("presentation_source") or metadata.get("source_page") or "").strip()
            if source:
                presentation_sources.add(source)
    if presentation_counts:
        merged["presentation_metadata_count"] = sum(presentation_counts.values())
        merged["presentation_type_counts"] = dict(sorted(presentation_counts.items()))
        if presentation_sources and not merged.get("presentation_metadata_source"):
            merged["presentation_metadata_source"] = "+".join(sorted(presentation_sources))
    source_scopes = [str(audit.get("source_scope") or "") for audit in audits if audit.get("source_scope")]
    if source_scopes:
        merged["source_scope"] = source_scopes[0] if len(set(source_scopes)) == 1 else "mixed"
    if not merged.get("venue_id"):
        for paper in papers:
            metadata = paper.get("metadata") if isinstance(paper, dict) and isinstance(paper.get("metadata"), dict) else {}
            venue_id = metadata.get("venue_id") if isinstance(metadata, dict) else ""
            if venue_id:
                merged["venue_id"] = venue_id
                break
    if not merged.get("venue"):
        for paper in papers:
            venue = paper.get("venue") if isinstance(paper, dict) else ""
            if venue:
                merged["venue"] = venue
                break
    if not merged.get("source_adapter"):
        merged["source_adapter"] = merged.get("adapter") or ""
    return merged


def _metadata_timeout(default: int = 6) -> int:
    try:
        value = int(float(os.environ.get("METADATA_TIMEOUT_SEC", "") or default))
    except Exception:
        value = default
    return max(2, min(30, value))


# ---- source parsing ----

import json
import os
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup



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
    abstract = _extract_between_markers(
        text,
        "Abstract",
        [
            "\nVideo\n",
            "\nSpotlight\n",
            "\nPoster\n",
            "\nName Change Policy\n",
            "\nChat is not available",
            "\nSuccessful Page Load",
        ],
    )

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
        track = _clean_text(_neurips_track_from_url(detail_url))
        authors = _neurips_authors_from_parent(parent, title, track)
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
            "category": "",
            "track": track,
            "classification_source": "official_track" if track else "llm_inferred",
            "metadata": {
                "venue_url": list_url,
                "detail_url": detail_url,
                "title_index_only": True,
                "source_page": "papers.nips.cc",
                "category_semantics": "presentation_track_only",
            },
        }
        _set_presentation_metadata(paper, _presentation_type_from_text(track), source="neurips_official_papers_track")
        papers.append(paper)
        if limit > 0 and len(papers) >= limit:
            break
    return papers


def _neurips_track_from_url(url: str) -> str:
    match = re.search(r"-Abstract-([A-Za-z0-9_]+)\.html(?:$|\?)", str(url or ""))
    if not match:
        return ""
    raw = match.group(1).replace("_", " ").strip()
    if raw.lower() == "conference":
        return "Main Conference Track"
    return " ".join(raw.split())


def _neurips_authors_from_parent(parent, title: str, track: str) -> str:
    if not parent:
        return ""

    def _strip_known_parts(text: str) -> str:
        authors = _clean_text(text)
        if authors.startswith(title):
            authors = authors[len(title):].strip()
        if track and authors.endswith(track):
            authors = authors[: -len(track)].strip()
        return _clean_text(authors)

    try:
        clone = BeautifulSoup(str(parent), "html.parser")
        for node in clone.select("a, span"):
            node.decompose()
        authors = _strip_known_parts(clone.get_text(" ", strip=True))
        if authors:
            return authors
    except Exception:
        pass
    return _strip_known_parts(parent.get_text(" ", strip=True))


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
    if source in {"icml_downloads", "icml_downloads_cache", "icml_official_virtual", "eccv_virtual", "neurips_virtual"}:
        if source == "icml_official_virtual":
            return source
        if metadata.get("title_index_only") or not (paper.get("abstract") and paper.get("pdf_url")):
            return source
    lowered = url.lower()
    if any(domain in lowered for domain in ["icml.cc/virtual/", "neurips.cc/virtual/", "eccv.ecva.net/virtual/"]):
        if metadata.get("title_index_only") or not (paper.get("abstract") and paper.get("pdf_url")):
            return source or "conference_virtual"
    return ""


def _conference_virtual_detail_label(paper: dict) -> str:
    source = _conference_virtual_detail_source(paper)
    if source in {"icml_downloads", "icml_downloads_cache"}:
        return "icml_virtual"
    if source and source.endswith("_virtual"):
        return f"{source}_detail"
    return "conference_virtual_detail"


# ---- source metadata ----

import hashlib
import re



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


# ---- journal sources ----

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup



NATURE_JOURNALS: dict[str, dict[str, str]] = {
    "nature": {"name": "Nature", "tier": "0", "group": "flagship", "issn": "0028-0836"},
    "natmachintell": {"name": "Nature Machine Intelligence", "tier": "1", "group": "ai_computational", "issn": "2522-5839"},
    "natcomputsci": {"name": "Nature Computational Science", "tier": "1", "group": "ai_computational", "issn": "2662-8457"},
    "nmeth": {"name": "Nature Methods", "tier": "1", "group": "ai_computational", "issn": "1548-7091"},
    "nbt": {"name": "Nature Biotechnology", "tier": "1", "group": "ai_computational", "issn": "1087-0156"},
    "natbiomedeng": {"name": "Nature Biomedical Engineering", "tier": "1", "group": "ai_computational", "issn": "2157-846X"},
    "ncomms": {"name": "Nature Communications", "tier": "1", "group": "ai_computational", "issn": "2041-1723"},
    "nmat": {"name": "Nature Materials", "tier": "2", "group": "ai_science_materials", "issn": "1476-1122"},
    "nchem": {"name": "Nature Chemistry", "tier": "2", "group": "ai_science_materials", "issn": "1755-4330"},
    "natchemeng": {"name": "Nature Chemical Engineering", "tier": "2", "group": "ai_science_materials", "issn": "2948-1198"},
    "natcatal": {"name": "Nature Catalysis", "tier": "2", "group": "ai_science_materials", "issn": "2520-1158"},
    "natsynth": {"name": "Nature Synthesis", "tier": "2", "group": "ai_science_materials", "issn": "2731-0582"},
    "nphys": {"name": "Nature Physics", "tier": "2", "group": "ai_science_materials", "issn": "1745-2473"},
    "natelectron": {"name": "Nature Electronics", "tier": "2", "group": "ai_science_materials", "issn": "2520-1131"},
    "nnano": {"name": "Nature Nanotechnology", "tier": "2", "group": "ai_science_materials", "issn": "1748-3387"},
    "nphoton": {"name": "Nature Photonics", "tier": "2", "group": "ai_science_materials", "issn": "1749-4885"},
    "nenergy": {"name": "Nature Energy", "tier": "2", "group": "ai_science_materials", "issn": "2058-7546"},
    "nm": {"name": "Nature Medicine", "tier": "3", "group": "broad_interdisciplinary", "issn": "1078-8956"},
    "ng": {"name": "Nature Genetics", "tier": "3", "group": "broad_interdisciplinary", "issn": "1061-4036"},
    "neuro": {"name": "Nature Neuroscience", "tier": "3", "group": "broad_interdisciplinary", "issn": "1097-6256"},
    "nathumbehav": {"name": "Nature Human Behaviour", "tier": "3", "group": "broad_interdisciplinary", "issn": "2397-3374"},
    "nclimate": {"name": "Nature Climate Change", "tier": "3", "group": "broad_interdisciplinary", "issn": "1758-678X"},
    "sustainability": {"name": "Nature Sustainability", "tier": "3", "group": "broad_interdisciplinary", "issn": "2398-9629"},
    "ngeo": {"name": "Nature Geoscience", "tier": "3", "group": "broad_interdisciplinary", "issn": "1752-0894"},
    "natecolevol": {"name": "Nature Ecology & Evolution", "tier": "3", "group": "broad_interdisciplinary", "issn": "2397-334X"},
    "s41545": {"name": "Nature Water", "tier": "3", "group": "broad_interdisciplinary", "issn": "2731-6084"},
    "s43016": {"name": "Nature Food", "tier": "3", "group": "broad_interdisciplinary", "issn": "2662-1355"},
}


SCIENCE_JOURNALS: dict[str, dict[str, str]] = {
    "science": {"name": "Science", "tier": "0", "group": "science_core", "issn": "0036-8075"},
    "sciadv": {"name": "Science Advances", "tier": "1", "group": "science_core", "issn": "2375-2548"},
    "scirobotics": {"name": "Science Robotics", "tier": "1", "group": "ai_robotics_engineering", "issn": "2470-9476"},
    "stm": {"name": "Science Translational Medicine", "tier": "2", "group": "bio_medicine", "issn": "1946-6234"},
    "sciimmunol": {"name": "Science Immunology", "tier": "2", "group": "bio_medicine", "issn": "2470-9468"},
    "stke": {"name": "Science Signaling", "tier": "2", "group": "bio_medicine", "issn": "1937-9145"},
    "adi": {"name": "Advanced Devices & Instrumentation", "tier": "SPJ", "group": "science_partner_journals"},
    "bmr": {"name": "Biomaterials Research", "tier": "SPJ", "group": "science_partner_journals"},
    "bmef": {"name": "BME Frontiers", "tier": "SPJ", "group": "science_partner_journals"},
    "csbj": {"name": "Computational and Structural Biotechnology Journal", "tier": "SPJ", "group": "science_partner_journals"},
    "csbr": {"name": "Computational and Structural Biotechnology Reports", "tier": "SPJ", "group": "science_partner_journals"},
    "ehs": {"name": "Ecosystem Health and Sustainability", "tier": "SPJ", "group": "science_partner_journals"},
    "energymatadv": {"name": "Energy Material Advances", "tier": "SPJ", "group": "science_partner_journals"},
    "hds": {"name": "Health Data Science", "tier": "SPJ", "group": "science_partner_journals"},
    "icomputing": {"name": "Intelligent Computing", "tier": "SPJ", "group": "science_partner_journals"},
    "jemdr": {"name": "Journal of EMDR Practice and Research", "tier": "SPJ", "group": "science_partner_journals"},
    "remotesensing": {"name": "Journal of Remote Sensing", "tier": "SPJ", "group": "science_partner_journals"},
    "olar": {"name": "Ocean-Land-Atmosphere Research", "tier": "SPJ", "group": "science_partner_journals"},
    "research": {"name": "Research", "tier": "SPJ", "group": "science_partner_journals"},
    "space": {"name": "Space: Science & Technology", "tier": "SPJ", "group": "science_partner_journals"},
    "ultrafastscience": {"name": "Ultrafast Science", "tier": "SPJ", "group": "science_partner_journals"},
    "plantphenomics": {"name": "Plant Phenomics", "tier": "SPJ", "group": "science_partner_journals", "status": "migrated"},
}


def _nature_journal_meta(slug: str) -> dict[str, str]:
    slug = (slug or "").strip().strip("/")
    return NATURE_JOURNALS.get(slug, {"name": slug or "Nature Portfolio", "tier": "", "group": "custom"})


def _nature_feed_url(slug: str, article_type: str) -> str:
    params = {"type": article_type or "article", "format": "feed"}
    return f"https://www.nature.com/{slug}/articles?" + urlencode(params)


def _nature_listing_url(slug: str, article_type: str, page: int) -> str:
    params: dict[str, str | int] = {"type": article_type or "article"}
    if page > 1:
        params["page"] = page
    return f"https://www.nature.com/{slug}/articles?" + urlencode(params)


def _looks_like_xml(text: str) -> bool:
    stripped = (text or "").lstrip()[:120].lower()
    return stripped.startswith("<?xml") or stripped.startswith("<feed") or stripped.startswith("<rss")


def _xml_text(node: ET.Element | None, names: list[str]) -> str:
    if node is None:
        return ""
    for name in names:
        found = node.find(name)
        if found is not None and found.text:
            return _clean_text(found.text)
    return ""


def _xml_attr(node: ET.Element | None, names: list[str], attr: str, value: str = "") -> str:
    if node is None:
        return ""
    for name in names:
        for found in node.findall(name):
            if value and found.attrib.get(attr) != value:
                continue
            href = found.attrib.get("href") or found.attrib.get("url") or ""
            if href:
                return href
    return ""


def _parse_nature_feed(xml_text: str, slug: str, article_type: str, feed_url: str) -> list[dict]:
    journal = _nature_journal_meta(slug)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    entries = list(root.findall(".//{http://www.w3.org/2005/Atom}entry"))
    if not entries:
        entries = list(root.findall(".//item"))
    papers: list[dict] = []
    for entry in entries:
        title = _xml_text(entry, ["{http://www.w3.org/2005/Atom}title", "title"])
        if not _looks_like_paper_title(title):
            continue
        url = _xml_attr(entry, ["{http://www.w3.org/2005/Atom}link", "link"], "rel", "alternate")
        if not url:
            url = _xml_text(entry, ["{http://www.w3.org/2005/Atom}id", "guid", "link"])
        url = urljoin(feed_url, url)
        published = _xml_text(entry, ["{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated", "pubDate"])
        summary = _xml_text(entry, ["{http://www.w3.org/2005/Atom}summary", "{http://www.w3.org/2005/Atom}content", "description"])
        authors = []
        for author in entry.findall("{http://www.w3.org/2005/Atom}author"):
            name = _xml_text(author, ["{http://www.w3.org/2005/Atom}name", "name"])
            if name:
                authors.append(name)
        year = int(published[:4]) if published[:4].isdigit() else date.today().year
        papers.append({
            "id": stable_id("nature", url or title),
            "source": "nature",
            "title": title,
            "authors": ", ".join(authors),
            "abstract": summary,
            "url": url,
            "pdf_url": "",
            "venue": journal["name"],
            "year": year,
            "category": article_type,
            "classification_source": "official",
            "metadata": {
                "journal_slug": slug,
                "journal_tier": journal.get("tier", ""),
                "journal_group": journal.get("group", ""),
                "article_type": article_type,
                "published": normalize_date(published[:10]),
                "feed_url": feed_url,
            },
        })
    return papers


def _parse_nature_listing_html(page_text: str, slug: str, article_type: str, page_url: str) -> list[dict]:
    journal = _nature_journal_meta(slug)
    soup = BeautifulSoup(page_text, "html.parser")
    papers: list[dict] = []
    seen: set[str] = set()
    for link in soup.select("article h3 a[href*='/articles/'], article a[href*='/articles/']"):
        title = _clean_text(link.get_text(" ", strip=True))
        if not _looks_like_paper_title(title):
            continue
        url = urljoin(page_url, link.get("href", ""))
        if url in seen:
            continue
        seen.add(url)
        container = link.find_parent("article") or link.find_parent("li") or link.parent
        text = _clean_text(container.get_text(" ", strip=True) if container else "")
        date_match = re.search(r"\b(\d{1,2}\s+[A-Z][a-z]{2}\s+20\d{2})\b", text)
        published = ""
        if date_match:
            try:
                published = datetime.strptime(date_match.group(1), "%d %b %Y").date().isoformat()
            except ValueError:
                published = ""
        summary = ""
        if container:
            for paragraph in container.find_all("p"):
                summary = _clean_text(paragraph.get_text(" ", strip=True))
                if summary and summary != title:
                    break
        papers.append({
            "id": stable_id("nature", url or title),
            "source": "nature",
            "title": title,
            "authors": "",
            "abstract": summary,
            "url": url,
            "pdf_url": "",
            "venue": journal["name"],
            "year": int(published[:4]) if published[:4].isdigit() else date.today().year,
            "category": article_type,
            "classification_source": "official",
            "metadata": {
                "journal_slug": slug,
                "journal_tier": journal.get("tier", ""),
                "journal_group": journal.get("group", ""),
                "article_type": article_type,
                "published": published,
                "listing_url": page_url,
            },
        })
    return papers


def _extract_nature_doi(soup: BeautifulSoup) -> str:
    for selector in ["meta[name='citation_doi']", "meta[name='dc.identifier']", "meta[property='og:url']"]:
        node = soup.select_one(selector)
        if not node or not node.get("content"):
            continue
        content = str(node["content"])
        if selector == "meta[property='og:url']" and "/articles/" in content:
            return content.rstrip("/").rsplit("/", 1)[-1]
        return content.replace("doi:", "").strip()
    return ""


def _science_journal_meta(slug: str) -> dict[str, str]:
    slug = (slug or "").strip()
    return SCIENCE_JOURNALS.get(slug, {"name": slug or "Science Family", "tier": "", "group": "custom"})


def _science_feed_url(slug: str) -> str:
    return "https://www.science.org/action/showFeed?" + urlencode({"type": "etoc", "feed": "rss", "jc": slug})


def _science_pdf_url(doi: str) -> str:
    doi = (doi or "").replace("doi:", "").strip()
    return f"https://www.science.org/doi/pdf/{doi}" if doi else ""


def _science_abs_url(doi: str, fallback_url: str = "") -> str:
    doi = (doi or "").replace("doi:", "").strip()
    return f"https://www.science.org/doi/abs/{doi}" if doi else fallback_url


def _extract_science_doi(soup: BeautifulSoup) -> str:
    for selector in ["meta[name='citation_doi']", "meta[name='dc.Identifier']", "meta[name='dc.identifier']"]:
        node = soup.select_one(selector)
        if node and node.get("content"):
            return str(node["content"]).replace("doi:", "").strip()
    return ""


def _extract_science_abstract(soup: BeautifulSoup) -> str:
    for selector in [
        "meta[name='description']",
        "meta[property='og:description']",
        "meta[name='citation_abstract']",
    ]:
        node = soup.select_one(selector)
        if node and node.get("content"):
            text = _clean_text(str(node["content"]))
            if text:
                return text
    for selector in [
        "section.abstract",
        "section[class*='abstract']",
        "div.abstract",
        "div[class*='abstract']",
        "[id*='abstract']",
        "[class*='Abstract']",
    ]:
        node = soup.select_one(selector)
        if node:
            text = _clean_text(node.get_text(" ", strip=True))
            text = re.sub(r"^Abstract\s*", "", text, flags=re.I).strip()
            if text:
                return text
    return ""


def _crossref_date(item: dict) -> str:
    for key in ["published-print", "published-online", "published"]:
        parts = item.get(key, {}).get("date-parts")
        if not parts or not parts[0]:
            continue
        values = [int(part) for part in parts[0]]
        year = values[0]
        month = values[1] if len(values) > 1 else 1
        day = values[2] if len(values) > 2 else 1
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return ""


def _crossref_first_text(value: object) -> str:
    if isinstance(value, list) and value:
        value = value[0]
    return _clean_text(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True))


def _crossref_authors(value: object) -> str:
    if not isinstance(value, list):
        return ""
    authors: list[str] = []
    for author in value[:12]:
        if not isinstance(author, dict):
            continue
        name = _clean_text(" ".join(part for part in [author.get("given", ""), author.get("family", "")] if part))
        if name:
            authors.append(name)
    return ", ".join(authors)


def _science_crossref_url(issn: str, start_date: str, end_date: str, rows: int, offset: int) -> str:
    filters = [f"issn:{issn}", "type:journal-article"]
    if start_date:
        filters.append(f"from-pub-date:{start_date}")
    if end_date:
        filters.append(f"until-pub-date:{end_date}")
    return "https://api.crossref.org/works?" + urlencode({
        "filter": ",".join(filters),
        "rows": max(1, min(100, rows)),
        "offset": max(0, offset),
        "sort": "published",
        "order": "desc",
    })


def _parse_science_crossref_items(items: list[dict], slug: str) -> list[dict]:
    journal = _science_journal_meta(slug)
    papers: list[dict] = []
    for item in items:
        doi = str(item.get("DOI") or "").strip()
        title = _crossref_first_text(item.get("title"))
        if not doi or not _looks_like_paper_title(title):
            continue
        container = _crossref_first_text(item.get("container-title")) or journal["name"]
        published = _crossref_date(item)
        abstract = _crossref_first_text(item.get("abstract"))
        year = int(published[:4]) if published[:4].isdigit() else date.today().year
        papers.append({
            "id": stable_id("science", doi),
            "source": "science",
            "title": title,
            "authors": _crossref_authors(item.get("author")),
            "abstract": abstract,
            "url": _science_abs_url(doi, str(item.get("URL") or "")),
            "pdf_url": _science_pdf_url(doi),
            "venue": container,
            "year": year,
            "category": "journal-article",
            "classification_source": "official",
            "metadata": {
                "journal_slug": slug,
                "journal_tier": journal.get("tier", ""),
                "journal_group": journal.get("group", ""),
                "article_type": "journal-article",
                "doi": doi,
                "published": published,
                "crossref_url": str(item.get("URL") or ""),
            },
        })
    return papers


def _parse_science_feed(xml_text: str, slug: str, allowed_types: set[str], feed_url: str) -> list[dict]:
    journal = _science_journal_meta(slug)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    ns = {
        "rss": "http://purl.org/rss/1.0/",
        "dc": "http://purl.org/dc/elements/1.1/",
        "prism": "http://prismstandard.org/namespaces/basic/2.0/",
        "content": "http://purl.org/rss/1.0/modules/content/",
    }
    papers: list[dict] = []
    for item in root.findall(".//rss:item", ns):
        title = _xml_text(item, ["{http://purl.org/rss/1.0/}title"])
        if not _looks_like_paper_title(title):
            continue
        article_type = _xml_text(item, ["{http://purl.org/dc/elements/1.1/}type"])
        if allowed_types and article_type.lower() not in allowed_types:
            continue
        doi = _xml_text(item, ["{http://prismstandard.org/namespaces/basic/2.0/}doi", "{http://purl.org/dc/elements/1.1/}identifier"]).replace("doi:", "").strip()
        url = _xml_text(item, ["{http://prismstandard.org/namespaces/basic/2.0/}url", "{http://purl.org/rss/1.0/}link"])
        published = _xml_text(item, ["{http://purl.org/dc/elements/1.1/}date", "{http://prismstandard.org/namespaces/basic/2.0/}coverDate"])
        description = _xml_text(item, ["{http://purl.org/rss/1.0/}description", "{http://purl.org/rss/1.0/modules/content/}encoded"])
        authors = _xml_text(item, ["{http://purl.org/dc/elements/1.1/}creator"])
        publication = _xml_text(item, ["{http://prismstandard.org/namespaces/basic/2.0/}publicationName"]) or journal["name"]
        published_date = normalize_date(published[:10])
        year = int(published_date[:4]) if published_date[:4].isdigit() else date.today().year
        canonical_url = _science_abs_url(doi, url)
        papers.append({
            "id": stable_id("science", doi or canonical_url or title),
            "source": "science",
            "title": title,
            "authors": authors,
            "abstract": description,
            "url": canonical_url,
            "pdf_url": _science_pdf_url(doi),
            "venue": publication,
            "year": year,
            "category": article_type,
            "classification_source": "official",
            "metadata": {
                "journal_slug": slug,
                "journal_tier": journal.get("tier", ""),
                "journal_group": journal.get("group", ""),
                "article_type": article_type,
                "doi": doi,
                "published": published_date,
                "feed_url": feed_url,
            },
        })
    return papers


# ---- conference sources ----

import json
import os
import re
import time
from typing import Any

import requests
from finding_runtime import FINDING_CACHE_DIR, read_json_safely, write_json_cache
from finding_runtime import display_path
from bs4 import BeautifulSoup



ICLR2026_GUIDE_URLS = (
    "https://raw.githubusercontent.com/JenniferZhao0531/ICLR2026-Guide-CN/main/ICLR2026_all_papers.json",
    "https://raw.githubusercontent.com/JenniferZhao0531/ICLR2026-Guide-CN/master/ICLR2026_all_papers.json",
)
ICML2026_OFFICIAL_ORALS_POSTERS_URL = "https://icml.cc/static/virtual/data/icml-2026-orals-posters.json"
ICML2026_OFFICIAL_ABSTRACTS_URL = "https://icml.cc/static/virtual/data/icml-2026-abstracts.json"
ICML2026_GUIDE_URLS = (
    "https://raw.githubusercontent.com/JenniferZhao0531/ICML2026-Guide-CN/main/ICML2026_all_papers.json",
    "https://raw.githubusercontent.com/JenniferZhao0531/ICML2026-Guide-CN/master/ICML2026_all_papers.json",
)


def _iclr2026_guide_cache_path() -> Any:
    return FINDING_CACHE_DIR / "ICLR2026_all_papers.json"


def _icml2026_official_cache_path(name: str) -> Any:
    return FINDING_CACHE_DIR / name


def _load_json_url_with_cache(url: str, cache_path: Any, *, user_agent: str = "TASTE-Finding/1.0") -> tuple[Any, str]:
    path = Path(cache_path)
    if path.exists():
        cached = read_json_safely(path, None)
        if cached is not None:
            return cached, display_path(path)
    response = requests.get(url, headers={"User-Agent": user_agent}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    write_json_cache(path, payload)
    return payload, url


def _load_iclr2026_guide_payload() -> tuple[dict[str, Any], str]:
    cache_path = _iclr2026_guide_cache_path()
    if cache_path.exists():
        cached = read_json_safely(cache_path, None)
        if cached is not None:
            return cached, display_path(cache_path)

    if os.environ.get("DISABLE_ICLR2026_GUIDE_NETWORK", "0").lower() in {"1", "true", "yes", "on"}:
        return {}, ""

    last_error = ""
    for url in ICLR2026_GUIDE_URLS:
        try:
            response = requests.get(url, headers={"User-Agent": "TASTE-Finding/1.0"}, timeout=30)
            response.raise_for_status()
            payload = response.json()
            write_json_cache(cache_path, payload)
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
            "ICLR 2026 accepted-paper metadata loaded from the Finding private cache or the declared "
            "ICLR2026-Guide-CN OpenReview mirror. It includes titles, abstracts, OpenReview URLs, and official primary areas."
        ),
    )
    return _attach_venue_metadata_audit(papers, audit)


def _icml_author_names(value: object) -> str:
    if not isinstance(value, list):
        return ""
    names: list[str] = []
    for item in value:
        if isinstance(item, dict):
            name = _clean_text(str(item.get("fullname") or item.get("name") or ""))
        else:
            name = _clean_text(str(item or ""))
        if name:
            names.append(name)
    return ", ".join(names)


def _load_icml2026_guide_payload() -> tuple[dict[str, Any], str]:
    cache_path = _icml2026_official_cache_path("ICML2026_all_papers.json")
    if cache_path.exists():
        cached = read_json_safely(cache_path, None)
        if cached is not None:
            return cached, display_path(cache_path)
    last_error = ""
    for url in ICML2026_GUIDE_URLS:
        try:
            payload, source_url = _load_json_url_with_cache(url, cache_path)
            return payload, source_url
        except Exception as exc:
            last_error = str(exc)[:240]
            time.sleep(0.2)
    if last_error:
        return {"_error": last_error}, ""
    return {}, ""


def _icml2026_guide_papers(max_items: int) -> list[dict]:
    raw, source_url = _load_icml2026_guide_payload()
    rows = raw.get("papers") if isinstance(raw, dict) else []
    if not isinstance(rows, list) or not rows:
        return []
    try:
        limit = int(max_items or 0)
    except Exception:
        limit = 0
    if limit <= 0:
        limit = len(rows)
    papers: list[dict] = []
    for item in rows[:limit]:
        if not isinstance(item, dict):
            continue
        title = _clean_text(str(item.get("title") or ""))
        if not _looks_like_paper_title(title):
            continue
        primary_area = _clean_text(str(item.get("primary_area") or item.get("primary_area_en") or item.get("icml_subtopic_en") or ""))
        category = primary_area or _clean_text(str(item.get("category") or ""))
        authors_raw = item.get("authors")
        authors = ", ".join(str(author).strip() for author in authors_raw if str(author).strip()) if isinstance(authors_raw, list) else _clean_text(str(authors_raw or ""))
        tier = _clean_text(str(item.get("tier") or item.get("decision") or ""))
        paper = {
            "id": stable_id("paper", f"icml:2026:{item.get('id') or title}"),
            "source": "icml_official_virtual",
            "title": title,
            "authors": authors,
            "abstract": _clean_text(str(item.get("abstract") or "")),
            "url": str(item.get("url") or item.get("paper_url") or ""),
            "pdf_url": str(item.get("pdf_url") or ""),
            "venue": "ICML",
            "year": 2026,
            "primary_area": primary_area,
            "category": category,
            "track": tier,
            "keywords": item.get("keywords") if isinstance(item.get("keywords"), list) else [],
            "classification_source": "official" if category else "llm_inferred",
            "metadata": {
                "venue_id": "dblp_icml",
                "source_record_id": str(item.get("id") or ""),
                "paper_url": str(item.get("paper_url") or ""),
                "primary_area_en": str(item.get("primary_area_en") or ""),
                "icml_subtopic_en": str(item.get("icml_subtopic_en") or ""),
                "subcategory": str(item.get("category") or ""),
                "decision": str(item.get("decision") or ""),
                "tier": tier,
            },
        }
        _set_presentation_metadata(paper, tier, source="icml2026_guide_tier")
        papers.append(paper)
    meta = raw.get("meta") if isinstance(raw, dict) else {}
    total = int(meta.get("total_accepted") or meta.get("total") or len(rows))
    truncated = len(papers) < total
    missing_abstracts = sum(1 for paper in papers if not str(paper.get("abstract") or "").strip())
    audit = _venue_metadata_audit(
        status="partial" if truncated else "complete",
        title_index_completeness_status="partial" if truncated else "complete",
        source_verified=bool(source_url),
        complete=not truncated,
        title_index_complete=not truncated,
        official_metadata_complete=True,
        adapter="icml2026_guide",
        source_adapter="icml2026_guide",
        source_url=source_url,
        requested_years=[2026],
        paper_count=len(papers),
        source_total_count=total,
        missing_abstract_count=missing_abstracts,
        has_abstracts=bool(papers) and missing_abstracts == 0,
        any_abstracts=missing_abstracts < len(papers) if papers else False,
        has_official_categories=any(str(paper.get("primary_area") or paper.get("category") or "").strip() for paper in papers),
        category_status="official_or_cached_categories",
        source_scope="official_icml_virtual_metadata",
        official_title_index_verified=True,
        official_accepted_list_verified=True,
        truncated=truncated,
        completeness_basis=(
            "ICML 2026 metadata loaded from the ICML2026-Guide-CN mirror, which declares the official "
            "ICML virtual static JSON endpoints as its metadata and abstract sources."
        ),
    )
    return _attach_venue_metadata_audit(papers, audit)


def _icml_official_paper_url(item: dict[str, Any], year: int) -> str:
    virtual = str(item.get("virtualsite_url") or "")
    if virtual:
        return urljoin(f"https://icml.cc/virtual/{year}/papers.html", virtual)
    return str(item.get("paper_url") or "")


def fetch_icml_official_virtual_2026(max_items: int) -> list[dict]:
    try:
        raw, source_url = _load_json_url_with_cache(
            ICML2026_OFFICIAL_ORALS_POSTERS_URL,
            _icml2026_official_cache_path("icml-2026-orals-posters.json"),
        )
        abstracts, abstract_source_url = _load_json_url_with_cache(
            ICML2026_OFFICIAL_ABSTRACTS_URL,
            _icml2026_official_cache_path("icml-2026-abstracts.json"),
        )
    except Exception:
        return _icml2026_guide_papers(max_items)
    results = raw.get("results") if isinstance(raw, dict) else []
    if not isinstance(results, list) or not results:
        return _icml2026_guide_papers(max_items)
    if not isinstance(abstracts, dict):
        abstracts = {}
    try:
        limit = int(max_items or 0)
    except Exception:
        limit = 0
    accepted: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision") or "")
        if not decision.lower().startswith("accept"):
            continue
        title = _clean_text(str(item.get("name") or ""))
        if not _looks_like_paper_title(title):
            continue
        key = title.lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        event_type = _clean_text(str(item.get("event_type") or item.get("eventtype") or ""))
        tier = "Spotlight" if "spotlight" in decision.lower() or event_type.lower() in {"oral", "spotlight"} else (event_type or "Poster")
        if event_type.lower() == "oral":
            tier = "Oral"
        item_id = str(item.get("id") or "")
        abstract = _clean_text(str(abstracts.get(item_id) or ""))
        topic = _clean_text(str(item.get("topic") or ""))
        paper_url = str(item.get("paper_url") or "")
        url = _icml_official_paper_url(item, 2026)
        paper = {
            "id": stable_id("paper", f"icml:2026:{title}"),
            "source": "icml_official_virtual",
            "title": title,
            "authors": _icml_author_names(item.get("authors")),
            "abstract": abstract,
            "url": url,
            "pdf_url": str(item.get("paper_pdf_url") or ""),
            "venue": "ICML",
            "year": 2026,
            "primary_area": topic,
            "category": topic,
            "track": tier,
            "keywords": item.get("keywords") if isinstance(item.get("keywords"), list) else [],
            "classification_source": "official" if topic else "llm_inferred",
            "metadata": {
                "venue_id": "dblp_icml",
                "icml_event_id": item_id,
                "icml_uid": str(item.get("uid") or ""),
                "sourceid": item.get("sourceid"),
                "decision": decision,
                "event_type": event_type,
                "paper_url": paper_url,
                "virtualsite_url": str(item.get("virtualsite_url") or ""),
            },
        }
        _set_presentation_metadata(paper, tier, source="icml_official_virtual_event_type")
        accepted.append(paper)
        if limit > 0 and len(accepted) >= limit:
            break
    total = len({ _clean_text(str(item.get("name") or "")).lower() for item in results if isinstance(item, dict) and str(item.get("decision") or "").lower().startswith("accept") and _looks_like_paper_title(str(item.get("name") or "")) })
    truncated = limit > 0 and len(accepted) < total
    missing_abstracts = sum(1 for paper in accepted if not str(paper.get("abstract") or "").strip())
    audit = _venue_metadata_audit(
        status="partial" if truncated else "complete",
        title_index_completeness_status="partial" if truncated else "complete",
        source_verified=bool(source_url and abstract_source_url),
        complete=not truncated,
        title_index_complete=not truncated,
        official_metadata_complete=True,
        adapter="icml_official_virtual",
        source_adapter="icml_official_virtual",
        source_url=source_url,
        abstract_source_url=abstract_source_url,
        requested_years=[2026],
        paper_count=len(accepted),
        source_total_count=total,
        missing_abstract_count=missing_abstracts,
        has_abstracts=bool(accepted) and missing_abstracts == 0,
        any_abstracts=missing_abstracts < len(accepted) if accepted else False,
        has_official_categories=any(str(paper.get("primary_area") or paper.get("category") or "").strip() for paper in accepted),
        category_status="official_or_cached_categories",
        source_scope="official_icml_virtual_metadata",
        official_title_index_verified=True,
        official_accepted_list_verified=True,
        truncated=truncated,
        completeness_basis=(
            "ICML 2026 metadata loaded from official ICML virtual static JSON endpoints "
            "icml-2026-orals-posters.json and icml-2026-abstracts.json, deduplicated by title "
            "to accepted papers."
        ),
    )
    return _attach_venue_metadata_audit(accepted, audit) if accepted else _icml2026_guide_papers(max_items)


def _dblp_page_url(url: str) -> str:
    cleaned = (url or "").strip()
    return re.sub(
        r"^https?://(?:www\.)?(?:dblp\.org|dblp\.uni-trier\.de|dblp\.dagstuhl\.de)(?=/|$)",
        "https://dblp.uni-trier.de",
        cleaned,
        count=1,
        flags=re.IGNORECASE,
    )


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

    def merge_key(paper: dict) -> str:
        metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
        for value in (
            paper.get("doi"),
            metadata.get("doi"),
            paper.get("url"),
            metadata.get("doi_url"),
            metadata.get("publisher_url"),
        ):
            doi = _doi_from_url(str(value or ""))
            if doi:
                return f"doi:{doi.lower()}"
        return _title_key(paper.get("title")) or str(metadata.get("dblp_key") or paper.get("url") or paper.get("id") or "")

    for paper in [*toc_papers, *stream_papers]:
        if not isinstance(paper, dict):
            continue
        key = merge_key(paper)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(dict(paper))
        if max_items is not None and len(merged) >= max_items:
            break
    stream_audit = venue_metadata_audit_from_papers(stream_papers)
    toc_audit = venue_metadata_audit_from_papers(toc_papers)
    truncated = bool(max_items is not None and len(merged) >= max_items and (len(stream_papers) > len(merged) or len(toc_papers) > len(merged)))
    complete = bool(toc_audit.get("complete")) and not truncated and len(merged) >= len(toc_papers)
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


# ---- preprint sources ----

import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta



def _title_match_queries(title: str) -> list[str]:
    clean_title = _clean_text(" ".join(re.findall(r"[A-Za-z0-9]+", title or "")))
    terms = [
        term
        for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", title or "")
        if len(term) >= 3 and term.lower() not in {"and", "for", "the", "with", "via"}
    ]
    queries: list[str] = []
    if clean_title:
        queries.append(f'ti:"{clean_title}"')
        queries.append(f'all:"{clean_title}"')
    if terms:
        queries.append(" AND ".join(f"ti:{term}" for term in terms[:16]))
        queries.append(" AND ".join(f"all:{term}" for term in terms[:10]))
        if len(terms[0]) >= 4:
            queries.append(f"ti:{terms[0]}")
            queries.append(f"all:{terms[0]}")
    deduped: list[str] = []
    for query in queries:
        if query and query not in deduped:
            deduped.append(query)
    return deduped


BIORXIV_DEFAULT_RECENT_DAYS = 180


def _biorxiv_category_matches(category: str, selected: list[str]) -> bool:
    if not selected or any(item.lower() == "all" for item in selected):
        return True
    normalized = category.strip().lower()
    return normalized in {item.strip().lower() for item in selected if item.strip()}


def _biorxiv_content_url(doi: str, version: str = "") -> str:
    if not doi:
        return ""
    suffix = f"v{version}" if str(version).strip() else ""
    return f"https://www.biorxiv.org/content/{doi}{suffix}"


def _arxiv_entry_id(entry_id: str) -> str:
    text = (entry_id or "").rstrip("/")
    if "/abs/" in text:
        text = text.rsplit("/abs/", 1)[1]
    if "/pdf/" in text:
        text = text.rsplit("/pdf/", 1)[1]
    return re.sub(r"\.pdf$", "", text)


ARXIV_QUERY_STOPWORDS = {
    "a", "an", "and", "are", "de", "for", "in", "of", "on", "or", "the", "to", "with",
}


def _arxiv_date_clause(start_date: str = "", end_date: str = "") -> str:
    if not (start_date or end_date):
        return ""
    start_stamp = (start_date or "1991-01-01").replace("-", "") + "0000"
    end_stamp = (end_date or "3000-01-01").replace("-", "") + "2359"
    return f"submittedDate:[{start_stamp} TO {end_stamp}]"


def _arxiv_phrase_clause(phrase: str, field: str = "all") -> str:
    text = " ".join(str(phrase or "").split()).strip().strip('"')
    if not text:
        return ""
    # Multi-word (or hyphenated) phrases must be quoted to search as a phrase;
    # bare single tokens are matched as terms.
    if " " in text or "-" in text:
        return f'{field}:"{text}"'
    return f"{field}:{text}"


def _arxiv_or_group(terms: list[str]) -> str:
    clauses = []
    for term in terms or []:
        title_clause = _arxiv_phrase_clause(term, "ti")
        abstract_clause = _arxiv_phrase_clause(term, "abs")
        if title_clause and abstract_clause:
            clauses.append(f"({title_clause} OR {abstract_clause})")
    # de-dup while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for clause in clauses:
        if clause.lower() not in seen:
            seen.add(clause.lower())
            unique.append(clause)
    if not unique:
        return ""
    if len(unique) == 1:
        return unique[0]
    return "(" + " OR ".join(unique) + ")"


def build_arxiv_targeted_queries(
    search_keywords: list[str],
    categories: list[str],
    start_date: str = "",
    end_date: str = "",
) -> list[tuple[str, str]]:
    """Build one arXiv query that ORs every extracted keyword equally."""
    keyword_expr = _arxiv_or_group(search_keywords)
    if not keyword_expr:
        return []
    cleaned_categories = [c.strip() for c in (categories or []) if c.strip()]
    cat_expr = ("(" + " OR ".join(f"cat:{c}" for c in cleaned_categories) + ")") if cleaned_categories else ""
    date_clause = _arxiv_date_clause(start_date, end_date)

    def assemble(parts: list[str]) -> str:
        return " AND ".join(p for p in parts if p)

    queries: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, parts: list[str]) -> None:
        text = assemble(parts)
        if text and text not in seen:
            seen.add(text)
            queries.append((label, text))

    add("keywords", [keyword_expr, cat_expr, date_clause])
    return queries


def _arxiv_search_queries(categories: list[str], topic_queries: list[str], start_date: str = "", end_date: str = "") -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    seen: set[str] = set()
    cleaned_categories = [category.strip() for category in (categories or []) if category.strip()]
    cleaned_topics = [" ".join(str(query).split()) for query in (topic_queries or []) if str(query).strip()]
    for topic in cleaned_topics:
        terms = [
            term
            for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", topic)
            if len(term) >= 3 and term.lower() not in ARXIV_QUERY_STOPWORDS
        ][:8]
        topic_expr = " AND ".join(f"all:{term}" for term in terms)
        if not topic_expr:
            continue
        category_expr = " OR ".join(f"cat:{category}" for category in cleaned_categories)
        query_text = f"({topic_expr}) AND ({category_expr})" if category_expr else topic_expr
        if query_text not in seen:
            queries.append((f"topic:{topic}", query_text))
            seen.add(query_text)
    if not queries:
        for category in cleaned_categories:
            query_text = f"cat:{category}"
            if query_text not in seen:
                queries.append((category, query_text))
                seen.add(query_text)
    if start_date or end_date:
        start_stamp = (start_date or "1991-01-01").replace("-", "") + "0000"
        end_stamp = (end_date or "3000-01-01").replace("-", "") + "2359"
        queries = [(label, f"{query_text} AND submittedDate:[{start_stamp} TO {end_stamp}]") for label, query_text in queries]
    return queries


def build_biorxiv_search_phrases(search_terms: dict, *, max_phrases: int = 0) -> list[str]:
    """Return equal-status bioRxiv phrases, searched independently and unioned."""
    if not isinstance(search_terms, dict):
        return []
    phrases = search_terms.get("search_keywords") or []
    out: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        text = " ".join(str(phrase or "").split()).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out[:max_phrases] if max_phrases > 0 else out


def _append_arxiv_entry(papers: list[dict], by_key: dict[str, dict], entry, ns: dict, query_label: str, query_text: str, start_date: str, end_date: str) -> None:
    published = (entry.findtext("a:published", default="", namespaces=ns) or "")[:10]
    updated = (entry.findtext("a:updated", default="", namespaces=ns) or "")[:10]
    if not _in_date_range(published, start_date, end_date):
        return
    title = " ".join((entry.findtext("a:title", default="", namespaces=ns) or "").split())
    abstract = " ".join((entry.findtext("a:summary", default="", namespaces=ns) or "").split())
    raw_entry_id = (entry.findtext("a:id", default="", namespaces=ns) or "").strip()
    entry_id = re.sub(
        r"^http://((?:(?:www|export)\.)?arxiv\.org)(?=[:/]|$)",
        r"https://\1",
        raw_entry_id,
        flags=re.I,
    )
    arxiv_id = _arxiv_entry_id(entry_id)
    key = arxiv_id or title.lower()
    if not key:
        return
    category_terms = [
        str(node.attrib.get("term") or "").strip()
        for node in entry.findall("a:category", ns)
        if str(node.attrib.get("term") or "").strip()
    ]
    primary_node = entry.find("{http://arxiv.org/schemas/atom}primary_category")
    primary_category = str(primary_node.attrib.get("term") or "").strip() if primary_node is not None else ""
    all_categories = list(dict.fromkeys(([primary_category] if primary_category else []) + category_terms))
    matched_query = {"label": query_label, "query": query_text}
    existing = by_key.get(key)
    if existing:
        categories_seen = existing.setdefault("categories", [])
        for category in all_categories:
            if category not in categories_seen:
                categories_seen.append(category)
        metadata = existing.setdefault("metadata", {})
        metadata["all_categories"] = categories_seen
        matched_queries = metadata.setdefault("matched_queries", [])
        if matched_query not in matched_queries:
            matched_queries.append(matched_query)
        return
    pdf_url = entry_id.replace("/abs/", "/pdf/") if "/abs/" in entry_id else ""
    paper = {
        "id": stable_id("paper", raw_entry_id or title),
        "source": "arxiv",
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": ", ".join(author.findtext("a:name", default="", namespaces=ns) or "" for author in entry.findall("a:author", ns)),
        "abstract": abstract,
        "url": entry_id,
        "pdf_url": pdf_url,
        "venue": "arXiv",
        "year": int(published[:4]) if published[:4].isdigit() else date.today().year,
        "category": primary_category or (all_categories[0] if all_categories else ""),
        "categories": all_categories,
        "classification_source": "official",
        "metadata": {
            "published": published,
            "updated": updated,
            "arxiv_query": query_text,
            "arxiv_query_label": query_label,
            "matched_queries": [matched_query],
            "primary_category": primary_category,
            "all_categories": all_categories,
        },
    }
    by_key[key] = paper
    papers.append(paper)


ARXIV_DEFAULT_RECENT_DAYS = 180


def _arxiv_date_window(start_date: str = "", end_date: str = "", *, today: date | None = None) -> tuple[str, str, str]:
    start = normalize_date(start_date)
    end = normalize_date(end_date)
    if start or end:
        return start, end, "configured"
    current_day = today or date.today()
    return (current_day - timedelta(days=ARXIV_DEFAULT_RECENT_DAYS)).isoformat(), current_day.isoformat(), "default_recent_180_days"


# ---- venue cache sources ----

import json
from pathlib import Path

from finding_runtime import RUNS_DIR


def _allow_old_run_venue_cache() -> bool:
    return os.environ.get("ALLOW_OLD_RUN_VENUE_CACHE", "0").lower() in {"1", "true", "yes", "on"}


def _venue_cache_candidate_paths() -> list[Path]:
    if not _allow_old_run_venue_cache():
        return []
    roots: list[Path] = []
    if RUNS_DIR.exists():
        roots.extend(sorted(RUNS_DIR.glob("find_*/find_results.json"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True))
    seen: set[Path] = set()
    paths: list[Path] = []
    for path in roots:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved in seen or not path.exists():
            continue
        seen.add(resolved)
        paths.append(path)
    return paths


def _verified_venue_yecache_from_paths(venue: dict, years: list[int], max_items: int | None, paths: list[Path]) -> list[dict]:
    venue_id = str(venue.get("id") or "").strip()
    venue_name = str(venue.get("name") or "").strip().upper()
    wanted_years = {int(year) for year in years if str(year).isdigit()}
    if not venue_id or not wanted_years:
        return []
    for find_path in paths:
        try:
            payload = json.loads(find_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        health_rows = payload.get("venue_health_report", []) if isinstance(payload, dict) else []
        verified_years: set[int] = set()
        for row in health_rows if isinstance(health_rows, list) else []:
            if not isinstance(row, dict):
                continue
            row_venue_id = str(row.get("venue_id") or "")
            row_venue = str(row.get("venue") or "").strip().upper()
            if row_venue_id != venue_id and row_venue != venue_name:
                continue
            if not row.get("ok"):
                continue
            if row.get("release_signal_source") not in {"source_observed_available", "cache_source_verified"}:
                continue
            if row.get("metadata_completeness_ok") is not True:
                continue
            for year in row.get("effective_years") or []:
                try:
                    yeint = int(year)
                except Exception:
                    continue
                if yeint in wanted_years:
                    verified_years.add(yeint)
        if not verified_years:
            continue
        rows: list[dict] = []
        for pool in ["raw_title_index", "evaluated_candidates", "title_candidates", "articles", "screened_ranking"]:
            for item in payload.get(pool, []) or []:
                if not isinstance(item, dict):
                    continue
                try:
                    yeint = int(item.get("year") or 0)
                except Exception:
                    continue
                item_venue_id = str((item.get("metadata") or {}).get("venue_id") or "") if isinstance(item.get("metadata"), dict) else ""
                item_venue = str(item.get("venue") or "").strip().upper()
                if yeint not in verified_years:
                    continue
                if item_venue_id != venue_id and item_venue != venue_name:
                    continue
                cached = dict(item)
                metadata = dict(cached.get("metadata") or {}) if isinstance(cached.get("metadata"), dict) else {}
                metadata.update({"venue_id": venue_id, "cache_source_run": payload.get("run_id") or find_path.parent.name, "cache_source_path": display_path(find_path)})
                cached["metadata"] = metadata
                cached["source"] = cached.get("source") or "venue_cache"
                cached["venue"] = cached.get("venue") or venue.get("name", "")
                rows.append(cached)
                if max_items is not None and len(rows) >= max_items:
                    break
            if max_items is not None and len(rows) >= max_items:
                break
        if rows:
            seen: set[str] = set()
            unique: list[dict] = []
            for item in rows:
                key = str(item.get("title") or item.get("url") or item.get("id") or "").strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                unique.append(item)
                if max_items is not None and len(unique) >= max_items:
                    return unique
            if unique:
                return unique
    return []


def _icml_verified_download_cache(venue: dict, years: list[int], max_items: int | None) -> list[dict]:
    if not is_icml_venue(venue):
        return []
    rows = _verified_venue_yecache_from_paths(venue, years, max_items, _venue_cache_candidate_paths())
    for row in rows:
        metadata = dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), dict) else {}
        metadata["cache_adapter"] = "icml_downloads_verified_cache"
        row["metadata"] = metadata
        row["source"] = row.get("source") or "icml_downloads"
    return rows


def _recent_verified_venue_yecache(venue: dict, years: list[int], max_items: int | None) -> list[dict]:
    import os

    if not _allow_old_run_venue_cache():
        return []
    venue_id = str(venue.get("id") or "").strip()
    venue_name = str(venue.get("name") or "").strip().upper()
    wanted_years = {int(year) for year in years if str(year).isdigit()}
    if not venue_id or not wanted_years:
        return []
    runs_roots = [RUNS_DIR] if RUNS_DIR.exists() else []
    if not runs_roots:
        return []
    for find_path in sorted((item for runs_root in runs_roots for item in runs_root.glob("find_*/find_results.json")), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True):
        try:
            payload = json.loads(find_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        health_rows = payload.get("venue_health_report", []) if isinstance(payload, dict) else []
        verified_years: set[int] = set()
        for row in health_rows if isinstance(health_rows, list) else []:
            if not isinstance(row, dict):
                continue
            if str(row.get("venue_id") or "") != venue_id:
                continue
            if row.get("release_signal_source") != "source_observed_available":
                continue
            if not row.get("ok"):
                continue
            if row.get("metadata_completeness_ok") is not True:
                continue
            for year in row.get("effective_years") or []:
                try:
                    yeint = int(year)
                except Exception:
                    continue
                if yeint in wanted_years:
                    verified_years.add(yeint)
        if not verified_years:
            continue
        rows: list[dict] = []
        for pool in ["raw_title_index", "evaluated_candidates", "title_candidates", "articles", "screened_ranking"]:
            for item in payload.get(pool, []) or []:
                if not isinstance(item, dict):
                    continue
                try:
                    yeint = int(item.get("year") or 0)
                except Exception:
                    continue
                item_venue_id = str((item.get("metadata") or {}).get("venue_id") or "") if isinstance(item.get("metadata"), dict) else ""
                item_venue = str(item.get("venue") or "").strip().upper()
                if yeint not in verified_years:
                    continue
                if item_venue_id != venue_id and item_venue != venue_name:
                    continue
                cached = dict(item)
                metadata = dict(cached.get("metadata") or {}) if isinstance(cached.get("metadata"), dict) else {}
                metadata.update({"venue_id": venue_id, "cache_source_run": payload.get("run_id") or find_path.parent.name, "cache_source_path": display_path(find_path)})
                cached["metadata"] = metadata
                cached["source"] = cached.get("source") or "dblp"
                cached["venue"] = cached.get("venue") or venue.get("name", "")
                rows.append(cached)
                if max_items is not None and len(rows) >= max_items:
                    break
            if max_items is not None and len(rows) >= max_items:
                break
        if rows:
            seen: set[str] = set()
            unique: list[dict] = []
            for item in rows:
                key = str(item.get("title") or item.get("url") or item.get("id") or "").strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                unique.append(item)
                if max_items is not None and len(unique) >= max_items:
                    return unique
            if unique:
                return unique
    return []


# ---- source choice ----

from typing import Any



def _paper_has_official_category(paper: dict) -> bool:
    if not isinstance(paper, dict):
        return False
    source = str(paper.get("classification_source") or "").lower()
    if source not in {"official", "official_cached", "venue_official", "openreview", "local_metadata_category"}:
        return False
    return bool(str(paper.get("primary_area") or paper.get("category") or "").strip())


def _venue_source_official_category_count(papers: list[dict]) -> int:
    return sum(1 for paper in papers if _paper_has_official_category(paper))


def _venue_source_audit(papers: list[dict], adapter: str) -> dict[str, Any]:
    audit = venue_metadata_audit_from_papers(papers)
    if not audit:
        missing_abstracts = sum(1 for paper in papers if not str((paper if isinstance(paper, dict) else {}).get("abstract") or "").strip())
        official_category_count = _venue_source_official_category_count(papers)
        audit = {
            "schema_version": 1,
            "status": "partial",
            "source_verified": bool(papers),
            "complete": False,
            "adapter": adapter,
            "paper_count": len(papers),
            "missing_abstract_count": missing_abstracts,
            "has_abstracts": bool(papers) and missing_abstracts == 0,
            "any_abstracts": bool(papers) and missing_abstracts < len(papers),
            "has_official_categories": official_category_count > 0,
            "category_status": "official_or_cached_categories" if official_category_count else "no_official_categories",
        }
    return audit


def _venue_source_has_official_categories(papers: list[dict], audit: dict[str, Any]) -> bool:
    status = str(audit.get("category_status") or "").lower()
    if status in {"no_official_categories", "missing_categories", "no_or_partial_categories"}:
        return False
    return bool(audit.get("has_official_categories")) or _venue_source_official_category_count(papers) > 0


def _venue_source_category_priority_eligible(papers: list[dict], audit: dict[str, Any], requested_limit: int | None, max_candidate_count: int | None = None) -> bool:
    if not papers or not _venue_source_has_official_categories(papers, audit):
        return False
    count = len(papers)
    if requested_limit and requested_limit > 0 and count >= requested_limit:
        return True
    if count >= 50:
        if not max_candidate_count or max_candidate_count <= 0:
            return True
        return count >= max(50, int(max_candidate_count * 0.10))
    if max_candidate_count and max_candidate_count > 0:
        return max_candidate_count < 50 and count >= max(1, int(max_candidate_count * 0.50))
    return False


def _venue_source_score(papers: list[dict], adapter: str, order: int, requested_limit: int | None, max_candidate_count: int) -> tuple:
    audit = _venue_source_audit(papers, adapter)
    category_priority = _venue_source_category_priority_eligible(papers, audit, requested_limit, max_candidate_count)
    official_title = bool(audit.get("official_title_index_verified") or audit.get("official_accepted_list_verified"))
    complete = bool(audit.get("complete") or audit.get("title_index_complete"))
    any_abstracts = bool(audit.get("any_abstracts") or audit.get("has_abstracts"))
    source_verified = bool(audit.get("source_verified"))
    acm_doi_count = 0
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
        doi = _doi_from_url(str(paper.get("doi") or metadata.get("doi") or metadata.get("doi_url") or metadata.get("publisher_url") or ""))
        if doi.lower().startswith("10.1145/"):
            acm_doi_count += 1
    acm_doi_complete = bool(papers) and acm_doi_count == len(papers)
    return (
        1 if acm_doi_complete else 0,
        1 if category_priority else 0,
        1 if complete else 0,
        1 if official_title else 0,
        1 if any_abstracts else 0,
        1 if source_verified else 0,
        len(papers),
        -order,
    )


def _choose_best_venue_source(candidates: list[tuple[str, list[dict]]], requested_limit: int | None) -> tuple[list[dict], str]:
    nonempty = [(adapter, papers) for adapter, papers in candidates if papers]
    if not nonempty:
        return [], "none"
    max_candidate_count = max(len(papers) for _adapter, papers in nonempty)
    scored = [
        (_venue_source_score(papers, adapter, order, requested_limit, max_candidate_count), adapter, papers)
        for order, (adapter, papers) in enumerate(nonempty)
    ]
    scored.sort(key=lambda row: row[0], reverse=True)
    _score, adapter, papers = scored[0]
    return papers, adapter


def _source_has_confident_official_categories(papers: list[dict], adapter: str, requested_limit: int | None) -> bool:
    audit = _venue_source_audit(papers, adapter)
    max_count = requested_limit if requested_limit and requested_limit > 0 else len(papers)
    return _venue_source_category_priority_eligible(papers, audit, requested_limit, max_count)


def _source_is_complete_official_title_index(papers: list[dict], adapter: str) -> bool:
    audit = _venue_source_audit(papers, adapter)
    return bool(papers) and bool(audit.get("complete") or audit.get("title_index_complete")) and bool(audit.get("official_title_index_verified") or audit.get("official_accepted_list_verified"))


# Backward-compatible dotted imports for callers that still use the old package layout.
def _register_compat_aliases(*aliases: str) -> None:
    import sys as _sys
    _module = _sys.modules.get(__name__)
    if _module is None:
        return
    globals().setdefault("__path__", [])
    for _alias in aliases:
        _sys.modules.setdefault(_alias, _module)

_register_compat_aliases('sources.common', 'sources.audit', 'sources.parsing', 'sources.metadata', 'sources.journals', 'sources.conferences', 'sources.preprints', 'sources.venue_cache', 'sources.source_choice')
