#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from project_paths import build_paths, load_project_config


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    paper_state = load_json(paths.root / 'paper' / 'metadata' / 'paper_pipeline.json', {})
    active_venues = list(paper_state.get('venues', {}).keys()) if isinstance(paper_state, dict) else []

    blueprint = {
        'project': args.project,
        'topic': cfg.get('topic', ''),
        'modules': [
            {
                'name': 'request_intake',
                'purpose': 'Turn user natural-language goals into logged project requests and loop kickoff state.',
                'inputs': ['user prompt', 'project config'],
                'outputs': ['state/natural_language_requests.json', 'state/loop_history.json'],
                'feeds_into': ['literature_discovery', 'initialization_and_arena'],
            },
            {
                'name': 'literature_discovery',
                'purpose': 'Create an adaptive recent-literature plan, collect papers, compile wiki, and surface novelty deltas, contradictions, and gap signals; if no qualified papers pass the gate, trigger repo-first backtracking rather than importing weak work.',
                'inputs': ['queries', 'raw/papers', 'manual imports'],
                'outputs': ['planning/literature_review_plan.md', 'wiki/', 'reports/shared_research.md', 'gaps/research_gaps.md', 'state/ingest_ranking.json'],
                'feeds_into': ['initialization_and_arena', 'paper_drafting'],
            },
            {
                'name': 'initialization_and_arena',
                'purpose': 'Rank repos and datasets, inspect machine/runtime, and build the hypothesis arena.',
                'inputs': ['repo candidates', 'dataset registry', 'machine profile', 'paper quality'],
                'outputs': ['planning/init_brief.md', 'planning/hypothesis_arena.md', 'reports/repo_candidates.md', 'reports/dataset_registry.md'],
                'feeds_into': ['parallel_experiment_planning'],
            },
            {
                'name': 'parallel_experiment_planning',
                'purpose': 'Generate parallel methods with explicit novelty, claim, counterexample, and prune contracts.',
                'inputs': ['hypothesis arena', 'repo choice', 'dataset choice', 'benchmark', 'metric'],
                'outputs': ['state/parallel_plan.json', 'planning/parallel_experiment_plan.md'],
                'feeds_into': ['execution_and_audit'],
            },
            {
                'name': 'execution_and_audit',
                'purpose': 'Run methods, require auditable outputs, and record machine-readable evidence.',
                'inputs': ['parallel plan', 'repo env', 'artifact contract'],
                'outputs': ['state/experiment_registry.json', 'experiments/experiment_log.md', 'artifacts/*/audit.json'],
                'feeds_into': ['failure_reflection_and_pruning', 'paper_evidence_gate'],
            },
            {
                'name': 'failure_reflection_and_pruning',
                'purpose': 'Diagnose failures, build method frontier, rank next actions, and decide continue/repair/prune.',
                'inputs': ['experiment registry', 'failure analyses', 'claim ledger', 'paper evidence audit'],
                'outputs': ['planning/next_actions.md', 'planning/method_frontier.md', 'reports/iteration_reflection.md'],
                'feeds_into': ['parallel_experiment_planning', 'paper_drafting'],
            },
            {
                'name': 'paper_evidence_gate',
                'purpose': 'Prevent paper promotion unless claims, bad cases, and counterexamples are sufficiently supported.',
                'inputs': ['experiment registry', 'claim ledger', 'method frontier', 'paper review state'],
                'outputs': ['reports/paper_evidence_audit.md', 'reports/research_manifest.md'],
                'feeds_into': ['paper_drafting', 'venue_latex_stage'],
            },
            {
                'name': 'paper_drafting',
                'purpose': 'Draft, review, revise, and re-review the paper in Markdown before any template promotion.',
                'inputs': ['shared research', 'claim ledger', 'paper evidence audit', 'research manifest'],
                'outputs': ['paper/drafts/*.md', 'paper/reviews/*'],
                'feeds_into': ['venue_latex_stage'],
            },
            {
                'name': 'venue_latex_stage',
                'purpose': 'Fetch venue template, render LaTeX, and compile PDF only after evidence and review gates clear.',
                'inputs': ['paper revision', 'target venue', 'template/runtime availability'],
                'outputs': ['paper/rendered/*', 'paper/compiled/*'],
                'feeds_into': ['request_intake'],
            },
        ],
        'active_venues': active_venues,
    }

    (paths.state / 'workflow_blueprint.json').write_text(json.dumps(blueprint, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    lines = ['# Workflow Blueprint\n\n']
    lines.append(f"- project: {args.project}\n")
    lines.append(f"- topic: {cfg.get('topic', '')}\n")
    lines.append(f"- active_venues: {', '.join(active_venues) if active_venues else 'none'}\n\n")
    for module in blueprint['modules']:
        lines.append(f"## {module['name']}\n\n")
        lines.append(f"- purpose: {module['purpose']}\n")
        lines.append(f"- inputs: {', '.join(module['inputs'])}\n")
        lines.append(f"- outputs: {', '.join(module['outputs'])}\n")
        lines.append(f"- feeds_into: {', '.join(module['feeds_into'])}\n\n")
    out = paths.planning / 'workflow_blueprint.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
