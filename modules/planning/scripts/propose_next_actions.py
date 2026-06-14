#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiment_contracts import PRUNE_RECOMMENDATIONS, claim_strength_score, counterexample_score, evidence_gate, method_is_baseline_or_control, metric_higher_is_better, novelty_score, parse_float, row_matches_method
from project_paths import build_paths, load_project_config


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def collect_methods(plan: dict | list) -> list[dict]:
    if isinstance(plan, list):
        return plan
    if isinstance(plan, dict):
        return plan.get('methods', [])
    return []


def best_metric(rows: list[dict], metric_name: str) -> float | None:
    values = [parse_float(row.get('metric_value')) for row in rows if (row.get('metric_name') or metric_name) == metric_name]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return max(values) if metric_higher_is_better(metric_name) else min(values)


def dataset_contract_label(files: list[str]) -> str:
    names = [str(item).strip() for item in files if str(item).strip()]
    if not names:
        return 'repo-specific data files'
    if set(names) == {'all_data.pkl', 'dist_mat.npy'}:
        return 'current repo trajectory/POI data bundle'
    if len(names) <= 4:
        return 'current repo dataset files: ' + ', '.join(names)
    return f'current repo dataset contract ({len(names)} required files)'


def display_labels(cfg: dict) -> dict:
    exp = cfg.get('experiment', {}) if isinstance(cfg, dict) else {}
    labels = exp.get('display_labels', {}) if isinstance(exp.get('display_labels', {}), dict) else {}
    return labels


def method_label(method: str, cfg: dict) -> str:
    labels = display_labels(cfg).get('method_labels', {})
    if isinstance(labels, dict) and labels.get(method):
        return str(labels[method])
    return str(method or 'unknown').replace('_', ' ')


