#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from literature_policy import build_literature_policy, dataset_readiness_component, idea_recommendation, normalize_label, paper_sort_key, repo_sort_key
from project_paths import build_paths, load_project_config


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def lexical_overlap(a: str, b: str) -> int:
    left = set(normalize_label(a).split())
    right = set(normalize_label(b).split())
    left.discard('')
    right.discard('')
    return len(left & right)


def repo_execution_ready(row: dict) -> bool:
    return bool(row.get('repo_execution_ready', bool(row.get('has_install') and row.get('has_entrypoint'))))


def repo_selection_bucket(row: dict) -> str:
    if row.get('repo_selection_bucket'):
        return str(row.get('repo_selection_bucket'))
    score = float(row.get('repo_reuse_score', row.get('score', 0)) or 0)
    if score >= 8:
        return 'promising'
    if score >= 6:
        return 'watch'
    return 'weak'


def build_idea_rows(cfg: dict, papers: list[dict], repos: list[dict], datasets: list[dict]) -> list[dict]:
    policy = build_literature_policy(cfg)
    eligible_papers = [
        row for row in papers
        if not row.get('not_positive_support') and row.get('selection_bucket') != 'deprioritized'
    ]
    top_papers = sorted(eligible_papers, key=paper_sort_key)[:5]
    top_repos = sorted(repos, key=repo_sort_key)[:5]
    top_datasets = [row for row in datasets if row.get('available')] or datasets[:2]
    if not top_datasets:
        top_datasets = [{'name': '', 'task': '', 'available': False, 'readiness_score': 0}]

    rows: list[dict] = []
    for paper in top_papers:
        for repo in top_repos or [{}]:
            for dataset in top_datasets:
                paper_title = str(paper.get('title', ''))
                repo_text = ' '.join([str(repo.get('name', '')), str(repo.get('summary', '')), ' '.join(repo.get('topics', []) or [])])
                dataset_text = ' '.join([str(dataset.get('name', '')), str(dataset.get('task', '')), str(dataset.get('metric', ''))])
                overlap = lexical_overlap(paper_title, repo_text) + lexical_overlap(str(cfg.get('topic', '')), repo_text)
                dataset_overlap = lexical_overlap(paper_title, dataset_text) + lexical_overlap(repo_text, dataset_text)
                paper_score = float(paper.get('idea_worthiness_score', paper.get('discovery_priority_score', 0)) or 0)
                repo_score = float(repo.get('repo_reuse_score', repo.get('score', 0)) or 0)
                dataset_score = dataset_readiness_component(dataset)
                exec_ready = repo_execution_ready(repo)
                execution_bonus = 2.0 if exec_ready else 0.0
                local_bonus = 1.0 if repo.get('local_path') else 0.0
                code_confidence_penalty = 1.5 if repo and not repo.get('local_path') and not repo.get('has_install') else 0.0
                weak_paper_penalty = 1.5 if paper.get('top_tier_readiness') == 'weak' else 0.0
                not_positive_penalty = 100.0 if paper.get('not_positive_support') else 0.0
                score = round((0.55 * paper_score) + (0.35 * repo_score) + dataset_score + execution_bonus + local_bonus + overlap + (0.5 * dataset_overlap) - code_confidence_penalty - weak_paper_penalty, 3)
                score = round(score - not_positive_penalty, 3)
                recommendation = idea_recommendation(score, policy)
                if paper.get('not_positive_support'):
                    recommendation = 'prune'
                elif paper.get('selection_bucket') == 'older_foundational' and recommendation == 'pursue' and not paper.get('claim_ready_anchor'):
                    recommendation = 'watch'
                rows.append({
                    'idea_id': f"idea_{paper.get('paper_id', 'paper')}_{normalize_label(str(repo.get('name', 'repo'))).replace(' ', '_') or 'repo'}_{normalize_label(str(dataset.get('name', 'dataset'))).replace(' ', '_') or 'dataset'}",
                    'paper_id': paper.get('paper_id', ''),
                    'paper_title': paper_title,
                    'paper_selection_bucket': paper.get('selection_bucket', ''),
                    'paper_top_tier_readiness': paper.get('top_tier_readiness', ''),
                    'paper_not_positive_support': bool(paper.get('not_positive_support')),
                    'paper_idea_worthiness_score': paper_score,
                    'repo_name': repo.get('name', ''),
                    'repo_url': repo.get('url', ''),
                    'repo_reuse_score': repo_score,
                    'repo_selection_bucket': repo_selection_bucket(repo),
                    'repo_execution_ready': exec_ready,
                    'dataset_name': dataset.get('name', ''),
                    'dataset_ready': dataset.get('available', False),
                    'dataset_readiness_component': dataset_score,
                    'lexical_overlap': overlap,
                    'dataset_overlap': dataset_overlap,
                    'idea_score': score,
                    'recommendation': recommendation,
                    'main_risks': [
                        risk for risk in [
                            'paper is not yet clearly top-tier-ready' if paper.get('top_tier_readiness') != 'promising' else '',
                            'paper was retained only as a critique/search-expansion signal, not positive support' if paper.get('not_positive_support') else '',
                            'paper is an older foundation/control; it needs fresh literature support before becoming a pursue-level idea anchor' if paper.get('selection_bucket') == 'older_foundational' else '',
                            'repo still lacks local audit / runnable proof' if repo and not repo.get('local_path') else '',
                            'repo execution path is still weak' if repo and not exec_ready else '',
                            'dataset is not marked available' if not dataset.get('available') else '',
                        ] if risk
                    ],
                })
    rows.sort(key=lambda row: (-float(row.get('idea_score', 0)), row.get('recommendation', ''), row.get('paper_id', '')))
    return rows[:12]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    paper_quality = load_json(paths.state / 'paper_quality.json', {'papers': []})
    repo_rows = load_json(paths.state / 'repo_candidates.json', [])
    dataset_rows = load_json(paths.state / 'dataset_registry.json', [])
    ideas = build_idea_rows(cfg, paper_quality.get('papers', []), repo_rows, dataset_rows)
    payload = {
        'generated_at': paper_quality.get('reference_time', ''),
        'project': args.project,
        'ideas': ideas,
        'summary': {
            'idea_count': len(ideas),
            'pursue_count': sum(1 for row in ideas if row.get('recommendation') == 'pursue'),
            'watch_count': sum(1 for row in ideas if row.get('recommendation') == 'watch'),
            'prune_count': sum(1 for row in ideas if row.get('recommendation') == 'prune'),
        },
    }
    (paths.state / 'idea_candidates.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    lines = ['# Idea Candidates\n\n']
    lines.extend([
        f"- idea_count: {payload['summary']['idea_count']}\n",
        f"- pursue_count: {payload['summary']['pursue_count']}\n",
        f"- watch_count: {payload['summary']['watch_count']}\n",
        f"- prune_count: {payload['summary']['prune_count']}\n\n",
        '## Interpretation Rule\n\n',
        '- A strong idea should combine a recent/high-signal paper direction, a borrowable or runnable codebase, and a dataset path that actually lets the loop test the claim.\n',
        '- arXiv freshness alone is not enough; GitHub/repo usability and execution feasibility should materially affect whether an idea is worth pursuing.\n\n',
        '| Rank | Recommendation | Idea Score | Paper | Repo | Dataset | Main Risks |\n',
        '| --- | --- | --- | --- | --- | --- | --- |\n',
    ])
    for idx, row in enumerate(ideas, start=1):
        lines.append(
            f"| {idx} | {row.get('recommendation', '')} | {row.get('idea_score', 0)} | {row.get('paper_id', '')} | {row.get('repo_name', '')} | {row.get('dataset_name', '')} | {'; '.join(row.get('main_risks', [])) or 'none'} |\n"
        )
    lines.append('\n')
    for row in ideas:
        lines.extend([
            f"## {row.get('idea_id', '')}\n\n",
            f"- recommendation: {row.get('recommendation', '')}\n",
            f"- idea_score: {row.get('idea_score', 0)}\n",
            f"- paper: {row.get('paper_id', '')} | {row.get('paper_title', '')}\n",
            f"- paper_top_tier_readiness: {row.get('paper_top_tier_readiness', '')}\n",
            f"- paper_idea_worthiness_score: {row.get('paper_idea_worthiness_score', 0)}\n",
            f"- repo: {row.get('repo_name', '')}\n",
            f"- repo_reuse_score: {row.get('repo_reuse_score', 0)}\n",
            f"- repo_execution_ready: {row.get('repo_execution_ready', False)}\n",
            f"- dataset: {row.get('dataset_name', '')}\n",
            f"- dataset_ready: {row.get('dataset_ready', False)}\n",
            f"- lexical_overlap: {row.get('lexical_overlap', 0)}\n",
            f"- dataset_overlap: {row.get('dataset_overlap', 0)}\n",
            '\n### Risks\n',
        ])
        for risk in row.get('main_risks', []):
            lines.append(f'- {risk}\n')
        if not row.get('main_risks'):
            lines.append('- no dominant structural risk detected at screening stage\n')
        lines.append('\n')

    out = paths.planning / 'idea_candidates.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
