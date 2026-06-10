#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from project_paths import build_paths


SECTIONS = [
    ("Overview", ["overview.md"]),
    ("Papers", ["papers"]),
    ("Concepts", ["concepts"]),
    ("Entities", ["entities"]),
    ("Comparisons", ["comparisons"]),
    ("Gaps", ["gaps"]),
    ("Synthesis", ["synthesis"]),
]


def one_line_summary(path: Path) -> str:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    for line in lines:
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        return line[:180]
    return ""


def gather_markdown(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(path.rglob("*.md"))
    return []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--log-entry")
    args = parser.parse_args()

    paths = build_paths(args.project)
    lines = ["# Research Wiki Index\n\n"]
    for title, rels in SECTIONS:
        lines.append(f"## {title}\n")
        for rel in rels:
            target = paths.wiki / rel
            for path in gather_markdown(target):
                relpath = path.relative_to(paths.wiki)
                summary = one_line_summary(path)
                lines.append(f"- [[{path.stem}]] `{relpath}` {summary}\n")
        lines.append("\n")
    paths.wiki_index.write_text("".join(lines), encoding="utf-8")

    if args.log_entry:
        with paths.wiki_log.open("a", encoding="utf-8") as fh:
            fh.write(f"- {dt.datetime.now(dt.timezone.utc).isoformat()} {args.log_entry}\n")

    print(paths.wiki_index)


if __name__ == "__main__":
    main()
