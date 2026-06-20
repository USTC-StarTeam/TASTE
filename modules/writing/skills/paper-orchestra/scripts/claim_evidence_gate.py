#!/usr/bin/env python3
"""
claim_evidence_gate.py — Verify that quantitative claims in a paper draft
are grounded in the experimental log (AutoSci-inspired claim-evidence gate).

Analogous to orphan_cite_gate.py for citations: this gate extracts numeric
claims from the LaTeX draft and checks each against experimental_log.md.
Claims that cannot be corroborated are flagged as UNSUPPORTED.

This is a WARN gate (like validate_consistency.py), not a hard-stop gate:
  exit 0 — PASS: all extracted claims corroborated, or no claims extracted
  exit 1 — WARN: one or more claims could not be corroborated
  exit 2 — ERROR: input file missing or unreadable

Run during content-refinement Step 0 (pre-refinement integrity gate), after
ai_failure_modes checks.

Usage:
    python claim_evidence_gate.py \\
        --paper  workspace/drafts/paper.tex \\
        --log    workspace/inputs/experimental_log.md \\
        --out    workspace/claim_evidence_report.json

Output JSON:
    {
      "supported":   [ {claim, value, context, evidence_snippet} ],
      "unsupported": [ {claim, value, context} ],
      "uncertain":   [ {claim, value, context, reason} ],
      "summary": {
        "total": N,
        "supported": N, "unsupported": N, "uncertain": N
      }
    }
"""
import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict


# ── numeric claim patterns ────────────────────────────────────────────────────
# These patterns capture quantitative claims that typically appear in results:
#   - percentage improvements / accuracies          e.g. "improves by 3.2%"
#   - absolute metric values in result context      e.g. "achieves 87.4 mAP"
#   - ratio/fold improvements                       e.g. "2.5× faster"
#   - comparison operators with numbers             e.g. "outperforms X by 5.1"
# Patterns are intentionally broad; false positives are marked UNCERTAIN.

CLAIM_PATTERNS: list[re.Pattern] = [
    # percentage: "by 3.2%", "of 87.4%", "achieves 92.1%"
    re.compile(
        r"(?:by|of|achieves?|improves?\s+(?:by|to)|gains?|reduces?\s+(?:by|to)|"
        r"increases?\s+(?:by|to)|decreases?\s+(?:by|to)|accuracy|f1|recall|"
        r"precision|score|performance)\s+([+-]?\d+\.?\d*)\s*%",
        re.IGNORECASE,
    ),
    # ratio: "2.5× faster", "3× more", "×1.8"
    re.compile(r"(\d+\.?\d*)\s*[×x]\s*(?:faster|slower|more|less|better|worse)", re.IGNORECASE),
    re.compile(r"[×x]\s*(\d+\.?\d*)", re.IGNORECASE),
    # absolute metric with label: "87.4 mAP", "0.923 AUC", "12.3 BLEU"
    re.compile(r"(\d+\.?\d*)\s+(?:mAP|AUC|BLEU|ROUGE|CIDEr|FID|IS|top-\d+|WER|CER|IoU)",
               re.IGNORECASE),
    # "outperforms / exceeds / surpasses ... by N"
    re.compile(
        r"(?:outperforms?|exceeds?|surpasses?|beats?|better\s+than|"
        r"superior\s+to|lags?\s+behind)\s+[^.]{0,60}?\s+by\s+([+-]?\d+\.?\d*)",
        re.IGNORECASE,
    ),
    # LaTeX table cells: numbers in tabular environments (heuristic)
    re.compile(r"\\textbf\{(\d+\.?\d*)\}", re.IGNORECASE),
    re.compile(r"(\d{2,3}\.\d{1,2})\s*(?:\\\\|&|\})", re.IGNORECASE),
]

# Patterns that indicate a sentence is in a related-work / prior-work context
# (these numbers belong to cited papers, not our claims — mark as UNCERTAIN)
PRIOR_WORK_CONTEXT = re.compile(
    r"(?:previous|prior|existing|baselines?|compared\s+to|cite|cited|"
    r"et\s+al\.|\\cite\{|\\citet\{|\\citep\{|concurrent|related)",
    re.IGNORECASE,
)

# Minimum number of characters around a match to extract as context snippet
CONTEXT_WINDOW = 120


# ── helpers ───────────────────────────────────────────────────────────────────

@dataclass
class Claim:
    value: str
    context: str
    pattern_id: int
    is_prior_work: bool = False


def strip_latex_commands(text: str) -> str:
    """Remove common LaTeX markup to reduce false-positive matches."""
    text = re.sub(r"\\(?:label|ref|cite[tp]?|footnote|url|href)\{[^}]*\}", " ", text)
    text = re.sub(r"\\(?:begin|end)\{[^}]*\}", " ", text)
    text = re.sub(r"%.*$", "", text, flags=re.MULTILINE)  # strip comments
    return text


