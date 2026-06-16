#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from pipeline_guard import guard_fresh_base_blocker_entry
from paper_common import ensure_paper_dirs, load_json, read_text, update_pipeline_state, write_json, write_text
from project_paths import build_paths


def respond_to_reviews(project: str, venue_arg: str = "") -> Path:
    guard_rc = guard_fresh_base_blocker_entry(project, venue_arg, "review_response_tools.py:respond_to_reviews", safe_unblock=False)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    paper = ensure_paper_dirs(project)
    metadata = load_json(paper["paper_metadata"], {})
    aggregate = load_json(paper["aggregate_review_json"], {})
    revised = read_text(paper["revised_md"])
    venue = venue_arg or metadata.get("target_venue", "")

    blockers = aggregate.get("top_blockers", [])
    changes = aggregate.get("required_changes", [])
    title = metadata.get("title", project)

    lines = [
        f"# Author Response: {title}\n\n",
        f'- target_venue: {venue or metadata.get("target_venue", "TBD")}\n',
        f'- review_verdict: {aggregate.get("verdict", "missing-reviews")}\n\n',
        "## Response Policy\n\n",
        "- Do not argue with missing evidence. Narrow the claim, add evidence, or admit the limitation.\n",
        "- Fatal reviewer findings should be answered with delete / downgrade / new experiment / scoped limitation.\n\n",
        "## Reviewer Concerns and Planned Responses\n\n",
    ]
    for idx, blocker in enumerate(blockers[:10], start=1):
        planned = changes[idx - 1] if idx - 1 < len(changes) else "No explicit fix written yet."
        lines.append(f"### Concern {idx}\n\n")
        lines.append(f"- concern: {blocker}\n")
        lines.append(f"- planned_response: {planned}\n")
        lines.append("- status: pending evidence or claim adjustment\n\n")
    lines.extend(["## Current Revised Draft Snapshot\n\n", revised])
    write_text(paper["author_response_md"], "".join(lines))
    update_pipeline_state(project, {"author_response_ready": True, "author_response_path": str(paper["author_response_md"])}, venue=venue)
    return paper["author_response_md"]


def re_review_paper(project: str, venue_arg: str = "") -> Path:
    guard_rc = guard_fresh_base_blocker_entry(project, venue_arg, "review_response_tools.py:re_review_paper", safe_unblock=False)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    paper = ensure_paper_dirs(project)
    paths = build_paths(project)
    metadata = load_json(paper["paper_metadata"], {})
    aggregate = load_json(paper["aggregate_review_json"], {})
    author_response = read_text(paper["author_response_md"])
    revised = read_text(paper["revised_md"])
    evidence_audit = read_text(paths.reports / "paper_evidence_audit.md")
    venue = venue_arg or metadata.get("target_venue", "")

    blockers = aggregate.get("top_blockers", [])
    evidence_issues = aggregate.get("evidence_issues", [])
    unresolved = []
    for blocker in blockers:
        key = blocker.lower().split(",")[0][:80]
        if key and key not in author_response.lower() and key not in revised.lower():
            unresolved.append(blocker)
    for issue in evidence_issues:
        key = issue.lower().split(",")[0][:80]
        if key and key not in author_response.lower() and key not in revised.lower():
            unresolved.append(issue)
    unresolved = list(dict.fromkeys(unresolved))

    if aggregate.get("verdict") in {"blocked", "evidence-blocked"}:
        verdict = "still-blocked"
    elif unresolved:
        verdict = "needs-more-evidence"
    elif "## Issues" in evidence_audit:
        verdict = "needs-more-evidence"
    else:
        verdict = "ready-for-template"

    summary = {
        "verdict": verdict,
        "unresolved_blockers": unresolved[:12],
        "resolved_by_response": max(0, len(blockers) + len(evidence_issues) - len(unresolved)),
        "original_blocker_count": len(blockers),
        "evidence_issue_count": len(evidence_issues),
    }
    write_json(paper["re_review_json"], summary)
    lines = [
        "# Re-Review Summary\n\n",
        f"- verdict: {verdict}\n",
        f"- original_blocker_count: {len(blockers)}\n",
        f'- evidence_issue_count: {len(evidence_issues)}\n',
        f'- resolved_by_response: {summary["resolved_by_response"]}\n',
        f"- unresolved_count: {len(unresolved)}\n\n",
        "## Unresolved Blockers\n\n",
    ]
    if unresolved:
        for blocker in unresolved[:12]:
            lines.append(f"- {blocker}\n")
    else:
        lines.append("- No unresolved blocker detected in this re-review.\n")
    write_text(paper["re_review_md"], "".join(lines))
    update_pipeline_state(project, {
        "re_review_ready": True,
        "re_review_verdict": verdict,
        "re_review_path": str(paper["re_review_md"]),
        "promotion_gate": "allow-template" if verdict == "ready-for-template" else "hold-markdown-only",
    }, venue=venue)
    return paper["re_review_md"]


def write_comparison(project: str, topic: str, content: str) -> Path:
    paths = build_paths(project)
    slug = topic.lower().replace(" ", "-").replace("/", "-")
    out = paths.wiki_comparisons / f"{slug}-comparison.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"# {topic} Comparison\n\n{content}\n", encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Writing review/response helper actions.")
    parser.add_argument("--tool-action", required=True, choices=["respond", "re_review", "comparison"])
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    parser.add_argument("--topic", default="")
    parser.add_argument("--content", default="")
    args = parser.parse_args()
    if args.tool_action == "respond":
        print(respond_to_reviews(args.project, args.venue))
    elif args.tool_action == "re_review":
        print(re_review_paper(args.project, args.venue))
    else:
        if not args.topic or not args.content:
            raise SystemExit("--topic and --content are required for --tool-action comparison")
        print(write_comparison(args.project, args.topic, args.content))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
