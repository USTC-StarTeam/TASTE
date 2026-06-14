#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from project_paths import validate_project_name

def _repo_root_from_script() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "framework").is_dir() and (candidate / "modules").is_dir() and (candidate / "web").is_dir():
            return candidate
    return current.parents[1]

ROOT = _repo_root_from_script()
ROOT_DIRS = [
    'projects',
    'tmp',
    'logs',
    '.runtime',
]


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr or proc.stdout or f'command failed: {cmd}')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project')
    parser.add_argument('--topic')
    parser.add_argument('--prompt')
    parser.add_argument('--conda-env', default='')
    args = parser.parse_args()

    for rel in ROOT_DIRS:
        (ROOT / rel).mkdir(parents=True, exist_ok=True)

    if args.project:
        project = validate_project_name(args.project)
        project_root = ROOT / 'projects' / project
        if not (project_root / 'project.json').exists():
            topic = args.topic or args.prompt or project
            create_cmd = [
                sys.executable,
                str(ROOT / 'framework' / 'scripts' / 'create_project.py'),
                '--name',
                project,
                '--conda-env',
                args.conda_env,
                '--topic',
                topic,
            ]
            if args.prompt:
                create_cmd.extend(['--prompt', args.prompt])
            run(create_cmd)
        else:
            run([sys.executable, str(ROOT / 'framework' / 'scripts' / 'init_project.py'), '--project', project])
        run([sys.executable, str(ROOT / 'framework' / 'scripts' / 'detect_machine_profile.py'), '--project', project])
        run([sys.executable, str(ROOT / 'framework' / 'scripts' / 'generate_handoff.py'), '--project', project])

    print(ROOT)


if __name__ == '__main__':
    main()
