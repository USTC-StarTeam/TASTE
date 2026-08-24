# Find Batched Repair and Progress Implementation Plan

**Goal:** Restore bounded batched LLM repair, score final candidates globally across sources, and make the web progress/count contract monotonic and truthful.

**Architecture:** Keep source-specific retrieval and detail enrichment, then flatten the globally selected candidates for one final-scoring call path. Parse score validity separately from recommendation-prose quality, normalize observed response aliases, and batch unresolved items for bounded repair. Emit one global scoring progress sequence and teach the web projection that this sequence is not source-local.

**Tech Stack:** Python 3.11, pytest, TypeScript/React progress consumer.

## Task 1: Lock the corrected contracts with failing tests

**Files:**
- Modify: `tests/test_taste_contracts.py`
- Modify: `tests/test_web_framework_bridge.py`

- [x] Add captured-response alias normalization and natural-English `find` regression tests.
- [x] Change title omission tests to require bounded batch repair and prohibit per-item repair.
- [x] Change final omission/error tests to require batches of at most 10 during repair.
- [x] Change the multi-source run test to require one global `_evaluate_items` call.
- [x] Add monotonic/global progress assertions and a global-scoring server projection test.
- [x] Add a framework summary test proving zero LLM-scored rows stay zero.
- [x] Run the focused tests and confirm they fail for the intended reasons.

## Task 2: Repair the response and scoring contracts

**Files:**
- Modify: `modules/finding/scripts/flow/pipeline.py`

- [x] Explicitly name canonical final-response keys and sentence requirements in the prompt.
- [x] Add deterministic aliases for the observed Chinese/English response fields.
- [x] Separate valid LLM scores from valid recommendation prose.
- [x] Make internal `Find` marker detection avoid normal `find`/`findings` false positives.
- [x] Preserve structured rejection and repair reasons.

## Task 3: Restore bounded batched repair

**Files:**
- Modify: `modules/finding/scripts/flow/pipeline.py`

- [x] Restore title repair rounds with unresolved items kept together in batches of at most 100.
- [x] Restore final repair rounds with unresolved items kept together in batches of at most 10.
- [x] Keep strict one-network-request behavior for every main or repair batch.
- [x] Keep valid rows from earlier rounds and only resend unresolved rows.

## Task 4: Globalize final scoring and progress

**Files:**
- Modify: `modules/finding/scripts/flow/pipeline.py`
- Modify: `web/backend/auto_research/web/server.py`
- Modify: `framework/scripts/orchestration/run_frontend.py`

- [x] Complete all source-specific detail work before final scoring.
- [x] Flatten selected candidates and invoke `_evaluate_items` once globally.
- [x] Preserve source identity and HuggingFace/GitHub result caps.
- [x] Emit global monotonic primary-plus-repair request progress and counts.
- [x] Project global scoring directly instead of dividing it by enabled sources.
- [x] Stop replacing a real zero LLM-scored count with evaluated-candidate count.

## Task 5: Verify behavior and review the diff

**Files:**
- Verify all modified files and generated progress artifacts.

- [x] Run focused scoring, multi-source, framework, and web projection tests.
- [x] Run the complete Finding contract tests.
- [x] Run the complete repository test suite.
- [x] Run Python compilation, frontend type/build checks, and `git diff --check`.
- [x] Review every changed hunk against the design and inspect git status/history before handoff.

## Task 6: Restore the configured Top-N recommendation result

**Files:**
- Modify: `modules/finding/scripts/flow/pipeline.py`
- Modify: `tests/test_taste_contracts.py`

- [x] Add a failing regression proving valid final-scored candidates fill `max(web value, selected sources × 5)` across multiple web values even when topic-evidence audit fields are weak.
- [x] Add a failing regression for `梯度无关优化` being falsely interpreted as an unrelated-topic verdict.
- [x] Restore topic-evidence fields to audit-only status while retaining real-abstract, valid-score, and reason-quality hard checks.
- [x] Restore calibrated final relevance bands and the ranking-only prompt contract.
- [x] Decouple stable title-score cache identity from final recommendation policy revisions.
- [x] Verify the previous 994-valid-score artifact deterministically produces 50 unique recommendations with no missing abstracts, invalid reasons, or unscored rows.
