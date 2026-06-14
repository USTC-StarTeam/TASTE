#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiment_contracts import (
    claim_strength_score,
    counterexample_score,
    extract_bad_case_summary,
    metric_higher_is_better,
    novelty_score,
    parse_float,
    row_matches_method,
    save_json,
)
from project_paths import build_paths, load_project_config


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def attach_failure_path(registry_path: Path, method: str, experiment_id: str, failure_path: Path, recommendation: str) -> None:
    rows = load_json(registry_path)
    changed = False
    for row in rows:
        if experiment_id and row.get('experiment_id') != experiment_id:
            continue
        if not experiment_id and row.get('method') != method:
            continue
        row['failure_analysis_path'] = str(failure_path)
        row['decision'] = recommendation
        changed = True
    if changed:
        save_json(registry_path, rows)


def collect_methods(plan: dict | list) -> list[dict]:
    if isinstance(plan, list):
        return plan
    if isinstance(plan, dict):
        return plan.get('methods', [])
    return []


def analyze_method(paths, cfg: dict, method: str, experiment_id: str, experiment_rows: list[dict], plan_method: dict, explicit_bad_case: str, result_summary: str, hparams: str, module_notes: str, impl_notes: str) -> tuple[Path, Path, str]:
    relevant = [row for row in experiment_rows if (row.get('experiment_id') == experiment_id if experiment_id else row_matches_method(row, plan_method or {'method': method, 'method_slug': method}))]
    relevant.sort(key=lambda row: row.get('timestamp', ''))
    if not relevant and not result_summary:
        raise SystemExit(f'No experiments found for method {method}')

    metric_name = plan_method.get('metric') or next((row.get('metric_name') for row in relevant if row.get('metric_name')), '') or 'metric'
    higher_is_better = metric_higher_is_better(metric_name)
    values = [parse_float(row.get('metric_value')) for row in relevant]
    values = [value for value in values if value is not None]
    best_metric = max(values) if values and higher_is_better else min(values) if values else None
    return_codes = [row.get('return_code') for row in relevant if row.get('return_code') is not None]
    failed_runs = [row for row in relevant if str(row.get('status', '')).lower() in {'failed', 'error', 'incomplete_audit'} or row.get('return_code') not in (None, 0)]
    completed_runs = [row for row in relevant if str(row.get('status', '')).lower() in {'completed', 'success'} and row.get('return_code') in (None, 0)]

    registry_path = paths.state / 'experiment_registry.json'
    all_rows = load_json(registry_path)
    all_metric_values = [parse_float(row.get('metric_value')) for row in all_rows if row.get('metric_name') == metric_name]
    all_metric_values = [value for value in all_metric_values if value is not None]
    overall_best = max(all_metric_values) if all_metric_values and higher_is_better else min(all_metric_values) if all_metric_values else None
    gap_to_best = None
    if best_metric is not None and overall_best is not None:
        gap_to_best = (overall_best - best_metric) if higher_is_better else (best_metric - overall_best)

    candidate_bad_case = explicit_bad_case or next((row.get('bad_case_path', '') for row in reversed(relevant) if row.get('bad_case_path')), '')
    bad_case_summary = extract_bad_case_summary(candidate_bad_case) if candidate_bad_case else {'exists': False, 'summary_lines': ['No bad-case file available.'], 'slices': []}

    attempts = len(relevant)
    min_followup = cfg.get('failure_analysis', {}).get('min_followup_attempts', 2)
    early_drop_patience = cfg.get('failure_analysis', {}).get('early_drop_patience', 2)
    max_attempts = cfg.get('failure_analysis', {}).get('max_total_attempts_per_method', 6)
    claim_score = claim_strength_score(relevant)
    novelty = novelty_score(relevant)
    counter_score = counterexample_score(relevant)
    missing_bad_cases = not bad_case_summary.get('exists')
    method_contract = plan_method.get('claim_contract', {}) if isinstance(plan_method, dict) else {}

    causes: list[str] = []
    actions: list[str] = []
    recommendation = 'continue_with_targeted_followup'
    decision_stage = 'repair'

    if any(str(row.get('status', '')).lower() == 'incomplete_audit' for row in relevant):
        causes.append('At least one run completed process-wise but failed the audit contract, so the evidence is not trustworthy yet.')
        actions.append('Repair audit.json emission before interpreting aggregate metrics.')
        recommendation = 'repair_metric_logging'
    if return_codes and any(code != 0 for code in return_codes):
        causes.append('Implementation or environment execution risk is present because at least one run exited non-zero.')
        actions.append('Re-run a minimal smoke test and inspect the exact failing code path before spending more search budget.')
        recommendation = 'fix_implementation_or_environment'
        decision_stage = 'repair'
    if best_metric is None:
        causes.append('No reliable scalar metric was parsed from the logged runs.')
        actions.append('Standardize metric emission into audit.json so comparison across methods is machine-readable.')
        recommendation = 'repair_metric_logging'
        decision_stage = 'repair'
    if bad_case_summary.get('exists'):
        causes.append('Bad-case evidence exists; the weakest slices should shape the next trial design rather than tuning globally.')
        actions.append('Cluster the worst examples by slice or failure type and target them explicitly in the next run.')
        if recommendation not in {'fix_implementation_or_environment', 'repair_metric_logging'}:
            recommendation = 'focus_on_bad_slices'
            decision_stage = 'diagnose'
    if attempts < min_followup:
        causes.append('This method has not yet received the minimum number of evidence-driven follow-up attempts.')
        actions.append('Keep one or two follow-up runs, but make each one hypothesis-driven rather than random retuning.')
    if claim_score < 0:
        causes.append('Claim-level evidence is currently weakening rather than supporting the intended story.')
        actions.append('Reformulate the claim or target a narrower slice before more broad tuning.')
    if novelty < 0:
        causes.append('Current novelty signal looks incremental or tuning-like rather than conceptually distinct.')
        actions.append('Do not scale this method unless it shows a clearer hard-slice or robustness advantage.')
    if counter_score < 0:
        causes.append('Counterexample or stress-test evidence is already breaking the current claim.')
        actions.append('Treat the current method story as unsafe and either narrow the claim or prune the path.')
        recommendation = 'pause_or_prune'
        decision_stage = 'prune'
    if missing_bad_cases and attempts >= min_followup:
        causes.append('The method has multiple attempts but still no bad-case evidence, which blocks targeted scientific diagnosis.')
        actions.append('Require bad-case export before granting more parallel budget to this method.')
        if recommendation not in {'pause_or_prune', 'fix_implementation_or_environment'}:
            recommendation = 'export_bad_cases_before_scaling'
            decision_stage = 'diagnose'
    if gap_to_best is not None and gap_to_best > 0 and attempts >= early_drop_patience:
        causes.append(f'This method currently trails the best observed {metric_name} by {gap_to_best:.4f}.')
        actions.append('Do not keep scaling this method blindly; compare its error profile with stronger methods before deciding to continue.')
        if recommendation in {'continue_with_targeted_followup', 'focus_on_bad_slices', 'export_bad_cases_before_scaling'}:
            recommendation = 'compare_then_prune_or_pause'
            decision_stage = 'prune'
    if claim_score < 0 and novelty <= 0 and attempts >= early_drop_patience:
        causes.append('Both claim strength and novelty quality are weak after repeated attempts.')
        actions.append('Pause this method unless there is a clear implementation bug or unusually strong slice-specific upside.')
        recommendation = 'pause_or_prune'
        decision_stage = 'prune'
    if attempts >= max_attempts:
        causes.append('This method has consumed the configured maximum number of total attempts.')
        actions.append('Pause the method unless new evidence suggests a concrete implementation bug or a clearly repairable slice issue.')
        recommendation = 'pause_or_prune'
        decision_stage = 'prune'
    if method_contract:
        actions.append(f"Claim contract reminder: {method_contract.get('continue_rule', 'Need explicit continue rule.')}")
        actions.append(f"Prune contract reminder: {method_contract.get('prune_rule', 'Need explicit prune rule.')}")
    if not causes:
        causes.append('Failure signal is weakly specified; enforce better logging and compare against the strongest baseline.')
        actions.append('Record richer metrics, git snapshot, bad cases, and counterexample outcomes on the next run.')

    latest_summary = result_summary or (relevant[-1].get('result', '') if relevant else 'No explicit result summary provided.')
    analysis = {
        'method': method,
        'experiment_id': experiment_id,
        'metric_name': metric_name,
        'higher_is_better': higher_is_better,
        'attempts': attempts,
        'completed_runs': len(completed_runs),
        'failed_runs': len(failed_runs),
        'best_metric': best_metric,
        'overall_best_metric': overall_best,
        'gap_to_best': gap_to_best,
        'return_codes': return_codes,
        'latest_result_summary': latest_summary,
        'hyperparameter_notes': hparams or 'Need explicit hyperparameter audit.',
        'module_notes': module_notes or 'Need explicit module interaction review.',
        'implementation_notes': impl_notes or 'Need explicit code-path sanity check.',
        'claim_strength_score': claim_score,
        'novelty_score': novelty,
        'counterexample_score': counter_score,
        'bad_case_summary': bad_case_summary,
        'bad_case_slices': bad_case_summary.get('slices', []),
        'causes': causes,
        'recommended_actions': actions,
        'recommendation': recommendation,
        'decision_stage': decision_stage,
    }

    json_out = paths.state / f'failure_analysis_{method}.json'
    save_json(json_out, analysis)
    md_out = paths.reports / f'failure_analysis_{method}.md'
    lines = [
        f'# Failure Analysis: {method}\n\n',
        f'- experiment_id: {experiment_id or ""}\n',
        f'- attempts: {attempts}\n',
        f'- completed_runs: {len(completed_runs)}\n',
        f'- failed_runs: {len(failed_runs)}\n',
        f'- metric_name: {metric_name}\n',
        f'- best_metric: {best_metric}\n',
        f'- overall_best_metric: {overall_best}\n',
        f'- gap_to_best: {gap_to_best}\n',
        f'- claim_strength_score: {claim_score}\n',
        f'- novelty_score: {novelty}\n',
        f'- counterexample_score: {counter_score}\n',
        f'- decision_stage: {decision_stage}\n',
        f'- recommendation: {recommendation}\n\n',
        '## Latest Result Summary\n',
        f'{latest_summary}\n\n',
        '## Hyperparameter Check\n',
        f'{analysis["hyperparameter_notes"]}\n\n',
        '## Module Coordination Check\n',
        f'{analysis["module_notes"]}\n\n',
        '## Implementation Risk Check\n',
        f'{analysis["implementation_notes"]}\n\n',
        '## Bad-Case Focus\n',
    ]
    for slice_name in bad_case_summary.get('slices', []):
        lines.append(f'- slice: {slice_name}\n')
    if not bad_case_summary.get('slices'):
        lines.append('- no extracted slices yet\n')
    lines.extend(['\n## Diagnosed Causes\n'])
    for cause in causes:
        lines.append(f'- {cause}\n')
    lines.extend(['\n## Recommended Actions\n'])
    for action in actions:
        lines.append(f'- {action}\n')
    md_out.write_text(''.join(lines), encoding='utf-8')
    attach_failure_path(registry_path, method, experiment_id, md_out, recommendation)
    return md_out, json_out, recommendation


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--method')
    parser.add_argument('--experiment-id', default='')
    parser.add_argument('--all-failed', action='store_true')
    parser.add_argument('--result-summary', default='')
    parser.add_argument('--bad-case-file')
    parser.add_argument('--hparams', default='')
    parser.add_argument('--module-notes', default='')
    parser.add_argument('--impl-notes', default='')
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    experiment_rows = load_json(paths.state / 'experiment_registry.json')
    plan_path = paths.state / 'parallel_plan.json'
    plan = json.loads(plan_path.read_text(encoding='utf-8')) if plan_path.exists() else {'methods': []}
    methods = collect_methods(plan)

    targets: list[tuple[str, str]] = []
    if args.method:
        targets = [(args.method, args.experiment_id)]
    elif args.all_failed:
        for row in experiment_rows:
            if str(row.get('status', '')).lower() in {'failed', 'error', 'incomplete_audit'} and row.get('method'):
                targets.append((row.get('method', ''), row.get('experiment_id', '')))
    else:
        raise SystemExit('Provide --method or --all-failed')

    outputs = []
    for method, experiment_id in targets:
        plan_method = next((row for row in methods if row_matches_method({'method': method, 'experiment_id': experiment_id}, row)), {'method': method, 'method_slug': method})
        md_out, _, recommendation = analyze_method(
            paths=paths,
            cfg=cfg,
            method=method,
            experiment_id=experiment_id,
            experiment_rows=experiment_rows,
            plan_method=plan_method,
            explicit_bad_case=args.bad_case_file or '',
            result_summary=args.result_summary,
            hparams=args.hparams,
            module_notes=args.module_notes,
            impl_notes=args.impl_notes,
        )
        outputs.append((method, md_out, recommendation))

    for method, path, recommendation in outputs:
        print(f'{method}\t{recommendation}\t{path}')


if __name__ == '__main__':
    main()
