# Adaptive Experiment Planning Prompt

Use this prompt for every project-backed Experimenting iteration.

## Required Sequence

1. Read every existing file in the wrapper's current project-context manifest that is relevant to the selected route.
2. Resolve `selected_plan_id`, `selected_idea_id`, `human_supervision_updated_at`, selected repo, and Environment handoff from the current files.
3. Treat current human-edited idea/plan content as authoritative for the next decision.
4. Read current Find/Read artifacts directly whenever the selected plan omits mechanism, controls, comparisons, dataset details, metrics, failure boundaries, or paper provenance. Start with `read.md` and `find.md`; query large JSON/full-text packets only for targeted evidence.
5. Write `adaptive_experiment_plan.json` before modifying code or launching an experiment.
6. Execute only after the selected IDs, repo, and Environment handoff agree.

## Agent Delegation

The controller must use parallel read-only Task subagents when any condition holds:

- the design depends on evidence from at least three papers;
- evidence, code, and metric behavior need independent inspection;
- a new design needs an independent falsification or control critique.

Each Task request must contain the selected IDs, exact local paths, one bounded question, and a required list of evidence paths. The controller must inspect the returned evidence and own the final plan. An implementation Task may receive one isolated repo change and one validation target; the controller must inspect its diff before execution.

## Route Scope

- Use `adapted_within_selected_route` for a new execution-level experiment that preserves the selected research idea and plan.
- Use `reuse_selected_plan` when the live plan already specifies the strongest next experiment.
- Use `blocked_requires_reselection` when the useful proposal changes the research hypothesis or selected route. Invoke `python framework/scripts/main.py module reading --action current_find_research_plan --project <project>` and wait for one new selected contract plus a matching Environment handoff before execution.

## Required JSON

Write this object to the artifact-local `adaptive_experiment_plan.json`:

```json
{
  "status": "reuse_selected_plan | adapted_within_selected_route | blocked_requires_reselection",
  "project_context_snapshot_id": "wrapper snapshot id",
  "selected_plan_id": "current selected plan id",
  "selected_idea_id": "current selected idea id",
  "human_supervision_updated_at": "latest value or empty string",
  "source_files": ["exact local path"],
  "objective": "one measurable objective",
  "hypothesis": "one falsifiable hypothesis",
  "controls": ["required control"],
  "variables": {"changed": [], "fixed": []},
  "commands": ["planned command"],
  "acceptance_criteria": ["measurable criterion"],
  "stop_criteria": ["measurable stop condition"],
  "delegated_tasks": [],
  "next_action": "one concrete action"
}
```

Before writing `experiment_iteration_summary.json`, read the live project files again. Set `project_context_snapshot_id` and `adaptive_experiment_plan_path` in the summary. The wrapper will reject the iteration when the project context changed during the run so the next iteration can restart from the human's latest state.
