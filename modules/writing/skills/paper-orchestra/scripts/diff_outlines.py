#!/usr/bin/env python3
"""
diff_outlines.py — Produce a human-readable summary of changes between
outline.json (Step 1) and outline_reconciled.json (Step 3.5).

Only diffs the `section_plan` array; ignores `plotting_plan` and
`intro_related_work_plan` (those are not permitted to change).

Exit codes:
    0  identical section_plans (nothing was reconciled)
    1  differences found (summary written to --summary)
    2  input error

Usage:
    python diff_outlines.py \\
        --original   workspace/outline.json \\
        --reconciled workspace/outline_reconciled.json \\
        --summary    workspace/reconciliation_summary.md
"""
import argparse
import json
import os
import sys
from difflib import unified_diff


def load(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"ERROR: {path}: {e}", file=sys.stderr)
        sys.exit(2)


def section_text(plan: list) -> str:
    lines = []
    for sec in plan:
        lines.append(f"## {sec.get('title', '(untitled)')}")
        for sub in sec.get("subsections", []):
            lines.append(f"  ### {sub}")
        for bullet in sec.get("content_bullets", []):
            lines.append(f"  - {bullet}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--original",   required=True)
    p.add_argument("--reconciled", required=True)
    p.add_argument("--summary",    required=True)
    args = p.parse_args()

    orig = load(args.original)
    recon = load(args.reconciled)

    orig_text = section_text(orig.get("section_plan", []))
    recon_text = section_text(recon.get("section_plan", []))

    if orig_text == recon_text:
        print("OK: section_plan unchanged — nothing to reconcile.")
        # Write empty summary
        os.makedirs(os.path.dirname(os.path.abspath(args.summary)) or ".", exist_ok=True)
        with open(args.summary, "w") as f:
            f.write("# Outline Reconciliation Summary\n\nNo changes — "
                    "section_plan is identical to original outline.\n")
        return 0

    diff_lines = list(unified_diff(
        orig_text.splitlines(keepends=True),
        recon_text.splitlines(keepends=True),
        fromfile="outline.json (original)",
        tofile="outline_reconciled.json",
        lineterm="",
    ))

    # Count sections that contain any changed lines
    # Track current section as we walk the diff; mark it when a +/- appears
    changed_sections: set[str] = set()
    current_section = "(unknown)"
    for line in diff_lines:
        stripped = line.rstrip("\n")
        # Context or changed section-header lines
        if stripped.startswith(("  ##", "+##", "-##", " ##")):
            current_section = stripped.lstrip("+-").strip()
        elif stripped.startswith(("+", "-")) and not stripped.startswith(("+++", "---")):
            changed_sections.add(current_section)

    summary_lines = [
        "# Outline Reconciliation Summary",
        "",
        f"**Changed sections:** {len(changed_sections)}",
        "",
        "## Diff (section_plan only)",
        "",
        "```diff",
    ] + diff_lines + ["```", ""]

    os.makedirs(os.path.dirname(os.path.abspath(args.summary)) or ".", exist_ok=True)
    with open(args.summary, "w") as f:
        f.write("\n".join(summary_lines))

    print(f"Reconciliation summary written to {args.summary}")
    print(f"  {len(changed_sections)} section(s) changed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
