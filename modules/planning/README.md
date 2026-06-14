# Planning Module

This folder is the standalone boundary for TASTE stage `planning`.

## Responsibility

Select and repair executable research plans from approved ideas; downstream modules consume only explicit selected plan contracts.

## Independence Contract

External inputs:
- `llm_api_or_claude`
- `idea_artifacts`
- `project_constraints`

Artifacts consumed:
- `ideas.json`
- `idea.md`
- `user selection/approval`

Artifacts produced:
- `plans.json`
- `plan.md`
- `experiment_plan.json`
- `taste_plan_bridge.json`
- `blocker action plans`

Module roots currently owned by this module:
- `modules/planning/auto_research/auto_plan`
- `modules/planning/scripts/plan_experiments.py`
- `modules/planning/scripts/build_workflow_blueprint.py`

The module must be able to run from those explicit inputs and artifact files. TASTE may orchestrate this module from the web UI, but TASTE-specific project state must stay an adapter concern rather than hidden stage logic.

## Compatibility

The module scripts now live under this module directory. `script_manifest.json` records the scripts owned by this module, including top-level functions and imports, so future cleanup can proceed without guessing.
