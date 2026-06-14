#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from project_paths import build_paths


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    args = parser.parse_args()

    paths = build_paths(args.project)
    paper_ids = sorted([p.name for p in paths.raw_papers.iterdir() if p.is_dir()]) if paths.raw_papers.exists() else []
    save_json(paths.state / "ingested_ids.json", paper_ids)
    save_json(paths.state / "compiled_ids.json", sorted([p.stem for p in paths.wiki_papers.glob('*.md')]))
    print(f"papers={len(paper_ids)}")


if __name__ == "__main__":
    main()
