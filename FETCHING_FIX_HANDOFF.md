# Fetching Fix Handoff

## Task

Test and fix venue fetching for the project source list, mainly for the 2025 audit result. The goal is shallow: for each failed venue-year row, find whether the venue is fetchable by the project fetch logic, add the smallest route/catalog fix when a real source is available, verify it, and log either success or the reason it remains unresolved.

## Original Logic To Reference

- Fetching logic lives in `auto_research/auto_find/sources.py`.
- Venue catalog lives in `auto_research/data/ccf_venues.json`.
- Catalog loading helpers live in `auto_research/auto_find/catalog.py`.
- The fetch test script should call `fetch_venue_title_index_all(venue, [year])`; do not invent a separate fetch behavior for the test.
- Ignore local cache behavior for this task.

## Materials Built

- `fetching_test/test_all_fetching.py`
  - Traverses all catalog venues for input years.
  - Writes JSON/CSV under `fetching_test/result-anual/`.
  - Records pass/fail, adapter, attempted routes, and short failure reason.
- `fetching_test/reroute_failed_fetches.py`
  - Helper for trying alternate DBLP venue routes against failed rows.
- Main audit file:
  - `fetching_test/result-anual/fetching_test_2025_20260528T181728Z.json`
- Original reroute audit:
  - `fetching_test/reroute-result/reroute_2025_20260528T200918Z/summary.json`
- Implemented route logs:
  - `fetching_test/reroute-result/implemented_routes_2025_batch1.json`
  - through `fetching_test/reroute-result/implemented_routes_2025_batch13.json`
- Current running notes:
  - `PROJECT_STATE.md`

## Current Code Changes

- `sources.py` now has Crossref fallback helpers for selected proceedings and journals.
- `sources.py` now has narrow direct DBLP routes for slow/special DBLP pages, e.g. CHI and IJHCI.
- DBLP fetch attempts are narrowed to real DBLP/direct DBLP routes so non-DBLP publisher URLs do not waste time before Crossref.
- `ccf_venues.json` has several confirmed catalog route fixes, including SANER, CogSci, and TOCE.

## Current Status

- The original 2025 audit had 451 failed rows.
- Implemented route work has logs through `batch13`.
- Latest completed batch:
  - `implemented_routes_2025_batch13.json`
  - result: 19 success, 6 unresolved.
- Batch 12 was fully resolved: 25 success, 0 unresolved.
- Last focused verification passed:
  - `conda run --no-capture-output -n TASTE pytest tests/test_source_enrichment.py tests/test_local_cache.py`
  - result: 8 passed.

Known unresolved from processed batches are listed in `PROJECT_STATE.md`. The latest added unresolved venues are:

- `CHES`
- `FSE`
- `DFRWS`
- `DRM`
- `HotSec`
- `ICDF2C`

## Where To Continue

Continue from the latest unfinished reroute work after `batch13`. Use the 2025 audit file as source of truth and process the next failed rows in small batches.

Before declaring coverage complete, verify the union of `implemented_routes_2025_batch*.json` against all 451 failed rows. Do not assume the batch numbers alone prove contiguous coverage.

For each remaining failed row:

1. Check the current configured fetch first with `fetch_venue_title_index_all(venue, [2025])`.
2. If it fails, look for the smallest real route:
   - corrected DBLP venue URL,
   - direct DBLP year/volume page,
   - Crossref proceedings query,
   - Crossref journal query.
3. Test the route.
4. If successful, implement the minimal route/catalog change.
5. If still failed after up to three attempts, log the short reason in the batch JSON and move on.
6. Run focused tests after source/catalog edits.
7. Update `PROJECT_STATE.md` after each major batch.

## Constraints

- Keep diffs minimal.
- Do not rewrite the fetching architecture.
- Do not add local cache behavior to the test.
- Do not commit generated run outputs unless explicitly wanted.
- Ignore existing `.DS_Store` changes unless the user asks.
