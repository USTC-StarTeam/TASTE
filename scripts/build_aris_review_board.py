#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from experiment_contracts import row_promotion_blockers
from project_paths import build_paths


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default


def numeric(value):
    try:
        return float(value)
    except Exception:
        return None


def method_rows(experiments: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in experiments:
        method = str(row.get('method') or row.get('method_slug') or row.get('experiment_id') or 'unknown')
        grouped[method].append(row)
    return grouped


def verdict_for(rows: list[dict]) -> dict:
    completed = [r for r in rows if str(r.get('status', '')).lower() in {'completed', 'success'}]
    excluded = [r for r in completed if row_promotion_blockers(r)]
    audit_ready = [r for r in completed if r.get('audit_ready') and r not in excluded]
    metrics = [numeric(r.get('metric_value')) for r in audit_ready]
    metrics = [m for m in metrics if m is not None]
    tail_values = []
    for row in audit_ready:
        payload = row.get('metrics') if isinstance(row.get('metrics'), dict) else {}
        val = numeric(payload.get('tail_ndcg_at_10') or payload.get('tail_metric') or payload.get('worst_slice_metric'))
        if val is not None:
            tail_values.append(val)
    claim_support = sum(1 for r in audit_ready if str(r.get('claim_verdict', '')).lower() in {'support', 'supported', 'pass', 'partially_supported'})
    counterexamples = sum(1 for r in audit_ready if str(r.get('counterexample_outcome', '')).strip())
    bad_cases = sum(1 for r in audit_ready if r.get('bad_case_path') or r.get('bad_case_slices'))
    synthetic_only = all(str(r.get('dataset', '')).startswith('synthetic') for r in audit_ready) if audit_ready else True
    best = max(metrics) if metrics else None
    weakest_tail = min(tail_values) if tail_values else None
    issues = []
    if not completed:
        issues.append('no completed run')
    if not audit_ready:
        issues.append('no audit-ready run')
    for row in excluded[:3]:
        issues.append(f"non-promotable evidence status: {row.get('experiment_id') or row.get('name')} ({', '.join(row_promotion_blockers(row)[:3])})")
    if bad_cases == 0:
        issues.append('no bad-case evidence')
    if counterexamples == 0:
        issues.append('no counterexample pressure')
    if synthetic_only:
        issues.append('synthetic-only evidence')
    if claim_support == 0:
        issues.append('no supporting claim verdict')
    if not issues and len(audit_ready) >= 2:
        recommendation = 'deepen'
    elif audit_ready and bad_cases and claim_support:
        recommendation = 'compare_or_repair'
    elif completed:
        recommendation = 'repair_or_prune'
    else:
        recommendation = 'block'
    return {
        'completed': len(completed),
        'audit_ready': len(audit_ready),
        'best_metric': best,
        'weakest_tail_metric': weakest_tail,
        'claim_support_count': claim_support,
        'bad_case_count': bad_cases,
        'counterexample_count': counterexamples,
        'synthetic_only': synthetic_only,
        'issues': issues,
        'recommendation': recommendation,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()
    paths = build_paths(args.project)
    experiments = load_json(paths.state / 'experiment_registry.json', [])
    if not isinstance(experiments, list):
        experiments = []
    pruned = load_json(paths.state / 'pruned_methods.json', {})
    if not isinstance(pruned, dict):
        pruned = {}
    pruned_set = set(pruned.get('methods', []))
    board = []
    for method, rows in sorted(method_rows(experiments).items()):
        entry = {'method': method, **verdict_for(rows)}
        if method in pruned_set:
            pruned_info = pruned.get('reasons', {}).get(method, 'manual prune decision')
            entry['recommendation'] = 'pruned'
            if 'issues' in entry:
                entry['issues'] = [f'pruned: {pruned_info}'] + [i for i in entry['issues'] if i != 'no supporting claim verdict']
        board.append(entry)
    blockers = []
    if not board:
        blockers.append('No experiments exist.')
    if not any(row['recommendation'] in {'deepen', 'compare_or_repair'} for row in board):
        blockers.append('No method is ready for confident deepening.')
    if not any(not row['synthetic_only'] for row in board):
        blockers.append('No method has real-dataset evidence; paper promotion must remain blocked.')
    payload = {'project': args.project, 'reviewers': ['executor', 'bad_case_critic', 'claim_skeptic', 'prune_chair'], 'methods': board, 'blockers': blockers}
    (paths.state / 'aris_review_board.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    lines = ['# ARIS-Style Review Board\n\n']
    lines.append('Reviewers: executor, bad_case_critic, claim_skeptic, prune_chair.\n\n')
    for row in board:
        lines.append(f"## {row['method']}\n\n")
        for key in ['recommendation','completed','audit_ready','best_metric','weakest_tail_metric','claim_support_count','bad_case_count','counterexample_count','synthetic_only']:
            lines.append(f"- {key}: {row.get(key)}\n")
        lines.append(f"- issues: {', '.join(row['issues']) if row['issues'] else 'none'}\n\n")
    lines.append('## Global Blockers\n\n')
    if blockers:
        for item in blockers:
            lines.append(f'- {item}\n')
    else:
        lines.append('- No global blocker detected.\n')
    out = paths.reports / 'aris_review_board.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