def method_list_label(methods: list[str], cfg: dict, limit: int = 4) -> str:
    return ', '.join(method_label(str(item), cfg) for item in methods[:limit])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    repo_rows = load_json(paths.state / 'repo_candidates.json')
    dataset_rows = load_json(paths.state / 'dataset_registry.json')
    experiments = load_json(paths.state / 'experiment_registry.json')
    plan = json.loads((paths.state / 'parallel_plan.json').read_text(encoding='utf-8')) if (paths.state / 'parallel_plan.json').exists() else {'methods': []}
    methods = collect_methods(plan)
    machine = json.loads((paths.reports / 'machine_profile.json').read_text(encoding='utf-8')) if (paths.reports / 'machine_profile.json').exists() else {}
    accelerator = machine.get('accelerator', {}) if isinstance(machine, dict) else {}
    visible_gpus = len(accelerator.get('gpus', [])) if isinstance(accelerator, dict) else 0
    quality = load_json(paths.state / 'paper_quality.json')
    ingest_ranking = load_json(paths.state / 'ingest_ranking.json') if (paths.state / 'ingest_ranking.json').exists() else {}
    repo_backtracking = load_json(paths.state / 'repo_first_backtracking.json') if (paths.state / 'repo_first_backtracking.json').exists() else {}
    active_repo = load_json(paths.state / 'active_repo.json') if (paths.state / 'active_repo.json').exists() else {}
    real_probe = load_json(paths.state / 'real_dataset_probe.json') if (paths.state / 'real_dataset_probe.json').exists() else {}
    repo_data_requirements = load_json(paths.state / 'repo_data_requirements.json') if (paths.state / 'repo_data_requirements.json').exists() else {}
    data_policy = load_json(paths.state / 'data_unavailability_policy.json') if (paths.state / 'data_unavailability_policy.json').exists() else {}
    claim_ledger = load_json(paths.state / 'claim_ledger.json') if (paths.state / 'claim_ledger.json').exists() else {'claims': []}
    manifest = load_json(paths.state / 'research_manifest.json') if (paths.state / 'research_manifest.json').exists() else {}
    audit_md = (paths.reports / 'paper_evidence_audit.md').read_text(encoding='utf-8') if (paths.reports / 'paper_evidence_audit.md').exists() else ''
    method_overrides = load_json(paths.state / 'method_overrides.json') if (paths.state / 'method_overrides.json').exists() else {'methods': {}, 'repos': {}}
    override_methods = method_overrides.get('methods', {}) if isinstance(method_overrides, dict) else {}
    override_repos = method_overrides.get('repos', {}) if isinstance(method_overrides, dict) else {}

    failure_files = sorted(paths.state.glob('failure_analysis_*.json'))
    failure_map = {}
    for path in failure_files:
        data = json.loads(path.read_text(encoding='utf-8'))
        failure_map[data.get('method', path.stem.replace('failure_analysis_', ''))] = data

    method_summaries = []
    for method in methods:
        method_rows = [row for row in experiments if row_matches_method(row, method)]
        completed = [row for row in method_rows if str(row.get('status', '')).lower() in {'completed', 'success'}]
        failed = [row for row in method_rows if str(row.get('status', '')).lower() in {'failed', 'error', 'incomplete_audit'}]
        metric_name = method.get('metric') or (method_rows[0].get('metric_name') if method_rows else '') or plan.get('metric', 'metric')
        failure_info = failure_map.get(method.get('method', ''), {})
        claim_score = claim_strength_score(method_rows)
        novelty = novelty_score(method_rows)
        counter_score = counterexample_score(method_rows)
        bad_case_slices = sorted({slice_name for row in method_rows for slice_name in row.get('bad_case_slices', []) or []})
        gate = evidence_gate(method_rows, failure_info.get('recommendation', ''))
        recommendation = failure_info.get('recommendation', '')
        override = override_methods.get(method.get('method', ''), {}) if isinstance(override_methods, dict) else {}
        if override.get('recommendation'):
            recommendation = override.get('recommendation', recommendation)
        if recommendation == 'repair_metric_logging' and not any(not row.get('audit_ready') for row in method_rows):
            recommendation = ''
        # If a non-control method repeatedly underperforms the current project-defined baseline/control, mark it for compare/prune rather than letting it absorb budget.
        role_policy = cfg.get('experiment', {}).get('method_role_policy', {}) if isinstance(cfg, dict) else {}
        method_is_control = method_is_baseline_or_control(method, role_policy)
        baseline_rows = [row for row in experiments if method_is_baseline_or_control(row, role_policy)]
        baseline_metric = best_metric(baseline_rows, metric_name) if not method_is_control else None
        method_metric = best_metric(method_rows, metric_name)
        if not recommendation and baseline_metric is not None and method_metric is not None:
            margin = float(cfg.get('decision_policy', {}).get('minimum_meaningful_gain', 0.005) or 0.005)
            higher = metric_higher_is_better(metric_name)
            underperforms = method_metric <= baseline_metric * (1.0 + margin) if higher else method_metric >= baseline_metric * (1.0 - margin)
            if underperforms and len(completed) >= 2:
                recommendation = 'compare_then_prune_or_pause'
        prune_penalty = 1.5 if recommendation in PRUNE_RECOMMENDATIONS else 0.0
        incomplete_penalty = 1.0 if any(str(row.get('status', '')).lower() == 'incomplete_audit' for row in method_rows) else 0.0
        decision_score = round((gate['claim_strength_score'] * 2.0) + novelty + counter_score + (0.3 * gate['bad_case_slice_count']) - prune_penalty - incomplete_penalty, 4)
        if method.get('repo_path', '') in override_repos and override_repos.get(method.get('repo_path', ''), {}).get('status') in {'paused_or_abandoned', 'abandoned'} and not recommendation:
            recommendation = 'pause_or_prune'
        method_summaries.append({
            'method': method.get('method', ''),
            'method_slug': method.get('method_slug', method.get('method', '')),
            'priority': method.get('priority', 0),
            'launch_ready': method.get('launch_ready', False),
            'planned_trials': method.get('planned_trials', 0),
            'completed': len(completed),
            'failed': len(failed),
            'best_metric': best_metric(method_rows, metric_name),
            'metric_name': metric_name,
            'recommendation': recommendation,
            'override_source': override.get('source', '') if isinstance(override, dict) else '',
            'decision_stage': failure_info.get('decision_stage', ''),
            'repo_path': method.get('repo_path', ''),
            'env_name': method.get('env_name', ''),
            'dataset': method.get('dataset', ''),
            'claim_verdicts': [row.get('claim_verdict', '') for row in method_rows if row.get('claim_verdict')],
            'claim_strength_score': claim_score,
            'novelty_score': novelty,
            'counterexample_score': counter_score,
            'bad_case_coverage': sum(1 for row in method_rows if row.get('bad_case_path')),
            'bad_case_slice_count': len(bad_case_slices),
            'bad_case_slices': bad_case_slices,
            'audit_ready_runs': sum(1 for row in method_rows if row.get('audit_ready')),
            'audit_incomplete_runs': sum(1 for row in method_rows if not row.get('audit_ready')),
            'deepen_ready': gate['deepen_ready'],
            'decision_score': decision_score,
            'claim_contract': method.get('claim_contract', {}),
        })

    def summary_sort_key(item: dict):
        metric = item.get('best_metric')
        metric_key = -metric if metric is not None else float('inf')
        return (-item.get('decision_score', 0.0), item.get('priority', 0), metric_key)

    ranked_methods = sorted(method_summaries, key=summary_sort_key)
    papers = quality.get('papers', []) if isinstance(quality, dict) else []
    weak_paper_signal = all(row.get('top_tier_readiness') == 'weak' for row in papers) if papers else False
    no_qualified_papers = bool(ingest_ranking.get('no_qualified_papers')) if isinstance(ingest_ranking, dict) else False
    repo_backtracking_done = bool(isinstance(repo_backtracking, dict) and (repo_backtracking.get('paper_title_hint') or repo_backtracking.get('run_commands') or repo_backtracking.get('dataset_audits')))
    probe_rows = real_probe.get('probes', []) if isinstance(real_probe, dict) else []
    probed_real_datasets = {row.get('dataset') for row in probe_rows if row.get('probe_success') and row.get('claim_ready')}
    active_repo_path = str(active_repo.get('repo_path', '')) if isinstance(active_repo, dict) else ''
    active_requirement_datasets = set(repo_data_requirements.get('datasets', [])) if isinstance(repo_data_requirements, dict) else set()
    real_datasets = active_requirement_datasets or {row.get('name') for row in dataset_rows if row.get('available') and not str(row.get('name', '')).startswith('synthetic_')}
    completed_real_experiments = [row for row in experiments if str(row.get('status', '')).lower() in {'completed', 'success'} and row.get('dataset') in real_datasets and (not active_repo_path or row.get('repo_path') == active_repo_path)]

    actions: list[dict[str, str]] = []
    if no_qualified_papers and not repo_backtracking_done:
        actions.append({'priority': 'P0', 'title': 'Run repo-first literature backtracking', 'reason': 'The strict literature gate found no qualified papers, so the loop should use discovered repo signals to backtrack to papers, datasets, and baselines without lowering quality thresholds.', 'evidence': str(ingest_ranking.get('no_qualified_reason', 'No qualified papers imported.'))})
    paused_repos = [repo for repo, info in (override_repos.items() if isinstance(override_repos, dict) else []) if info.get('status') in {'paused_or_abandoned', 'abandoned'}]
    active_repo_paused = active_repo_path in paused_repos
    if paused_repos and active_repo_paused:
        actions.append({'priority': 'P0', 'title': 'Restart repo/literature search after critic veto', 'reason': 'The active selected repo has been paused/abandoned by evidence-based critic/planner veto, so the loop should not keep tuning it.', 'evidence': f"Active paused repo: {active_repo_path}."})
    elif paused_repos:
        actions.append({'priority': 'P2', 'title': 'Keep old critic vetoes as historical guardrails', 'reason': 'A previous route was vetoed, but the current active repo is different; do not let stale vetoes dominate the new route.', 'evidence': f"Historical paused repos: {', '.join(paused_repos[:2])}. Active repo: {active_repo_path or 'unknown'}."})
    if repo_rows:
        top_repo = repo_rows[0]
        if no_qualified_papers and not repo_backtracking_done:
            actions.append({'priority': 'P0', 'title': 'Audit top repo README and linked paper before selecting an idea', 'reason': 'Repo candidates are currently the strongest signal; inspect install commands, data requirements, claimed paper title, and benchmark before planning experiments.', 'evidence': f"Top repo is {top_repo.get('name', '')} ({top_repo.get('url', '')})."})
        if not (paths.state / 'repo_env_bootstrap.json').exists():
            actions.append({'priority': 'P0', 'title': 'Bootstrap the selected repo environment', 'reason': 'Real experiment execution should not wait until after hypothesis generation.', 'evidence': f"Top repo is {top_repo.get('name', '')} and no environment bootstrap record exists yet."})
    else:
        actions.append({'priority': 'P0', 'title': 'Register at least one runnable repo', 'reason': 'The loop cannot enter execution without a concrete codebase.', 'evidence': 'No repo candidates are registered.'})

    blocked_data = repo_data_requirements.get('blocked_datasets', []) if isinstance(repo_data_requirements, dict) else []
    ready_data = repo_data_requirements.get('ready_datasets', []) if isinstance(repo_data_requirements, dict) else []
    download_sources = repo_data_requirements.get('download_sources', []) if isinstance(repo_data_requirements, dict) else []
    if blocked_data and isinstance(data_policy, dict) and data_policy.get('decision'):
        actions.append({'priority': 'P0', 'title': f"Follow data-unavailability policy: {data_policy.get('decision')}", 'reason': data_policy.get('rationale', 'Active repo data is unavailable and requires a bounded policy decision.'), 'evidence': f"Policy file: {paths.reports / 'data_unavailability_policy.md'}"})
    if blocked_data:
        source_bits = []
        for source in download_sources[:2]:
            code = ','.join(source.get('passwords_or_codes_found', []) or []) or 'none'
            source_bits.append(f"{source.get('url', '')} (code={code})")
        contract = dataset_contract_label(repo_data_requirements.get('contract', {}).get('required_files_per_dataset', []))
        actions.append({'priority': 'P0', 'title': 'Acquire and verify active-repo real datasets', 'reason': 'The selected repo declares additional datasets, but they remain candidate data gaps until the active repo loader passes. Real experiments and paper claims should use loader-ready datasets only.', 'evidence': f"Blocked candidate datasets: {', '.join(blocked_data[:6])}; loader contract: {contract}; sources: {'; '.join(source_bits) or 'none detected'}."})
    elif ready_data and not probed_real_datasets:
        actions.append({'priority': 'P0', 'title': 'Probe real repo dataset loaders before real experiments', 'reason': 'Data files are present, but the selected repo loader must instantiate them before claims can rely on them.', 'evidence': f"Ready data directories: {', '.join(ready_data[:6])}."})
    elif ready_data and not completed_real_experiments:
        actions.append({'priority': 'P0', 'title': 'Launch a real-dataset repo reproduction smoke run', 'reason': 'At least one real repo dataset loader has passed; the loop should now produce auditable real-dataset experiment artifacts before any paper claim can advance.', 'evidence': f"Probe-passed real datasets: {', '.join(sorted(str(x) for x in probed_real_datasets if x)) or ', '.join(ready_data[:3])}."})
    elif not dataset_rows:
        actions.append({'priority': 'P0', 'title': 'Register and audit a dataset', 'reason': 'No dataset-backed benchmark can run yet.', 'evidence': 'No datasets are registered.'})

    audit_incomplete = [row for row in ranked_methods if row.get('audit_incomplete_runs', 0) > 0]
    if audit_incomplete:
        actions.append({'priority': 'P0', 'title': 'Repair audit-contract failures before more tuning', 'reason': 'Runs without valid metrics/bad-cases/claim audit create false scientific momentum.', 'evidence': f"Audit-incomplete methods: {method_list_label([row['method'] for row in audit_incomplete], cfg)}."})

    pending_launch = [row for row in ranked_methods if row.get('launch_ready') and row.get('completed', 0) + row.get('failed', 0) == 0 and row.get('recommendation') not in PRUNE_RECOMMENDATIONS and row.get('repo_path', '') not in paused_repos]
    if pending_launch:
        actions.append({'priority': 'P1', 'title': 'Launch the first wave of parallel methods', 'reason': 'Planned methods exist but have not generated any evidence yet.', 'evidence': f"Launch-ready methods without runs: {method_list_label([row['method'] for row in pending_launch], cfg, 3)}."})

    missing_bad_cases = [row for row in ranked_methods if row.get('completed', 0) + row.get('failed', 0) > 0 and row.get('bad_case_slice_count', 0) == 0]
    if missing_bad_cases:
        actions.append({'priority': 'P1', 'title': 'Require bad-case export for active methods', 'reason': 'Without bad-case slices, the loop cannot do targeted scientific diagnosis or slice-aware improvement.', 'evidence': f"Methods without bad-case slice evidence: {method_list_label([row['method'] for row in missing_bad_cases], cfg, 3)}."})

    weak_claim = [row for row in ranked_methods if row.get('claim_strength_score', 0) < 0]
    if weak_claim:
        actions.append({'priority': 'P1', 'title': 'Re-evaluate weak claim paths before more tuning', 'reason': 'Methods that weaken their own claim should not silently absorb more budget.', 'evidence': f"Claim-weak methods: {method_list_label([row['method'] for row in weak_claim], cfg, 3)}."})

    counterexample_negative = [row for row in ranked_methods if row.get('counterexample_score', 0) < 0]
    if counterexample_negative:
        actions.append({'priority': 'P1', 'title': 'Prune or narrow methods broken by counterexamples', 'reason': 'Negative counterexample outcomes mean the current story is unsafe.', 'evidence': f"Methods failing counterexample pressure: {method_list_label([row['method'] for row in counterexample_negative], cfg, 3)}."})

    prunable = [row for row in ranked_methods if row.get('recommendation') in PRUNE_RECOMMENDATIONS]
    if prunable:
        actions.append({'priority': 'P1', 'title': 'Prune or pause the weakest lagging method', 'reason': 'Resource should shift toward methods with stronger evidence or more repairable failure modes.', 'evidence': f"Prune candidates: {method_list_label([row['method'] for row in prunable], cfg, 3)}."})

    strongest = next((row for row in ranked_methods if row.get('deepen_ready')), None)
    if strongest:
        actions.append({'priority': 'P1', 'title': f"Deepen the strongest current path: {method_label(strongest['method'], cfg)}", 'reason': 'This method has supportive claim signal, bad-case evidence, and acceptable novelty/counterexample pressure.', 'evidence': f"decision_score={strongest['decision_score']}, best_{strongest['metric_name']}={strongest['best_metric']}."})
    elif ranked_methods:
        actions.append({'priority': 'P1', 'title': 'Do not deepen any method yet', 'reason': 'No active path has passed the evidence gate for deepening.', 'evidence': 'Current methods are missing either supportive claim evidence, bad-case slices, or acceptable novelty/counterexample signals.'})

    if weak_paper_signal:
        actions.append({'priority': 'P1', 'title': 'Tighten novelty framing before scaling experiments', 'reason': 'If imported papers all look weak or incremental, more compute alone is unlikely to create a top-tier contribution.', 'evidence': 'Paper quality heuristics currently do not show a clearly promising top-tier direction.'})

    unsupported_claims = [claim.get('claim_type', '') for claim in claim_ledger.get('claims', []) if claim.get('status') in {'unsupported', 'weak'}]
    if unsupported_claims:
        actions.append({'priority': 'P1', 'title': 'Repair unsupported paper claims with targeted experiments', 'reason': 'Paper-level claims are not yet evidence-backed enough for top-tier writing.', 'evidence': f"Unsupported or weak claims: {', '.join(unsupported_claims[:3])}."})

    if audit_md and 'No experiment exported useful bad-case slice evidence.' in audit_md:
        actions.append({'priority': 'P1', 'title': 'Block paper promotion until experiment evidence is richer', 'reason': 'The paper audit is still flagging missing empirical support.', 'evidence': 'Paper evidence audit still reports absent bad-case or claim-verdict evidence.'})

    if manifest and manifest.get('methods_deepen_ready_count', 0) == 0 and ranked_methods:
        actions.append({'priority': 'P2', 'title': 'Improve evidence density before more paper polishing', 'reason': 'Manifest-level evidence still says no method is ready to deepen.', 'evidence': f"methods_deepen_ready_count={manifest.get('methods_deepen_ready_count', 0)}."})

    out_json = paths.state / 'next_actions.json'
    out_json.write_text(json.dumps({'actions': actions, 'method_summaries': method_summaries}, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    lines = ['# Next Actions\n\n']
    lines.append(f'- visible_gpu_count: {visible_gpus}\n')
    lines.append(f"- tracked_methods: {len(method_summaries)}\n")
    lines.append(f"- experiments_logged: {len(experiments)}\n")
    lines.append(f"- real_loader_probe_passed: {bool(probed_real_datasets)}\n")
    lines.append(f"- completed_real_dataset_experiments: {len(completed_real_experiments)}\n")
    if isinstance(data_policy, dict) and data_policy.get('decision'):
        lines.append(f"- data_unavailability_decision: {data_policy.get('decision')}\n")
    lines.append('\n')
    lines.append('## Priority Queue\n')
    for index, action in enumerate(actions, start=1):
        lines.append(f"{index}. [{action['priority']}] {action['title']}\n")
        lines.append(f"   reason: {action['reason']}\n")
        lines.append(f"   evidence: {action['evidence']}\n")
    lines.append('\n## Method Decision Board\n')
    lines.append('| Method | Completed | Failed | Best Metric | Claim Score | Novelty | Counterexample | Bad Slices | Audit Ready | Decision Score | Recommendation |\n')
    lines.append('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n')
    for row in ranked_methods:
        lines.append(
            f"| {method_label(row['method'], cfg)} | {row['completed']} | {row['failed']} | {row['best_metric']} | {row['claim_strength_score']} | {row['novelty_score']} | {row['counterexample_score']} | {row['bad_case_slice_count']} | {row['audit_ready_runs']} | {row['decision_score']} | {row['recommendation'] or 'open'} |\n"
        )
    out = paths.planning / 'next_actions.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
