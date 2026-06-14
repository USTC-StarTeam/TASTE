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
    quality = load_json(paths.state / 'paper_quality.json', {})
    repos = load_json(paths.state / 'repo_candidates.json', [])
    datasets = load_json(paths.state / 'dataset_registry.json', [])
    ideas = load_json(paths.state / 'idea_candidates.json', {'ideas': []})
    gaps = (paths.wiki_gaps / 'research_gaps.md').read_text(encoding='utf-8') if (paths.wiki_gaps / 'research_gaps.md').exists() else ''
    next_actions = load_json(paths.state / 'next_actions.json', {'method_summaries': []})

    top_paper = next((row for row in quality.get('papers', []) if row.get('top_tier_readiness') == 'promising'), (quality.get('papers', []) or [{}])[0])
    top_repo = repos[0] if repos else {}
    top_dataset = next((row for row in datasets if row.get('available')), datasets[0] if datasets else {})
    top_idea = (ideas.get('ideas', []) or [{}])[0] if isinstance(ideas, dict) else {}

    hypotheses = [
        {
            'hypothesis_id': 'arena_h1_recent_high_signal_with_runnable_code',
            'title': 'Prioritize recent paper directions that also have runnable or borrowable code',
            'motivation': 'A fresh paper signal is much more actionable when a credible codebase exists to accelerate evidence collection.',
            'novelty_delta': 'Choose directions where recent literature novelty and code borrowability reinforce each other.',
            'nearest_neighbor': top_paper.get('paper_id', ''),
            'repo_anchor': top_repo.get('name', ''),
            'dataset_anchor': top_dataset.get('name', ''),
            'support_evidence_needed': 'A reproducible baseline and at least one targeted variant with valid audit artifacts and bad-case slices.',
            'kill_criteria': 'Recent literature looks interesting but no usable code path or dataset path emerges after focused scouting.',
            'counterexample_test': 'Try the top paper direction on the best runnable repo and see whether the claimed value survives hard-slice analysis.',
            'priority': 1,
        },
        {
            'hypothesis_id': 'arena_h2_target_hard_slices_not_only_average',
            'title': 'Target hard-slice gains, not only average gains',
            'motivation': 'A good idea should survive bad-case slicing and reveal a sharper claim than aggregate-score chasing.',
            'novelty_delta': 'Move from average-only optimization to slice-aware improvement with explicit failure targeting.',
            'nearest_neighbor': top_paper.get('paper_id', ''),
            'repo_anchor': top_repo.get('name', ''),
            'dataset_anchor': top_dataset.get('name', ''),
            'support_evidence_needed': 'Improved aggregate metric or clearly improved worst-case slice profile with exported bad-case evidence.',
            'kill_criteria': 'No bad-case slice advantage after 2 evidence-valid attempts, or novelty remains tuning-noise only.',
            'counterexample_test': 'Stress the weakest known slice first and compare against the nearest strong baseline.',
            'priority': 2,
        },
        {
            'hypothesis_id': 'arena_h3_do_not_trust_freshness_alone',
            'title': 'Do not trust freshness alone when choosing an idea',
            'motivation': 'Some new arXiv items are inspiring but not yet sturdy enough to justify the main loop unless citations, code, and evaluation path line up.',
            'novelty_delta': 'Add citation/reuse/execution discipline to paper freshness so the loop prunes fragile hype earlier.',
            'nearest_neighbor': top_idea.get('paper_id', top_paper.get('paper_id', '')),
            'repo_anchor': top_idea.get('repo_name', top_repo.get('name', '')),
            'dataset_anchor': top_idea.get('dataset_name', top_dataset.get('name', '')),
            'support_evidence_needed': 'An idea candidate that combines recent paper signal, runnable repo signal, and dataset readiness should consistently outscore freshness-only candidates.',
            'kill_criteria': 'Fresh-paper-heavy choices repeatedly fail to produce runnable baselines or convincing claim tests.',
            'counterexample_test': 'Compare a freshness-first idea against a balanced idea seed and see which reaches auditable evidence faster.',
            'priority': 3,
        },
    ]

    arena = {
        'project': args.project,
        'topic': cfg.get('topic', ''),
        'top_paper': top_paper,
        'top_repo': top_repo,
        'top_dataset': top_dataset,
        'top_idea': top_idea,
        'hypotheses': hypotheses,
        'method_frontier_hint': next_actions.get('method_summaries', []),
        'gap_context_excerpt': gaps[:2000],
    }

    (paths.state / 'hypothesis_arena.json').write_text(json.dumps(arena, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    lines = ['# Hypothesis Arena\n\n']
    if top_idea:
        lines.extend([
            '## Best Current Idea Seed\n\n',
            f"- idea_id: {top_idea.get('idea_id', '')}\n",
            f"- recommendation: {top_idea.get('recommendation', '')}\n",
            f"- idea_score: {top_idea.get('idea_score', 0)}\n",
            f"- paper_anchor: {top_idea.get('paper_id', '')}\n",
            f"- repo_anchor: {top_idea.get('repo_name', '')}\n",
            f"- dataset_anchor: {top_idea.get('dataset_name', '')}\n\n",
        ])
    for row in hypotheses:
        lines.extend([
            f"## {row['hypothesis_id']}: {row['title']}\n\n",
            f"- priority: {row['priority']}\n",
            f"- motivation: {row['motivation']}\n",
            f"- novelty_delta: {row['novelty_delta']}\n",
            f"- nearest_neighbor: {row['nearest_neighbor']}\n",
            f"- repo_anchor: {row['repo_anchor']}\n",
            f"- dataset_anchor: {row['dataset_anchor']}\n",
            f"- support_evidence_needed: {row['support_evidence_needed']}\n",
            f"- kill_criteria: {row['kill_criteria']}\n",
            f"- counterexample_test: {row['counterexample_test']}\n\n",
        ])
    out = paths.planning / 'hypothesis_arena.md'
    out.write_text(''.join(lines), encoding='utf-8')

    gaps_lines = ['# Research Gaps\n\n']
    gaps_lines.append(f"- project: {args.project}\n")
    gaps_lines.append(f"- topic: {cfg.get('topic', '')}\n")
    gaps_lines.append('- status: evidence-gated draft; update after each real-data experiment or literature refresh.\n\n')
    gaps_lines.append('## Novelty Delta\n')
    gaps_lines.append('- A direction is not novel enough merely because it combines fashionable components; it must identify a concrete mechanism-level delta over the nearest project-specific baselines recorded in current literature and experiment state.\n')
    gaps_lines.append('- Prioritize deltas that change the evidence contract: better cold/hard-slice behavior, stronger sequence/semantic alignment, or a clearer generative objective that survives ablation.\n\n')
    gaps_lines.append('## Counterexample And Falsification\n')
    gaps_lines.append('- For every candidate idea, define at least one counterexample slice before scaling: sparse users, long-tail items/POIs, temporal shift, semantic mismatch, or popularity leakage.\n')
    gaps_lines.append('- If a method improves only aggregate metrics while failing its declared counterexample slice, the claim must be narrowed or the method pruned.\n\n')
    gaps_lines.append('## Bad-Case Slicing\n')
    gaps_lines.append('- Required slices must be derived from the project data contract and current failure analysis; each slice should name the affected instances, stress condition, and expected failure mode before promotion.\n')
    gaps_lines.append('- Each experiment should export bad-case artifacts; missing bad-case evidence blocks paper promotion even if the average metric improves.\n\n')
    gaps_lines.append('## Claim Strength\n')
    gaps_lines.append('- Strong claims require real datasets, repo loader success, reproducible baselines, ablations, and counterexample pressure.\n')
    gaps_lines.append('- Synthetic or smoke evidence may validate plumbing only; it cannot support final venue claims.\n\n')
    gaps_lines.append('## Prune Or Pause Rules\n')
    gaps_lines.append('- Stop a method after two evidence-valid attempts if it lacks meaningful gain, lacks novelty signal, or repeatedly fails the declared hard slice.\n')
    gaps_lines.append('- Switch or backtrack repos when real data cannot be acquired after bounded attempts and an alternative evidence-ready route exists.\n')
    gaps_lines.append('- Preserve vetoed routes as guardrails so the loop does not rediscover the same dead end.\n\n')
    gaps_lines.append('## Active Hypothesis Arena Links\n')
    for row in hypotheses:
        gaps_lines.append(f"- {row['hypothesis_id']}: novelty={row['novelty_delta']} | counterexample={row['counterexample_test']} | kill={row['kill_criteria']}\n")
    paths.wiki_gaps.mkdir(parents=True, exist_ok=True)
    (paths.wiki_gaps / 'research_gaps.md').write_text(''.join(gaps_lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
