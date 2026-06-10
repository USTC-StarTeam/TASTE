#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from project_paths import ROOT, build_paths, load_project_config

WORKSPACE_ROOT = ROOT / "modules" / "taste"
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
from auto_research.source_selection import canonical_source_selection

GENERIC_RESTART_QUERIES = [
    'open source reproducible code dataset benchmark',
    'recent high quality research code public dataset',
    'reproducible baseline implementation benchmark',
]


def run(cmd: list[str], cwd: Path) -> int:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=240)
    if proc.stdout:
        print(proc.stdout, end='')
    if proc.stderr:
        print(proc.stderr, end='', file=sys.stderr)
    return proc.returncode


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def main() -> None:
    parser = argparse.ArgumentParser(description='Restart discovery after evidence-based critic veto, excluding paused repos.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--limit', type=int, default=8)
    args = parser.parse_args()
    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    project_queries = [str(item).strip() for item in (cfg.get('queries', []) or []) if str(item).strip()]
    if not project_queries:
        topic = str(cfg.get('topic') or cfg.get('research_interest') or '').strip()
        if topic:
            project_queries = [f"{topic} reproducible code dataset", f"{topic} public benchmark implementation"]
    queries = list(dict.fromkeys(project_queries + GENERIC_RESTART_QUERIES))[:8]
    selection = canonical_source_selection(project_config_path=paths.config)
    for query in queries:
        if selection.get('include_github'):
            run([sys.executable, 'scripts/discover_github_repos.py', '--project', args.project, '--query', query, '--limit', str(args.limit)], ROOT)
        if selection.get('include_arxiv'):
            run([sys.executable, 'scripts/discover_arxiv.py', '--project', args.project, '--query', query, '--max-results', '5'], ROOT)
    run([sys.executable, 'scripts/ingest_discovery.py', '--project', args.project, '--limit', '8'], ROOT)
    run([sys.executable, 'scripts/assess_repo_candidates.py', '--project', args.project], ROOT)
    run([sys.executable, 'scripts/assess_paper_quality.py', '--project', args.project], ROOT)
    run([sys.executable, 'scripts/assess_idea_candidates.py', '--project', args.project], ROOT)
    rows = load_json(paths.state / 'repo_candidates.json', [])
    non_vetoed = [row for row in rows if row.get('repo_selection_bucket') != 'paused_by_veto']
    out = paths.reports / 'restart_after_veto.md'
    lines = ['# Restart After Veto\n\n', f'- query_count: {len(queries)}\n', f'- non_vetoed_repo_candidates: {len(non_vetoed)}\n\n', '## Top Non-Vetoed Repos\n']
    for row in non_vetoed[:8]:
        lines.append(f"- score={row.get('score', row.get('repo_reuse_score', 0))} | {row.get('name', '')} | {row.get('url', '')}\n")
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
