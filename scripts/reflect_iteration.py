#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiment_contracts import PRUNE_RECOMMENDATIONS
from project_paths import build_paths


def read(path: Path) -> str:
    return path.read_text(encoding='utf-8') if path.exists() else ''


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def ratio(num: int, den: int) -> float:
    return 0.0 if den == 0 else round(num / den, 4)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    paths = build_paths(args.project)
    lint = read(paths.reports / 'lint_report.md')
    health = read(paths.reports / 'healthcheck.md')
    init_brief = read(paths.planning / 'init_brief.md')
    quality = load_json(paths.state / 'paper_quality.json')
    quality_md = read(paths.planning / 'paper_quality.md')
    machine = read(paths.reports / 'machine_profile.md')
    parallel_plan = read(paths.planning / 'parallel_experiment_plan.md')
    next_actions = read(paths.planning / 'next_actions.md')
    next_actions_json = load_json(paths.state / 'next_actions.json')
    experiments = load_json(paths.state / 'experiment_registry.json')
    if not isinstance(experiments, list):
        experiments = []
    failure_analyses = [load_json(path) for path in sorted(paths.state.glob('failure_analysis_*.json'))]
    lint_json = load_json(paths.state / 'lint_report.json')
    manifest = load_json(paths.state / 'research_manifest.json')

    completed = [row for row in experiments if str(row.get('status', '')).lower() in {'completed', 'success'}]
    failed = [row for row in experiments if str(row.get('status', '')).lower() in {'failed', 'error', 'incomplete_audit'}]
    analyzed_failed = [row for row in failed if row.get('failure_analysis_path')]
    bad_case_runs = [row for row in experiments if row.get('bad_case_slices')]
    claim_checked = [row for row in experiments if row.get('claim_verdict')]
    counterexample_checked = [row for row in experiments if row.get('counterexample_outcome')]
    audit_ready = [row for row in experiments if row.get('audit_ready')]
    prune_ready = [item for item in failure_analyses if item.get('recommendation') in PRUNE_RECOMMENDATIONS]
    deepen_ready_count = manifest.get('methods_deepen_ready_count', 0) if isinstance(manifest, dict) else 0
    method_summaries = next_actions_json.get('method_summaries', []) if isinstance(next_actions_json, dict) else []
    active_methods = len(method_summaries)
    claim_ratio = ratio(sum(1 for row in method_summaries if row.get('claim_strength_score', 0) != 0), active_methods)
    bad_case_ratio = ratio(sum(1 for row in method_summaries if row.get('bad_case_slice_count', 0) > 0), active_methods)
    counterexample_ratio = ratio(sum(1 for row in method_summaries if row.get('counterexample_score', 0) != 0), active_methods)
    audit_ratio = ratio(len(audit_ready), len(experiments))
    prune_ratio = ratio(len(prune_ready), active_methods)

    closure_signals = {
        'planning_ready': 1.0 if parallel_plan.strip() else 0.0,
        'execution_ready': 1.0 if any(row.get('command') for row in experiments) else 0.0,
        'execution_evidence': 1.0 if (completed or failed) else 0.0,
        'failure_diagnosis': ratio(len(analyzed_failed), len(failed)) if failed else 0.0,
        'claim_coverage': claim_ratio,
        'bad_case_coverage': bad_case_ratio,
        'counterexample_coverage': counterexample_ratio,
        'audit_contract_coverage': audit_ratio,
        'prune_discipline': prune_ratio,
        'deepen_gate_readiness': ratio(deepen_ready_count, active_methods) if active_methods else 0.0,
    }
    score = round(sum(closure_signals.values()), 2)

    paper_rows = quality.get('papers', []) if isinstance(quality, dict) else []
    promising_papers = [row for row in paper_rows if row.get('top_tier_readiness') == 'promising']
    weak_papers = [row for row in paper_rows if row.get('top_tier_readiness') == 'weak']

    lines = [
        '# Iteration Reflection\n\n',
        f'- closure_score: {score}/{len(closure_signals)}\n',
        f'- experiments_completed: {len(completed)}\n',
        f'- experiments_failed_or_incomplete: {len(failed)}\n',
        f'- failed_with_analysis: {len(analyzed_failed)}\n',
        f'- bad_case_runs: {len(bad_case_runs)}\n',
        f'- claim_checked_runs: {len(claim_checked)}\n',
        f'- counterexample_checked_runs: {len(counterexample_checked)}\n',
        f'- audit_ready_runs: {len(audit_ready)}\n',
        f'- prune_candidates: {len(prune_ready)}\n\n',
        '## Loop Health\n',
    ]
    for key, value in closure_signals.items():
        lines.append(f'- {key}: {value}\n')

    lines.extend([
        '\n## Decision Quality\n',
        f'- active_methods: {active_methods}\n',
        f'- methods_deepen_ready_count: {deepen_ready_count}\n',
        f'- methods_with_claim_signal_ratio: {claim_ratio}\n',
        f'- methods_with_bad_case_ratio: {bad_case_ratio}\n',
        f'- methods_with_counterexample_ratio: {counterexample_ratio}\n',
        f'- methods_with_prune_signal_ratio: {prune_ratio}\n',
        '\n## Research Quality Scorecard\n',
        f"- promising_paper_signals: {len(promising_papers)}\n",
        f"- weak_paper_signals: {len(weak_papers)}\n",
        f"- lint_quality_pressure: {lint_json.get('quality_pressure_count', 0) if isinstance(lint_json, dict) else 0}\n",
        '\n## What improved this round\n',
        '- Experiment planning and execution are now expected to emit audit-contract artifacts, not only stdout logs.\n',
        '- Next-step planning now ranks methods using claim strength, novelty pressure, counterexample outcomes, and bad-case slice evidence.\n',
        '- Reflection now scores evidence density and pruning discipline rather than only checking whether files exist.\n',
        '\n## What remains weak\n',
    ])
    if not completed and not failed:
        lines.append('- The loop still lacks real experiment evidence; it has not yet entered the run-diagnose-decide stage.\n')
    if failed and len(analyzed_failed) < len(failed):
        lines.append('- Some failed or audit-incomplete runs still do not have matching failure analyses.\n')
    if audit_ratio < 1.0:
        lines.append('- Not every run passes the audit contract yet, so some evidence is still untrustworthy.\n')
    if bad_case_ratio == 0:
        lines.append('- Bad-case extraction is still not consistently attached to active methods.\n')
    if claim_ratio == 0:
        lines.append('- Claim verdicts are still too sparse to support strong scientific pruning or promotion.\n')
    if deepen_ready_count == 0 and active_methods:
        lines.append('- No method is currently strong enough to justify deeper scaling.\n')

    lines.extend([
        '\n## Machine Constraints\n\n', machine or 'Machine profile not generated yet.\n', '\n\n',
        '## Signals from Lint\n\n', lint or 'No lint report generated yet.\n', '\n\n',
        '## Signals from Healthcheck\n\n', health or 'No healthcheck generated yet.\n', '\n\n',
        '## Initialization Pressure Points\n\n', init_brief or 'No initialization brief generated yet.\n', '\n\n',
        '## Paper Quality and Taste Checks\n\n', quality_md or 'No paper quality report generated yet.\n', '\n\n',
        '## Parallel Method Plan\n\n', parallel_plan or 'No parallel plan created yet.\n', '\n\n',
        '## Priority Next Actions\n\n', next_actions or 'No next-actions file created yet.\n',
    ])

    out = paths.reports / 'iteration_reflection.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
