#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from pipeline_guard import guard_fresh_base_blocker_entry

from paper_common import ensure_paper_dirs, load_json, read_text, update_pipeline_state, write_json, write_text
from project_paths import build_paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    args = parser.parse_args()

    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name, safe_unblock=False)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    paper = ensure_paper_dirs(args.project)
    paths = build_paths(args.project)
    metadata = load_json(paper['paper_metadata'], {})
    aggregate = load_json(paper['aggregate_review_json'], {})
    author_response = read_text(paper['author_response_md'])
    revised = read_text(paper['revised_md'])
    evidence_audit = read_text(paths.reports / 'paper_evidence_audit.md')
    venue = args.venue or metadata.get('target_venue', '')

    blockers = aggregate.get('top_blockers', [])
    evidence_issues = aggregate.get('evidence_issues', [])
    unresolved = []
    for blocker in blockers:
        key = blocker.lower().split(',')[0][:80]
        if key and key not in author_response.lower() and key not in revised.lower():
            unresolved.append(blocker)
    for issue in evidence_issues:
        key = issue.lower().split(',')[0][:80]
        if key and key not in author_response.lower() and key not in revised.lower():
            unresolved.append(issue)
    unresolved = list(dict.fromkeys(unresolved))

    if aggregate.get('verdict') in {'blocked', 'evidence-blocked'}:
        verdict = 'still-blocked'
    elif unresolved:
        verdict = 'needs-more-evidence'
    elif '## Issues' in evidence_audit:
        verdict = 'needs-more-evidence'
    else:
        verdict = 'ready-for-template'

    summary = {
        'verdict': verdict,
        'unresolved_blockers': unresolved[:12],
        'resolved_by_response': max(0, len(blockers) + len(evidence_issues) - len(unresolved)),
        'original_blocker_count': len(blockers),
        'evidence_issue_count': len(evidence_issues),
    }
    write_json(paper['re_review_json'], summary)
    lines = [
        '# Re-Review Summary\n\n',
        f'- verdict: {verdict}\n',
        f'- original_blocker_count: {len(blockers)}\n',
        f"- evidence_issue_count: {len(evidence_issues)}\n",
        f'- resolved_by_response: {summary["resolved_by_response"]}\n',
        f'- unresolved_count: {len(unresolved)}\n\n',
        '## Unresolved Blockers\n\n',
    ]
    if unresolved:
        for blocker in unresolved[:12]:
            lines.append(f'- {blocker}\n')
    else:
        lines.append('- No unresolved blocker detected in this re-review.\n')
    write_text(paper['re_review_md'], ''.join(lines))
    update_pipeline_state(args.project, {
        're_review_ready': True,
        're_review_verdict': verdict,
        're_review_path': str(paper['re_review_md']),
        'promotion_gate': 'allow-template' if verdict == 'ready-for-template' else 'hold-markdown-only',
    }, venue=venue)
    print(paper['re_review_md'])


if __name__ == '__main__':
    main()
