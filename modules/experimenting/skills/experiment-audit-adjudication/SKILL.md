---
name: experiment-audit-adjudication
description: Adjudicate Experimenting audit evidence in a fresh Claude Code session from project/runtime/reference facts and fixed audit prompts.
allowed-tools: Bash Read Grep Glob Write
---

# Experiment Audit Adjudication Skill

Use this skill when Experimenting asks Claude Code to adjudicate experiment, runtime, reference reproduction, or claim-promotion readiness from collected audit evidence.

## Hard Contract

- Task scope must be audit adjudication only.
- Read scope must be the provided audit pack, project evidence files, and module skill/prompt contracts.
- Write scope must be the provided Experimenting run directory.
- Project code, project data, TASTE framework, web, and modules are read-only.
- Deterministic process/file evidence and project state are the primary evidence sources.
- Runtime process facts must come from `main.py --action watchdog` output or packed watchdog JSON when needed.
- Claude adjudication must cite local evidence paths for every blocking or passing claim.
- Missing evidence must produce `status=blocked` or `status=running`.
- Running jobs must produce `status=running` with the exact job evidence path.
- Paper/claim promotion is allowed only when reference reproduction, runtime integrity, experiment iteration evidence, metrics, logs, and bad-case/counterexample pressure all pass.

## Required Output

Write `audit_adjudication.json` in the provided output directory. The file must be a JSON object:

```json
{
  "status": "pass|warn|blocked|running",
  "audit_kind": "full_cycle|experiment_iteration|runtime_integrity|reference_reproduction|claim_progress|experiment_recording",
  "summary": "one concise evidence-grounded sentence",
  "decision": "continue_experiment|repair_runtime|refresh_plan|wait_running|maintain_records|block_paper|ready_for_next_stage",
  "claim_promotion_allowed": false,
  "findings": [
    {
      "severity": "block|warn|info",
      "claim": "specific finding",
      "evidence_paths": ["path"],
      "required_next_action": "one concrete action"
    }
  ],
  "gate_alignment": {
    "experiment_iteration": "pass|warn|blocked|running|missing|not_applicable",
    "runtime_integrity": "pass|warn|blocked|running|missing|not_applicable",
    "reference_reproduction": "pass|warn|blocked|running|missing|not_applicable"
  },
  "next_action": "one concrete action"
}
```

Also print the same JSON object to stdout.
