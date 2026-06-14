#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from project_paths import ROOT, build_paths, load_project_config

from taste_pythonpath import ensure_taste_pythonpath
ensure_taste_pythonpath(ROOT)
from auto_research.source_selection import canonical_source_selection

GENERIC_DATA_BLOCKER_QUERIES = [
    'reproducible public dataset benchmark code',
    'open source implementation public benchmark dataset',
    'recent research code data reproducible benchmark',
    'public dataset loader reproducible baseline code',
]


def run(cmd: list[str], timeout: int = 300) -> dict:
    started = dt.datetime.now(dt.timezone.utc)
    try:
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
        rc = proc.returncode
        stdout = proc.stdout or ''
        stderr = proc.stderr or ''
    except subprocess.TimeoutExpired as exc:
        rc = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b'').decode('utf-8', 'ignore')
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b'').decode('utf-8', 'ignore')
        stderr = (stderr or '') + f'\nTIMEOUT after {timeout}s'
    finished = dt.datetime.now(dt.timezone.utc)
    return {
        'command': ' '.join(cmd),
        'return_code': rc,
        'started_at': started.isoformat(),
        'finished_at': finished.isoformat(),
        'duration_sec': round((finished - started).total_seconds(), 3),
        'stdout_tail': stdout[-2000:],
        'stderr_tail': stderr[-2000:],
    }


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


def summarize_candidates(paths, policy: dict) -> tuple[dict, list[dict]]:
    pool_audit = load_json(paths.state / 'repo_candidate_pool_audit.json', {})
    repos = load_json(paths.state / 'repo_candidates.json', [])
    active_path = str(policy.get('active_repo', {}).get('repo_path', '')) if isinstance(policy, dict) else ''
    top = []
    for row in repos if isinstance(repos, list) else []:
        if str(row.get('local_path', '')) == active_path:
            continue
        if row.get('repo_selection_bucket') == 'paused_by_veto' or row.get('hard_topic_mismatch'):
            continue
        top.append({
            'name': row.get('name', ''),
            'url': row.get('url', ''),
            'score': row.get('repo_reuse_score', row.get('score', 0)),
            'execution_ready': bool(row.get('repo_execution_ready')),
            'support_signals': row.get('repo_support_signals', []),
        })
    top = sorted(top, key=lambda x: (-float(x.get('score', 0) or 0), x.get('name', '')))[:10]
    return pool_audit, top



