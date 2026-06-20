#!/usr/bin/env python3
"""
update_critique_memory.py — Build and maintain a structured critique memory
across content-refinement iterations (AutoSci-inspired persistent context).

Reads the refinement worklog.json plus the current iteration's review.json
and produces/updates workspace/refinement/critique_memory.json, which tracks:

  - persistent_issues   : weaknesses flagged in 2+ iterations without resolution
  - resolved_issues     : weaknesses addressed and not re-flagged
  - da_critical_unresolved : Devil's Advocate CRITICAL findings still open
  - focus_on            : short prompts the next reviewer should prioritise
  - do_not_reflag       : resolved issues the next reviewer must NOT re-flag

The content-refinement-agent passes critique_memory.json to the reviewer
prompt at the start of every iteration so the reviewer has memory of prior
rounds.

Exit codes:
    0  OK
    1  Input error (missing file, bad JSON)

Usage:
    python update_critique_memory.py \\
        --worklog workspace/refinement/worklog.json \\
        --review  workspace/refinement/iter2/review.json \\
        --iter    2 \\
        --out     workspace/refinement/critique_memory.json
"""
import argparse
import json
import os
import sys
from difflib import SequenceMatcher


# ── helpers ─────────────────────────────────────────────────────────────────

def load_json(path: str) -> dict | list:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON in {path}: {e}", file=sys.stderr)
        sys.exit(1)


