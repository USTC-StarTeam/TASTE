from __future__ import annotations

import hashlib
import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from urllib.parse import quote_plus, urlencode

import requests
from bs4 import BeautifulSoup

from auto_research.paths import REFERENCE_ROOT


HEADERS = {
    "User-Agent": "TASTE/0.1 (+local research assistant)"
}


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _clean_text(value: str) -> str:
    return " ".join((value or "").split())


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


def fetch_dblp_stream_api(venue: dict, years: list[int], max_items: int) -> list[dict]:
    stream_id = _dblp_stream_id(venue.get("address", ""))
    if not stream_id:
        return []
    wanted = {str(year) for year in years}
    try:
        response = requests.get(
            "http://dblp.org/search/publ/api",
            params={"q": f"stream:streams/{stream_id}:", "h": max(100, max_items * 20), "format": "json"},
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
        if len(papers) >= max_items:
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


def _openreview_venue_ids(venue: dict, year: int) -> list[str]:
    venue_ids = []
    for pattern in _openreview_patterns_for_venue(venue):
        venue_ids.append(pattern.format(year=year) if "{year}" in pattern else pattern)
    return list(dict.fromkeys(venue_ids))


def fetch_openreview_venue(venue: dict, years: list[int], max_items: int) -> list[dict]:
    papers: list[dict] = []
    queried_venue_ids: set[str] = set()
    for year in years:
        venue_ids = _openreview_venue_ids(venue, year)
        for venue_id in venue_ids:
            if venue_id in queried_venue_ids:
                continue
            queried_venue_ids.add(venue_id)
            notes = []
            try:
                response = requests.get(
                    "https://api2.openreview.net/notes",
                    params={"content.venueid": venue_id, "details": "replyCount,invitation,original", "limit": max_items},
                    headers=HEADERS,
                    timeout=12,
                )
                response.raise_for_status()
                notes = response.json().get("notes", [])
            except Exception:
                notes = []
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
                    "category": "",
                    "classification_source": "llm_inferred",
                    "metadata": {"venue_id": venue.get("id"), "openreview_venueid": venue_id},
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
        match = re.search(r"\b(20\d{2}|19\d{2})\b", text)
        if not match or match.group(1) not in wanted:
            continue
        href = anchor["href"]
        if href.startswith("http") and "dblp" not in href:
            continue
        if not href.startswith("http"):
            href = requests.compat.urljoin(address, href)
        href = _dblp_page_url(href)
        if "#" in href:
            continue
        if "/rec/conf/" in href:
            continue
        year = int(match.group(1))
        if (year, href) not in links:
            links.append((year, href))
        if len(links) >= max_years:
            break
    if not links:
        links = direct_links()
    return links


def fetch_dblp_venue(venue: dict, years: list[int], max_items: int) -> list[dict]:
    papers = fetch_dblp_stream_api(venue, years, max_items)
    if papers:
        return papers

    links = _parse_dblp_year_links(venue.get("address", ""), years)
    papers = []
    for year, url in links:
        url = _dblp_page_url(url)
        xml_url = re.sub(r"\.html?$", ".xml", url)
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
                if len(papers) >= max_items:
                    return papers
            if papers:
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
            if len(papers) >= max_items:
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
        "deduped_count": 0,
    }
    for category in categories:
        query_text = f"cat:{category}"
        if start_date or end_date:
            start_stamp = (start_date or "1991-01-01").replace("-", "") + "0000"
            end_stamp = (end_date or "3000-01-01").replace("-", "") + "2359"
            query_text = f"{query_text} AND submittedDate:[{start_stamp} TO {end_stamp}]"
        query = quote_plus(query_text)
        status["queries"].append(query_text)
        start = 0
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
                by_key[key] = paper
                papers.append(paper)
            if len(entries) < page_size:
                break
            start += page_size
            time.sleep(0.5)
        time.sleep(0.5)
    status["count"] = len(papers)
    status["deduped_count"] = len(papers)
    status["ok"] = bool(papers)
    if papers:
        status["message"] = f"ok; fetched all available pages; queries={'; '.join(status['queries'])}"
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
