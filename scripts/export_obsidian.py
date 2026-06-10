#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from project_paths import build_paths, load_project_config


def mirror_markdown(src_dir: Path, dst_dir: Path) -> None:
    if not src_dir.exists():
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    for path in src_dir.rglob("*.md"):
        rel = path.relative_to(src_dir)
        out = dst_dir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    args = parser.parse_args()

    paths = build_paths(args.project)
    cfg = load_project_config(args.project)

    vault = paths.obsidian
    mirror_markdown(paths.wiki, vault / "wiki")
    mirror_markdown(paths.wiki_gaps, vault / "wiki" / "gaps")
    mirror_markdown(paths.wiki_synthesis, vault / "wiki" / "synthesis")
    mirror_markdown(paths.reports, vault / "reports")
    mirror_markdown(paths.planning, vault / "planning")
    mirror_markdown(paths.experiments, vault / "experiments")
    mirror_markdown(paths.benchmarks, vault / "benchmarks")
    mirror_markdown(paths.repos_selected, vault / "repos" / "selected")

    overview = vault / "README.md"
    overview.write_text(
        f"# Obsidian Export: {cfg.get('name', args.project)}\n\n"
        f"- topic: {cfg.get('topic', '')}\n"
        f"- conda_env: {cfg.get('conda_env', '')}\n"
        "- mode: read-only mirror of generated research assets\n\n"
        "## Entry points\n"
        "- [wiki/index.md](wiki/index.md)\n"
        "- [wiki/overview.md](wiki/overview.md)\n"
        "- [wiki/synthesis/field-map.md](wiki/synthesis/field-map.md)\n"
        "- [wiki/synthesis/shared-assumptions.md](wiki/synthesis/shared-assumptions.md)\n"
        "- [wiki/gaps/confirmed-gaps.md](wiki/gaps/confirmed-gaps.md)\n"
        "- [planning/init_brief.md](planning/init_brief.md)\n"
        "- [planning/paper_quality.md](planning/paper_quality.md)\n"
        "- [experiments/experiment_log.md](experiments/experiment_log.md)\n",
        encoding="utf-8",
    )
    print(vault)


if __name__ == "__main__":
    main()
