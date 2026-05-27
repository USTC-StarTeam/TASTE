from __future__ import annotations

import hashlib
import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote, quote_plus, urlencode, urljoin

import requests
from bs4 import BeautifulSoup

from auto_research.paths import REFERENCE_ROOT


HEADERS = {
    "User-Agent": "TASTE/0.1 (+local research assistant)"
}


NATURE_JOURNALS: dict[str, dict[str, str]] = {
    "nature": {"name": "Nature", "tier": "0", "group": "flagship"},
    "natmachintell": {"name": "Nature Machine Intelligence", "tier": "1", "group": "ai_computational"},
    "natcomputsci": {"name": "Nature Computational Science", "tier": "1", "group": "ai_computational"},
    "nmeth": {"name": "Nature Methods", "tier": "1", "group": "ai_computational"},
    "nbt": {"name": "Nature Biotechnology", "tier": "1", "group": "ai_computational"},
    "natbiomedeng": {"name": "Nature Biomedical Engineering", "tier": "1", "group": "ai_computational"},
    "ncomms": {"name": "Nature Communications", "tier": "1", "group": "ai_computational"},
    "nmat": {"name": "Nature Materials", "tier": "2", "group": "ai_science_materials"},
    "nchem": {"name": "Nature Chemistry", "tier": "2", "group": "ai_science_materials"},
    "natchemeng": {"name": "Nature Chemical Engineering", "tier": "2", "group": "ai_science_materials"},
    "natcatal": {"name": "Nature Catalysis", "tier": "2", "group": "ai_science_materials"},
    "natsynth": {"name": "Nature Synthesis", "tier": "2", "group": "ai_science_materials"},
    "nphys": {"name": "Nature Physics", "tier": "2", "group": "ai_science_materials"},
    "natelectron": {"name": "Nature Electronics", "tier": "2", "group": "ai_science_materials"},
    "nnano": {"name": "Nature Nanotechnology", "tier": "2", "group": "ai_science_materials"},
    "nphoton": {"name": "Nature Photonics", "tier": "2", "group": "ai_science_materials"},
    "nenergy": {"name": "Nature Energy", "tier": "2", "group": "ai_science_materials"},
    "nm": {"name": "Nature Medicine", "tier": "3", "group": "broad_interdisciplinary"},
    "ng": {"name": "Nature Genetics", "tier": "3", "group": "broad_interdisciplinary"},
    "neuro": {"name": "Nature Neuroscience", "tier": "3", "group": "broad_interdisciplinary"},
    "nathumbehav": {"name": "Nature Human Behaviour", "tier": "3", "group": "broad_interdisciplinary"},
    "nclimate": {"name": "Nature Climate Change", "tier": "3", "group": "broad_interdisciplinary"},
    "sustainability": {"name": "Nature Sustainability", "tier": "3", "group": "broad_interdisciplinary"},
    "ngeo": {"name": "Nature Geoscience", "tier": "3", "group": "broad_interdisciplinary"},
    "natecolevol": {"name": "Nature Ecology & Evolution", "tier": "3", "group": "broad_interdisciplinary"},
    "s41545": {"name": "Nature Water", "tier": "3", "group": "broad_interdisciplinary"},
    "s43016": {"name": "Nature Food", "tier": "3", "group": "broad_interdisciplinary"},
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


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _clean_text(value: str) -> str:
    return " ".join((value or "").split())


def _title_key(value: str) -> str:
    text = html.unescape(_clean_text(value)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


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
    ]
    return not any(item == lowered or lowered.startswith(item) for item in blocked)


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


def fetch_openreview_iclr_2026(max_items: int) -> list[dict]:
    path = REFERENCE_ROOT / "ICLR2026-Guide-CN" / "ICLR2026_all_papers.json"
    if not path.exists():
        return []
    data = path.read_text(encoding="utf-8")
    import json
    raw = json.loads(data)
    papers = []
    for item in raw.get("papers", [])[:max_items]:
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
            "category": item.get("primary_area") or item.get("category") or "",
            "classification_source": "official",
            "metadata": {
                "primary_area": item.get("primary_area", ""),
                "subcategory": item.get("category", ""),
                "tier": item.get("tier", ""),
            },
        })
    return papers


def is_neurips_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return "neurips" in text or "neural information processing systems" in text


def is_acl_family_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return any(key in text for key in ["acl", "emnlp", "naacl", "association for computational linguistics"])


def is_iclr_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')} {venue.get('address', '')}".lower()
    return "iclr" in text or "learning representations" in text


def is_icml_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return "icml" in text or "international conference on machine learning" in text


