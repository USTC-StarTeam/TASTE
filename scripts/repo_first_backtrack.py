#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path

from project_paths import build_paths

CONFIG_DATASET_RE = re.compile(r'^\s*dataset\s*:\s*["\']?([^"\'\n#]+)', re.IGNORECASE | re.MULTILINE)
RUN_RE = re.compile(r'(?:python|python3)\s+[^\n`]+')
TITLE_HINT_RE = re.compile(r'(?:GitHub for|anonymous github for)\s+([^\n:]+:\s*[^\n]+|[^\n]+)', re.IGNORECASE)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def read_text(path: Path, limit: int = 20000) -> str:
    if not path.exists() or not path.is_file():
        return ''
    return path.read_text(encoding='utf-8', errors='ignore')[:limit]


def infer_repo_path(paths, explicit: str = '') -> Path | None:
    if explicit:
        return Path(explicit)
    rows = load_json(paths.state / 'repo_candidates.json', [])
    for row in rows:
        local = row.get('local_path')
        if local and Path(local).exists():
            return Path(local)
    return None



def infer_required_files(repo: Path) -> list[str]:
    texts = []
    for rel in ['utils/dataloader.py', 'dataset.py', 'README.md', 'readme.md']:
        path = repo / rel
        if path.exists() and path.is_file():
            texts.append(read_text(path, 60000))
    blob = '\n'.join(texts)
    if 'all_data.pkl' in blob or 'dist_mat.npy' in blob:
        return ['all_data.pkl', 'dist_mat.npy']
    if 'train_3.txt' in blob and 'test_3.txt' in blob:
        files = ['train_3.txt', 'test_3.txt']
        if 'trust_3.txt' in blob:
            files.append('trust_3.txt')
        return files
    found = []
    for item in re.findall(r"['\"]([^'\"]+\.(?:txt|csv|json|jsonl|pkl|pickle|npy|npz))['\"]", blob):
        name = Path(item).name
        if name and name not in found:
            found.append(name)
    return found[:6]


def split_label(required_files: list[str], available: bool) -> str:
    if not available:
        return 'incomplete-or-missing'
    return ','.join(required_files) if required_files else 'repo-specific'


def inspect_repo(repo: Path) -> dict:
    readme = read_text(repo / 'README.md') or read_text(repo / 'readme.md')
    summary = readme.splitlines()[0] if readme else repo.name
    title_match = TITLE_HINT_RE.search(readme)
    paper_title = title_match.group(1).strip() if title_match else ''
    commands = []
    for source in [readme, *[read_text(path, 4000) for path in sorted(repo.glob('*.md'))[:3]]]:
        for match in RUN_RE.findall(source or ''):
            command = ' '.join(match.strip().split())
            if command not in commands:
                commands.append(command)
    datasets = []
    config_files = []
    for path in sorted(repo.rglob('*.yaml')) + sorted(repo.rglob('*.yml')):
        if '.git' in path.parts:
            continue
        text = read_text(path, 6000)
        config_files.append(str(path.relative_to(repo)))
        for match in CONFIG_DATASET_RE.findall(text):
            name = match.strip().strip('"\'')
            if name and name not in datasets:
                datasets.append(name)
    data_dirs = []
    for name in ['data', 'dataset', 'datasets', 'raw', 'processed']:
        target = repo / name
        if target.exists():
            data_dirs.append(str(target.relative_to(repo)))
    required_files = infer_required_files(repo)
    dataset_audits = []
    available_datasets = []
    missing_datasets = []
    incomplete_datasets = []
    for name in datasets:
        candidates = [repo / 'data' / name]
        # Some loaders use case variants; keep matching exact first, then case-insensitive dirs.
        data_root = repo / 'data'
        if data_root.exists():
            for child in data_root.iterdir():
                if child.is_dir() and child.name.lower() == name.lower() and child not in candidates:
                    candidates.append(child)
        existing = next((path for path in candidates if path.exists() and path.is_dir()), None)
        present = []
        missing = required_files[:]
        if existing:
            present = [item for item in required_files if (existing / item).exists()]
            missing = [item for item in required_files if item not in present]
        status = 'available' if existing and not missing else 'incomplete' if existing else 'missing'
        if status == 'available':
            available_datasets.append(name)
        elif status == 'incomplete':
            incomplete_datasets.append(name)
        else:
            missing_datasets.append(name)
        dataset_audits.append({
            'name': name,
            'status': status,
            'local_path': str(existing) if existing else '',
            'present_required_files': present,
            'missing_required_files': missing,
            'required_files': required_files,
        })
    return {
        'repo_path': str(repo),
        'repo_name': repo.name,
        'summary': summary,
        'paper_title_hint': paper_title,
        'run_commands': commands[:8],
        'config_files': config_files[:20],
        'dataset_names': datasets,
        'available_dataset_names': available_datasets,
        'missing_dataset_names': missing_datasets,
        'incomplete_dataset_names': incomplete_datasets,
        'dataset_audits': dataset_audits,
        'data_dirs': data_dirs,
        'evidence_gaps': [
            'linked_paper_not_imported' if paper_title else 'paper_title_not_found_in_readme',
            'real_dataset_files_not_present' if missing_datasets and not available_datasets else '',
            'run_command_needs_real_dataset_verification' if commands else 'no_run_command_found',
        ],
    }


