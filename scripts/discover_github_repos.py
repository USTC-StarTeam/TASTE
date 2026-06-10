#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError
from pathlib import Path

from literature_policy import now_utc, repo_sort_key, score_repo_candidate
from project_paths import build_paths, load_project_config

WORKSPACE_ROOT = Path(__file__).resolve().parents[1] / "modules" / "taste"
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
from auto_research.source_selection import canonical_source_selection, source_enabled

API = 'https://api.github.com/search/repositories'
DEFAULT_USER_AGENT = 'research-workflow/0.2'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--query')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--sort')
    parser.add_argument('--order')
    parser.add_argument('--ignore-source-selection', action='store_true')
    return parser.parse_args()


def load_json(path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def save_json(path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def update_repo_registry(paths, rows: list[dict[str, object]]) -> None:
    registry_path = paths.state / 'repo_candidates.json'
    existing = load_json(registry_path)
    by_name = {row.get('name'): row for row in existing if isinstance(row, dict) and row.get('name')}
    for row in rows:
        name = row.get('name')
        if not name:
            continue
        previous = by_name.get(name, {})
        merged = dict(previous)
        merged.update(row)
        merged.setdefault('notes', 'github search candidate')
        by_name[name] = merged
    merged_rows = sorted(by_name.values(), key=repo_sort_key)
    save_json(registry_path, merged_rows)

    md = ['# Repo Candidates\n\n', '| Score | Name | URL | Fit | Activity | Install | Entrypoint | Notes |\n', '| --- | --- | --- | --- | --- | --- | --- | --- |\n']
    for row in merged_rows:
        md.append(
            f"| {row.get('score', row.get('repo_reuse_score', 0))} | {row.get('name', '')} | {row.get('url', '')} | {row.get('task_fit', False)} | {row.get('recent_activity', False) or row.get('activity_bucket', '')} | {row.get('has_install', False)} | {row.get('has_entrypoint', False)} | {row.get('notes', '')} |\n"
        )
    (paths.reports / 'repo_candidates.md').write_text(''.join(md), encoding='utf-8')


def main() -> int:
    args = parse_args()
    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    github_cfg = cfg.get('discovery', {}).get('github', {})
    query = args.query or cfg.get('queries', [cfg.get('topic', 'research')])[0]
    limit = args.limit or github_cfg.get('limit', 5)
    sort = args.sort or github_cfg.get('sort', 'updated')
    order = args.order or github_cfg.get('order', 'desc')
    selection = canonical_source_selection(project_config_path=paths.config)
    if not args.ignore_source_selection and not source_enabled(selection, "github"):
        print("github discovery skipped by canonical source selection")
        return 0

    reference_time = now_utc()
    ts = reference_time.strftime('%Y%m%dT%H%M%SZ')

    params = urllib.parse.urlencode({'q': query, 'per_page': limit, 'sort': sort, 'order': order})
    url = f'{API}?{params}'
    out = paths.discover / f"{ts}_github_{'_'.join(query.lower().split())[:80]}.json"
    headers = {
        'User-Agent': os.environ.get('GITHUB_USER_AGENT', DEFAULT_USER_AGENT),
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    token = os.environ.get('GITHUB_TOKEN', '')
    if token:
        headers['Authorization'] = f'Bearer {token}'

    payload: dict[str, object] = {
        'generated_at': ts,
        'reference_time': reference_time.isoformat(),
        'project': args.project,
        'source': 'github',
        'query': query,
        'items': [],
        'status': 'prepared',
    }
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=int(os.environ.get('GITHUB_TIMEOUT_SEC', '30'))) as response:
            raw = json.loads(response.read().decode('utf-8', 'ignore'))
        items = []
        imported_rows = []
        for item in raw.get('items', []) or []:
            topics = item.get('topics', []) or []
            row: dict[str, object] = {
                'source': 'github_search',
                'name': item.get('full_name') or item.get('name') or '',
                'url': item.get('html_url') or '',
                'summary': item.get('description') or '',
                'task_fit': False,
                'recent_activity': True,
                'has_readme': False,
                'has_license': bool(item.get('license')),
                'has_install': False,
                'has_entrypoint': False,
                'has_tests': False,
                'has_dataset_docs': False,
                'notes': 'github search candidate; verify locally before execution',
                'stars': item.get('stargazers_count', 0),
                'forks': item.get('forks_count', 0),
                'watchers': item.get('watchers_count', 0),
                'open_issues': item.get('open_issues_count', 0),
                'last_pushed_at': item.get('pushed_at') or '',
                'created_at': item.get('created_at') or '',
                'updated_at': item.get('updated_at') or '',
                'topics': topics,
                'language': item.get('language') or '',
                'is_archived': bool(item.get('archived')),
                'is_fork': bool(item.get('fork')),
                'default_branch': item.get('default_branch') or '',
                'query': query,
            }
            row['task_fit'] = row['repo_topic_match_score'] > 0 if 'repo_topic_match_score' in row else False
            row.update(score_repo_candidate(row, cfg, reference_time=reference_time))
            row['task_fit'] = row.get('repo_topic_match_score', 0) > 0
            row['score'] = row.get('repo_reuse_score', 0)
            items.append(row)
            imported_rows.append(row)
        items = sorted(items, key=repo_sort_key)
        payload['items'] = items
        payload['status'] = 'ok'
        update_repo_registry(paths, imported_rows)
    except (HTTPError, URLError, TimeoutError) as exc:
        payload['status'] = 'unavailable'
        payload['error'] = str(exc)

    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(out)
    print(f"items={len(payload['items'])}")
    print(f"status={payload.get('status')}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
