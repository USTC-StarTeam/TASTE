# Find Batched Repair and Progress Implementation Plan

**Goal:** Restore bounded batched LLM repair, score final candidates globally across sources, and make the web progress/count contract monotonic and truthful.

**Architecture:** Keep source-specific retrieval and detail enrichment, then flatten the globally selected candidates for one final-scoring call path. Parse score validity separately from recommendation-prose quality, normalize observed response aliases, and batch unresolved items for bounded repair. Emit one global scoring progress sequence and teach the web projection that this sequence is not source-local.

**Tech Stack:** Python 3.11, pytest, TypeScript/React progress consumer.

## Task 1: Lock the corrected contracts with failing tests

**Files:**
- Modify: `tests/test_taste_contracts.py`
- Modify: `tests/test_web_framework_bridge.py`

- [ ] Add captured-response alias normalization and natural-English `find` regression tests.
- [ ] Change title omission tests to require bounded batch repair and prohibit per-item repair.
- [ ] Change final omission/error tests to require batches of at most 10 during repair.
- [ ] Change the multi-source run test to require one global `_evaluate_items` call.
- [ ] Add monotonic/global progress assertions and a global-scoring server projection test.
- [ ] Add a framework summary test proving zero LLM-scored rows stay zero.
- [ ] Run the focused tests and confirm they fail for the intended reasons.

## Task 2: Repair the response and scoring contracts

**Files:**
- Modify: `modules/finding/scripts/flow/pipeline.py`

- [ ] Explicitly name canonical final-response keys and sentence requirements in the prompt.
- [ ] Add deterministic aliases for the observed Chinese/English response fields.
- [ ] Separate valid LLM scores from valid recommendation prose.
- [ ] Make internal `Find` marker detection avoid normal `find`/`findings` false positives.
- [ ] Preserve structured rejection and repair reasons.

## Task 3: Restore bounded batched repair

**Files:**
- Modify: `modules/finding/scripts/flow/pipeline.py`

- [ ] Restore title repair rounds with unresolved items kept together in batches of at most 100.
- [ ] Restore final repair rounds with unresolved items kept together in batches of at most 10.
- [ ] Keep strict one-network-request behavior for every main or repair batch.
- [ ] Keep valid rows from earlier rounds and only resend unresolved rows.

## Task 4: Globalize final scoring and progress

**Files:**
- Modify: `modules/finding/scripts/flow/pipeline.py`
- Modify: `web/backend/auto_research/web/server.py`
- Modify: `framework/scripts/orchestration/run_frontend.py`

- [ ] Complete all source-specific detail work before final scoring.
- [ ] Flatten selected candidates and invoke `_evaluate_items` once globally.
- [ ] Preserve source identity and HuggingFace/GitHub result caps.
- [ ] Emit global monotonic primary-plus-repair request progress and counts.
- [ ] Project global scoring directly instead of dividing it by enabled sources.
- [ ] Stop replacing a real zero LLM-scored count with evaluated-candidate count.

## Task 5: Verify behavior and review the diff

**Files:**
- Verify all modified files and generated progress artifacts.

- [ ] Run focused scoring, multi-source, framework, and web projection tests.
- [ ] Run the complete Finding contract tests.
- [ ] Run the complete repository test suite.
- [ ] Run Python compilation, frontend type/build checks, and `git diff --check`.
- [ ] Review every changed hunk against the design and inspect git status/history before handoff.

