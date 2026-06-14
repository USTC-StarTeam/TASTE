#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from literature_policy import now_utc, score_paper
from project_paths import ROOT, build_paths, load_project_config

import sys
from taste_pythonpath import ensure_taste_pythonpath
ensure_taste_pythonpath(ROOT)
from auto_research.source_selection import canonical_source_selection, paper_source_allowed


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--authors", default="")
    parser.add_argument("--published", default="")
    parser.add_argument("--categories", default="")
    parser.add_argument("--abs-url", default="")
    parser.add_argument("--pdf-url", default="")
    parser.add_argument("--summary", default="")
    parser.add_argument("--source", default="manual")
    parser.add_argument("--venue", default="")
    parser.add_argument("--journal", default="")
    parser.add_argument("--citations", default="")
    parser.add_argument("--influential-citations", default="")
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    item = {
        "source": args.source,
        "paper_id": args.paper_id,
        "entry_id": args.abs_url or args.paper_id,
        "title": args.title,
        "summary": args.summary,
        "published": args.published,
        "updated": args.published,
        "authors": [x.strip() for x in args.authors.split(",") if x.strip()],
        "categories": [x.strip() for x in args.categories.split(",") if x.strip()],
        "pdf_url": args.pdf_url,
        "abs_url": args.abs_url,
        "citations": args.citations or None,
        "influential_citations": args.influential_citations or None,
        "tldr": None,
        "venue": args.venue,
        "journal": args.journal,
    }
    selection = canonical_source_selection(project_config_path=paths.config)
    if not paper_source_allowed(item, selection):
        print("source disabled by canonical source selection; import skipped")
        return

    item.update(score_paper(item, cfg, reference_time=now_utc()))

    paper_dir = paths.raw_papers / args.paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / "metadata.json").write_text(json.dumps(item, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (paper_dir / "source.md").write_text(
        f"# {args.title}\n\n"
        f"- source: {args.source}\n"
        f"- paper_id: `{args.paper_id}`\n"
        f"- authors: {args.authors}\n"
        f"- published: {args.published}\n"
        f"- venue: {args.venue}\n"
        f"- journal: {args.journal}\n"
        f"- categories: {args.categories}\n"
        f"- abs: {args.abs_url}\n"
        f"- pdf: {args.pdf_url}\n"
        f"- citations: {args.citations}\n"
        f"- selection_bucket: {item.get('selection_bucket', '')}\n"
        f"- discovery_priority_score: {item.get('discovery_priority_score', '')}\n\n"
        "## Abstract\n\n"
        f"{args.summary}\n",
        encoding="utf-8",
    )

    ingested = load_json(paths.state / "ingested_ids.json")
    if args.paper_id not in ingested:
        ingested.append(args.paper_id)
        save_json(paths.state / "ingested_ids.json", ingested)
    print(paper_dir)


if __name__ == "__main__":
    main()