def update_dataset_registry(paths, report: dict) -> None:
    registry_path = paths.state / 'dataset_registry.json'
    rows = load_json(registry_path, [])
    existing = {row.get('name'): row for row in rows}
    audits = {row.get('name'): row for row in report.get('dataset_audits', [])}
    for name in report.get('dataset_names', []):
        audit = audits.get(name, {})
        available = audit.get('status') == 'available'
        item = {
            'name': name,
            'task': 'repo-declared recommendation benchmark dataset',
            'access': 'local repo data directory' if available else 'declared by selected repo config; files not assumed available until audited',
            'format': 'txt' if available else 'unknown until required files are present',
            'split': split_label(audit.get('required_files', []), available),
            'metric': 'repo default top-k recommendation metrics',
            'url': '',
            'notes': 'repo-first backtracking audit; available only if required repo loader files are present',
            'available': available,
            'download_tested': available,
            'repo_backtracked': True,
            'local_path': audit.get('local_path', ''),
            'required_files': audit.get('required_files', []),
            'missing_required_files': audit.get('missing_required_files', []),
            'readiness_score': 8 if available else 3 if audit.get('status') == 'incomplete' else 1,
        }
        if name in existing:
            existing[name].update(item)
        else:
            rows.append(item)
    save_json(registry_path, rows)


def write_report(paths, report: dict) -> None:
    lines = [
        '# Repo-First Backtracking\n\n',
        f"- generated_at: {report['generated_at']}\n",
        f"- repo_path: {report.get('repo_path', '')}\n",
        f"- paper_title_hint: {report.get('paper_title_hint', '') or 'none'}\n",
        f"- dataset_names: {', '.join(report.get('dataset_names', [])) or 'none'}\n",
        f"- available_dataset_names: {', '.join(report.get('available_dataset_names', [])) or 'none'}\n",
        f"- missing_dataset_names: {', '.join(report.get('missing_dataset_names', [])) or 'none'}\n",
        f"- incomplete_dataset_names: {', '.join(report.get('incomplete_dataset_names', [])) or 'none'}\n\n",
        '## Run Commands\n',
    ]
    for command in report.get('run_commands', []):
        lines.append(f'- `{command}`\n')
    if not report.get('run_commands'):
        lines.append('- none found\n')
    lines.append('\n## Evidence Gaps\n')
    for gap in [g for g in report.get('evidence_gaps', []) if g]:
        lines.append(f'- {gap}\n')
    lines.append('\n## Rules\n')
    lines.append('- Do not claim real-dataset evidence until dataset files/downloads and repo commands are verified.\n')
    lines.append('- Use paper_title_hint only as a query seed; do not cite it until imported from a paper source.\n')
    out = paths.reports / 'repo_first_backtracking.md'
    out.write_text(''.join(lines), encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--repo-path', default='')
    args = parser.parse_args()
    paths = build_paths(args.project)
    repo = infer_repo_path(paths, args.repo_path)
    if not repo or not repo.exists():
        raise SystemExit('No selected local repo found for repo-first backtracking.')
    report = inspect_repo(repo)
    report['generated_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_json(paths.state / 'repo_first_backtracking.json', report)
    update_dataset_registry(paths, report)
    write_report(paths, report)
    print(paths.reports / 'repo_first_backtracking.md')


if __name__ == '__main__':
    main()
