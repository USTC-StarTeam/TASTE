#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path

from project_paths import build_paths

PRUNE_WORDS = ('veto', 'prune', 'abandon', 'cancel', 'desk-rejected', 'broken implementation', 'zero novelty')


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


def mentioned_methods(text: str, known_methods: list[str]) -> list[str]:
    lowered = text.lower()
    out = []
    for method in known_methods:
        if method and method.lower() in lowered:
            out.append(method)
    return sorted(set(out))


def main() -> None:
    parser = argparse.ArgumentParser(description='Convert LLM critic/planner vetoes into executable method/repo overrides.')
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    paths = build_paths(args.project)
    team = load_json(paths.state / 'llm_research_team_state.json', {'roles': []})
    experiments = load_json(paths.state / 'experiment_registry.json', [])
    known_methods = sorted({str(row.get('method', '')) for row in experiments if row.get('method')})
    known_repos = sorted({str(row.get('repo_path', '')) for row in experiments if row.get('repo_path')})

    vetoes = []
    method_overrides = load_json(paths.state / 'method_overrides.json', {'methods': {}, 'repos': {}})
    if not isinstance(method_overrides, dict):
        method_overrides = {'methods': {}, 'repos': {}}
    method_overrides.setdefault('methods', {})
    method_overrides.setdefault('repos', {})

    for role in team.get('roles', []) if isinstance(team, dict) else []:
        role_name = str(role.get('role', '')).lower()
        if role_name not in {'critic', 'planner', 'analyst', 'debugger'}:
            continue
        blob = ' '.join([
            str(role.get('summary', '')),
            ' '.join(map(str, role.get('decisions', []) or [])),
            ' '.join(str(action.get('action', '')) for action in (role.get('actions', []) or []) if isinstance(action, dict)),
        ])
        lowered = blob.lower()
        if not any(word in lowered for word in PRUNE_WORDS):
            continue
        methods = mentioned_methods(blob, known_methods)
        repo_veto = any(token in lowered for token in ['abandon the selected route', 'abandon current route', 'abandon the repo', 'abandon repository', 'sole codebase'])
        for method in methods:
            method_overrides['methods'][method] = {
                'recommendation': 'pause_or_prune',
                'source': f'llm_{role_name}_veto',
                'reason': blob[:1200],
                'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            vetoes.append({'type': 'method', 'target': method, 'source_role': role_name, 'reason': blob[:1200]})
        if repo_veto:
            for repo_path in known_repos:
                if repo_path:
                    method_overrides['repos'][repo_path] = {
                        'status': 'paused_or_abandoned',
                        'source': f'llm_{role_name}_veto',
                        'reason': blob[:1200],
                        'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                    vetoes.append({'type': 'repo', 'target': repo_path, 'source_role': role_name, 'reason': blob[:1200]})

    payload = {
        'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'project': args.project,
        'vetoes': vetoes,
        'method_overrides_path': str(paths.state / 'method_overrides.json'),
    }
    save_json(paths.state / 'research_vetoes.json', payload)
    save_json(paths.state / 'method_overrides.json', method_overrides)

    lines = ['# Research Vetoes\n\n', f"- generated_at: {payload['generated_at']}\n", f"- veto_count: {len(vetoes)}\n\n"]
    if vetoes:
        for item in vetoes:
            lines.append(f"- {item['type']}: {item['target']} | source={item['source_role']}\n")
    else:
        lines.append('- No executable veto detected.\n')
    out = paths.reports / 'research_vetoes.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
