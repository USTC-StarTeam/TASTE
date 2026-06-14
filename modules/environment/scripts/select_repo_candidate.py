#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from project_paths import build_paths


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--name')
    args = parser.parse_args()

    paths = build_paths(args.project)
    rows = load_json(paths.state / 'repo_candidates.json')
    if not rows:
        raise SystemExit('No repo candidates registered')

    if args.name:
        matches = [row for row in rows if row.get('name') == args.name]
        if not matches:
            raise SystemExit(f'No repo candidate named {args.name}')
        winner = matches[0]
    else:
        winner = sorted(rows, key=lambda x: (-int(x.get('score', 0)), x.get('name', '')))[0]

    out = paths.repos_selected / 'selected_repo.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        '# Selected Repo\n\n'
        f"- name: {winner.get('name', '')}\n"
        f"- url: {winner.get('url', '')}\n"
        f"- score: {winner.get('score', 0)}\n"
        f"- summary: {winner.get('summary', '')}\n"
        f"- notes: {winner.get('notes', '')}\n",
        encoding='utf-8',
    )
    print(out)


if __name__ == '__main__':
    main()
