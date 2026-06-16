# Finding Module

This folder is the standalone boundary for TASTE stage `finding`.

## Responsibility

Collect, filter, score, and rank literature/tool candidates from the research topic and profile. Full-text reading evidence is explicitly outside this module.

## Independence Contract

External inputs:
- `llm_api`
- `research_topic`
- `research_interest`
- `researcher_profile`
- `source_selection`

Artifacts consumed:
- `config/profile JSON`
- `venue/source selection JSON`

Artifacts produced:
- `find_results.json`
- `article.md`
- `source_status.md`
- `category/title/detail/scoring reports`

Module roots currently owned by this module:
- `modules/finding/scripts/find_pipeline.py`
- `modules/finding/scripts/discover_*.py`
- `modules/finding/scripts/build_literature_tool_packet.py`

The module must be able to run from those explicit inputs and artifact files. TASTE may orchestrate this module from the web UI, but TASTE-specific project state must stay an adapter concern rather than hidden stage logic.

## Compatibility

The module scripts now live under this module directory. `script_manifest.json` records the scripts owned by this module, including top-level functions and imports, so future cleanup can proceed without guessing.
