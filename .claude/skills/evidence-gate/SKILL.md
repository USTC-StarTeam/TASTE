---
name: evidence-gate
description: Applies native evidence assurance gates to experiments, trajectory memory, claims, citations, repo/data decisions, and paper promotion.
allowed-tools: Bash Read Grep Glob
---

# Evidence Gate Skill

Use this skill before accepting experiment results, changing repo/data/env direction, promoting paper claims, or marking autonomous research progress as real. It connects skeptical evidence review, long-horizon trajectory memory, and paper-promotion control into one native evidence contract.

## Required Evidence Reads

Inspect the strongest local evidence available before judging:

- `state/research_assurance_layer.json`
- `state/research_evidence_integrity.json`
- `state/research_evidence_manifest.json`
- `state/claim_ledger.json`
- `state/experiment_registry.json`
- `state/dataset_registry.json`
- `state/repo_data_requirements.json`
- `state/real_dataset_probe.json`
- `state/evidence_review_board.json`
- `reports/paper_evidence_audit.md`
- Any artifact paths named by the queue item or experiment registry.

## Verdict Rules

- `pass`: all claims point to auditable local artifacts or verifiable external records, and real-data experiments are audit-ready.
- `warn`: evidence is present but incomplete, limited, or only enough for planning/engineering claims.
- `block`: evidence is missing, local paths do not exist, claims are weak/unsupported, paper audit says hold-markdown-only, or only synthetic/dry-run artifacts exist.

A blocked gate is a success of the assurance layer when it prevents overclaiming. Never weaken the gate to make the UI look green.

## Claim Promotion Rules

- Metrics require `metrics.json` or an experiment registry entry with command, env, dataset, method, metric name/value, and artifact path.
- Scientific claims require real loader-ready data, audit-ready experiment evidence, claim verdict, bad-case or missing-reason evidence, and counterexample pressure.
- Repo/data/env recommendations require inspected local files or explicit external source references.
- Paper claims require citation/evidence checks and must not be promoted from workflow smoke, dry-run, synthetic-only, or text-only outputs.

## Anti-Self-Deception Checks

Always ask:

- Is this evidence a local file, external record, or only a generated sentence?
- Does the local file exist now?
- Does the evidence support the exact claim, or only a weaker engineering/plumbing statement?
- Is there a failed-hypothesis or recoverable-exception memory that contradicts the claim?
- Would a future agent be able to reproduce this judgment without chat context?

## Required Output

Return a concise verdict with:

- `Verdict`: pass, warn, or block.
- `Evidence inspected`: exact local paths and external records.
- `Unsupported or weak claims`: list them explicitly.
- `Required repair`: smallest action that can produce missing evidence, or a truthful blocker/prune decision.
