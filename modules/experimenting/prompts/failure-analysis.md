# Failure Analysis Prompt

Use this prompt instead of a standalone failure-analysis script.

Inspect the experiment registry, selected run summaries, stdout/stderr logs, metrics, audit files, and bad-case files. For the target method or experiment id, produce a concise diagnosis with:

- attempted runs and statuses
- best metric and comparison target, if meaningful
- implementation or environment failures
- missing metric/audit/bad-case evidence
- weakest data slice or counterexample pressure
- decision: `repair`, `compare`, `deepen`, `prune`, or `blocked_missing_resource`
- one next command or one explicit blocker

The diagnosis must rank or reject a method only from local metrics/logs. Missing logs or metrics must produce `blocked_missing_evidence`.
