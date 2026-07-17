#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys

from project.project_paths import validate_project_name
from runtime.framework_paths import ROOT
from runtime.taste_pythonpath import resolve_script_path
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
                str(resolve_script_path('create_project.py', ROOT)),
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
            run([sys.executable, str(resolve_script_path('init_project.py', ROOT)), '--project', project])
        run([sys.executable, str(resolve_script_path('detect_machine_profile.py', ROOT)), '--project', project])
        run([sys.executable, str(resolve_script_path('generate_handoff.py', ROOT)), '--project', project])

    print(ROOT)


if __name__ == '__main__':
    main()
