#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from project.project_paths import build_paths


def exists(path: Path) -> bool:
    return path.exists() and bool(path.read_text(encoding='utf-8').strip()) if path.is_file() else path.exists()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    paths = build_paths(args.project)
    checks = [
        ('request_to_loop_state', exists(paths.state / 'natural_language_requests.json') and exists(paths.state / 'loop_history.json')),
        ('discovery_to_shared_research', exists(paths.reports / 'shared_research.md')),
        ('shared_research_to_ideas', exists(paths.planning / 'finding' / 'idea.md') and exists(paths.planning / 'finding' / 'ideas.json')),
        ('parallel_plan_to_experiment_registry', exists(paths.state / 'parallel_plan.json') and exists(paths.state / 'experiment_registry.json')),
        ('experiment_registry_to_next_actions', exists(paths.state / 'experiment_registry.json') and exists(paths.planning / 'next_actions.md')),
        ('next_actions_to_method_frontier', exists(paths.planning / 'next_actions.md') and exists(paths.planning / 'method_frontier.md')),
        ('method_frontier_to_aris_review_board', exists(paths.planning / 'method_frontier.md') and exists(paths.reports / 'aris_review_board.md')),
        ('paper_orchestra_audit_present', exists(paths.reports / 'paper_orchestra_audit.md')),
        ('experiments_to_claim_ledger', exists(paths.state / 'experiment_registry.json') and exists(paths.planning / 'claim_ledger.md')),
        ('claim_ledger_to_paper_audit', exists(paths.planning / 'claim_ledger.md') and exists(paths.reports / 'paper_evidence_audit.md')),
        ('paper_audit_to_manifest', exists(paths.reports / 'paper_evidence_audit.md') and exists(paths.reports / 'research_manifest.md')),
        ('manifest_to_markdown_paper', exists(paths.reports / 'research_manifest.md') and exists(paths.root / 'paper' / 'drafts' / 'paper_draft.md')),
        ('markdown_paper_to_review_loop', exists(paths.root / 'paper' / 'reviews' / 'aggregated_review.md') and exists(paths.root / 'paper' / 'reviews' / 're_review' / 're_review_summary.md')),
    ]

    issues = [name for name, ok in checks if not ok]
    payload = {
        'project': args.project,
        'checks': [{'name': name, 'ok': ok} for name, ok in checks],
        'issue_count': len(issues),
        'issues': issues,
    }
    (paths.state / 'workflow_connectivity.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    lines = ['# Workflow Connectivity Audit\n\n']
    for name, ok in checks:
        lines.append(f"- {name}: {ok}\n")
    if issues:
        lines.append('\n## Issues\n')
        for issue in issues:
            lines.append(f"- {issue}\n")
    else:
        lines.append('\nAll critical module links are present.\n')
    out = paths.reports / 'workflow_connectivity.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
