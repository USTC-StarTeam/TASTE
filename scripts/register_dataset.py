#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from project_paths import build_paths


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def readiness_score(item: dict[str, object]) -> int:
    score = 0
    score += 2 if item.get("access") in {"public", "requestable"} else 0
    score += 2 if item.get("format") else 0
    score += 2 if item.get("split") else 0
    score += 2 if item.get("metric") else 0
    score += 2 if item.get("available") else 0
    score += 1 if item.get("download_tested") else 0
    score += 1 if item.get("notes") else 0
    return score


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--access", default="unknown")
    parser.add_argument("--format", default="")
    parser.add_argument("--split", default="")
    parser.add_argument("--metric", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--notes", default="")
    parser.add_argument("--available", action="store_true")
    parser.add_argument("--download-tested", action="store_true")
    args = parser.parse_args()

    paths = build_paths(args.project)
    registry_path = paths.state / "dataset_registry.json"
    rows = load_json(registry_path)
    item = {
        "name": args.name,
        "task": args.task,
        "access": args.access,
        "format": args.format,
        "split": args.split,
        "metric": args.metric,
        "url": args.url,
        "notes": args.notes,
        "available": args.available,
        "download_tested": args.download_tested,
    }
    item["readiness_score"] = readiness_score(item)
    rows = [row for row in rows if row.get("name") != args.name]
    rows.append(item)
    rows.sort(key=lambda x: (-int(x.get("readiness_score", 0)), x.get("name", "")))
    save_json(registry_path, rows)

    md = ["# Dataset Registry\n\n", "| Score | Name | Task | Access | Format | Split | Metric | Available | Notes |\n", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"]
    for row in rows:
        md.append(
            f"| {row.get('readiness_score', 0)} | {row.get('name', '')} | {row.get('task', '')} | {row.get('access', '')} | {row.get('format', '')} | {row.get('split', '')} | {row.get('metric', '')} | {row.get('available', False)} | {row.get('notes', '')} |\n"
        )
    (paths.reports / "dataset_registry.md").write_text("".join(md), encoding="utf-8")
    print(paths.reports / "dataset_registry.md")


if __name__ == "__main__":
    main()
