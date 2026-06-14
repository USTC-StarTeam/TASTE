# Writing Module

This folder is the standalone boundary for TASTE stage `writing`.

## Responsibility

Resolve venue requirements, draft/revise/compile the manuscript, and audit citations/figures/submission readiness from experiment evidence.

## Independence Contract

External inputs:
- `venue`
- `selected_plan_contract`
- `experiment_evidence`
- `paper_config`

Artifacts consumed:
- `experiment_registry.json`
- `claim ledger`
- `venue template/requirements`

Artifacts produced:
- `paper draft/revision`
- `compiled PDF`
- `paper_pipeline.json`
- `submission_readiness.json`

Module roots currently owned by this module:
- `modules/writing`
- `modules/writing/scripts/run_paper_pipeline.py`
- `modules/writing/scripts/paper_common.py`

The module must be able to run from those explicit inputs and artifact files. TASTE may orchestrate this module from the web UI, but TASTE-specific project state must stay an adapter concern rather than hidden stage logic.

## Compatibility

The module scripts now live under this module directory. `script_manifest.json` records the scripts owned by this module, including top-level functions and imports, so future cleanup can proceed without guessing.
