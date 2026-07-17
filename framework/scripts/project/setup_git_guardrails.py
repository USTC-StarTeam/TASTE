#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from project.project_paths import build_paths


GITIGNORE = """
# TASTE research-repo guardrails: track source/config/docs only.
# Do not commit datasets, checkpoints, generated papers, experiment runs, logs, or caches.
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
.DS_Store
*.log
logs/
tmp/
tmp_*.txt
.runtime/
wandb/
outputs/
runs/
artifacts/
reports/
state/
paper/output/
paper/rendered/
paper/compiled/
data/
dataset/
datasets/
checkpoints/
saved_models/
*.pt
*.pth
*.ckpt
*.bin
*.safetensors
*.npz
*.npy
*.parquet
*.csv
*.tsv
*.pdf
*.zip
*.tar
*.tar.gz
*.tgz
*.rar
*.7z
*.orig
*.rej
""".lstrip()


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)


def merge_gitignore(existing: str, addition: str) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for line in (existing + "\n" + addition).splitlines():
        key = line.strip()
        if key and not key.startswith("#"):
            if key in seen:
                continue
            seen.add(key)
        lines.append(line.rstrip())
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--repo-path', required=True)
    args = parser.parse_args()

    repo = Path(args.repo_path)
    if not (repo / '.git').exists():
        run(['git', 'init'], repo)
    gi = repo / '.gitignore'
    existing = gi.read_text(encoding='utf-8') if gi.exists() else ''
    gi.write_text(merge_gitignore(existing, GITIGNORE), encoding='utf-8')

    paths = build_paths(args.project)
    out = paths.reports / 'git_guardrails.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        '# Git Guardrails\n\n'
        f'- repo: {repo}\n'
        '- Initialized git if needed.\n'
        '- Added source/config/docs-only guardrails for generated artifacts, data, checkpoints, logs, caches, and paper outputs.\n'
        '- Track core code and configs, not bulky outputs.\n',
        encoding='utf-8'
    )
    print(out)


if __name__ == '__main__':
    main()
