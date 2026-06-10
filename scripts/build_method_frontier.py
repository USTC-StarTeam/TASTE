#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from project_paths import build_paths


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    paths = build_paths(args.project)
    next_actions = load_json(paths.state / 'next_actions.json', {'method_summaries': []})
    methods = next_actions.get('method_summaries', []) if isinstance(next_actions, dict) else []

    ranked = sorted(methods, key=lambda row: (-row.get('decision_score', 0.0), row.get('priority', 0)))
    pruned_recommendations = {'compare_then_prune_or_pause', 'pause_or_prune'}
    active_ranked = [row for row in ranked if row.get('recommendation') not in pruned_recommendations]
    elite = active_ranked[:2]
    frontier = {
        'elite_methods': elite,
        'repair_queue': [row for row in ranked if row.get('audit_incomplete_runs', 0) > 0],
        'slice_focus_queue': [row for row in ranked if row.get('bad_case_slice_count', 0) > 0 and not row.get('deepen_ready')],
        'prune_queue': [row for row in ranked if row.get('recommendation') in {'compare_then_prune_or_pause', 'pause_or_prune'}],
        'explore_queue': [row for row in active_ranked if row.get('completed', 0) + row.get('failed', 0) == 0],
    }
    (paths.state / 'method_frontier.json').write_text(json.dumps(frontier, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    lines = ['# Method Frontier\n\n']
    lines.append('## Elite Pool\n')
    if elite:
        for row in elite:
            lines.append(f"- {row.get('method')}: decision_score={row.get('decision_score')} deepen_ready={row.get('deepen_ready')}\n")
    else:
        lines.append('- none yet\n')
    lines.append('\n## Repair Queue\n')
    for row in frontier['repair_queue']:
        lines.append(f"- {row.get('method')}: audit_incomplete_runs={row.get('audit_incomplete_runs', 0)}\n")
    lines.append('\n## Slice Focus Queue\n')
    for row in frontier['slice_focus_queue']:
        lines.append(f"- {row.get('method')}: bad_case_slice_count={row.get('bad_case_slice_count', 0)}\n")
    lines.append('\n## Prune Queue\n')
    for row in frontier['prune_queue']:
        lines.append(f"- {row.get('method')}: recommendation={row.get('recommendation')}\n")
    lines.append('\n## Explore Queue\n')
    for row in frontier['explore_queue']:
        lines.append(f"- {row.get('method')}\n")
    out = paths.planning / 'method_frontier.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
