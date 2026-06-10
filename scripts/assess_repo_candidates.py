#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from literature_policy import now_utc, repo_sort_key, score_repo_candidate
from project_paths import build_paths, load_project_config


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    registry = paths.state / 'repo_candidates.json'
    rows = load_json(registry)
    overrides = load_json(paths.state / 'method_overrides.json') if (paths.state / 'method_overrides.json').exists() else {'repos': {}}
    paused_repos = overrides.get('repos', {}) if isinstance(overrides, dict) else {}
    refreshed = []
    for row in rows:
        item = dict(row)
        item.update(score_repo_candidate(item, cfg, reference_time=now_utc()))
        local_path = str(item.get('local_path', ''))
        if local_path and paused_repos.get(local_path, {}).get('status') in {'paused_or_abandoned', 'abandoned'}:
            item['repo_selection_bucket'] = 'paused_by_veto'
            item['score'] = -100.0
            item['repo_reuse_score'] = -100.0
            item['notes'] = (str(item.get('notes', '')) + ' | paused_by_research_veto').strip(' |')
        else:
            item['score'] = item.get('repo_reuse_score', item.get('score', 0))
        refreshed.append(item)
    refreshed.sort(key=repo_sort_key)
    registry.write_text(json.dumps(refreshed, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    md = ['# Repo Candidates\n\n', '| Score | Name | URL | Fit | Activity | Install | Entrypoint | Notes |\n', '| --- | --- | --- | --- | --- | --- | --- | --- |\n']
    for row in refreshed:
        md.append(
            f"| {row.get('score', 0)} | {row.get('name', '')} | {row.get('url', '')} | {row.get('task_fit', False)} | {row.get('activity_bucket', row.get('recent_activity', False))} | {row.get('has_install', False)} | {row.get('has_entrypoint', False)} | {row.get('notes', '')} |\n"
        )
    (paths.reports / 'repo_candidates.md').write_text(''.join(md), encoding='utf-8')
    print(paths.reports / 'repo_candidates.md')


if __name__ == '__main__':
    main()
