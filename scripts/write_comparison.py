#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from project_paths import build_paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--topic', required=True)
    parser.add_argument('--content', required=True)
    args = parser.parse_args()

    paths = build_paths(args.project)
    slug = args.topic.lower().replace(' ', '-').replace('/', '-')
    out = paths.wiki_comparisons / f'{slug}-comparison.md'
    out.write_text(
        f"# {args.topic} Comparison\n\n"
        f"{args.content}\n",
        encoding='utf-8',
    )
    print(out)


if __name__ == '__main__':
    main()