def _similarity(a: str, b: str) -> float:
    """Return a 0–1 similarity ratio between two strings (case-insensitive)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _find_match(finding: str, pool: list[str], threshold: float = 0.55) -> str | None:
    """Return the closest string in pool if similarity >= threshold, else None."""
    best, best_score = None, 0.0
    for item in pool:
        s = _similarity(finding, item)
        if s > best_score:
            best, best_score = item, s
    return best if best_score >= threshold else None


def _extract_weaknesses(review: dict) -> list[dict]:
    """
    Extract weakness entries from a review.json.
    Supports both flat list and dict-with-axis variants:

      {"weaknesses": ["...", ...]}
      {"weaknesses": [{"text": "...", "axis": "...", "da_critical": bool}]}
    """
    raw = review.get("weaknesses") or []
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append({"text": item, "axis": None, "da_critical": False})
        elif isinstance(item, dict):
            out.append({
                "text": item.get("text") or item.get("finding") or str(item),
                "axis": item.get("axis"),
                "da_critical": bool(item.get("da_critical", False)),
            })
    # Also pick up top-level da_critical findings from worklog entry format
    for item in review.get("da_critical_findings") or []:
        text = item if isinstance(item, str) else item.get("finding", str(item))
        out.append({"text": text, "axis": None, "da_critical": True})
    return out


def _extract_addressed(review: dict) -> list[str]:
    """
    Extract weaknesses that the revision addressed (from the worklog_entry /
    actions block emitted by the revision agent).
    """
    addressed = []
    for field in ("addressed_weaknesses", "actions_taken"):
        for item in review.get(field) or []:
            if isinstance(item, str):
                addressed.append(item)
            elif isinstance(item, dict):
                addressed.append(item.get("weakness") or item.get("action") or str(item))
    return addressed


# ── core logic ───────────────────────────────────────────────────────────────

def build_memory(worklog: dict, current_review: dict, current_iter: int) -> dict:
    """
    Produce an updated critique_memory dict.

    Algorithm:
      1. Collect all weakness texts across all previous iterations from worklog.
      2. Collect all "addressed" texts from revision agents in worklog.
      3. Merge with current_review weaknesses.
      4. A weakness is "persistent" if it (or a near-duplicate) was flagged in
         ≥2 distinct iterations and not matched by any "addressed" text.
      5. A weakness is "resolved" if it was flagged before and matched by an
         "addressed" text in a subsequent iteration, and not re-flagged since.
    """

    # ── build per-iter weakness and addressed maps from worklog ─────────────
    iter_weaknesses: dict[int, list[dict]] = {}   # iter → [{text, axis, da_critical}]
    iter_addressed: dict[int, list[str]] = {}     # iter → [addressed text]

    for entry in worklog.get("iterations", []):
        i = entry.get("iter", -1)
        review_block = entry.get("review") or {}
        actions_block = entry.get("actions") or {}
        # weaknesses from the review sub-block
        iter_weaknesses[i] = _extract_weaknesses(review_block)
        # addressed texts from the actions sub-block
        iter_addressed[i] = _extract_addressed(actions_block)

    # Add current iteration's weaknesses (not yet in worklog)
    iter_weaknesses[current_iter] = _extract_weaknesses(current_review)

    # ── collect all unique finding texts seen so far ─────────────────────────
    # Map canonical_text → {first_iter, iter_set, axis, da_critical}
    canonical: dict[str, dict] = {}

    all_iters_sorted = sorted(iter_weaknesses.keys())
    for i in all_iters_sorted:
        for w in iter_weaknesses[i]:
            text = w["text"].strip()
            if not text:
                continue
            existing = _find_match(text, list(canonical.keys()))
            if existing:
                canonical[existing]["iter_set"].add(i)
                if w["axis"]:
                    canonical[existing]["axis"] = w["axis"]
                if w["da_critical"]:
                    canonical[existing]["da_critical"] = True
            else:
                canonical[text] = {
                    "first_iter": i,
                    "iter_set": {i},
                    "axis": w["axis"],
                    "da_critical": w["da_critical"],
                }

    # ── collect all addressed texts across all iterations ───────────────────
    all_addressed: list[str] = []
    for i in sorted(iter_addressed.keys()):
        all_addressed.extend(iter_addressed[i])

    # ── classify each canonical finding ─────────────────────────────────────
    persistent_issues: list[dict] = []
    resolved_issues: list[dict] = []
    da_critical_unresolved: list[dict] = []

    for text, meta in canonical.items():
        times_flagged = len(meta["iter_set"])
        was_addressed = _find_match(text, all_addressed) is not None
        still_open = not was_addressed

        if meta["da_critical"] and still_open:
            da_critical_unresolved.append({
                "finding": text,
                "first_flagged_iter": meta["first_iter"],
                "times_flagged": times_flagged,
                "axis": meta["axis"],
            })
        elif times_flagged >= 2 and still_open:
            persistent_issues.append({
                "finding": text,
                "first_flagged_iter": meta["first_iter"],
                "times_flagged": times_flagged,
                "axis": meta["axis"],
            })
        elif was_addressed and not still_open:
            resolved_issues.append({
                "finding": text,
                "first_flagged_iter": meta["first_iter"],
                "resolved_by_iter": max(
                    (i for i in iter_addressed if _find_match(text, iter_addressed[i])),
                    default=current_iter,
                ),
                "axis": meta["axis"],
            })

    # Sort persistent by times_flagged DESC, then first_iter ASC
    persistent_issues.sort(key=lambda x: (-x["times_flagged"], x["first_flagged_iter"]))

    # ── build focus/do-not-reflag prompt fragments ───────────────────────────
    focus_on = [
        f"[iter {p['first_flagged_iter']}+, {p['times_flagged']}x] {p['finding']}"
        for p in persistent_issues[:5]
    ]
    focus_on = [
        f"[DA-CRITICAL, iter {d['first_flagged_iter']}+] {d['finding']}"
        for d in da_critical_unresolved
    ] + focus_on

    do_not_reflag = [
        f"{r['finding']}"
        for r in resolved_issues[-10:]  # cap at 10 to keep prompt size reasonable
    ]

    return {
        "current_iter": current_iter,
        "persistent_issues": persistent_issues,
        "resolved_issues": resolved_issues,
        "da_critical_unresolved": da_critical_unresolved,
        "focus_on": focus_on,
        "do_not_reflag": do_not_reflag,
        "_stats": {
            "total_unique_findings": len(canonical),
            "persistent_count": len(persistent_issues),
            "resolved_count": len(resolved_issues),
            "da_critical_open": len(da_critical_unresolved),
        },
    }


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--worklog", required=True, help="Path to worklog.json")
    p.add_argument("--review",  required=True, help="Path to current iter's review.json")
    p.add_argument("--iter",    type=int, required=True, help="Current iteration number")
    p.add_argument("--out",     required=True, help="Output path for critique_memory.json")
    args = p.parse_args()

    worklog = load_json(args.worklog) if os.path.exists(args.worklog) else {"iterations": []}
    current_review = load_json(args.review)

    memory = build_memory(worklog, current_review, args.iter)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(memory, f, indent=2, ensure_ascii=False)

    stats = memory["_stats"]
    print(f"OK: critique_memory.json updated for iter {args.iter}")
    print(f"    persistent: {stats['persistent_count']}  "
          f"resolved: {stats['resolved_count']}  "
          f"da_critical_open: {stats['da_critical_open']}")
    if memory["focus_on"]:
        print("    focus_on:")
        for line in memory["focus_on"]:
            print(f"      - {line}")
    if memory["do_not_reflag"]:
        print(f"    do_not_reflag: {len(memory['do_not_reflag'])} resolved issue(s) suppressed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
