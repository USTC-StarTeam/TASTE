# Reading Module

This folder is the standalone boundary for TASTE stage `reading`.

## Responsibility

Acquire verified paper-body text for the selected Find packet and synthesize reading notes. Same-run replacements for unavailable public full text happen here, never inside Finding.

## Independence Contract

External inputs:
- `llm_api_or_claude`
- `finding_artifact_packet`
- `artifact_root`

Artifacts consumed:
- `find_results.json`
- `article.md`
- `full_text_reading/manual_full_text_sources.json`

Artifacts produced:
- `read_results.json`
- `read.md`
- `full_text_reading/full_text_packet.json`
- `current_find_full_text_evidence_repair.json`

Module roots currently owned by this module:
- `modules/reading/scripts/read_pipeline.py`
- `modules/reading/scripts/repair_current_find_full_text_evidence.py`
- `modules/reading/scripts/ensure_current_find_research_plan.py`

The module must be able to run from those explicit inputs and artifact files. TASTE may orchestrate this module from the web UI, but TASTE-specific project state must stay an adapter concern rather than hidden stage logic.

## Compatibility

The module scripts now live under this module directory. `script_manifest.json` records the scripts owned by this module, including top-level functions and imports, so future cleanup can proceed without guessing.
