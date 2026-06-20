from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from sources.common import _clean_text, _in_date_range, normalize_date, stable_id


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
    deduped: list[str] = []
    for query in queries:
        if query and query not in deduped:
            deduped.append(query)
    return deduped


def _arxiv_entry_authors(entry: ET.Element, ns: dict[str, str]) -> list[str]:
    return [node.text or "" for node in entry.findall("a:author/a:name", ns)]


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


def _arxiv_entry_id(entry_id: str) -> str:
    text = (entry_id or "").rstrip("/")
    if "/abs/" in text:
        text = text.rsplit("/abs/", 1)[1]
    if "/pdf/" in text:
        text = text.rsplit("/pdf/", 1)[1]
    return re.sub(r"\.pdf$", "", text)


def _arxiv_fallback_queries(categories: list[str], start_date: str = "", end_date: str = "") -> list[tuple[str, str]]:
    queries = [(category, f"cat:{category}") for category in ([c.strip() for c in categories if c.strip()] or ["cs.AI"])]
    if start_date or end_date:
        start_stamp = (start_date or "1991-01-01").replace("-", "") + "0000"
        end_stamp = (end_date or "3000-01-01").replace("-", "") + "2359"
        queries = [(label, f"{query_text} AND submittedDate:[{start_stamp} TO {end_stamp}]") for label, query_text in queries]
    return queries


def _arxiv_search_queries(categories: list[str], topic_queries: list[str], start_date: str = "", end_date: str = "") -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    seen: set[str] = set()
    cleaned_topics = [" ".join(str(query).split()) for query in (topic_queries or []) if str(query).strip()]
    cleaned_categories = [category.strip() for category in (categories or []) if category.strip()] or ["cs.AI"]
    for topic in cleaned_topics:
        terms = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", topic)[:8]
        topic_expr = " AND ".join(f"all:{term}" for term in terms)
        if not topic_expr:
            continue
        category_expr = " OR ".join(f"cat:{category}" for category in cleaned_categories)
        query_text = f"({topic_expr}) AND ({category_expr})" if category_expr else topic_expr
        if query_text not in seen:
            queries.append((f"topic:{topic}", query_text))
            seen.add(query_text)
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


def _append_arxiv_entry(papers: list[dict], by_key: dict[str, dict], entry, ns: dict, query_label: str, query_text: str, start_date: str, end_date: str, *, fallback_query: bool = False) -> None:
    published = (entry.findtext("a:published", default="", namespaces=ns) or "")[:10]
    updated = (entry.findtext("a:updated", default="", namespaces=ns) or "")[:10]
    if not _in_date_range(published, start_date, end_date):
        return
    title = " ".join((entry.findtext("a:title", default="", namespaces=ns) or "").split())
    abstract = " ".join((entry.findtext("a:summary", default="", namespaces=ns) or "").split())
    entry_id = entry.findtext("a:id", default="", namespaces=ns) or ""
    arxiv_id = _arxiv_entry_id(entry_id)
    key = arxiv_id or title.lower()
    if not key:
        return
    existing = by_key.get(key)
    if existing:
        categories_seen = existing.setdefault("categories", [existing.get("category", "")])
        category_name = str(query_label).replace("topic:", "")
        if category_name not in categories_seen:
            categories_seen.append(category_name)
        existing.setdefault("metadata", {})["all_categories"] = categories_seen
        return
    pdf_url = entry_id.replace("/abs/", "/pdf/") if "/abs/" in entry_id else ""
    all_categories = [str(query_label).replace("topic:", "")]
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
        "category": query_label,
        "categories": all_categories,
        "classification_source": "llm_inferred",
        "metadata": {"published": published, "updated": updated, "arxiv_query": query_text, "arxiv_query_label": query_label, "fallback_query": fallback_query, "all_categories": all_categories},
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
