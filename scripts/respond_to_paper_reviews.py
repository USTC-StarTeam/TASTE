#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from pipeline_guard import guard_fresh_base_blocker_entry

from paper_common import ensure_paper_dirs, load_json, read_text, update_pipeline_state, write_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    args = parser.parse_args()

    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name, safe_unblock=False)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    paper = ensure_paper_dirs(args.project)
    metadata = load_json(paper['paper_metadata'], {})
    aggregate = load_json(paper['aggregate_review_json'], {})
    revised = read_text(paper['revised_md'])
    venue = args.venue or metadata.get('target_venue', '')

    blockers = aggregate.get('top_blockers', [])
    changes = aggregate.get('required_changes', [])
    title = metadata.get('title', args.project)

    lines = [
        f'# Author Response: {title}\n\n',
        f'- target_venue: {venue or metadata.get("target_venue", "TBD")}\n',
        f'- review_verdict: {aggregate.get("verdict", "missing-reviews")}\n\n',
        '## Response Policy\n\n',
        '- Do not argue with missing evidence. Narrow the claim, add evidence, or admit the limitation.\n',
        '- Fatal reviewer findings should be answered with delete / downgrade / new experiment / scoped limitation.\n\n',
        '## Reviewer Concerns and Planned Responses\n\n',
    ]
    for idx, blocker in enumerate(blockers[:10], start=1):
        planned = changes[idx - 1] if idx - 1 < len(changes) else 'No explicit fix written yet.'
        lines.append(f'### Concern {idx}\n\n')
        lines.append(f'- concern: {blocker}\n')
        lines.append(f'- planned_response: {planned}\n')
        lines.append('- status: pending evidence or claim adjustment\n\n')
    lines.extend([
        '## Current Revised Draft Snapshot\n\n',
        revised,
    ])
    write_text(paper['author_response_md'], ''.join(lines))
    update_pipeline_state(args.project, {
        'author_response_ready': True,
        'author_response_path': str(paper['author_response_md']),
    }, venue=venue)
    print(paper['author_response_md'])


if __name__ == '__main__':
    main()