OPENREVIEW_VENUE_PATTERNS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (
        ("neurips", "neural information processing systems"),
        ("NeurIPS.cc/{year}/Conference",),
    ),
    (
        ("iclr", "learning representations"),
        ("ICLR.cc/{year}/Conference",),
    ),
    (
        ("icml", "international conference on machine learning"),
        ("ICML.cc/{year}/Conference",),
    ),
    (
        ("aistats", "artificial intelligence and statistics"),
        ("aistats.org/AISTATS/{year}/Conference",),
    ),
    (
        ("uai", "uncertainty in artificial intelligence"),
        ("auai.org/UAI/{year}/Conference",),
    ),
    (
        ("colt", "conference on learning theory"),
        ("learningtheory.org/COLT/{year}/Conference",),
    ),
    (
        ("corl", "conference on robot learning"),
        ("robot-learning.org/CoRL/{year}/Conference",),
    ),
    (
        ("colm", "conference on language modeling"),
        ("colmweb.org/COLM/{year}/Conference",),
    ),
    (
        ("rlc", "reinforcement learning conference"),
        ("rl-conference.cc/RLC/{year}/Conference",),
    ),
    (
        ("log", "learning on graphs"),
        ("logconference.io/LOG/{year}/Conference",),
    ),
    (
        ("midl", "medical imaging with deep learning"),
        ("MIDL.io/{year}/Conference",),
    ),
    (
        ("tmlr", "transactions on machine learning research"),
        ("TMLR",),
    ),
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


def is_cvf_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return any(key in text for key in ["cvpr", "iccv", "eccv"])


def is_pmlr_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return any(key in text for key in ["icml", "aistats", "colt", "uai"])


def _request(url: str, timeout: int = 12) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response


def _dblp_page_url(url: str) -> str:
    cleaned = (url or "").strip()
    for prefix in ("https://dblp.org", "http://dblp.org", "http://dblp.uni-trier.de"):
        if cleaned.startswith(prefix):
            return cleaned.replace(prefix, "https://dblp.uni-trier.de", 1)
    return cleaned


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


def fetch_dblp_stream_api(venue: dict, years: list[int], max_items: int | None) -> list[dict]:
    stream_id = _dblp_stream_id(venue.get("address", ""))
    if not stream_id:
        return []
    wanted = {str(year) for year in years}
    request_size = 1000 if max_items is None else min(1000, max(100, max_items * 20))
    try:
        response = requests.get(
            "http://dblp.org/search/publ/api",
            params={"q": f"stream:streams/{stream_id}:", "h": request_size, "format": "json"},
            headers=HEADERS,
            timeout=12,
        )
        response.raise_for_status()
        hits = response.json().get("result", {}).get("hits", {}).get("hit", [])
    except Exception:
        return []
    if isinstance(hits, dict):
        hits = [hits]
    papers: list[dict] = []
    for hit in hits:
        info = hit.get("info", {}) if isinstance(hit, dict) else {}
        year = str(info.get("year") or "")
        if wanted and year not in wanted:
            continue
        title = _clean_text(html.unescape(str(info.get("title") or ""))).rstrip(".")
        if not _looks_like_paper_title(title):
            continue
        paper_url = str(info.get("ee") or info.get("url") or "")
        papers.append({
            "id": stable_id("paper", f"{venue.get('id')}:{year}:{title}"),
            "source": "dblp",
            "title": title,
            "authors": _dblp_authors(info.get("authors")),
            "abstract": "",
            "url": paper_url,
            "pdf_url": "",
            "venue": venue.get("name", ""),
            "year": int(year) if year.isdigit() else 0,
            "category": "",
            "classification_source": "llm_inferred",
            "metadata": {"venue_id": venue.get("id"), "dblp_stream": stream_id},
        })
        if max_items is not None and len(papers) >= max_items:
            return papers
    return papers


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


def _content_first_text(content: dict, keys: list[str]) -> str:
    for key in keys:
        value = _content_value(content, key)
        if value:
            return value
    return ""


def _content_keywords(content: dict) -> list[str]:
    values: list[str] = []
    for key in ["keywords", "Keywords", "TLDR", "tldr"]:
        raw_list = _content_list(content, key)
        if raw_list:
            values.extend(raw_list)
            continue
        raw_text = _content_value(content, key)
        if raw_text:
            values.extend(item.strip() for item in re.split(r"[,;]", raw_text) if item.strip())
    return list(dict.fromkeys(values))


def _openreview_venue_ids(venue: dict, year: int) -> list[str]:
    venue_ids = []
    for pattern in _openreview_patterns_for_venue(venue):
        venue_ids.append(pattern.format(year=year) if "{year}" in pattern else pattern)
    return list(dict.fromkeys(venue_ids))


