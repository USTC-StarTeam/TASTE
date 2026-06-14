#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from literature_policy import repo_sort_key
from project_paths import build_paths


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description='Keep active repo evidence and exploratory pool audit evidence separate.')
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    paths = build_paths(args.project)
    active = load_json(paths.state / 'active_repo.json', {})
    pool = load_json(paths.state / 'repo_candidate_pool_audit.json', {})
    rows = load_json(paths.state / 'repo_candidates.json', [])
    active_path = str(active.get('repo_path', ''))
    active_name = str(active.get('name', ''))
    pool_by_name = {item.get('name'): item for item in pool.get('audited_candidates', [])} if isinstance(pool, dict) else {}
    changes = []
    reconciled = []
    for row in rows if isinstance(rows, list) else []:
        row = dict(row)
        name = row.get('name', '')
        local_path = str(row.get('local_path', ''))
        if name == active_name or (active_path and local_path == active_path):
            # Active repo evidence belongs in active_repo.json and should be reflected in candidate table.
            row['repo_execution_ready'] = bool(active.get('repo_execution_ready', row.get('repo_execution_ready')))
            row['repo_support_signals'] = active.get('repo_support_signals', row.get('repo_support_signals', []))
            if row.get('repo_execution_ready'):
                base = float(row.get('repo_reuse_score', row.get('score', 0)) or 0)
                row['repo_reuse_score'] = max(base, float(active.get('repo_reuse_score', base) or base))
                row['score'] = row['repo_reuse_score']
            row['notes'] = 'active repo; local audit evidence stored in active_repo.json'
        elif name in pool_by_name and row.get('notes') == 'auto-audited local repo':
            # Pool audits are exploratory. Preserve clone paths, but do not let side-effect audit scores promote route switching.
            original = float(row.get('repo_reuse_score', row.get('score', 0)) or 0)
            penalty = 3.0 if not pool_by_name[name].get('execution_ready_after_audit') else 0.0
            row['repo_reuse_score'] = max(0.0, original - penalty)
            row['score'] = row['repo_reuse_score']
            row['repo_execution_ready'] = False
            row['repo_support_signals'] = []
            row['repo_selection_bucket'] = 'needs_audit'
            row['notes'] = 'pool-audited candidate; not switch-ready until repo_candidate_pool_audit shows execution/data evidence'
            changes.append({'name': name, 'old_score': original, 'new_score': row['score'], 'reason': 'pool audit separated from active selection'})
        reconciled.append(row)
    reconciled = sorted(reconciled, key=repo_sort_key)
    save_json(paths.state / 'repo_candidates.json', reconciled)
    payload = {'project': args.project, 'changes': changes, 'active_repo': active, 'guardrail': 'Only active repo audits may promote active selection; pool audits must prove execution/data evidence before switching.'}
    save_json(paths.state / 'candidate_reconciliation.json', payload)
    lines = ['# Candidate Reconciliation\n\n', f"- changes: {len(changes)}\n", f"- guardrail: {payload['guardrail']}\n\n"]
    for item in changes:
        lines.append(f"- {item['name']}: {item['old_score']} -> {item['new_score']} | {item['reason']}\n")
    out = paths.reports / 'candidate_reconciliation.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