def rotate_queries(paths, cfg: dict, query_budget: int) -> tuple[list[str], dict]:
    project_queries = [str(item).strip() for item in (cfg.get('queries', []) or []) if str(item).strip()]
    if not project_queries:
        topic = str(cfg.get('topic') or cfg.get('research_interest') or '').strip()
        if topic:
            project_queries = [f"{topic} public dataset code", f"{topic} reproducible benchmark"]
    all_queries = list(dict.fromkeys(project_queries + GENERIC_DATA_BLOCKER_QUERIES))
    if not all_queries:
        all_queries = ['reproducible public dataset benchmark code']
    cursor_path = paths.state / 'data_blocker_restart_cursor.json'
    cursor = load_json(cursor_path, {'next_index': 0, 'history': []})
    next_index = int(cursor.get('next_index', 0) or 0) % len(all_queries)
    budget = max(1, min(query_budget, len(all_queries)))
    queries = [all_queries[(next_index + offset) % len(all_queries)] for offset in range(budget)]
    cursor.update({
        'total_queries': len(all_queries),
        'last_index': next_index,
        'next_index': (next_index + budget) % len(all_queries),
        'last_queries': queries,
        'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    })
    history = cursor.get('history', []) if isinstance(cursor.get('history', []), list) else []
    history.append({'timestamp': cursor['updated_at'], 'start_index': next_index, 'queries': queries})
    cursor['history'] = history[-50:]
    save_json(cursor_path, cursor)
    return queries, cursor

def write_report(paths, payload: dict) -> None:
    save_json(paths.state / 'data_blocker_restart.json', payload)
    lines = ['# Restart After Data Blocker\n\n']
    lines.append(f"- generated_at: {payload['generated_at']}\n")
    lines.append(f"- status: {payload['status']}\n")
    lines.append(f"- decision: {payload.get('decision', '')}\n")
    lines.append(f"- query_budget: {payload.get('query_budget', '')}\n")
    lines.append(f"- query_cursor_next_index: {payload.get('query_cursor', {}).get('next_index', '') if isinstance(payload.get('query_cursor', {}), dict) else ''}\n")
    lines.append(f"- command_count: {len(payload.get('commands', []))}\n")
    if payload.get('reason'):
        lines.append(f"- reason: {payload['reason']}\n")
    lines.append('\n## Queries\n')
    for query in payload.get('queries', []):
        lines.append(f"- {query}\n")
    if not payload.get('queries'):
        lines.append('- none\n')
    lines.append('\n## Command Outcomes\n')
    for cmd in payload.get('commands', [])[-20:]:
        lines.append(f"- rc={cmd.get('return_code')} duration={cmd.get('duration_sec', '')}s | `{cmd.get('command')}`\n")
        if int(cmd.get('return_code', 0) or 0) != 0:
            tail = (cmd.get('stderr_tail') or cmd.get('stdout_tail') or '').strip().replace('\n', ' | ')[:500]
            if tail:
                lines.append(f"  error_tail: {tail}\n")
    if not payload.get('commands'):
        lines.append('- none\n')
    lines.append('\n## Top Non-Active Candidates\n')
    for row in payload.get('top_non_active_candidates', []):
        lines.append(f"- score={row.get('score')} | ready={row.get('execution_ready')} | {row.get('name')} | {row.get('url')}\n")
    if not payload.get('top_non_active_candidates'):
        lines.append('- none yet\n')
    lines.append('\n## Guardrail\n')
    lines.append(f"- {payload.get('guardrail', 'No route switch without evidence.')}\n")
    out = paths.reports / 'data_blocker_restart.md'
    out.write_text(''.join(lines), encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description='Expand discovery after bounded active-repo data acquisition attempts fail.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--limit', type=int, default=8)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--query-budget', type=int, default=2, help='Maximum data-blocker queries per restart cycle.')
    parser.add_argument('--command-timeout-sec', type=int, default=45, help='Per-discovery-command timeout for bounded restart cycles.')
    args = parser.parse_args()

    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    policy = load_json(paths.state / 'data_unavailability_policy.json', {})
    decision = policy.get('decision', '') if isinstance(policy, dict) else ''
    allowed = decision in {'expand_discovery_or_request_user_data_before_switching', 'ask_user_for_data_or_expand_discovery', 'switch_or_backtrack_to_evidence_ready_repo'}
    payload = {
        'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'status': 'running',
        'decision': decision,
        'query_budget': max(1, args.query_budget),
        'queries': [],
        'query_cursor': {},
        'commands': [],
        'repo_candidate_pool_audit': {},
        'top_non_active_candidates': [],
        'guardrail': 'Do not switch to a newly discovered repo until local audit shows runnable entrypoints, dataset path, and loader/metric evidence.',
    }
    write_report(paths, payload)

    if not args.force and not allowed:
        payload.update({
            'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'status': 'skipped_policy_not_ready',
            'reason': 'Data blocker policy has not exhausted bounded acquisition attempts or already has a more direct action.',
        })
        write_report(paths, payload)
        print(paths.reports / 'data_blocker_restart.md')
        return

    queries, cursor = rotate_queries(paths, cfg, max(1, args.query_budget))
    payload['queries'] = queries
    payload['query_cursor'] = cursor
    write_report(paths, payload)
    selection = canonical_source_selection(project_config_path=paths.config)
    for query in queries:
        commands = []
        if selection.get('include_github'):
            commands.append([sys.executable, 'modules/finding/scripts/discover_github_repos.py', '--project', args.project, '--query', query, '--limit', str(args.limit)])
        if selection.get('include_arxiv'):
            commands.append([sys.executable, 'modules/finding/scripts/discover_arxiv.py', '--project', args.project, '--query', query, '--max-results', '5'])
        if not commands:
            payload['commands'].append({
                'command': 'external discovery skipped by canonical source selection',
                'return_code': 0,
                'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'finished_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'duration_sec': 0,
                'stdout_tail': '',
                'stderr_tail': '',
            })
            write_report(paths, payload)
        for cmd in commands:
            payload['commands'].append(run(cmd, timeout=args.command_timeout_sec))
            payload['generated_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            write_report(paths, payload)

    followups = [
        ([sys.executable, 'modules/finding/scripts/ingest_discovery.py', '--project', args.project, '--limit', str(args.limit)], min(120, args.command_timeout_sec)),
        ([sys.executable, 'modules/environment/scripts/assess_repo_candidates.py', '--project', args.project], min(120, args.command_timeout_sec)),
        ([sys.executable, 'modules/finding/scripts/assess_paper_quality.py', '--project', args.project], min(120, args.command_timeout_sec)),
        ([sys.executable, 'modules/ideation/scripts/assess_idea_candidates.py', '--project', args.project], min(120, args.command_timeout_sec)),
        ([sys.executable, 'modules/environment/scripts/audit_repo_candidate_pool.py', '--project', args.project, '--limit', str(max(1, min(3, args.limit)))], max(120, min(360, args.command_timeout_sec * 3))),
        ([sys.executable, 'modules/environment/scripts/reconcile_active_and_pool_candidates.py', '--project', args.project], min(120, args.command_timeout_sec)),
        ([sys.executable, 'modules/environment/scripts/data_unavailability_policy.py', '--project', args.project], min(120, args.command_timeout_sec)),
    ]
    for cmd, timeout in followups:
        payload['commands'].append(run(cmd, timeout=timeout))
        pool_audit, top = summarize_candidates(paths, policy)
        payload['repo_candidate_pool_audit'] = pool_audit
        payload['top_non_active_candidates'] = top
        payload['generated_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_report(paths, payload)

    pool_audit, top = summarize_candidates(paths, policy)
    payload.update({
        'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'status': 'completed_with_evidence_gates',
        'repo_candidate_pool_audit': pool_audit,
        'top_non_active_candidates': top,
    })
    write_report(paths, payload)
    print(paths.reports / 'data_blocker_restart.md')


if __name__ == '__main__':
    main()
