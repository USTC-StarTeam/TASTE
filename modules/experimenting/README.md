# Experimenting Module

This folder is the standalone boundary for TASTE stage `experimenting`.

## Responsibility

Modify or execute selected project code, run auditable experiments, parse metrics/logs, and produce evidence gates for claims.

## Independence Contract

External inputs:
- `selected_plan_contract`
- `locked_environment`
- `repo_path`
- `experiment_python`

Artifacts consumed:
- `evidence_ready_repo_selection.json`
- `repo_env_bootstrap.json`
- `experiment_plan.json`

Artifacts produced:
- `experiment_registry.json`
- `experiment artifacts/logs`
- `runtime integrity audit`
- `reference/scientific progress gates`

Module roots currently owned by this module:
- `modules/experimenting/scripts/run_coding_agent.py`
- `modules/experimenting/scripts/launch_experiment_run.py`
- `modules/experimenting/scripts/experiment_contracts.py`

The module must be able to run from those explicit inputs and artifact files. TASTE may orchestrate this module from the web UI, but TASTE-specific project state must stay an adapter concern rather than hidden stage logic.

## Compatibility

The module scripts now live under this module directory. `script_manifest.json` records the scripts owned by this module, including top-level functions and imports, so future cleanup can proceed without guessing.
