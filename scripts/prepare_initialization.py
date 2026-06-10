#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from literature_policy import build_literature_policy
from project_paths import build_paths, load_project_config


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def load_mapping(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    repo_rows = load_json(paths.state / 'repo_candidates.json')
    dataset_rows = load_json(paths.state / 'dataset_registry.json')
    machine = load_mapping(paths.reports / 'machine_profile.json')
    quality = load_mapping(paths.state / 'paper_quality.json')
    lint = load_mapping(paths.state / 'lint_report.json')
    idea_payload = load_mapping(paths.state / 'idea_candidates.json')
    taste_state = load_mapping(paths.state / 'finding_frontend.json')
    taste_sync = load_mapping(paths.state / 'taste_sync.json')
    taste_intermediates = load_mapping(paths.state / 'taste_literature_intermediates.json')
    literature_packet = load_mapping(paths.state / 'literature_tool_packet.json')
    policy = build_literature_policy(cfg)

    best_repo = repo_rows[0] if repo_rows else None
    best_dataset = dataset_rows[0] if dataset_rows else None
    accelerator = machine.get('accelerator', {}) if isinstance(machine, dict) else {}
    gpu_rows = accelerator.get('gpus', []) if isinstance(accelerator, dict) else []

    quality_rows = quality.get('papers', []) if isinstance(quality, dict) else []
    recent_high = [row for row in quality_rows if row.get('selection_bucket') == 'recent_high_priority']
    recent_candidates = [row for row in quality_rows if row.get('selection_bucket') == 'recent_candidate']
    older_foundational = [row for row in quality_rows if row.get('selection_bucket') == 'older_foundational']
    strongest_paper = recent_high[0] if recent_high else (recent_candidates[0] if recent_candidates else (older_foundational[0] if older_foundational else (quality_rows[0] if quality_rows else None)))
    top_idea = (idea_payload.get('ideas', []) or [None])[0] if isinstance(idea_payload, dict) else None

    text = [
        '# Initialization Brief\n\n',
        f"- project: {cfg.get('name', args.project)}\n",
        f"- topic: {cfg.get('topic', '')}\n",
        f"- conda_env: {cfg.get('conda_env', '')}\n",
        f"- visible_gpus: {len(gpu_rows)}\n",
        f"- literature_reference_time_utc: {quality.get('reference_time', '') if isinstance(quality, dict) else ''}\n\n",
        '## Machine-aware startup\n',
        '- Environment should be adapted to the detected accelerator and CUDA profile before the first real run.\n',
        '- Prefer installing directly against the selected repo rather than building an isolated toy env.\n',
        '- Require machine-readable metrics and bad-case outputs in the very first executable baseline.\n\n',
        '## Literature scouting rule\n',
        f"- primary recent window: last {policy.get('primary_window_days', 180)} days relative to runtime.\n",
        f"- secondary recent window: last {policy.get('secondary_window_days', 365)} days relative to runtime.\n",
        f"- deprioritize when older than about {policy.get('deprioritize_older_than_days', 730)} days unless the paper is still foundational.\n",
        f"- preferred venues: {', '.join(policy.get('preferred_venues', [])[:10])}\n",
        f"- preferred journals: {', '.join(policy.get('preferred_journals', [])[:6])}\n",
        '- Search should bias toward recent top-tier papers first, then a very small number of older still-useful anchors.\n',
        '- Fresh arXiv/GitHub signals are valuable, but they should be filtered by citation signal, borrowability, and runnable code quality before driving a research bet.\n\n',
        '## Current literature signal\n',
        f"- recent_high_priority_papers: {len(recent_high)}\n",
        f"- recent_candidate_papers: {len(recent_candidates)}\n",
        f"- older_foundational_papers: {len(older_foundational)}\n",
        f"- tracked_repo_candidates: {len(repo_rows)}\n",
        f"- idea_candidates: {idea_payload.get('summary', {}).get('idea_count', 0) if isinstance(idea_payload, dict) else 0}\n\n",
        '## Finding Literature Artifacts\n',
        f"- taste_status: {taste_state.get('status', 'available') if isinstance(taste_state, dict) else ''}\n",
        f"- taste_output_dir: {taste_state.get('output_dir', '') if isinstance(taste_state, dict) else ''}\n",
        f"- papers_synced: {taste_sync.get('counts', {}).get('papers_synced', 0) if isinstance(taste_sync, dict) else 0}\n",
        f"- ideas_synced: {taste_sync.get('counts', {}).get('ideas_synced', 0) if isinstance(taste_sync, dict) else 0}\n",
        f"- plans_synced: {taste_sync.get('counts', {}).get('plans_synced', 0) if isinstance(taste_sync, dict) else 0}\n",
        f"- survey_stats: {json.dumps(taste_state.get('survey_stats', {}) if isinstance(taste_state, dict) else {}, ensure_ascii=False)}\n",
        f"- intermediate_state_file: {paths.state / 'taste_literature_intermediates.json'}\n",
        f"- literature_tool_packet: {paths.planning / 'literature_tool_packet.md'}\n",
        f"- literature_packet_status: {literature_packet.get('status', 'not_built') if isinstance(literature_packet, dict) else 'not_built'}\n",
        f"- literature_packet_summary: {json.dumps(literature_packet.get('summary', {}) if isinstance(literature_packet, dict) else {}, ensure_ascii=False)}\n",
        '- Required use: treat finding outputs as the first literature-discovery layer for idea generation, base-work selection, repo search, and experiment planning; do not cite them as result evidence until TASTE verifies repo/data/experiment artifacts.\n',
        '- Key files for Claude Code: planning/literature_tool_packet.md, state/literature_tool_packet.json, planning/finding/find_results.json, category_scan_report.json, title_filter_report.json, arxiv_raw.json, arxiv_prefiltered.json, read.md, idea.md, plan.md.\n',
        '- If the packet is stale or too generic for the current blocker, Claude Code may run scripts/run_literature_tool.py with a targeted --query, then rebuild the packet.\n\n',
        '## Best Repo Candidate\n',
    ]
    if best_repo:
        text.extend([
            f"- name: {best_repo.get('name', '')}\n",
            f"- url: {best_repo.get('url', '')}\n",
            f"- score: {best_repo.get('score', best_repo.get('repo_reuse_score', 0))}\n",
            f"- repo_selection_bucket: {best_repo.get('repo_selection_bucket', '')}\n",
            f"- notes: {best_repo.get('notes', '')}\n",
            '- why it matters: adapting a mature runnable repo is usually higher leverage than reimplementing from scratch.\n\n',
        ])
    else:
        text.append('- No repo candidates registered yet.\n\n')

    text.append('## Best Dataset Candidate\n')
    if best_dataset:
        text.extend([
            f"- name: {best_dataset.get('name', '')}\n",
            f"- task: {best_dataset.get('task', '')}\n",
            f"- readiness_score: {best_dataset.get('readiness_score', 0)}\n",
            f"- notes: {best_dataset.get('notes', '')}\n",
            '- why it matters: a strong idea without a clean dataset path usually stalls the loop.\n\n',
        ])
    else:
        text.append('- No dataset candidates registered yet.\n\n')

    text.extend([
        '## Top-Tier Research Gates\n',
        '1. Novelty gate: define the exact delta over the nearest strong baseline or nearest paper, not a vague improvement.\n',
        '2. Claim gate: every planned method must state what benchmark result would actually support its central claim.\n',
        '3. Counterexample gate: list at least one slice, stress setting, or failure mode that could falsify the claim.\n',
        '4. Bad-case gate: the first executable run must emit examples or slices of failure, not only one aggregate score.\n',
        '5. Prune gate: specify when a method should be paused, pruned, or compared once more before stopping.\n\n',
        '## Strong-Research Filters\n',
        '1. Is the repo good enough to modify directly instead of rebuild?\n',
        '2. Does the dataset support a decisive minimal experiment?\n',
        '3. Is the evaluation likely to reveal a real scientific claim rather than just engineering polish?\n',
        '4. If this project worked, would it move a real assumption in the field?\n',
        '5. Are there at least 2-3 plausible methods worth parallel exploration rather than a single bet?\n\n',
    ])

    if strongest_paper:
        text.extend([
            '## Strongest Current Paper Signal\n',
            f"- paper_id: {strongest_paper.get('paper_id', '')}\n",
            f"- title: {strongest_paper.get('title', '')}\n",
            f"- selection_bucket: {strongest_paper.get('selection_bucket', '')}\n",
            f"- recency_bucket: {strongest_paper.get('recency_bucket', '')}\n",
            f"- paper_age_days: {strongest_paper.get('paper_age_days', '')}\n",
            f"- venue_quality: {strongest_paper.get('venue_quality', '')}\n",
            f"- discovery_priority_score: {strongest_paper.get('discovery_priority_score', 0)}\n",
            f"- idea_worthiness_score: {strongest_paper.get('idea_worthiness_score', 0)}\n",
            f"- novelty: {strongest_paper.get('novelty', '')}\n",
            f"- claim_strength: {strongest_paper.get('claim_strength', '')}\n",
            f"- counterexample_pressure: {strongest_paper.get('counterexample_pressure', '')}\n",
            f"- actionability_score: {strongest_paper.get('actionability_score', 0)}\n\n",
        ])

    if top_idea:
        text.extend([
            '## Best Current Idea Seed\n',
            f"- idea_id: {top_idea.get('idea_id', '')}\n",
            f"- recommendation: {top_idea.get('recommendation', '')}\n",
            f"- idea_score: {top_idea.get('idea_score', 0)}\n",
            f"- paper_anchor: {top_idea.get('paper_id', '')}\n",
            f"- repo_anchor: {top_idea.get('repo_name', '')}\n",
            f"- dataset_anchor: {top_idea.get('dataset_name', '')}\n",
            f"- main_risks: {'; '.join(top_idea.get('main_risks', [])) or 'none'}\n\n",
        ])

    pressure_count = lint.get('quality_pressure_count', 0) if isinstance(lint, dict) else 0
    text.extend([
        '## Hypothesis Tournament Pattern\n',
        'Borrowing from multi-agent research systems, every promising direction should be expressed as a falsifiable hypothesis with support evidence and kill criteria before large-scale execution.\n\n',
        '## Method Frontier Pattern\n',
        'Keep a small elite pool, a repair queue, a slice-focus queue, and a prune queue instead of treating all methods equally.\n\n',
        '## Recommended Next Steps\n',
        '1. Auto-bootstrap the conda environment against the selected repo and detected GPU stack.\n',
        '2. Confirm at least one dataset is available in the configured environment.\n',
        '3. Plan 2-3 promising methods in parallel with explicit novelty, claim, counterexample, and prune contracts.\n',
        '4. For each weak result, analyze hyperparameters, module coordination, implementation risk, and bad-case slices.\n',
        '5. Use git to track only core code/config checkpoints, not bulky artifacts.\n',
        '6. Keep literature refresh biased toward recent top-tier work before widening to older references.\n',
        '7. Let GitHub/repo usability affect idea choice, not just paper freshness.\n',
        f'8. Resolve {pressure_count} wiki-level research-quality pressure signals before trusting the synthesis too much.\n',
    ])

    out = paths.planning / 'init_brief.md'
    out.write_text(''.join(text), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