def _openreview_api2_notes(venue_id: str, max_items: int) -> list[dict]:
    notes: list[dict] = []
    max_total = max(1, int(max_items or 1000))
    page_limit = max(1, min(1000, max_total))
    offset = 0
    while len(notes) < max_total:
        limit = min(page_limit, max_total - len(notes))
        try:
            response = requests.get(
                "https://api2.openreview.net/notes",
                params={
                    "content.venueid": venue_id,
                    "details": "replyCount,invitation,original",
                    "limit": limit,
                    "offset": offset,
                },
                headers=HEADERS,
                timeout=12,
            )
            response.raise_for_status()
            batch = response.json().get("notes", [])
        except Exception:
            return notes
        if not isinstance(batch, list) or not batch:
            break
        notes.extend(batch)
        if len(batch) < limit:
            break
        offset += len(batch)
    return notes[:max_total]


def fetch_openreview_venue(venue: dict, years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    queried_venue_ids: set[str] = set()
    for year in years:
        venue_ids = _openreview_venue_ids(venue, year)
        for venue_id in venue_ids:
            if venue_id in queried_venue_ids:
                continue
            queried_venue_ids.add(venue_id)
            notes = _openreview_api2_notes(venue_id, max_items)
            if not notes:
                for invitation in [f"{venue_id}/-/Blind_Submission", f"{venue_id}/-/Submission"]:
                    try:
                        response = requests.get(
                            "https://api.openreview.net/notes",
                            params={"invitation": invitation, "limit": max_items},
                            headers=HEADERS,
                            timeout=12,
                        )
                        response.raise_for_status()
                        notes = response.json().get("notes", [])
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
                primary_area = _content_first_text(content, ["primary_area", "Primary Area", "area", "Area", "subject_area", "Subject Area"])
                track = _content_first_text(content, ["track", "Track", "venue", "Venue"])
                category = primary_area or track or _content_first_text(content, ["category", "Category"])
                keywords = _content_keywords(content)
                papers.append({
                    "id": stable_id("paper", url),
                    "source": "openreview",
                    "title": title,
                    "authors": ", ".join(_content_list(content, "authors")),
                    "abstract": _clean_text(_content_value(content, "abstract")),
                    "url": url,
                    "pdf_url": f"https://openreview.net/pdf?id={note_id}" if note_id else "",
                    "venue": venue.get("name", ""),
                    "year": year,
                    "category": category,
                    "primary_area": primary_area,
                    "track": track,
                    "keywords": keywords,
                    "classification_source": "official" if category or keywords else "llm_inferred",
                    "metadata": {
                        "venue_id": venue.get("id"),
                        "openreview_venueid": venue_id,
                        "note_id": str(note_id or ""),
                        "forum": str(forum or ""),
                        "content_keys": sorted(str(key) for key in content.keys()),
                    },
                })
                if len(papers) >= max_items:
                    return papers
    return papers


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
            papers.append({
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
                "metadata": {"virtual_url": list_url},
            })
            if len(papers) >= max_items:
                return papers
    return papers


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
            year_match = re.search(r"\b(20\d{2})\b", text)
            if not year_match:
                continue
            year = int(year_match.group(1))
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
    return {
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


def _parse_neurips_list(html: str, list_url: str, max_items: int) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        title = _clean_text(anchor.get_text(" ", strip=True))
        if "/poster/" not in href or not _looks_like_paper_title(title):
            continue
        detail_url = requests.compat.urljoin(list_url, href)
        if detail_url in seen:
            continue
        seen.add(detail_url)
        candidates.append((detail_url, title))
        if len(candidates) >= max_items:
            break
    return candidates


def fetch_neurips_title_index(year: int, max_items: int, raise_errors: bool = False) -> list[dict]:
    list_url = f"https://neurips.cc/virtual/{year}/papers.html"
    try:
        candidates = _parse_neurips_list(_request(list_url).text, list_url, max_items)
    except Exception:
        if raise_errors:
            raise
        return []

    return [
        {
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
        for detail_url, title in candidates
    ]


def fetch_neurips_details(candidates: list[dict], year: int) -> list[dict]:
    papers: list[dict] = []
    for candidate in candidates:
        detail_url = candidate.get("metadata", {}).get("detail_url") or candidate.get("url", "")
        title = candidate.get("title", "")
        try:
            detail_html = _request(detail_url).text
            papers.append(_parse_neurips_detail(detail_html, detail_url, title, year))
            time.sleep(0.2)
        except Exception:
            papers.append({
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
                "metadata": {"venue_url": detail_url, "detail_parse_error": True},
            })
    return papers


def fetch_neurips_virtual(year: int, max_items: int, raise_errors: bool = False) -> list[dict]:
    return fetch_neurips_details(fetch_neurips_title_index(year, max_items, raise_errors), year)


def _parse_dblp_year_links(address: str, years: list[int], max_years: int = 4) -> list[tuple[int, str]]:
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
        matched_years = [year for year in re.findall(r"(20\d{2}|19\d{2})", f"{text} {href}") if year in wanted]
        if not matched_years:
            continue
        if "#" in href:
            continue
        if "/rec/conf/" in href:
            continue
        year = int(matched_years[0])
        if (year, href) not in links:
            links.append((year, href))
        if len(links) >= max_years:
            break
    if not links:
        links = direct_links()
    return links


def fetch_dblp_venue(venue: dict, years: list[int], max_items: int | None) -> list[dict]:
    papers = fetch_dblp_stream_api(venue, years, max_items)
    if papers:
        return papers

    def reached_limit() -> bool:
        return max_items is not None and len(papers) >= max_items

    papers = []
    links = _parse_dblp_year_links(venue.get("address", ""), years, max_years=max(4, len(years)))
    if not links:
        return papers
    for year, url in links:
        url = _dblp_page_url(url)
        xml_url = re.sub(r"\.html?$", ".xml", url)
        count_before_xml = len(papers)
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
                authors = ", ".join(_clean_text(html.unescape(author)) for author in re.findall(r"<author[^>]*>(.*?)</author>", record, flags=re.S))
                papers.append({
                    "id": stable_id("paper", f"{venue.get('id')}:{year}:{title}"),
                    "source": "dblp",
                    "title": title,
                    "authors": authors,
                    "abstract": "",
                    "url": paper_url,
                    "pdf_url": "",
                    "venue": venue.get("name", ""),
                    "year": year,
                    "category": "",
                    "classification_source": "llm_inferred",
                    "metadata": {"venue_id": venue.get("id"), "dblp_url": url, "dblp_xml_url": xml_url},
                })
                if reached_limit():
                    return papers
            if len(papers) > count_before_xml:
                continue
        except Exception:
            pass
        try:
            soup = BeautifulSoup(_request(url).text, "html.parser")
        except Exception:
            continue
        entries = soup.select("li.entry.inproceedings, li.entry.article")
        for entry in entries:
            title_node = entry.select_one("span.title")
            if not title_node:
                continue
            title = title_node.get_text(" ", strip=True).rstrip(".")
            authors = ", ".join(node.get_text(" ", strip=True) for node in entry.select("span[itemprop='name']")[:-1])
            paper_url = ""
            drop = entry.select_one("li.drop-down a[href]")
            if drop:
                paper_url = drop.get("href", "")
            papers.append({
                "id": stable_id("paper", f"{venue.get('id')}:{year}:{title}"),
                "source": "dblp",
                "title": title,
                "authors": authors,
                "abstract": "",
                "url": paper_url,
                "pdf_url": "",
                "venue": venue.get("name", ""),
                "year": year,
                "category": "",
                "classification_source": "llm_inferred",
                "metadata": {"venue_id": venue.get("id"), "dblp_url": url},
            })
            if reached_limit():
                return papers
        time.sleep(0.5)
    return papers


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


def fetch_venue_title_index(venue: dict, years: list[int], max_items: int) -> tuple[list[dict], str]:
    if is_iclr_venue(venue):
        papers = fetch_openreview_venue(venue, years, max_items)
        if papers:
            return papers, "openreview"
        if 2026 in years:
            papers = fetch_openreview_iclr_2026(max_items)
            if papers:
                return papers, "openreview_reference"

    if is_neurips_venue(venue):
        papers = fetch_openreview_venue(venue, years, max_items)
        if papers:
            return papers, "openreview"
        papers: list[dict] = []
        for year in years:
            papers.extend(fetch_neurips_title_index(year, max_items))
            if len(papers) >= max_items:
                break
        if papers:
            return papers[:max_items], "neurips_virtual"

    if venue.get("address"):
        papers = fetch_dblp_venue(venue, years, max_items)
        if papers:
            return papers, "dblp"

    if is_acl_family_venue(venue):
        papers = fetch_acl_anthology(venue, years, max_items)
        if papers:
            return papers, "acl_anthology"

    if is_cvf_venue(venue):
        papers = fetch_cvf_openaccess(venue, years, max_items)
        if papers:
            return papers, "cvf_openaccess"
        if (venue.get("name") or "").upper() == "ECCV":
            papers = fetch_eccv_virtual(years, max_items)
            if papers:
                return papers, "eccv_virtual"

    if is_pmlr_venue(venue):
        papers = fetch_pmlr_index(venue, years, max_items)
        if papers:
            return papers, "pmlr"

    if is_openreview_supported_venue(venue):
        papers = fetch_openreview_venue(venue, years, max_items)
        if papers:
            return papers, "openreview"

    return [], "none"


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
    for key in ["primary_area", "track"]:
        if enrichment.get(key) and not merged.get(key):
            merged[key] = enrichment[key]
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
    if is_pmlr_venue(venue):
        papers = fetch_pmlr_index(venue, years, 100000)
        if papers:
            enrichments.append(("pmlr", papers))
    return enrichments


def fetch_venue_title_index_all(venue: dict, years: list[int]) -> tuple[list[dict], str]:
    if venue.get("address"):
        base_papers = fetch_dblp_venue(venue, years, None)
        if base_papers:
            merged, used_adapters = _merge_enrichments(base_papers, _fetch_enrichment_sources(venue, years))
            adapter = "dblp"
            if used_adapters:
                adapter = f"dblp+{'+'.join(used_adapters)}"
            return merged, adapter

    if is_iclr_venue(venue):
        papers = fetch_openreview_venue(venue, years, 100000)
        if papers:
            return papers, "openreview"
        if 2026 in years:
            papers = fetch_openreview_iclr_2026(100000)
            if papers:
                return papers, "openreview_reference"

    if is_neurips_venue(venue):
        papers = fetch_openreview_venue(venue, years, 100000)
        if papers:
            return papers, "openreview"
        papers = []
        for year in years:
            papers.extend(fetch_neurips_title_index(year, 100000))
        if papers:
            return papers, "neurips_virtual"

    if is_acl_family_venue(venue):
        papers = fetch_acl_anthology(venue, years, 100000)
        if papers:
            return papers, "acl_anthology"

    if is_cvf_venue(venue):
        papers = fetch_cvf_openaccess(venue, years, 100000)
        if papers:
            return papers, "cvf_openaccess"
        if (venue.get("name") or "").upper() == "ECCV":
            papers = fetch_eccv_virtual(years, 100000)
            if papers:
                return papers, "eccv_virtual"

    if is_pmlr_venue(venue):
        papers = fetch_pmlr_index(venue, years, 100000)
        if papers:
            return papers, "pmlr"

    if is_openreview_supported_venue(venue):
        papers = fetch_openreview_venue(venue, years, 100000)
        if papers:
            return papers, "openreview"

    return [], "none"


def fetch_selected_venue_details(candidates: list[dict]) -> list[dict]:
    details: list[dict] = []
    neurips_by_year: dict[int, list[dict]] = {}
    for candidate in candidates:
        if candidate.get("source") == "neurips_virtual" and candidate.get("metadata", {}).get("title_index_only"):
            neurips_by_year.setdefault(int(candidate.get("year") or date.today().year), []).append(candidate)
        else:
            details.append(candidate)

    for year, items in neurips_by_year.items():
        details.extend(fetch_neurips_details(items, year))
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
    for paper in papers[:limit]:
        if paper.get("abstract"):
            continue
        query = quote_plus(re.sub(r"[():/\\-]", " ", paper.get("title", "")))
        url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={query}&limit=1&fields=abstract,tldr,openAccessPdf,url"
        try:
            response = requests.get(url, headers=headers, timeout=12)
            if response.status_code != 200:
                continue
            data = response.json().get("data", [])
            if not data:
                continue
            item = data[0]
            paper["abstract"] = item.get("abstract") or ""
            paper["url"] = paper.get("url") or item.get("url") or ""
            pdf = item.get("openAccessPdf") or {}
            paper["pdf_url"] = paper.get("pdf_url") or pdf.get("url", "")
            if item.get("tldr") and item["tldr"].get("text"):
                paper.setdefault("metadata", {})["tldr"] = item["tldr"]["text"]
            time.sleep(0.2)
        except Exception:
            continue
    return papers


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
        url = requests.compat.urljoin(feed_url, url)
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
        url = requests.compat.urljoin(page_url, link.get("href", ""))
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


def enrich_nature_details(papers: list[dict], limit: int | None = None) -> tuple[list[dict], dict]:
    attempted = 0
    abstracts_filled = 0
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
                page_text = _request(feed_url).text
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
                    page_text = _request(page_url).text
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


def enrich_science_details(papers: list[dict], limit: int | None = None) -> tuple[list[dict], dict]:
    attempted = 0
    abstracts_filled = 0
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
        "pdfs_filled": pdfs_filled,
        "dois_filled": dois_filled,
    }


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
            while not reached_limit():
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
                    response = _request(crossref_url, timeout=20)
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
            papers = _parse_science_feed(_request(feed_url).text, slug, allowed_types, feed_url)
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


def _arxiv_entry_id(entry_id: str) -> str:
    text = (entry_id or "").rstrip("/")
    if "/abs/" in text:
        text = text.rsplit("/abs/", 1)[1]
    if "/pdf/" in text:
        text = text.rsplit("/pdf/", 1)[1]
    return re.sub(r"\.pdf$", "", text)


def _request_arxiv_page(url: str, attempts: int = 3):
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            try:
                return _request(url, timeout=20)
            except TypeError:
                return _request(url)
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.8 * attempt)
    if last_error:
        raise last_error
    raise RuntimeError("arXiv request failed")


def _arxiv_date_stamp(value: str, fallback: str, suffix: str) -> str:
    normalized = normalize_date(value) or fallback
    return normalized.replace("-", "") + suffix


def _arxiv_add_or_merge_paper(by_key: dict[str, dict], papers: list[dict], paper: dict, category: str) -> None:
    key = paper.get("arxiv_id") or str(paper.get("title", "")).lower()
    if not key:
        return
    existing = by_key.get(key)
    if existing:
        categories_seen = existing.setdefault("categories", [existing.get("category", "")])
        if category not in categories_seen:
            categories_seen.append(category)
        existing.setdefault("metadata", {})["all_categories"] = categories_seen
        return
    by_key[key] = paper
    papers.append(paper)


def _arxiv_abs_metadata(abs_url: str) -> dict[str, Any]:
    try:
        text = _request_arxiv_page(abs_url, attempts=2).text
    except Exception:
        return {}
    soup = BeautifulSoup(text, "html.parser")

    def meta_values(name: str) -> list[str]:
        return [str(node.get("content") or "").strip() for node in soup.select(f'meta[name="{name}"]') if node.get("content")]

    abstract_values = meta_values("citation_abstract")
    abstract = abstract_values[0] if abstract_values else ""
    if not abstract:
        abstract_node = soup.select_one("blockquote.abstract")
        if abstract_node:
            abstract = re.sub(r"^Abstract:\s*", "", abstract_node.get_text(" ", strip=True))
    return {
        "title": (meta_values("citation_title") or [""])[0],
        "authors": ", ".join(meta_values("citation_author")),
        "published": normalize_date((meta_values("citation_date") or [""])[0]),
        "pdf_url": (meta_values("citation_pdf_url") or [""])[0],
        "arxiv_id": (meta_values("citation_arxiv_id") or [""])[0],
        "abstract": " ".join(abstract.split()),
    }


def _parse_arxiv_list_date(text: str) -> str:
    match = re.search(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+(\d{1,2}\s+\w{3}\s+\d{4})", text or "")
    if not match:
        return ""
    try:
        return datetime.strptime(match.group(1), "%d %b %Y").date().isoformat()
    except ValueError:
        return ""


def _fetch_arxiv_recent_fallback(
    category: str,
    max_items: int,
    start_date: str,
    end_date: str,
    by_key: dict[str, dict],
    papers: list[dict],
) -> tuple[int, list[str], bool]:
    errors: list[str] = []
    fetched = 0
    reached_older_date = False
    page_size = max(1, min(100, int(max_items or 100)))
    category_count_before = len(papers)
    category_path = quote(category, safe=".")
    for start in range(0, max(1, max_items), page_size):
        url = f"https://arxiv.org/list/{category_path}/recent?skip={start}&show={page_size}"
        try:
            text = _request_arxiv_page(url).text
        except Exception as exc:
            errors.append(f"{category} recent start={start}: {exc}")
            break
        fetched += 1
        soup = BeautifulSoup(text, "html.parser")
        page_date = _parse_arxiv_list_date(" ".join(node.get_text(" ", strip=True) for node in soup.select("h3")))
        entries = list(zip(soup.select("dl dt"), soup.select("dl dd")))
        if not entries:
            break
        added_this_page = 0
        for dt_node, dd_node in entries:
            abs_link = dt_node.select_one('a[title="Abstract"]')
            if not abs_link:
                continue
            abs_url = urljoin("https://arxiv.org", abs_link.get("href") or "")
            arxiv_id = _arxiv_entry_id(abs_url) or str(abs_link.get("id") or "")
            title_node = dd_node.select_one(".list-title")
            title = title_node.get_text(" ", strip=True) if title_node else ""
            title = re.sub(r"^Title:\s*", "", title)
            authors = ", ".join(node.get_text(" ", strip=True) for node in dd_node.select(".list-authors a"))
            pdf_link = dt_node.select_one('a[title="Download PDF"]')
            pdf_url = urljoin("https://arxiv.org", pdf_link.get("href") or "") if pdf_link else f"https://arxiv.org/pdf/{arxiv_id}"
            subjects_text = (dd_node.select_one(".list-subjects") or dd_node).get_text(" ", strip=True)
            all_categories = re.findall(r"\(([a-z-]+\.[A-Z]{2})\)", subjects_text) or [category]
            metadata = _arxiv_abs_metadata(abs_url)
            published = metadata.get("published") or page_date
            if not _in_date_range(published, start_date, end_date):
                if start_date and published and published < start_date:
                    reached_older_date = True
                continue
            arxiv_id = metadata.get("arxiv_id") or arxiv_id
            paper = {
                "id": stable_id("paper", abs_url or title),
                "source": "arxiv",
                "arxiv_id": arxiv_id,
                "title": metadata.get("title") or title,
                "authors": metadata.get("authors") or authors,
                "abstract": metadata.get("abstract") or "",
                "url": abs_url,
                "pdf_url": metadata.get("pdf_url") or pdf_url,
                "venue": "arXiv",
                "year": int(published[:4]) if published[:4].isdigit() else date.today().year,
                "category": category,
                "categories": all_categories,
                "classification_source": "llm_inferred",
                "metadata": {"published": published, "updated": "", "arxiv_category": category, "primary_category": category, "all_categories": all_categories, "fallback": "arxiv_recent"},
            }
            before = len(papers)
            _arxiv_add_or_merge_paper(by_key, papers, paper, category)
            added_this_page += len(papers) - before
            if len(papers) - category_count_before >= max_items:
                break
        if reached_older_date or len(papers) - category_count_before >= max_items or added_this_page == 0:
            break
        time.sleep(0.5)
    return fetched, errors, reached_older_date


def fetch_arxiv(categories: list[str], max_items: int, start_date: str = "", end_date: str = "") -> tuple[list[dict], dict]:
    papers: list[dict] = []
    by_key: dict[str, dict] = {}
    start_date = normalize_date(start_date)
    end_date = normalize_date(end_date)
    categories = [category.strip() for category in (categories or []) if category.strip()] or ["cs.AI"]
    page_size = max(1, min(100, int(max_items or 100)))
    status = {
        "source": "arxiv",
        "ok": False,
        "limited": False,
        "count": 0,
        "message": "",
        "categories": categories,
        "start_date": start_date,
        "end_date": end_date,
        "queries": [],
        "errors": [],
        "pages_fetched": 0,
        "fallback_pages_fetched": 0,
        "fallback_used": False,
        "deduped_count": 0,
    }
    for category in categories:
        query_text = f"cat:{category}"
        if start_date or end_date:
            start_stamp = _arxiv_date_stamp(start_date, "1991-01-01", "0000")
            end_stamp = _arxiv_date_stamp(end_date, "3000-01-01", "2359")
            query_text = f"{query_text} AND submittedDate:[{start_stamp} TO {end_stamp}]"
        query = quote_plus(query_text)
        status["queries"].append(query_text)
        start = 0
        category_count_before = len(papers)
        while True:
            url = f"https://export.arxiv.org/api/query?search_query={query}&sortBy=submittedDate&sortOrder=descending&start={start}&max_results={page_size}"
            try:
                text = _request_arxiv_page(url).text
                root = ET.fromstring(text)
            except Exception as exc:
                status["errors"].append(f"{category} start={start}: {exc}")
                break
            status["pages_fetched"] += 1
            ns = {"a": "http://www.w3.org/2005/Atom"}
            entries = root.findall("a:entry", ns)
            if not entries:
                break
            for entry in entries:
                published = (entry.findtext("a:published", default="", namespaces=ns) or "")[:10]
                updated = (entry.findtext("a:updated", default="", namespaces=ns) or "")[:10]
                if not _in_date_range(published, start_date, end_date):
                    continue
                title = " ".join((entry.findtext("a:title", default="", namespaces=ns) or "").split())
                abstract = " ".join((entry.findtext("a:summary", default="", namespaces=ns) or "").split())
                entry_id = entry.findtext("a:id", default="", namespaces=ns) or ""
                arxiv_id = _arxiv_entry_id(entry_id)
                key = arxiv_id or title.lower()
                paper = by_key.get(key)
                if paper:
                    categories_seen = paper.setdefault("categories", [paper.get("category", "")])
                    if category not in categories_seen:
                        categories_seen.append(category)
                    paper.setdefault("metadata", {})["all_categories"] = categories_seen
                    continue
                pdf_url = entry_id.replace("/abs/", "/pdf/") if "/abs/" in entry_id else ""
                all_categories = [category]
                paper = {
                    "id": stable_id("paper", entry_id or title),
                    "source": "arxiv",
                    "arxiv_id": arxiv_id,
                    "title": title,
                    "authors": ", ".join(author.findtext("a:name", default="", namespaces=ns) or "" for author in entry.findall("a:author", ns)),
                    "abstract": abstract,
                    "url": entry_id,
                    "pdf_url": pdf_url,
                    "venue": "arXiv",
                    "year": int(published[:4]) if published[:4].isdigit() else date.today().year,
                    "category": category,
                    "categories": all_categories,
                    "classification_source": "llm_inferred",
                    "metadata": {"published": published, "updated": updated, "arxiv_category": category, "primary_category": category, "all_categories": all_categories},
                }
                _arxiv_add_or_merge_paper(by_key, papers, paper, category)
                if len(papers) - category_count_before >= max_items:
                    break
            if len(papers) - category_count_before >= max_items:
                break
            if len(entries) < page_size:
                break
            start += page_size
            time.sleep(0.5)
        if len(papers) == category_count_before:
            fallback_pages, fallback_errors, fallback_limited = _fetch_arxiv_recent_fallback(category, max_items, start_date, end_date, by_key, papers)
            if fallback_pages:
                status["fallback_used"] = True
                status["fallback_pages_fetched"] += fallback_pages
                status["limited"] = status["limited"] or fallback_limited
            status["errors"].extend(fallback_errors)
        time.sleep(0.5)
    status["count"] = len(papers)
    status["deduped_count"] = len(papers)
    status["ok"] = bool(papers)
    if papers:
        suffix = "; used arxiv.org recent fallback" if status["fallback_used"] else ""
        status["message"] = f"ok{suffix}; queries={'; '.join(status['queries'])}"
    elif status["errors"]:
        status["message"] = "arXiv unavailable or query failed: " + " | ".join(status["errors"][:3])
    else:
        status["message"] = f"No arXiv papers found; queries={'; '.join(status['queries'])}"
    return papers, status


def _biorxiv_default_start_date() -> str:
    return (date.today() - timedelta(days=30)).isoformat()


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


def fetch_biorxiv(categories: list[str], max_items: int, start_date: str = "", end_date: str = "") -> tuple[list[dict], dict]:
    papers: list[dict] = []
    by_key: dict[str, dict] = {}
    start_date = normalize_date(start_date) or _biorxiv_default_start_date()
    end_date = normalize_date(end_date) or date.today().isoformat()
    categories = [category.strip() for category in (categories or []) if category.strip()] or ["bioinformatics"]
    max_items = max(1, int(max_items or 100))
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
    }
    cursor = 0
    while len(papers) < max_items:
        url = f"https://api.biorxiv.org/details/biorxiv/{start_date}/{end_date}/{cursor}/json"
        try:
            response = _request(url, timeout=20)
            data = response.json()
        except Exception as exc:
            status["errors"].append(f"cursor={cursor}: {exc}")
            break
        status["pages_fetched"] += 1
        records = data.get("collection") if isinstance(data, dict) else []
        if not isinstance(records, list) or not records:
            break
        status["raw_count"] += len(records)
        for record in records:
            if not isinstance(record, dict):
                continue
            published = normalize_date(str(record.get("date") or ""))
            if not _in_date_range(published, start_date, end_date):
                continue
            category = str(record.get("category") or "").strip()
            if not _biorxiv_category_matches(category, categories):
                continue
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
                },
            }
            by_key[key] = paper
            papers.append(paper)
            if len(papers) >= max_items:
                status["limited"] = True
                break
        if len(records) < 100:
            break
        cursor += len(records)
        time.sleep(0.5)
    status["count"] = len(papers)
    status["deduped_count"] = len(papers)
    status["ok"] = bool(papers)
    if papers:
        limit_message = "limited by max_items" if status["limited"] else "fetched available pages"
        status["message"] = f"ok; {limit_message}; queries={'; '.join(status['queries'])}"
    elif status["errors"]:
        status["message"] = "bioRxiv unavailable or query failed: " + " | ".join(status["errors"][:3])
    else:
        status["message"] = f"No bioRxiv papers found; queries={'; '.join(status['queries'])}"
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
        except Exception:
            status["message"] = "HuggingFace daily papers unavailable."
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
        except Exception:
            status["message"] = "HuggingFace models unavailable."
    status["count"] = len(papers) + len(models)
    status["ok"] = status["count"] > 0
    if status["ok"] and not status["message"]:
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
            star_link = article.select_one("a[href$='/stargazers']")
            if star_link:
                try:
                    stars = int(star_link.get_text(strip=True).replace(",", ""))
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
