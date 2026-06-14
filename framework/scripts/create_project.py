#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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
TEMPLATE = ROOT / 'templates' / 'project.json'


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', required=True)
    parser.add_argument('--topic')
    parser.add_argument('--prompt')
    parser.add_argument('--conda-env', default='')
    parser.add_argument('--query', action='append', default=[])
    args = parser.parse_args()
    name = validate_project_name(args.name)

    topic = args.topic or name
    queries = args.query or [topic]
    project_root = ROOT / 'projects' / name
    project_root.mkdir(parents=True, exist_ok=True)

    data = json.loads(TEMPLATE.read_text(encoding='utf-8'))
    data['name'] = name
    data['topic'] = topic
    data['conda_env'] = args.conda_env or ''
    data['queries'] = queries
    data['user_prompt'] = args.prompt or ''
    data['research_interest'] = args.prompt or ''
    data['researcher_profile'] = ''
    data.pop('target_venue', None)
    data.pop('venue', None)
    paper = dict(data.get('paper') or {}) if isinstance(data.get('paper'), dict) else {}
    for key in ['target_venue', 'venue_slug', 'template_family', 'template_source_url']:
        paper.pop(key, None)
    if paper:
        data['paper'] = paper
    data.setdefault('startup', {})['last_bootstrap_request'] = args.prompt or ''
    save_json(project_root / 'project.json', data)

    proc = subprocess.run([sys.executable, str(ROOT / 'framework' / 'scripts' / 'init_project.py'), '--project', name], cwd=ROOT, text=True, capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr or proc.stdout or 'failed to initialize project structure')

    activate = project_root / 'activate_env.sh'
    activate.write_text(
        '#!/usr/bin/env bash\n'
        'set -euo pipefail\n'
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        'ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"\n'
        f'exec "$ROOT/framework/scripts/run_in_conda.sh" "{name}" "$@"\n',
        encoding='utf-8',
    )
    activate.chmod(0o755)
    print(project_root)


if __name__ == '__main__':
    main()
