#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from literature_policy import (
    build_literature_policy,
    dataset_readiness_component,
    idea_recommendation,
    normalize_label,
    paper_sort_key,
    repo_sort_key,
)
from project_paths import build_paths, load_project_config

def load_json_default(path: Path, default):
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


def run_assess_idea_candidates(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args(argv)

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    paper_quality = load_json_default(paths.state / 'paper_quality.json', {'papers': []})
    repo_rows = load_json_default(paths.state / 'repo_candidates.json', [])
    dataset_rows = load_json_default(paths.state / 'dataset_registry.json', [])
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




def run_build_hypothesis_arena(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args(argv)

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    quality = load_json_default(paths.state / 'paper_quality.json', {})
    repos = load_json_default(paths.state / 'repo_candidates.json', [])
    datasets = load_json_default(paths.state / 'dataset_registry.json', [])
    ideas = load_json_default(paths.state / 'idea_candidates.json', {'ideas': []})
    gaps = (paths.wiki_gaps / 'research_gaps.md').read_text(encoding='utf-8') if (paths.wiki_gaps / 'research_gaps.md').exists() else ''
    next_actions = load_json_default(paths.state / 'next_actions.json', {'method_summaries': []})

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




def load_json_list(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def load_mapping(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def run_prepare_initialization(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args(argv)

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    repo_rows = load_json_list(paths.state / 'repo_candidates.json')
    dataset_rows = load_json_list(paths.state / 'dataset_registry.json')
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
        '- If the packet is stale or too generic for the current blocker, Claude Code may run framework/scripts/run_module.py finding --action literature_tool with a targeted --query as an internal project-agent survey, read the packet under state/internal_literature_runs/..., and must not publish to web-facing current Find unless TASTE/user explicitly asks for --publish-current-find.\n\n',
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




TOOL_ACTIONS = {
    "assess": run_assess_idea_candidates,
    "arena": run_build_hypothesis_arena,
    "initialization": run_prepare_initialization,
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Ideation module private tool dispatcher.")
    parser.add_argument("--tool-action", required=True, choices=sorted(TOOL_ACTIONS))
    ns, rest = parser.parse_known_args(argv)
    TOOL_ACTIONS[ns.tool_action](rest)


if __name__ == "__main__":
    main()
