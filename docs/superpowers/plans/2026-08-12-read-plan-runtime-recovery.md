# Read Cooldown and Plan Timeout Recovery Implementation Plan

> **For Codex:** Execute this plan in the current workspace with `superpowers:executing-plans`; the user explicitly requested direct work on the current branch.

**Goal:** Recover Read papers after the module's own HTTP 429 cooldown and preserve only fully audited `plan.md` files that Claude finishes writing immediately before a timeout.

**Architecture:** Keep both existing pipelines and retry counts unchanged. Derive Read's run-wide recovery wait budget from the configured cooldown instead of contradicting it. In Planning, treat `TimeoutExpired` separately, re-read the target, require a real file change, and run the existing deterministic publication audit before accepting the round.

**Tech Stack:** Python 3, pytest, existing TASTE Reading and Planning pipelines.

---

### Task 1: Make Read recovery capable of outwaiting configured 429 cooldowns

**Files:**
- Modify: `tests/test_taste_contracts.py`
- Modify: `modules/reading/scripts/pipeline/read_pipeline.py`
- Modify: `modules/reading/scripts/core/common.py`
- Modify: `modules/reading/config/reading.json`

1. Add a regression test where a 119-second service cooldown clears within the configured 120-second rate-limit period and the paper is retried exactly once.
2. Run that test and confirm it fails because the current recovery budget is 30 seconds.
3. Set the documented/default batch recovery cap to 120 seconds and compute the effective run budget as at least `rate_limit_cooldown_sec`, without changing the one-retry/one-worker behavior.
4. Update existing literal expectations and run all Reading cooldown tests.

### Task 2: Preserve an audited plan file written just before Claude timeout

**Files:**
- Modify: `tests/test_web_framework_bridge.py`
- Modify: `modules/planning/scripts/core/plan_pipeline.py`

1. Add a regression test whose Claude subprocess writes a complete valid `plan.md` and then raises `TimeoutExpired`; assert the round succeeds only because the file changed and passes the existing audit.
2. Add the complementary invalid/partial-file timeout assertion.
3. Run the new test and confirm the valid-file case fails in the current broad exception branch.
4. Handle only `subprocess.TimeoutExpired`: capture diagnostics, re-read the file, require a change, audit against expected plan rows, accept on audit pass, otherwise preserve the failure.
5. Pass expected plan rows from initial generation, repairs, polish, and selection; keep exact repair-round behavior unchanged.
6. Run Planning and framework bridge tests.

### Task 3: Verify live workflow and artifacts

**Files:**
- Inspect: `projects/diffrl_prot/planning/finding/**`
- Inspect: `modules/reading/.runtime/**`
- Inspect: `modules/planning/.runtime/**`

1. Run targeted and broader regression suites and inspect the git diff for unrelated changes.
2. Commit the two fixes in narrow, reviewable commits while leaving the user's Find runtime config uncommitted.
3. Through the running web UI, rerun Read, then Idea and Plan for the current Find; monitor jobs through Plan completion.
4. Audit counts, full-text evidence, deep-read failures, idea quality, Plan audit status, taskbar ownership, and final artifacts.
