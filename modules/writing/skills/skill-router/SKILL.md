---
name: skill-router
description: TASTE Writing 的 Claude Code skill 路由表。Use when a writing, audit, repair, benchmark, figure, citation, venue, or paper-quality task starts so Claude selects the exact modules/writing/skills/* skills to load.
---

# Skill Router

## Default Route

For `main.py --action work`, load these skills in order:

1. `taste-paper-writing`
2. `venue-intelligence`
3. `citation-integrity`
4. `writing-quality`
5. `paper-orchestra`

`paper-orchestra` then loads:

1. `outline-agent`
2. `plotting-agent`
3. `literature-review-agent`
4. `section-writing-agent`
5. `content-refinement-agent`

## Conditional Route

Load `nature-family-writing` when `venue_requirements.json` identifies Springer Nature or Nature-family venue shape.

Load `agent-research-aggregator` when `workspace/inputs/idea.md` or `workspace/inputs/experimental_log.md` is missing, thin, or replaced by agent/cache logs.

Load `paper-autoraters` when the task asks for scoring, side-by-side comparison, reviewer-style rating, or post-generation quality measurement beyond the required audit.

Load `paper-writing-bench` when the task asks to build benchmark inputs from an existing paper, compare against PaperWritingBench, or reverse-engineer sparse/dense idea plus experimental log from a PDF.

Load `writing-audit` for each fresh independent audit and for any pass/blocked judgment on the canonical project manuscript.

Load `taste-paper-writing`, `writing-quality`, `content-refinement-agent`, `citation-integrity`, and `venue-intelligence` for any repair round created from a blocked writing audit. Add `section-writing-agent`, `plotting-agent`, or `literature-review-agent` when the audit instructions name paper sections, figures/tables, or references.

## Skill Utility Map

| Skill | Required use |
| --- | --- |
| `taste-paper-writing` | Own the TASTE writing contract, canonical project layout, required outputs, and evidence boundaries. |
| `venue-intelligence` | Resolve current official venue rules and template sources. |
| `citation-integrity` | Build and check real references, BibTeX keys, citation coverage. |
| `writing-quality` | Raise paper prose and structure to oral-level submission quality while preserving evidence bounds. |
| `paper-orchestra` | Coordinate the end-to-end multi-agent writing pipeline. |
| `outline-agent` | Produce `workspace/outline.json` from idea, log, template, and venue guidance. |
| `plotting-agent` | Produce figures, diagrams, and captions from the outline and experimental evidence. |
| `literature-review-agent` | Search, verify, deduplicate, and write literature coverage plus `refs.bib`. |
| `section-writing-agent` | Draft method, experiments, abstract, conclusion, tables, and integrated LaTeX. |
| `content-refinement-agent` | Run review-driven revisions with snapshots, worklog, and accept/revert rules. |
| `nature-family-writing` | Convert section shape, tone, and policy handling for Nature-family journals. |
| `agent-research-aggregator` | Convert scattered agent logs into structured idea/log inputs before writing. |
| `paper-autoraters` | Score or compare drafts with PaperOrchestra-style autoraters. |
| `paper-writing-bench` | Build benchmark cases from existing papers. |
| `writing-audit` | Produce pass/blocked audit verdicts for the canonical project manuscript. |

## Execution Rule

Before writing, auditing, repairing, scoring, or benchmarking, select skills from this route and read each selected `SKILL.md` before acting. A blocked audit repair must read the archived audit JSON/Markdown before editing paper artifacts.
