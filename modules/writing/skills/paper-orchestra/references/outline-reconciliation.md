# Outline Reconciliation (Step 3.5)

**Inspired by:** AutoSci (arXiv:2605.31468) — hypothesis refinement based on early findings.

## Why this step exists

The outline is generated in Step 1 from `idea.md` and `experimental_log.md` alone — before
any literature is actually found. Step 3 (Literature Review) may discover that:

- A Related Work cluster the outline assumed was well-populated returns few or no verified papers
- The baseline comparisons framed in `section_plan` rely on papers that failed S2/Crossref
  verification
- The actual landscape of citations warrants splitting, merging, or re-ordering clusters
- The Introduction strategy should emphasise different foundational context based on what
  was found

Without reconciliation the section-writing agent receives an outline whose `section_plan`
may contradict the actual `citation_pool.json`. The reconciliation step closes this gap with
**one lightweight LLM call** after Step 3 completes, before Step 4 begins.

## What gets updated

Only `section_plan` is updated — **never** `plotting_plan` (Step 2 may still be running)
and **never** `intro_related_work_plan` (Step 3 has already acted on it).

Permitted changes:
- Reframe section claims to match what the verified citation pool actually supports
- Adjust or remove baseline comparisons that cannot be backed by found papers
- Reorder or rename subsections to reflect the actual literature structure
- Add a note to a section that a certain comparison is "concurrent work" rather than
  a beaten baseline (per the TIMELINE RULE)

Forbidden changes:
- Adding or removing top-level sections defined in `template.tex`
- Changing any field in `plotting_plan`
- Changing any field in `intro_related_work_plan`
- Inventing new citation hints for papers not in `citation_pool.json`

## Input / Output

| File | Role |
|---|---|
| `workspace/outline.json` | Read — original outline (Step 1 output) |
| `workspace/citation_pool.json` | Read — what was actually verified (Step 3 output) |
| `workspace/cross_verification_report.json` | Read — confidence tiers (Step 3 output) |
| `workspace/drafts/intro_relwork.tex` | Read — what claims Step 3 actually wrote |
| `workspace/outline_reconciled.json` | Write — updated outline for Step 4 |

If `citation_pool.json` is absent (Step 3 is still running or failed), skip this step
and use `outline.json` directly in Step 4.

## Prompt for the reconciliation call

Load `skills/outline-agent/references/prompt.md` as the base system prompt, then prepend:

```
RECONCILIATION MODE — do not regenerate the full outline.

You are reviewing the original outline.json against what was actually found
during the literature review. Your only task is to update the `section_plan`
array so that every claim in each section is backed by the verified citation
pool.

RULES:
1. Output a complete JSON object with the same three top-level keys as outline.json.
   Copy `plotting_plan` and `intro_related_work_plan` VERBATIM — do not change a
   single character.
2. Update only `section_plan` entries where the original content_bullets reference
   papers or comparisons that (a) failed verification, or (b) are post-cutoff
   (concurrent work only).
3. For each changed bullet: keep the scientific claim, adjust only the framing
   (e.g. "outperforms [X]" → "compares favourably with the concurrent work [X]",
   or drop the comparison if [X] is entirely absent from citation_pool.json).
4. Do not add subsections or remove top-level sections.
5. Output raw JSON only — no prose, no markdown fences.
```

Input context to provide:
- `outline.json` (full)
- `citation_pool.json` (paper titles + bibtex_keys only — no full abstracts needed)
- `cross_verification_report.json` (only the `low` and `conflict` tier entries)

## Validation

After the call, validate the output:

```bash
python skills/outline-agent/scripts/validate_outline.py workspace/outline_reconciled.json
```

If validation fails, fall back to `outline.json` for Step 4 and log a warning.

Diff the two files to produce a human-readable reconciliation summary:

```bash
python skills/paper-orchestra/scripts/diff_outlines.py \
    --original  workspace/outline.json \
    --reconciled workspace/outline_reconciled.json \
    --summary   workspace/reconciliation_summary.md
```

Report the summary to the user before starting Step 4.

## When to skip

- Step 3 failed or produced an empty citation pool → skip, use `outline.json`
- `cross_verification_report.json` shows zero `low`/`conflict` entries AND
  all section_plan citation hints are already present in `citation_pool.json`
  → skip (nothing to reconcile), use `outline.json`
- Host does not support a 4th parallel call at this stage → skip, use `outline.json`

Skipping is safe. Reconciliation is a quality improvement, not a hard gate.
