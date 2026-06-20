#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

def _repo_root_from_script() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "framework").is_dir() and (candidate / "modules").is_dir() :
            return candidate
    return current.parents[1]

ROOT = _repo_root_from_script()


def _project_from_args(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--project', default='')
    ns, _ = parser.parse_known_args(argv)
    if ns.project:
        return ns.project
    import os
    return os.environ.get('PROJECT_ID', '')


def main() -> int:
    project = _project_from_args(sys.argv[1:])
    if not project:
        print(json.dumps({
            'status': 'blocked',
            'decision': 'project_required_for_project_adapter',
            'message': 'This framework entrypoint dispatches to a project-local adapter; pass --project or set PROJECT_ID.',
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    adapter = ROOT / 'projects' / project / 'scripts' / 'adapters' / Path(__file__).name
    if not adapter.exists():
        print(json.dumps({
            'status': 'blocked',
            'decision': 'missing_project_adapter',
            'project': project,
            'adapter_path': str(adapter),
            'message': 'No project-local adapter is available for this repo/probe operation.',
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    proc = subprocess.run([sys.executable, str(adapter), *sys.argv[1:]], cwd=ROOT)
    return int(proc.returncode)


if __name__ == '__main__':
    raise SystemExit(main())
