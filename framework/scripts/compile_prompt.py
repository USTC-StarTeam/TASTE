#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from project_paths import build_paths, load_project_config
from literature_policy import paper_sort_key, repo_sort_key


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def maybe_include(path: Path, title: str) -> str:
    if not path.exists():
        return ""
    return f"\n## {title}\n\n" + path.read_text(encoding="utf-8") + "\n"


def text_excerpt(value: str, limit: int = 700) -> str:
    cleaned = ' '.join(str(value or '').split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + '...'


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    compile_limit = int(cfg.get('loop', {}).get('compile_limit', 8) or 8)
    quality = load_json(paths.state / 'paper_quality.json') if (paths.state / 'paper_quality.json').exists() else {'papers': []}
    idea_path = paths.planning / 'finding' / 'ideas.json'
    ideas = load_json(idea_path) if idea_path.exists() else {'ideas': []}
    repos = load_json(paths.state / 'repo_candidates.json') if (paths.state / 'repo_candidates.json').exists() else []
    papers = quality.get('papers', []) if isinstance(quality, dict) else []
    papers = sorted(papers, key=paper_sort_key)
    repos = sorted(repos, key=repo_sort_key)

    shared = [
        "# Shared Research Context\n",
        f"Project: {cfg.get('name', args.project)}\n",
        f"Topic: {cfg.get('topic', 'research')}\n",
        f"Conda env: {cfg.get('conda_env', '') or 'auto-detect/project-specific'}\n",
        f"Literature reference time: {quality.get('reference_time', '') if isinstance(quality, dict) else ''}\n",
        "## Queries\n",
    ]
    for query in cfg.get("queries", []):
        shared.append(f"- {query}\n")

    if isinstance(quality, dict):
        policy = quality.get('literature_policy', {})
        summary = quality.get('summary', {})
        shared.extend([
            "\n## Literature Prioritization Policy\n",
            f"- primary_window_days: {policy.get('primary_window_days', '')}\n",
            f"- secondary_window_days: {policy.get('secondary_window_days', '')}\n",
            f"- deprioritize_older_than_days: {policy.get('deprioritize_older_than_days', '')}\n",
            f"- recent_high_priority_count: {summary.get('recent_high_priority_count', 0)}\n",
            f"- recent_candidate_count: {summary.get('recent_candidate_count', 0)}\n",
            f"- older_foundational_count: {summary.get('older_foundational_count', 0)}\n",
            "- reading rule: prefer recent top-tier papers first, then a few still-useful older anchors.\n",
            "- idea rule: arXiv freshness is useful, but GitHub/repo borrowability and dataset execution viability must affect what gets explored.\n",
        ])

    shared.append("\n## Selected Papers For Prompt Budget\n")
    if not papers:
        shared.append("- No imported papers yet.\n")
    else:
        for row in papers[:compile_limit]:
            shared.extend([
                f"### {row.get('paper_id', '')}\n",
                f"- title: {row.get('title', '')}\n",
                f"- source: {row.get('source', '')}\n",
                f"- top_tier_readiness: {row.get('top_tier_readiness', '')}\n",
                f"- selection_bucket: {row.get('selection_bucket', '')}\n",
                f"- recency_bucket: {row.get('recency_bucket', '')}\n",
                f"- paper_age_days: {row.get('paper_age_days', '')}\n",
                f"- venue_quality: {row.get('venue_quality', '')}\n",
                f"- venue_candidates: {', '.join(row.get('venue_candidates', []) or [])}\n",
                f"- discovery_priority_score: {row.get('discovery_priority_score', 0)}\n",
                f"- idea_worthiness_score: {row.get('idea_worthiness_score', 0)}\n",
                f"- actionability_score: {row.get('actionability_score', 0)}\n",
                f"- authors: {', '.join(row.get('authors', []) or [])}\n",
                f"- published: {row.get('published', '')}\n",
                f"- categories: {', '.join(row.get('categories', []) or [])}\n",
                f"- citations: {row.get('citations', '')}\n",
                f"- abstract: {text_excerpt(row.get('summary', '') or row.get('tldr', ''))}\n\n",
            ])

    shared.append("## Selected Code/Repo Signals\n")
    if not repos:
        shared.append("- No repo candidates yet.\n")
    else:
        for row in repos[: min(5, len(repos))]:
            shared.extend([
                f"### {row.get('name', '')}\n",
                f"- url: {row.get('url', '')}\n",
                f"- repo_reuse_score: {row.get('repo_reuse_score', row.get('score', 0))}\n",
                f"- repo_selection_bucket: {row.get('repo_selection_bucket', '')}\n",
                f"- activity_bucket: {row.get('activity_bucket', '')}\n",
                f"- stars: {row.get('stars', 0)}\n",
                f"- forks: {row.get('forks', 0)}\n",
                f"- repo_execution_ready: {row.get('repo_execution_ready', False)}\n",
                f"- summary: {row.get('summary', '')}\n\n",
            ])

    shared.append("## Best Idea Seeds\n")
    if not ideas.get('ideas'):
        shared.append("- No idea candidates synthesized yet.\n")
    else:
        for row in ideas.get('ideas', [])[: min(5, len(ideas.get('ideas', [])) )]:
            shared.extend([
                f"### {row.get('idea_id', '')}\n",
                f"- recommendation: {row.get('recommendation', '')}\n",
                f"- idea_score: {row.get('idea_score', 0)}\n",
                f"- paper_anchor: {row.get('paper_id', '')}\n",
                f"- repo_anchor: {row.get('repo_name', '')}\n",
                f"- dataset_anchor: {row.get('dataset_name', '')}\n",
                f"- risks: {'; '.join(row.get('main_risks', [])) or 'none'}\n\n",
            ])

    extras = [
        maybe_include(paths.wiki_overview, 'Overview'),
        maybe_include(paths.wiki_synthesis / 'field-map.md', 'Field Map'),
        maybe_include(paths.wiki_synthesis / 'shared-assumptions.md', 'Shared Assumptions'),
        maybe_include(paths.wiki_gaps / 'confirmed-gaps.md', 'Confirmed Gaps'),
        maybe_include(paths.wiki_gaps / 'hypotheses.md', 'Hypotheses'),
        maybe_include(paths.wiki_gaps / 'questions.md', 'Open Questions'),
        maybe_include(paths.reports / 'repo_candidates.md', 'Repo Candidates'),
        maybe_include(paths.reports / 'dataset_registry.md', 'Dataset Registry'),
        maybe_include(paths.planning / 'init_brief.md', 'Initialization Brief'),
        maybe_include(paths.planning / 'paper_quality.md', 'Paper Quality Assessment'),
        maybe_include(paths.experiments / 'experiment_log.md', 'Experiment Log'),
        maybe_include(paths.benchmarks / 'benchmark_matrix.md', 'Benchmark Matrix'),
        maybe_include(paths.reports / 'iteration_reflection.md', 'Iteration Reflection'),
        maybe_include(paths.planning / 'method_frontier.md', 'Method Frontier'),
        maybe_include(paths.planning / 'workflow_blueprint.md', 'Workflow Blueprint'),
        maybe_include(paths.reports / 'workflow_connectivity.md', 'Workflow Connectivity Audit'),
    ]
    shared.extend(extras)

    out = paths.reports / "shared_research.md"
    out.write_text("".join(shared), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
