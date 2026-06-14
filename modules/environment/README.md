# Environment Module

This folder is the standalone boundary for TASTE stage `environment`.

## Responsibility

Select audited code/data bases, probe loaders, and lock the experiment runtime. It does not run novel experiments or write paper claims.

## Independence Contract

External inputs:
- `selected_plan_contract`
- `candidate_repo_data_artifacts`
- `runtime_config`

Artifacts consumed:
- `plans.json`
- `literature_tool_packet.json`
- `repo/data candidates`

Artifacts produced:
- `evidence_ready_repo_selection.json`
- `repo_env_bootstrap.json`
- `dataset registry`
- `reference/data gates`

Module roots currently owned by this module:
- `modules/environment/scripts/run_environment_stage.py`
- `modules/environment/scripts/select_evidence_ready_repo.py`
- `modules/environment/scripts/bootstrap_repo_env.py`

The module must be able to run from those explicit inputs and artifact files. TASTE may orchestrate this module from the web UI, but TASTE-specific project state must stay an adapter concern rather than hidden stage logic.

## Compatibility

The module scripts now live under this module directory. `script_manifest.json` records the scripts owned by this module, including top-level functions and imports, so future cleanup can proceed without guessing.
