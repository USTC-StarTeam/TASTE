# Ideation Module

This folder is the standalone boundary for TASTE stage `ideation`.

## Responsibility

Turn reading/finding artifacts into editable research ideas without selecting an execution route.

## Independence Contract

External inputs:
- `llm_api_or_claude`
- `reading_artifacts`
- `research_profile`

Artifacts consumed:
- `find_results.json`
- `read_results.json`
- `read.md`

Artifacts produced:
- `ideas.json`
- `idea.md`
- `hypothesis_arena.md`
- `idea candidate audits`

Module roots currently owned by this module:
- `modules/ideation/scripts/idea_pipeline.py`
- `modules/ideation/scripts/assess_idea_candidates.py`
- `modules/ideation/scripts/build_hypothesis_arena.py`

The module must be able to run from those explicit inputs and artifact files. TASTE may orchestrate this module from the web UI, but TASTE-specific project state must stay an adapter concern rather than hidden stage logic.

## Compatibility

The module scripts now live under this module directory. `script_manifest.json` records the scripts owned by this module, including top-level functions and imports, so future cleanup can proceed without guessing.
