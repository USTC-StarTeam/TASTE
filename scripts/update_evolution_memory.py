#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from paper_common import get_active_paper_state
from project_paths import build_paths, load_project_config

PRUNE_RECOMMENDATIONS = {'compare_then_prune_or_pause', 'pause_or_prune'}
REPAIR_RECOMMENDATIONS = {'repair_metric_logging', 'fix_implementation_or_environment', 'export_bad_cases_before_scaling'}


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def summarize_method(row: dict) -> dict:
    return {
        'method': row.get('method', ''),
        'decision_score': row.get('decision_score', 0),
        'recommendation': row.get('recommendation', ''),
        'best_metric': row.get('best_metric'),
        'metric_name': row.get('metric_name', ''),
        'claim_strength_score': row.get('claim_strength_score', 0),
        'novelty_score': row.get('novelty_score', 0),
        'counterexample_score': row.get('counterexample_score', 0),
        'bad_case_slice_count': row.get('bad_case_slice_count', 0),
        'deepen_ready': row.get('deepen_ready', False),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    experiments = load_json(paths.state / 'experiment_registry.json', [])
    next_actions = load_json(paths.state / 'next_actions.json', {'method_summaries': []})
    ideas = load_json(paths.state / 'idea_candidates.json', {'ideas': []})
    paper_quality = load_json(paths.state / 'paper_quality.json', {'papers': [], 'summary': {}})
    paper_state = get_active_paper_state(args.project, venue=args.venue)
    methods = next_actions.get('method_summaries', []) if isinstance(next_actions, dict) else []

    ranked_methods = sorted(methods, key=lambda row: (-float(row.get('decision_score', 0) or 0), row.get('priority', 999)))
    elite_pool = [summarize_method(row) for row in ranked_methods if row.get('deepen_ready')][:3]
    if not elite_pool:
        elite_pool = [summarize_method(row) for row in ranked_methods[:3]]
    repair_queue = [summarize_method(row) for row in ranked_methods if row.get('recommendation') in REPAIR_RECOMMENDATIONS or row.get('audit_incomplete_runs', 0) > 0][:5]
    slice_queue = [summarize_method(row) for row in ranked_methods if row.get('bad_case_slice_count', 0) > 0 and not row.get('deepen_ready')][:5]
    prune_queue = [summarize_method(row) for row in ranked_methods if row.get('recommendation') in PRUNE_RECOMMENDATIONS or row.get('counterexample_score', 0) < 0 or row.get('claim_strength_score', 0) < 0][:5]
    active_method_names = {row.get('method', '') for row in methods}
    exploration_backlog = []
    for row in ideas.get('ideas', [])[:8]:
        if row.get('repo_name', '') in active_method_names or row.get('paper_id', '') in active_method_names:
            continue
        exploration_backlog.append({
            'idea_id': row.get('idea_id', ''),
            'recommendation': row.get('recommendation', ''),
            'idea_score': row.get('idea_score', 0),
            'paper_id': row.get('paper_id', ''),
            'repo_name': row.get('repo_name', ''),
            'dataset_name': row.get('dataset_name', ''),
        })
    top_papers = [
        {
            'paper_id': row.get('paper_id', ''),
            'title': row.get('title', ''),
            'selection_bucket': row.get('selection_bucket', ''),
            'idea_worthiness_score': row.get('idea_worthiness_score', 0),
        }
        for row in (paper_quality.get('papers', []) if isinstance(paper_quality, dict) else [])[:5]
    ]

    completed = sum(1 for row in experiments if str(row.get('status', '')).lower() in {'completed', 'success'})
    failed = sum(1 for row in experiments if str(row.get('status', '')).lower() in {'failed', 'error', 'incomplete_audit'})
    memory = {
        'project': args.project,
        'topic': cfg.get('topic', ''),
        'user_prompt': cfg.get('user_prompt', ''),
        'paper_gate': paper_state.get('promotion_gate', ''),
        'paper_review_verdict': paper_state.get('paper_review_verdict', ''),
        're_review_verdict': paper_state.get('re_review_verdict', ''),
        'literature_reference_time': paper_quality.get('reference_time', '') if isinstance(paper_quality, dict) else '',
        'recent_high_priority_papers': paper_quality.get('summary', {}).get('recent_high_priority_count', 0) if isinstance(paper_quality, dict) else 0,
        'recent_candidate_papers': paper_quality.get('summary', {}).get('recent_candidate_count', 0) if isinstance(paper_quality, dict) else 0,
        'completed_experiments': completed,
        'failed_or_incomplete_experiments': failed,
        'elite_pool': elite_pool,
        'repair_queue': repair_queue,
        'slice_queue': slice_queue,
        'prune_queue': prune_queue,
        'exploration_backlog': exploration_backlog,
        'top_papers': top_papers,
        'principles': {
            'ideation_memory': 'Keep only ideas that survive freshness, borrowability, dataset readiness, and novelty pressure together.',
            'experimentation_memory': 'Promote methods that survive claim checks, counterexamples, and bad-case slicing; demote methods that only improve weak aggregate metrics.',
            'resource_discipline': 'Do not keep every method alive. Maintain an elite pool, a repair queue, a slice-focused queue, and a prune queue.',
        },
    }

    out_json = paths.state / 'evolution_memory.json'
    out_md = paths.reports / 'evolution_memory.md'
    out_json.write_text(json.dumps(memory, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    lines = [
        '# Evolution Memory\n\n',
        f"- project: {memory['project']}\n",
        f"- topic: {memory['topic']}\n",
        f"- paper_gate: {memory['paper_gate']}\n",
        f"- paper_review_verdict: {memory['paper_review_verdict']}\n",
        f"- re_review_verdict: {memory['re_review_verdict']}\n",
        f"- literature_reference_time: {memory['literature_reference_time']}\n",
        f"- recent_high_priority_papers: {memory['recent_high_priority_papers']}\n",
        f"- recent_candidate_papers: {memory['recent_candidate_papers']}\n",
        f"- completed_experiments: {memory['completed_experiments']}\n",
        f"- failed_or_incomplete_experiments: {memory['failed_or_incomplete_experiments']}\n\n",
        '## Elite Pool\n',
    ]
    for row in elite_pool:
        lines.append(f"- {row['method']}: decision_score={row['decision_score']} | best_{row['metric_name']}={row['best_metric']} | claim={row['claim_strength_score']} | novelty={row['novelty_score']} | counterexample={row['counterexample_score']}\n")
    if not elite_pool:
        lines.append('- no elite method yet\n')
    lines.append('\n## Repair Queue\n')
    for row in repair_queue:
        lines.append(f"- {row['method']}: recommendation={row['recommendation']} | bad_case_slices={row['bad_case_slice_count']}\n")
    if not repair_queue:
        lines.append('- no repair queue item yet\n')
    lines.append('\n## Slice Queue\n')
    for row in slice_queue:
        lines.append(f"- {row['method']}: slices={row['bad_case_slice_count']} | recommendation={row['recommendation']}\n")
    if not slice_queue:
        lines.append('- no slice-focused queue item yet\n')
    lines.append('\n## Prune Queue\n')
    for row in prune_queue:
        lines.append(f"- {row['method']}: recommendation={row['recommendation']} | claim={row['claim_strength_score']} | counterexample={row['counterexample_score']}\n")
    if not prune_queue:
        lines.append('- no prune candidate yet\n')
    lines.append('\n## Exploration Backlog\n')
    for row in exploration_backlog:
        lines.append(f"- {row['idea_id']}: recommendation={row['recommendation']} | idea_score={row['idea_score']} | paper={row['paper_id']} | repo={row['repo_name']} | dataset={row['dataset_name']}\n")
    if not exploration_backlog:
        lines.append('- no exploration backlog item yet\n')
    lines.append('\n## Memory Principles\n')
    for key, value in memory['principles'].items():
        lines.append(f'- {key}: {value}\n')
    out_md.write_text(''.join(lines), encoding='utf-8')
    print(out_md)


if __name__ == '__main__':
    main()
