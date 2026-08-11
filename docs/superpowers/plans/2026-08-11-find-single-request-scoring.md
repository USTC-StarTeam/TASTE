# Find Single-Request Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce one LLM network request for each title batch of at most 100 papers and each final title-plus-abstract batch of at most 10 papers.

**Architecture:** Add an opt-in strict single-request mode to the shared Find LLM client, then use it only in the two scoring paths. Preserve current category-aware title batching and deterministic fallback/exclusion behavior while removing scoring-specific repair calls.

**Tech Stack:** Python 3.11, urllib, pytest, Pydantic configuration models.

## Global Constraints

- Title prefilter batches contain at most 100 papers and make exactly one LLM network request.
- Final title-plus-abstract batches contain at most 10 papers and make exactly one LLM network request.
- Missing or invalid title rows use local title fallback without another LLM call.
- Missing or invalid final rows are audit-marked and excluded without another LLM call.
- Source selection, category context, ranking formulas, caches, and recommendation gates remain unchanged.

---

### Task 1: Lock the strict request contract in tests

**Files:**
- Modify: `tests/test_taste_contracts.py:5639`
- Modify: `tests/test_taste_contracts.py:5790`

**Interfaces:**
- Consumes: `_prefilter_titles(...)` and `_evaluate_items(...)`.
- Produces: Regression tests asserting title call sizes `[100, 2]`, empty-title call count `1`, and final-scoring call sizes `[10]`.

- [x] **Step 1: Change the title recovery test to require one call per constructed batch**

Make the fake LLM omit three rows from the 100-item batch and assert that the only call sizes are `[100, 2]`, with three rows using local fallback.

- [x] **Step 2: Change the empty-title test to require one call**

Assert `llm.calls == 1` and keep the existing local-fallback assertions.

- [x] **Step 3: Change the final scoring mismatch test to prohibit single-item recovery**

Assert call sizes `[10]`, nine `llm abstract evaluation` rows, and one audit-marked unresolved row.

- [x] **Step 4: Run the three tests and verify RED**

Run: `conda run -n taste pytest -q tests/test_taste_contracts.py::test_find_title_prefilter_scores_each_batch_once_and_falls_back_missing_rows tests/test_taste_contracts.py::test_find_title_prefilter_falls_back_locally_without_retry tests/test_taste_contracts.py::test_find_abstract_scoring_scores_each_batch_once_and_marks_mismatched_id`

Expected: failures showing `[100, 3, 2, 1]`, five title calls, and `[10, 1]` under the current implementation.

### Task 2: Add strict single-request support to the LLM client

**Files:**
- Modify: `modules/finding/scripts/core/finding_runtime.py:1120`
- Test: `tests/test_taste_contracts.py`

**Interfaces:**
- Produces: `LLMClient.chat(..., single_request: bool = False)` and `LLMClient.json_or_error(..., single_request: bool = False)`.
- Strict mode performs one `urlopen`, one response extraction, and one JSON parse with no retry/fallback call.

- [x] **Step 1: Add a failing client-level network-attempt test**

Monkeypatch `urllib.request.urlopen` to raise a retryable error, configure `llm.retries > 1`, call `json_or_error(..., single_request=True)`, and assert the patched function was called once.

- [x] **Step 2: Run the client test and verify RED**

Expected: `TypeError` because `single_request` is not accepted yet.

- [x] **Step 3: Implement the optional strict mode**

Limit the transport loop and response-format attempt list to one entry in strict mode, and skip JSON parse retry in `json_or_error`.

- [x] **Step 4: Run the client test and verify GREEN**

Expected: one network attempt and a structured error result.

### Task 3: Enforce one request in title and final scoring

**Files:**
- Modify: `modules/finding/scripts/flow/pipeline.py:1666`
- Modify: `modules/finding/scripts/flow/pipeline.py:4520`
- Modify: `modules/finding/scripts/flow/pipeline.py:5525`

**Interfaces:**
- Produces: `_json_or_error_single_request(...)` compatibility wrapper.
- Title unresolved rows continue through existing `fallback_items` handling.
- Final unresolved rows continue through `mark_items_unscored(...)` and recommendation exclusion.

- [x] **Step 1: Add the compatibility wrapper**

Call `llm.json_or_error(..., single_request=True)` for the real client while retaining TypeError-compatible fake clients used by tests.

- [x] **Step 2: Replace title repair logic with one strict call**

Build one prompt per batch, parse once, return unresolved rows, and keep `request_count=1`. Do not call the daemon wall-timeout helper.

- [x] **Step 3: Remove final-scoring single-item retry queues**

Use one strict call per prompt. Mark failed and omitted rows immediately with their existing audit reasons.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run the four strict-contract tests and confirm all pass.

### Task 4: Regression verification

**Files:**
- Verify: `modules/finding/scripts/core/finding_runtime.py`
- Verify: `modules/finding/scripts/flow/pipeline.py`
- Verify: `tests/test_taste_contracts.py`

**Interfaces:**
- Consumes the completed strict request contract.
- Produces verified code with no unrelated behavior changes.

- [x] **Step 1: Run the complete Finding contract tests**

Run: `conda run -n taste pytest -q tests/test_taste_contracts.py`

- [x] **Step 2: Run the complete repository test suite**

Run: `conda run -n taste pytest -q`

- [x] **Step 3: Run static and diff checks**

Run: `python -m py_compile modules/finding/scripts/core/finding_runtime.py modules/finding/scripts/flow/pipeline.py` and `git diff --check`.

- [x] **Step 4: Confirm the cancelled job stays stopped**

Confirm the user-authorized old Find process tree remains absent and the web service remains healthy. A newly started Find job will load the patched Python code.
