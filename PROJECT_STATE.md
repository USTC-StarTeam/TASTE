# Project State

## Fetching Fix Work

- Main 2025 fetch audit source: `fetching_test/result-anual/fetching_test_2025_20260528T181728Z.json`.
- Latest 2025 full fetch test: `fetching_test/result-anual/fetching_test_2025_20260531T175450Z.json`.
- Latest 2025 full fetch test summary: 577 success, 73 empty, 0 errors, out of 650 venues.
- Latest failed-venue retest: `fetching_test/result-anual/fetching_test_2025_20260601T022042Z.json` → **11/48 flaky pass**, **30 remaining** (was 37).
- Resolved (removed from task list): `fetching_test/resolved_venues_2025.json` (43 venues total).
- Next-level tasks only: `fetching_test/failed_venues_2025.json` (30 venues).
- Reroute audit log: `fetching_test/reroute-result/reroute_2025_20260528T200918Z/summary.json`.
- Reroute audit processed 451/451 failed 2025 venue rows; 0 missing.
- Cursor handoff file: `FETCHING_FIX_HANDOFF.md`.
- Confirmed catalog fix: `SANER` now uses `https://dblp.org/db/conf/saner/`.
- Added Crossref fallback routes in `auto_research/auto_find/sources.py`:
  - `crossref_proceedings`: validated for `CCGRID`, `FPT`, `HPCC`, `ISPA`.
  - `crossref_proceedings`: also validated for selected networking venues including `APNOMS`, `LCN`, `MSN`.
  - `crossref_journal`: validated for selected journal gaps including `DKE`, `PR`, `TASLP`, `EAAI`, `ESWA`, `FGCS`, `JETTA`, `JGC`, `JSAC`, `Neurocomputing`, `TJSC`.
- Verification logs:
  - `fetching_test/reroute-result/implemented_routes_2025_batch1.json`
  - `fetching_test/reroute-result/implemented_routes_2025_batch2.json`
  - `fetching_test/reroute-result/implemented_routes_2025_batch3.json`
  - `fetching_test/reroute-result/implemented_routes_2025_batch4.json`
  - `fetching_test/reroute-result/implemented_routes_2025_batch5.json`
  - `fetching_test/reroute-result/implemented_routes_2025_batch6.json`
  - `fetching_test/reroute-result/implemented_routes_2025_batch7.json`
  - `fetching_test/reroute-result/implemented_routes_2025_batch8.json`
  - `fetching_test/reroute-result/implemented_routes_2025_batch9.json`
  - `fetching_test/reroute-result/implemented_routes_2025_batch10.json`
  - `fetching_test/reroute-result/implemented_routes_2025_batch11.json`
  - `fetching_test/reroute-result/implemented_routes_2025_batch12.json`
  - `fetching_test/reroute-result/implemented_routes_2025_batch13.json`
  - `fetching_test/reroute-result/implemented_routes_2025_batch14.json`
- Batch verification counts:
  - batch1: 4 success, 4 unresolved.
  - batch2: 11 success, 2 unresolved.
  - batch3: 26 success, 4 unresolved.
  - batch4: 21 success, 4 unresolved.
  - batch5: 24 success, 1 unresolved.
  - batch6: 24 success, 1 unresolved.
  - batch7: 23 success, 2 unresolved.
  - batch8: 18 success, 7 unresolved.
  - batch9: 25 success, 0 unresolved.
  - batch10: 20 success, 5 unresolved.
  - batch11: 22 success, 3 unresolved.
  - batch12: 25 success, 0 unresolved.
  - batch13: 19 success, 6 unresolved.
  - batch14: 24 success, 1 unresolved.
- Pass 2 catalog fixes (2025): QRS→dblp conf/qrs; FSE→ToSC; SCA→PACMCGIT; SGP/EGSR→CGF; WICSA→ICSA; ITS→PACMHCI; IPSN→SenSys; JSLHR→Crossref; CL→Crossref rename.
- Pass 3 catalog fixes: ICAPS→dblp conf/icaps (was aips); RTA→dblp conf/fscd (RTA merged into FSCD). ICAPS also Crossref AAAI OJS vol 35.
- Pass 4 fixes (2026-06-01): Performance→SIGMETRICS; CGI/GMP/SMI/CAD/Graphics/SPM→CGF vol 44 via `DBLP_DIRECT_VOLUME_LINKS`; BMVC→`fetch_bmvc_proceedings` (bmva.org).
- Remaining unresolved (30): see `fetching_test/failed_venues_2025.json`. Mostly no 2025 edition (ECCV, FM, ETAPS, LISA, PPSN, ICPR, ACML), proceedings not yet on DBLP/Crossref (AMIA, ANCS, SECON, …), HotSec (no stable index), NLE/MT (no 2025 articles indexed).

## Constraints From User

- Keep diffs minimal.
- Do not explain unless asked.
- Do not scan the whole repo unless necessary.
- Read only files relevant to fetching task.
- Ask before broad searches.
- Update this file after major changes.
