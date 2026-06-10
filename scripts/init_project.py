#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from project_paths import build_paths

PROJECT_AGENTS_TEMPLATE = """# Project AGENTS

This file applies to the entire project directory tree rooted here.

## Role
You are the disciplined research maintainer of this project knowledge base and experiment loop.
Your job is to turn `raw/` materials, repo audits, datasets, experiments, and paper artifacts into structured knowledge that helps discover research gaps, strong experiments, and high-taste AI paper directions.
You write `wiki/`, `planning/`, `reports/`, `experiments/`, and `paper/`. Raw source materials are append-only.
The workflow should remain backend-neutral: generic LLM execution is the baseline, while Codex or other compatible runners may be used when available.

## Directory Contract
- `raw/papers/`: source paper markdown, read-only
- `raw/notes/`: user notes and clips, read-only unless explicitly imported
- `wiki/papers/`: paper summary pages
- `wiki/concepts/`: cross-paper method/theory pages
- `wiki/entities/`: authors, datasets, systems, benchmarks
- `wiki/comparisons/`: high-value comparison pages written back from good queries
- `wiki/gaps/`: confirmed gaps, hypotheses, open questions
- `wiki/synthesis/`: field map, shared assumptions, discussion notes
- `planning/`: research plan, init brief, next actions, quality assessment
- `experiments/`: experiment log and trial reasoning
- `paper/drafts/`: Markdown paper drafts
- `paper/reviews/`: paper review packets and critique outputs
- `paper/venues/`: downloaded venue templates and metadata
- `paper/output/`: generated TeX, compile logs, and PDFs
- `reports/`: machine profile, lint, healthcheck, failure analysis, shared context

## Research Quality Rules
- Adapt environments to the currently detected machine profile; never assume a fixed GPU type or count.
- Prefer modifying strong existing repos over rebuilding from scratch.
- Explore several promising methods in parallel when resources allow.
- Do not stop after one failed run: inspect hyperparameters, module interactions, implementation bugs, and bad cases.
- But also do not cling to a weak method forever; compare and prune.
- Use git to track core code/config versions, not bulky artifacts.
- Do not write final paper claims that outrun the current evidence.

## Ingest Rules
1. Read `raw/papers/<id>/source.md` and metadata.
2. Summarize the core claim in your own words.
3. Record assumptions, limitations, contradictions, and concrete gap clues.
4. Update related concept/entity pages.
5. Update `wiki/gaps/questions.md` when new open questions appear.
6. Refresh `wiki/index.md` and append to `wiki/log.md`.

## Query Rules
- Read `wiki/index.md` first, then navigate relevant pages.
- Valuable comparison answers go to `wiki/comparisons/`.
- Valuable synthesis answers go to `wiki/synthesis/`.
- Good discussion outputs should be written back rather than lost in chat.

## Lint Rules
Check for:
- orphan pages
- broken or stale claims
- explicit contradictions
- high-frequency concepts missing concept pages
- draft hypotheses that should be upgraded or rejected
- questions that now have evidence

## Idea Generation Rules
When discussing research ideas:
1. Read `wiki/synthesis/shared-assumptions.md`.
2. Read `wiki/gaps/confirmed-gaps.md` and `wiki/gaps/questions.md`.
3. Propose 2-3 hypotheses with assumptions, strongest counterarguments, and minimal decisive experiments.
4. Reject ordinary ideas directly.
5. Write useful outputs to `wiki/gaps/hypotheses.md` or `wiki/synthesis/discussion-<date>.md`.
"""


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def ensure_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    paths = build_paths(args.project)
    paper_dirs = [
        paths.root / 'paper',
        paths.root / 'paper' / 'drafts',
        paths.root / 'paper' / 'reviews',
        paths.root / 'paper' / 'reviews' / 'internal',
        paths.root / 'paper' / 'venues',
        paths.root / 'paper' / 'output',
        paths.root / 'paper' / 'metadata',
    ]
    for path in [
        paths.discover, paths.raw_papers, paths.raw_notes, paths.raw_assets,
        paths.wiki_papers, paths.wiki_concepts, paths.wiki_entities, paths.wiki_comparisons,
        paths.wiki_gaps, paths.wiki_synthesis, paths.reports, paths.logs, paths.state,
        paths.obsidian, paths.planning, paths.experiments, paths.artifacts,
        paths.repos_candidates, paths.repos_selected, paths.datasets_registry,
        paths.datasets_notes, paths.benchmarks, *paper_dirs,
    ]:
        path.mkdir(parents=True, exist_ok=True)

    defaults = {
        paths.state / 'seen_ids.json': [],
        paths.state / 'ingested_ids.json': [],
        paths.state / 'compiled_ids.json': [],
        paths.state / 'loop_history.json': [],
        paths.state / 'repo_candidates.json': [],
        paths.state / 'dataset_registry.json': [],
        paths.state / 'experiment_registry.json': [],
        paths.state / 'natural_language_requests.json': [],
    }
    for path, payload in defaults.items():
        if not path.exists():
            save_json(path, payload)

    ensure_text(paths.agents_file, PROJECT_AGENTS_TEMPLATE)
    ensure_text(paths.wiki_index, '# Research Wiki Index\n\n')
    ensure_text(paths.wiki_log, '# Research Wiki Log\n\n')
    ensure_text(paths.wiki_overview, '# Overview\n\nStatus: draft\n\nThis page should track the field-level landscape and what currently matters.\n')
    ensure_text(paths.wiki_gaps / 'confirmed-gaps.md', '# Confirmed Gaps\n\n')
    ensure_text(paths.wiki_gaps / 'hypotheses.md', '# Hypotheses\n\n')
    ensure_text(paths.wiki_gaps / 'questions.md', '# Open Questions\n\n')
    ensure_text(paths.wiki_synthesis / 'field-map.md', '# Field Map\n\n')
    ensure_text(paths.wiki_synthesis / 'shared-assumptions.md', '# Shared Assumptions\n\n')
    ensure_text(paths.planning / 'research_plan.md', '# Research Plan\n\n## Goal\n\n## Hypotheses\n\n## Candidate repos\n\n## Datasets\n\n## Risks\n')
    ensure_text(paths.planning / 'init_brief.md', '# Initialization Brief\n\n')
    ensure_text(paths.planning / 'paper_quality.md', '# Paper Quality Assessment\n\n')
    ensure_text(paths.planning / 'next_actions.md', '# Next Actions\n\n')
    ensure_text(paths.experiments / 'experiment_log.md', '# Experiment Log\n\n')
    ensure_text(paths.benchmarks / 'benchmark_matrix.md', '# Benchmark Matrix\n\n| Benchmark | Split | Metric | Available | Notes |\n| --- | --- | --- | --- | --- |\n')
    ensure_text(paths.root / 'paper' / 'drafts' / 'paper_draft.md', '# Paper Draft\n\n')
    ensure_text(paths.root / 'paper' / 'drafts' / 'paper_revision.md', '# Paper Revision\n\n')
    ensure_text(paths.root / 'paper' / 'reviews' / 'paper_review_packet.md', '# Paper Review Packet\n\n')
    ensure_text(paths.root / 'paper' / 'reviews' / 'aggregated_review.md', '# Aggregated Internal Review\n\n')
    print(paths.root)


if __name__ == '__main__':
    main()
