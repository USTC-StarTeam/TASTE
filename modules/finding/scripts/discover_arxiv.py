#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from pathlib import Path

from literature_policy import now_utc, score_paper
from project_paths import build_paths, load_project_config

from taste_pythonpath import ensure_taste_pythonpath
ensure_taste_pythonpath(Path(__file__).resolve().parents[1])
from auto_research.source_selection import canonical_source_selection, source_enabled

API = "https://export.arxiv.org/api/query"
NS = {"atom": "http://www.w3.org/2005/Atom"}
DEFAULT_USER_AGENT = "research-workflow/0.2"


def safe_discovery_slug(value: str, limit: int = 80) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "_", str(value or "").lower()).strip("._-")
    slug = slug[:limit].rstrip("._-")
    return slug or "query"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--query")
    parser.add_argument("--max-results", type=int)
    parser.add_argument("--sort-by")
    parser.add_argument("--sort-order")
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--ignore-source-selection", action="store_true")
    return parser.parse_args()


def build_arxiv_search_query(query: str, cfg: dict[str, object]) -> str:
    raw = (query or '').strip()
    discovery = cfg.get('discovery', {}) if isinstance(cfg.get('discovery', {}), dict) else {}
    arxiv_cfg = discovery.get('arxiv', {}) if isinstance(discovery.get('arxiv', {}), dict) else {}
    template = str(arxiv_cfg.get('search_query_template') or '').strip()
    if template:
        topic = str(cfg.get('topic', '') or '')
        queries = ' '.join(str(q) for q in cfg.get('queries', []) or [])
        return template.format(query=raw, topic=topic, queries=queries)
    if ':' in raw or raw.startswith('('):
        return raw
    return 'all:' + raw


def fetch(query: str, max_results: int, sort_by: str, sort_order: str, retries: int, cfg: dict[str, object]) -> str:
    params = {
        "search_query": build_arxiv_search_query(query, cfg),
        "start": 0,
        "max_results": max_results,
        "sortBy": sort_by,
        "sortOrder": sort_order,
    }
    url = API + "?" + urllib.parse.urlencode(params)
    user_agent = os.environ.get("ARXIV_USER_AGENT", DEFAULT_USER_AGENT)

    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        try:
            with urllib.request.urlopen(request, timeout=int(os.environ.get('ARXIV_TIMEOUT_SEC', '20'))) as response:
                return response.read().decode("utf-8", "ignore")
        except HTTPError as exc:
            if exc.code != 429 or attempt >= retries:
                raise
            sleep_sec = min(60, (2 ** attempt) * 5 + random.uniform(0.5, 2.0))
            print(f"arxiv rate limited, retrying in {sleep_sec:.1f}s", file=sys.stderr)
            time.sleep(sleep_sec)
        except URLError:
            if attempt >= retries:
                raise
            sleep_sec = min(30, (attempt + 1) * 3)
            print(f"temporary network error, retrying in {sleep_sec}s", file=sys.stderr)
            time.sleep(sleep_sec)
    raise RuntimeError("unreachable")


def parse_feed(xml_text: str, cfg: dict[str, object], query: str, reference_time: dt.datetime) -> list[dict[str, object]]:
    root = ET.fromstring(xml_text)
    items: list[dict[str, object]] = []
    for entry in root.findall("atom:entry", NS):
        entry_id = entry.findtext("atom:id", default="", namespaces=NS).strip()
        authors = [
            author.findtext("atom:name", default="", namespaces=NS).strip()
            for author in entry.findall("atom:author", NS)
        ]
        tags = [category.attrib.get("term", "") for category in entry.findall("atom:category", NS)]
        links = {
            link.attrib.get("title") or link.attrib.get("rel", "link"): link.attrib.get("href", "")
            for link in entry.findall("atom:link", NS)
        }
        paper_id = entry_id.rsplit("/", 1)[-1]
        item: dict[str, object] = {
            "source": "arxiv",
            "paper_id": paper_id,
            "entry_id": entry_id,
            "title": " ".join(entry.findtext("atom:title", default="", namespaces=NS).split()),
            "summary": " ".join(entry.findtext("atom:summary", default="", namespaces=NS).split()),
            "published": entry.findtext("atom:published", default="", namespaces=NS),
            "updated": entry.findtext("atom:updated", default="", namespaces=NS),
            "authors": authors,
            "categories": [tag for tag in tags if tag],
            "pdf_url": links.get("pdf") or f"https://arxiv.org/pdf/{paper_id}.pdf",
            "abs_url": entry_id,
            "citations": None,
            "influential_citations": None,
            "tldr": None,
            "query": query,
        }
        item.update(score_paper(item, cfg, reference_time=reference_time))
        items.append(item)
    return items


def main() -> int:
    args = parse_args()
    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    arxiv_cfg = cfg.get("discovery", {}).get("arxiv", {})
    query = args.query or cfg.get("queries", [cfg.get("topic", "research")])[0]
    max_results = args.max_results or arxiv_cfg.get("max_results", 5)
    sort_by = args.sort_by or arxiv_cfg.get("sort_by", "submittedDate")
    sort_order = args.sort_order or arxiv_cfg.get("sort_order", "descending")
    selection = canonical_source_selection(project_config_path=paths.config)
    if not args.ignore_source_selection and not source_enabled(selection, "arxiv"):
        print("arxiv discovery skipped by canonical source selection")
        return 0

    reference_time = now_utc()

    xml_text = fetch(query, max_results, sort_by, sort_order, args.retries, cfg)
    items = parse_feed(xml_text, cfg, query, reference_time)
    ts = reference_time.strftime("%Y%m%dT%H%M%SZ")
    slug = safe_discovery_slug(query)
    out = paths.discover / f"{ts}_arxiv_{slug}.json"
    payload = {
        "generated_at": ts,
        "reference_time": reference_time.isoformat(),
        "project": args.project,
        "source": "arxiv",
        "query": query,
        "arxiv_search_query": build_arxiv_search_query(query, cfg),
        "max_results": max_results,
        "literature_policy": items[0].get('literature_policy', {}) if items else cfg.get('literature', {}),
        "items": items,
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(out)
    print(f"items={len(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