def _extract_sentence(text: str, match_start: int, match_end: int) -> str:
    """
    Return the sentence(s) most immediately containing the match.
    Uses sentence boundaries (. ! ?) rather than a fixed char window for the
    prior-work detection, so adjacent sections don't bleed in.
    """
    # Find the sentence start: last sentence-ending punctuation before the match
    before = text[:match_start]
    sent_start = max(
        before.rfind(". "),
        before.rfind(".\n"),
        before.rfind("! "),
        before.rfind("? "),
        before.rfind("\n\n"),
    )
    sent_start = sent_start + 1 if sent_start >= 0 else 0

    # Find the sentence end: next sentence-ending punctuation after the match
    after = text[match_end:]
    ends = [after.find(". "), after.find(".\n"), after.find("! "), after.find("? "),
            after.find("\n\n")]
    ends = [e for e in ends if e >= 0]
    sent_end = match_end + (min(ends) + 1 if ends else len(after))

    return text[sent_start:sent_end].replace("\n", " ").strip()


def extract_claims(tex: str) -> list[Claim]:
    """Extract all quantitative claims from LaTeX source."""
    clean = strip_latex_commands(tex)
    seen_values: set[str] = set()
    claims: list[Claim] = []

    for pid, pat in enumerate(CLAIM_PATTERNS):
        for m in pat.finditer(clean):
            val = m.group(1).strip()
            if not val or val in seen_values:
                continue
            # Skip very small numbers that are likely formatting (0, 1, 2…)
            try:
                if float(val) < 0.5:
                    continue
            except ValueError:
                pass

            # Sentence-bounded context for prior-work detection (prevents
            # adjacent-section bleed where "Previous methods..." in Related Work
            # falsely flags numbers in the Results section)
            sentence = _extract_sentence(clean, m.start(), m.end())
            is_prior = bool(PRIOR_WORK_CONTEXT.search(sentence))

            # Wider context for the human-readable context snippet
            start = max(0, m.start() - CONTEXT_WINDOW)
            end = min(len(clean), m.end() + CONTEXT_WINDOW)
            ctx = clean[start:end].replace("\n", " ").strip()

            claims.append(Claim(value=val, context=ctx, pattern_id=pid, is_prior_work=is_prior))
            seen_values.add(val)

    return claims


def build_number_index(log_text: str) -> set[str]:
    """
    Extract all numeric strings from experimental_log.md for O(1) lookup.
    Returns a set of string representations (e.g. "87.4", "3.2", "2.5").
    """
    nums: set[str] = set()
    for m in re.finditer(r"\b(\d+\.?\d*)\b", log_text):
        nums.add(m.group(1))
    return nums


def find_evidence(value: str, log_text: str) -> str | None:
    """
    Return a snippet from the log containing the given numeric value, or None.
    Matches whole decimal numbers (e.g. "87.4" matches "87.4" but not "87.41").
    """
    pat = re.compile(r"(?<!\d)" + re.escape(value) + r"(?!\d)")
    m = pat.search(log_text)
    if not m:
        return None
    start = max(0, m.start() - 80)
    end = min(len(log_text), m.end() + 80)
    return log_text[start:end].replace("\n", " ").strip()


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--paper", required=True, help="Path to paper.tex (draft)")
    p.add_argument("--log",   required=True, help="Path to experimental_log.md")
    p.add_argument("--out",   required=True, help="Output path for claim_evidence_report.json")
    args = p.parse_args()

    for path in (args.paper, args.log):
        if not os.path.exists(path):
            print(f"ERROR: file not found: {path}", file=sys.stderr)
            return 2

    with open(args.paper) as f:
        tex = f.read()
    with open(args.log) as f:
        log_text = f.read()

    claims = extract_claims(tex)
    log_nums = build_number_index(log_text)

    supported: list[dict] = []
    unsupported: list[dict] = []
    uncertain: list[dict] = []

    for c in claims:
        evidence = find_evidence(c.value, log_text)

        if c.is_prior_work:
            uncertain.append({
                "claim": c.context[:200],
                "value": c.value,
                "context": c.context,
                "reason": "appears in prior-work / citation context — not our claim",
            })
        elif evidence is not None:
            supported.append({
                "claim": c.context[:200],
                "value": c.value,
                "context": c.context,
                "evidence_snippet": evidence,
            })
        elif c.value in log_nums:
            # Number is in the log but not in matching sentence context — weak support
            supported.append({
                "claim": c.context[:200],
                "value": c.value,
                "context": c.context,
                "evidence_snippet": f"[number {c.value!r} found in log, no local snippet]",
            })
        else:
            unsupported.append({
                "claim": c.context[:200],
                "value": c.value,
                "context": c.context,
            })

    report = {
        "supported":   supported,
        "unsupported": unsupported,
        "uncertain":   uncertain,
        "summary": {
            "total":       len(claims),
            "supported":   len(supported),
            "unsupported": len(unsupported),
            "uncertain":   len(uncertain),
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    s = report["summary"]
    print(f"Claim-evidence gate: {s['total']} claims extracted")
    print(f"  supported:   {s['supported']}")
    print(f"  unsupported: {s['unsupported']}")
    print(f"  uncertain:   {s['uncertain']}")

    if unsupported:
        print("\nUNSUPPORTED claims (not found in experimental_log.md):")
        for item in unsupported:
            print(f"  [{item['value']}] {item['claim'][:120]}")
        print(f"\nWARN: {len(unsupported)} unsupported claim(s) — review before submission.")
        print(f"Full report: {args.out}")
        return 1

    print(f"PASS — all extracted claims are corroborated by experimental_log.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
