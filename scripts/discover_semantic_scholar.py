#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

from literature_policy import now_utc, score_paper
from project_paths import build_paths, load_project_config

API = "https://api.semanticscholar.org/graph/v1/paper/search"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--query")
    parser.add_argument("--limit", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    ss_cfg = cfg.get("discovery", {}).get("semantic_scholar", {})
    query = args.query or cfg.get("queries", [cfg.get("topic", "research")])[0]
    limit = args.limit or ss_cfg.get("limit", 5)
    fields = ",".join(ss_cfg.get("fields", [
        "title", "year", "authors", "url", "externalIds", "citationCount",
        "influentialCitationCount", "tldr", "venue", "journal", "publicationVenue",
        "publicationDate", "openAccessPdf",
    ]))

    params = urllib.parse.urlencode({"query": query, "limit": limit, "fields": fields})
    url = f"{API}?{params}"
    reference_time = now_utc()
    ts = reference_time.strftime("%Y%m%dT%H%M%SZ")
    out = paths.discover / f"{ts}_semantic_schol{'_'.join(query.lower().split())[:80]}.json"

    payload: dict[str, object] = {
        "generated_at": ts,
        "reference_time": reference_time.isoformat(),
        "project": args.project,
        "source": "semantic_scholar",
        "query": query,
        "items": [],
    }
    headers = {"User-Agent": os.environ.get("SEMANTIC_SCHOLAR_USER_AGENT", "research-workflow/0.2")}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    if api_key:
        headers["x-api-key"] = api_key
    retries = int(os.environ.get("SEMANTIC_SCHOLAR_RETRIES", "2"))
    try:
        raw = None
        for attempt in range(retries + 1):
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=60) as response:
                    raw = json.loads(response.read().decode("utf-8", "ignore"))
                break
            except HTTPError as exc:
                if exc.code != 429 or attempt >= retries:
                    raise
                sleep_sec = min(90, 15 * (attempt + 1) + random.uniform(1, 5))
                print(f"semantic scholar rate limited, retrying in {sleep_sec:.1f}s", file=sys.stderr)
                time.sleep(sleep_sec)
        if raw is None:
            raw = {"data": []}
        items = []
        for item in raw.get("data", []) or []:
            publication_date = item.get("publicationDate") or str(item.get("year") or "")
            journal = item.get("journal") or {}
            if isinstance(journal, dict):
                journal_name = journal.get("name", "")
            else:
                journal_name = str(journal or "")
            metadata: dict[str, object] = {
                "source": "semantic_scholar",
                "paper_id": (item.get("externalIds") or {}).get("ArXiv") or item.get("paperId") or item.get("url") or "unknown",
                "entry_id": item.get("paperId") or item.get("url") or "",
                "title": item.get("title") or "",
                "summary": (item.get("tldr") or {}).get("text") if isinstance(item.get("tldr"), dict) else "",
                "published": publication_date,
                "updated": publication_date,
                "authors": [author.get("name", "") for author in item.get("authors", [])],
                "categories": [],
                "pdf_url": ((item.get("openAccessPdf") or {}) if isinstance(item.get("openAccessPdf"), dict) else {}).get("url", ""),
                "abs_url": item.get("url") or "",
                "citations": item.get("citationCount"),
                "influential_citations": item.get("influentialCitationCount"),
                "tldr": (item.get("tldr") or {}).get("text") if isinstance(item.get("tldr"), dict) else None,
                "venue": item.get("venue") or "",
                "journal": journal_name,
                "publicationVenue": item.get("publicationVenue") if isinstance(item.get("publicationVenue"), dict) else {},
                "query": query,
            }
            metadata.update(score_paper(metadata, cfg, reference_time=reference_time))
            items.append(metadata)
        payload["items"] = items
        payload["status"] = "ok"
        payload["literature_policy"] = items[0].get('literature_policy', {}) if items else cfg.get('literature', {})
    except (HTTPError, URLError, TimeoutError) as exc:
        payload["status"] = "unavailable"
        payload["error"] = str(exc)

    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(out)
    print(f"items={len(payload['items'])}")
    print(f"status={payload.get('status')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
