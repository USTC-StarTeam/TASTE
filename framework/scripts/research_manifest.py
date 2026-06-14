#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from project_paths import build_paths, load_project_config
from paper_common import get_active_paper_state


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    experiments = load_json(paths.state / 'experiment_registry.json', [])
    repos = load_json(paths.state / 'repo_candidates.json', [])
    datasets = load_json(paths.state / 'dataset_registry.json', [])
    next_actions = load_json(paths.state / 'next_actions.json', {'method_summaries': []})
    paper_state = get_active_paper_state(args.project, venue=args.venue)
    paper_orchestra = load_json(paths.state / 'paper_orchestra_state.json', {})
    paper_orchestra_bridge = load_json(paths.state / 'paper_orchestra_bridge.json', {})
    paper_normality = load_json(paths.state / 'paper_normality_audit.json', {})
    submission_readiness = load_json(paths.state / 'submission_readiness.json', {})
    method_summaries = next_actions.get('method_summaries', []) if isinstance(next_actions, dict) else []
    manifest = {
        'project': args.project,
        'topic': cfg.get('topic', ''),
        'user_prompt': cfg.get('user_prompt', ''),
        'active_venue': paper_state.get('venue', ''),
        'paper_gate': paper_state.get('promotion_gate', ''),
        'paper_orchestra_state_status': paper_orchestra.get('status', '') if isinstance(paper_orchestra, dict) else '',
        'paper_orchestra_promotion_gate': paper_orchestra.get('promotion_gate_recommendation', '') if isinstance(paper_orchestra, dict) else '',
        'paper_orchestra_bridge_status': paper_orchestra_bridge.get('status', '') if isinstance(paper_orchestra_bridge, dict) else '',
        'paper_orchestra_bridge_workspace': paper_orchestra_bridge.get('workspace', '') if isinstance(paper_orchestra_bridge, dict) else '',
        'paper_normality_status': paper_normality.get('status', '') if isinstance(paper_normality, dict) else '',
        'normal_preview_ready': bool(paper_normality.get('normal_preview_ready')) if isinstance(paper_normality, dict) else False,
        'paper_venue_format_status': paper_normality.get('metrics', {}).get('venue_template_validation', {}).get('status', '') if isinstance(paper_normality, dict) and isinstance(paper_normality.get('metrics'), dict) else paper_state.get('paper_venue_format_status', ''),
        'venue_template_format_ready': bool(paper_state.get('venue_template_format_ready')) if isinstance(paper_state, dict) else False,
        'submission_readiness_status': submission_readiness.get('status', '') if isinstance(submission_readiness, dict) else '',
        'submission_ready': bool(submission_readiness.get('submission_ready')) if isinstance(submission_readiness, dict) else False,
        'paper_review_verdict': paper_state.get('paper_review_verdict', ''),
        're_review_verdict': paper_state.get('re_review_verdict', ''),
        'repo_count': len(repos),
        'dataset_count': len(datasets),
        'experiment_count': len(experiments),
        'completed_experiment_count': sum(1 for row in experiments if str(row.get('status', '')).lower() in {'completed', 'success'}),
        'bad_case_experiment_count': sum(1 for row in experiments if row.get('bad_case_slices')),
        'claim_checked_experiment_count': sum(1 for row in experiments if row.get('claim_verdict')),
        'counterexample_checked_experiment_count': sum(1 for row in experiments if row.get('counterexample_outcome')),
        'audit_ready_experiment_count': sum(1 for row in experiments if row.get('audit_ready')),
        'methods_with_claim_support_count': sum(1 for row in method_summaries if row.get('claim_strength_score', 0) > 0),
        'methods_with_counterexample_results_count': sum(1 for row in method_summaries if row.get('counterexample_score', 0) != 0),
        'methods_with_bad_case_slices_count': sum(1 for row in method_summaries if row.get('bad_case_slice_count', 0) > 0),
        'methods_pruned_count': sum(1 for row in method_summaries if row.get('recommendation') in {'compare_then_prune_or_pause', 'pause_or_prune'}),
        'methods_deepen_ready_count': sum(1 for row in method_summaries if row.get('deepen_ready')),
        'novelty_supported_method_count': sum(1 for row in method_summaries if row.get('novelty_score', 0) > 0),
        'artifacts': {
            'status': str(paths.reports / 'status.md'),
            'healthcheck': str(paths.reports / 'healthcheck.md'),
            'iteration_reflection': str(paths.reports / 'iteration_reflection.md'),
            'paper_draft': str(paths.root / 'paper' / 'drafts' / 'paper_draft.md'),
            'paper_revision': str(paths.root / 'paper' / 'drafts' / 'paper_revision.md'),
            'paper_orchestra_state': str(paths.state / 'paper_orchestra_state.json'),
            'paper_orchestra_state_report': str(paths.reports / 'paper_orchestra_state.md'),
            'paper_orchestra_bridge': str(paths.state / 'paper_orchestra_bridge.json'),
            'paper_orchestra_bridge_report': str(paths.reports / 'paper_orchestra_bridge.md'),
            'paper_normality_audit': str(paths.state / 'paper_normality_audit.json'),
            'paper_normality_report': str(paths.reports / 'paper_normality_audit.md'),
            'submission_readiness': str(paths.state / 'submission_readiness.json'),
            'submission_readiness_report': str(paths.reports / 'submission_readiness.md'),
            'aggregated_review': str(paths.root / 'paper' / 'reviews' / 'aggregated_review.md'),
            'author_response': str(paths.root / 'paper' / 'reviews' / 'responses' / 'author_response.md'),
            're_review_summary': str(paths.root / 'paper' / 'reviews' / 're_review' / 're_review_summary.md'),
        },
    }
    out_json = paths.state / 'research_manifest.json'
    out_md = paths.reports / 'research_manifest.md'
    out_json.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    lines = ['# Research Manifest\n\n']
    for key, value in manifest.items():
        if isinstance(value, dict):
            lines.append(f'## {key}\n\n')
            for sub_key, sub_value in value.items():
                lines.append(f'- {sub_key}: {sub_value}\n')
            lines.append('\n')
        else:
            lines.append(f'- {key}: {value}\n')
    out_md.write_text(''.join(lines), encoding='utf-8')
    print(out_md)


if __name__ == '__main__':
    main()
