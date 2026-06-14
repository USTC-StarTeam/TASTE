#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from literature_policy import now_utc, repo_sort_key, score_repo_candidate
from project_paths import build_paths, load_project_config


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--summary", default="")
    parser.add_argument("--task-fit", action="store_true")
    parser.add_argument("--recent-activity", action="store_true")
    parser.add_argument("--has-readme", action="store_true")
    parser.add_argument("--has-license", action="store_true")
    parser.add_argument("--has-install", action="store_true")
    parser.add_argument("--has-entrypoint", action="store_true")
    parser.add_argument("--has-tests", action="store_true")
    parser.add_argument("--has-dataset-docs", action="store_true")
    parser.add_argument("--stars", type=int, default=0)
    parser.add_argument("--forks", type=int, default=0)
    parser.add_argument("--last-pushed-at", default="")
    parser.add_argument("--topics", default="")
    parser.add_argument("--language", default="")
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    registry_path = paths.state / "repo_candidates.json"
    rows = load_json(registry_path)
    item = {
        "name": args.name,
        "url": args.url,
        "summary": args.summary,
        "task_fit": args.task_fit,
        "recent_activity": args.recent_activity,
        "has_readme": args.has_readme,
        "has_license": args.has_license,
        "has_install": args.has_install,
        "has_entrypoint": args.has_entrypoint,
        "has_tests": args.has_tests,
        "has_dataset_docs": args.has_dataset_docs,
        "stars": args.stars,
        "forks": args.forks,
        "last_pushed_at": args.last_pushed_at,
        "topics": [x.strip() for x in args.topics.split(',') if x.strip()],
        "language": args.language,
        "notes": args.notes,
        "updated_from": "manual_registration",
    }
    item.update(score_repo_candidate(item, cfg, reference_time=now_utc()))
    item["score"] = item.get('repo_reuse_score', 0)
    rows = [row for row in rows if row.get("name") != args.name]
    rows.append(item)
    rows.sort(key=repo_sort_key)
    save_json(registry_path, rows)

    md = ["# Repo Candidates\n\n", "| Score | Name | URL | Fit | Activity | Install | Entrypoint | Notes |\n", "| --- | --- | --- | --- | --- | --- | --- | --- |\n"]
    for row in rows:
        md.append(
            f"| {row.get('score', 0)} | {row.get('name', '')} | {row.get('url', '')} | {row.get('task_fit', False)} | {row.get('activity_bucket', row.get('recent_activity', False))} | {row.get('has_install', False)} | {row.get('has_entrypoint', False)} | {row.get('notes', '')} |\n"
        )
    (paths.reports / "repo_candidates.md").write_text("".join(md), encoding="utf-8")
    print(paths.reports / "repo_candidates.md")


if __name__ == "__main__":
    main()
