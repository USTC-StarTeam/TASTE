---
name: experiment-loop
description: Executes native autonomous experiment trajectories with propose, implement, run, diagnose, repair/prune, memory, and assurance discipline.
allowed-tools: Bash Read Grep Glob Edit Write
---

# Experiment Loop Skill

Use this skill whenever the workflow asks Claude Code to run environment work, repo/data selection, implementation, experiment iteration, repair, pruning, or autonomous scientific exploration.

## Non-negotiable Contract

- Treat the task as a trajectory, not a single answer. Continue plan -> implement -> run -> evaluate -> repair/prune until the local objective is resolved or a real missing-resource blocker is proven.
- Read persistent state before acting: `state/evolutionary_memory_index.json`, `state/trajectory_optimization_plan.json`, `state/research_memory.json`, `state/research_direction_memory.json`, `state/research_graph_history.json`, `state/research_landscape_assessment.json`, `state/research_evidence_integrity.json`, `state/research_evidence_manifest.json`, `state/evolutionary_memory_ledger.json`, `state/trajectory_checkpoints.json`, and `state/research_assurance_layer.json`.
- Do not rely on chat context as memory. Every material decision must either update a state/report/artifact file or explain why it is blocked.
- Use only project-specific repo/env/data evidence. If the current repo, conda env, or dataset should change, decide from local evidence and record the reason; never hard-code topic-specific gates.
- Synthetic smoke, dry-run, and text-only claims prove workflow plumbing only. They never support paper or scientific claims.

## Evidence Review Discipline

Before deepening a method, inspect workflow evidence: `state/evidence_review_board.json`, `reports/evidence_review_board.md`, `state/research_assurance_layer.json`, and `state/research_evidence_manifest.json`.

- If there is no audit-ready real-data experiment, keep paper claims blocked.
- If bad-case, counterexample, or claim verdict evidence is missing, create the smallest repair task or record a prune decision.
- If a method repeatedly fails for the same local reason, append that failure to the failed-hypothesis trajectory instead of endlessly retrying.
- If a method passes evidence gates, deepen it with bounded comparisons and update memory.

## Recoverable Cycle

For each queue item, follow this loop:

1. Intake: identify the exact trajectory queue item, success checks, evidence inputs, active repo, env, and data status.
2. Plan: propose the smallest evidence-producing action and the validation command.
3. Code/execute: make bounded repo-scoped edits or commands; use the project conda env, not `base`, unless local evidence proves no env exists.
4. Evaluate: inspect metrics, audits, bad cases, stdout/stderr, and state deltas.
5. Decide: choose one of `deepen`, `repair`, `compare`, `prune`, `switch_repo_or_data`, or `blocked_missing_resource`.
6. Persist: update or preserve `research_memory`, `failed_hypothesis_graph`, `unexplored_niche_graph`, `research_evidence_integrity`, `research_evidence_manifest`, `trajectory_checkpoints`, and any experiment artifacts.

## Required Outputs

A completed loop must leave enough local evidence for the next agent to resume without conversation context:

- A command, env, repo path, dataset path/status, and artifact path for any executed work.
- A metric/audit/bad-case/counterexample record for any experimental claim, or an explicit missing-evidence blocker.
- A memory update that states what was tried, what changed, what failed, and whether the branch should deepen, repair, compare, prune, or switch.
- A concise final report with `Conclusion`, `Evidence Inspected`, `Actions Taken`, `Validation`, and `Remaining Queue/Blockers`.

## Stop Rules

Stop only when a queue objective is satisfied, evidence proves a missing resource/account/compute blocker, or bounded retries are exhausted and the branch is pruned with evidence. Do not stop after a shallow inspection when local tools can still verify or repair the issue.


## Experiment Launch Contract

- Management scripts run from the repository root with the configured management Python recorded by the wrapper or environment, not a bare system interpreter.
- Training scripts run only through `scripts/launch_experiment_run.py`, and the command after `--` must start with the project experiment Python resolved from project config or runtime environment.
- Do not use system `python`, bare `python3`, `conda run`, shell redirection, `nohup`, background `&`, `tmux`, or `screen` for experiments. The launcher owns PID, stdout/stderr, lock, artifact dir, manifest, and audit-refresh contract.
- A contaminated, wrong-interpreter, reused-artifact, or failed launch is not registry evidence and must not be imported as a candidate result.
