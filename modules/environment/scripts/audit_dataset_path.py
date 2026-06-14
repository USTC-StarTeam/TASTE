#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from project_paths import build_paths


SPLIT_NAMES = {'train', 'valid', 'val', 'test', 'dev'}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--dataset-path', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--metric', default='')
    parser.add_argument('--access', default='local')
    parser.add_argument('--url', default='local')
    parser.add_argument('--notes', default='')
    args = parser.parse_args()

    ds_path = Path(args.dataset_path)
    if not ds_path.exists():
        raise SystemExit(f'Dataset path not found: {ds_path}')

    files = list(ds_path.rglob('*'))
    dirs = [p for p in files if p.is_dir()]
    leaf_files = [p for p in files if p.is_file()]
    splits = sorted({p.name for p in dirs if p.name.lower() in SPLIT_NAMES})
    suffixes = sorted({p.suffix for p in leaf_files if p.suffix})
    available = bool(leaf_files)
    format_str = ','.join(suffixes[:6])
    split_str = ','.join(splits)
    score = 0
    score += 2 if available else 0
    score += 2 if format_str else 0
    score += 2 if split_str else 0
    score += 2 if args.metric else 0
    score += 2 if args.access in {'local', 'public', 'requestable'} else 0

    paths = build_paths(args.project)
    registry = paths.state / 'dataset_registry.json'
    rows = json.loads(registry.read_text(encoding='utf-8')) if registry.exists() else []
    item = {
        'name': args.name,
        'task': args.task,
        'access': args.access,
        'format': format_str,
        'split': split_str,
        'metric': args.metric,
        'url': args.url,
        'notes': args.notes or 'auto-audited local dataset path',
        'available': available,
        'download_tested': True,
        'local_path': str(ds_path),
        'file_count': len(leaf_files),
        'readiness_score': score,
    }
    rows = [row for row in rows if row.get('name') != args.name]
    rows.append(item)
    rows.sort(key=lambda x: (-int(x.get('readiness_score', 0)), x.get('name', '')))
    registry.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    report = paths.reports / 'dataset_audit.md'
    report.write_text(
        '# Dataset Audit\n\n'
        f"- dataset: {args.name}\n"
        f"- local_path: {ds_path}\n"
        f"- file_count: {len(leaf_files)}\n"
        f"- format: {format_str}\n"
        f"- split: {split_str}\n"
        f"- readiness_score: {score}\n",
        encoding='utf-8',
    )
    print(report)


if __name__ == '__main__':
    main()
