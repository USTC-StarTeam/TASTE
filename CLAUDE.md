# Claude Code Guide for TASTE

You are operating TASTE from this repository root.

This file is a repository-maintainer guide for operating and repairing TASTE. It is not a project-scientist handoff. A TASTE-launched project Claude Code session must keep its own notes, memory, handoff, and scientific evidence under `projects/<project>/`.

## Hard Rules

- Work only inside this repository root and the selected repo under `projects/<project>/repos/`.
- Never fabricate metrics, citations, logs, datasets, or paper claims.
- Every scientific claim must point to local evidence such as `metrics.json`, `audit.json`, `bad_cases.json`, `claim_ledger.md`, `paper_evidence_audit.md`, or discovered paper/repo metadata.
- Never use conda `base` for project experiments unless explicitly allowed for diagnostics.
- Keep runtime projects, datasets, generated papers, logs, and downloaded research repos out of git.

## Claude Code Agent Pack

For experiment execution, use the `experiment-coordinator` agent concept and the `experiment-loop` / `evidence-gate` skills. The coordinator should reason like an evidence panel: implementer, bad-case analyst, evidence auditor, and prune/deepen critic.

For paper work, use the `paper-orchestrator` agent concept and the `writing` skill. Paper drafting must be section-wise, citation-checked, claim-checked, and evidence-gated before promotion.

## Final Response Discipline

Report validation status, files changed, metrics/audit/bad-case artifact paths, weakest slice, claim verdict, and whether the next action is deepen, repair, compare, or prune.
