#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from literature_policy import now_utc, repo_sort_key, score_repo_candidate
from project_paths import build_paths, load_project_config

ENTRYPOINT_HINTS = ['train.py', 'run.py', 'main.py', 'eval.py']
INSTALL_HINTS = ['requirements.txt', 'environment.yml', 'setup.py', 'pyproject.toml']
TEST_HINTS = ['tests', 'test']
DATA_DOC_HINTS = ['dataset', 'data', 'benchmark']


def contains_any(paths: list[Path], names: list[str]) -> bool:
    lowered = {name.lower() for name in names}
    for path in paths:
        if path.name.lower() in lowered:
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--repo-path', required=True)
    parser.add_argument('--name')
    parser.add_argument('--url', default='local')
    parser.add_argument('--summary', default='')
    parser.add_argument('--recent-activity', action='store_true')
    parser.add_argument('--task-fit', action='store_true')
    parser.add_argument('--stars', type=int, default=0)
    parser.add_argument('--forks', type=int, default=0)
    parser.add_argument('--last-pushed-at', default='')
    parser.add_argument('--topics', default='')
    parser.add_argument('--language', default='')
    args = parser.parse_args()

    repo = Path(args.repo_path)
    if not repo.exists() or not repo.is_dir():
        raise SystemExit(f'Repo path not found: {repo}')

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    all_paths = list(repo.rglob('*'))
    top_paths = list(repo.iterdir())
    has_readme = any(p.name.lower().startswith('readme') for p in top_paths)
    has_license = any(p.name.lower().startswith('license') for p in top_paths)
    has_install = contains_any(top_paths, INSTALL_HINTS)
    has_entrypoint = contains_any(all_paths, ENTRYPOINT_HINTS)
    has_tests = any(p.is_dir() and p.name.lower() in TEST_HINTS for p in top_paths)
    has_dataset_docs = any(any(hint in p.name.lower() for hint in DATA_DOC_HINTS) for p in top_paths)

    item = {
        'name': args.name or repo.name,
        'url': args.url,
        'local_path': str(repo),
        'summary': args.summary,
        'task_fit': args.task_fit,
        'recent_activity': args.recent_activity,
        'has_readme': has_readme,
        'has_license': has_license,
        'has_install': has_install,
        'has_entrypoint': has_entrypoint,
        'has_tests': has_tests,
        'has_dataset_docs': has_dataset_docs,
        'notes': 'auto-audited local repo',
        'stars': args.stars,
        'forks': args.forks,
        'last_pushed_at': args.last_pushed_at,
        'topics': [x.strip() for x in args.topics.split(',') if x.strip()],
        'language': args.language,
    }
    item.update(score_repo_candidate(item, cfg, reference_time=now_utc()))
    item['score'] = item.get('repo_reuse_score', 0)

    registry = paths.state / 'repo_candidates.json'
    rows = json.loads(registry.read_text(encoding='utf-8')) if registry.exists() else []
    rows = [row for row in rows if row.get('name') != item['name']]
    rows.append(item)
    rows.sort(key=repo_sort_key)
    registry.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    report = paths.reports / 'repo_audit.md'
    report.write_text(
        '# Repo Audit\n\n'
        f"- repo: {item['name']}\n"
        f"- local_path: {item['local_path']}\n"
        f"- score: {item['score']}\n"
        f"- repo_reuse_score: {item.get('repo_reuse_score', 0)}\n"
        f"- repo_selection_bucket: {item.get('repo_selection_bucket', '')}\n"
        f"- activity_bucket: {item.get('activity_bucket', '')}\n"
        f"- stars: {item.get('stars', 0)}\n"
        f"- forks: {item.get('forks', 0)}\n"
        f"- has_readme: {has_readme}\n"
        f"- has_license: {has_license}\n"
        f"- has_install: {has_install}\n"
        f"- has_entrypoint: {has_entrypoint}\n"
        f"- has_tests: {has_tests}\n"
        f"- has_dataset_docs: {has_dataset_docs}\n",
        encoding='utf-8',
    )
    print(report)


if __name__ == '__main__':
    main()
