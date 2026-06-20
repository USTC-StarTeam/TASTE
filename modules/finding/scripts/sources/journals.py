from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from sources.common import _clean_text, normalize_date, stable_id
from sources.parsing import _looks_like_paper_title


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
