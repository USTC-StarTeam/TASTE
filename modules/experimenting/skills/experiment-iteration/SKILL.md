---
name: experiment-iteration
description: Run TASTE experiment iteration as the Experimenting controller Claude Code, using live project Find/Read/Idea/Plan evidence, human-revision-aware adaptive experiment plans, focused Task subagents, and deterministic evidence gates.
allowed-tools: Bash Read Grep Glob Edit Write
---

# Experiment Iteration Skill

Use this skill when Experimenting asks the controller Claude Code to design, implement, run, repair, compare, or prune experiments.

## Hard Contract

- Keep the Claude working directory at `projects/<project>/`; modify only the selected experiment repository and project experiment artifacts.
- At every iteration, read the current on-disk `state/current_find_research_plan.json`, `state/experiment_plan.json`, `state/taste_plan_bridge.json`, `planning/finding/ideas.json`, and `planning/finding/plans.json` before choosing work.
- Treat the current on-disk project files as authoritative.
- Apply the newest `human_supervision_updated_at` content before the next implementation, launch, comparison, or pruning decision.
- Use exactly the selected `selected_plan_id` and `selected_idea_id`. Non-selected ideas and plans are backlog only.
- Verify that `state/environment_handoff.json` and `state/evidence_ready_repo_selection.json` still match the selected IDs and repo before launching work.
- Treat TASTE framework/module code as read-only context during an experiment iteration.
- Task subagents must omit worktree isolation unless their cwd is an independent Git repository whose top-level remains inside the current `projects/<project>/` directory.
- Ground metrics, logs, bad cases, citations, data availability, and claims in local evidence files.
- Use the experiment environment locked by the current Environment handoff.
- Record weak or missing evidence as `blocked` with `acceptance_blockers`.

## Live Evidence And Adaptive Planning

1. Read the current selected contract and the selected idea/plan rows.
2. Start from `planning/finding/read.md`; use targeted Grep/Read on `read_results.json` or the full-text packet whenever the plan omits mechanism, comparison, dataset, metric, or failure-boundary evidence.
3. Start from `planning/finding/find.md`; use targeted Grep/Read on `find_results.json` whenever provenance, competing methods, or additional paper evidence is needed.
4. Write `adaptive_experiment_plan.json` in the iteration artifact directory before changing code or launching a command.
5. Keep execution-level adaptations inside the current selected idea/plan and record every source file used.
6. If missing current-Find reading blocks the selected experiment, invoke `python framework/scripts/main.py module reading --action current_find_research_plan --project <project>` through Conda `taste`, then reload the project evidence.
7. If a useful route requires a different research idea, invoke `python framework/scripts/main.py module ideation --action idea --project <project>` through Conda `taste`, then wait for one selected Plan and matching Environment handoff.

## Controller And Task Agents

The Experimenting controller owns the final plan, repo diff, command choice, evidence judgment, and records.

- Use parallel read-only Task subagents when the decision requires evidence from at least three papers, simultaneous evidence/code/metric inspection, or independent criticism of a new experiment design.
- Give each Task subagent the exact project paths, selected IDs, one bounded question, and the required evidence-path output.
- Use an implementation Task subagent only for one isolated repo change with an explicit validation target; inspect its diff and evidence before accepting it.
- Merge subagent findings into `adaptive_experiment_plan.json`. Subagent prose alone is not experiment evidence.

## Iteration Loop

1. Intake: reload live project context, Environment handoff, recent records, and audit blockers.
2. Plan: write the adaptive experiment plan from selected-route Find/Read evidence.
3. Implement: make the smallest repo-scoped code/config change that can produce evidence.
4. Run: execute the smallest credible validation command and save logs under the artifact directory.
5. Validate: wait for the final validation command and inspect its return code, metrics, stdout/stderr, audit files, and bad cases.
6. Persist: write the summary, metrics, bad cases, registry, CSV, and Markdown records only from completed validation output.
7. Audit: run the deterministic and independent Claude audit actions, then choose `deepen`, `repair`, `compare`, `prune`, or `blocked_missing_resource`.

## Required Adaptive Plan

`adaptive_experiment_plan.json` must contain:

- `status`: `reuse_selected_plan`, `adapted_within_selected_route`, or `blocked_requires_reselection`.
- `project_context_snapshot_id`, `selected_plan_id`, `selected_idea_id`, and `human_supervision_updated_at` from the current project files read immediately before the decision.
- `source_files`: exact project files read, including the human-edited idea/plan files when a human revision exists.
- `objective`, `hypothesis`, `controls`, `variables`, `commands`, `acceptance_criteria`, `stop_criteria`, and `next_action`.
- `delegated_tasks`: Task subagent assignments and returned evidence paths, or an empty list.

## Required Summary Shape

`experiment_iteration_summary.json` must be a JSON object with:

- `status`: `success`, `completed`, `blocked`, or `failed`.
- `acceptance_status`: `accepted` only when real command/log/artifact evidence exists; otherwise a specific blocked/skipped status.
- `changed_files`: repo files changed this round.
- `commands`: commands run, with status and log paths.
- `metrics`: parsed metrics or an empty object.
- `acceptance_blockers`: structured blockers when evidence is missing or weak.
- `project_context_snapshot_id`: the snapshot used for this iteration.
- `adaptive_experiment_plan_path`: the artifact-local adaptive plan path.
- `next_action`: one concrete next step.

Synthetic/demo/smoke-only results may prove plumbing; scientific or paper claims require selected benchmark evidence and local logs.

## Required Record Ownership

Claude Code owns experiment record maintenance. Deterministic gates only check required files and block missing or weak evidence.

- `experiment_record.json` must describe the current artifact.
- Every new registry row must include `validation_finished_at`, `validation_return_code`, and a later or equal `recorded_at`.
- `state/experiment_registry.json` must preserve previous rows and upsert the current row.
- `experiments/experiment_records.csv` and `experiments/实验记录.md` must match the registry evidence.
- Metrics must come from local metric files or parseable logs.
