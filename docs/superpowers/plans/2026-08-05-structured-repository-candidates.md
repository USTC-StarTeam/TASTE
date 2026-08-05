# Structured Repository Candidates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded OpenAIRE and HAL same-paper full-text candidates ahead of broad Web search while keeping Find and Read connected only through paper metadata.

**Architecture:** Find enriches ACM-family paper dictionaries with verified OpenAIRE repository URLs. A new Read-owned acquisition helper consumes those standard fields and independently performs strict HAL title/DOI/author lookup; the existing PDF downloader and extracted-text identity gate remain the final authority.

**Tech Stack:** Python 3.11, requests, BeautifulSoup, OpenAIRE Graph API, HAL Search API, pytest.

## Global Constraints

- Preserve all existing official conference, publisher, OpenReview, OpenAlex, Unpaywall, Semantic Scholar, arXiv, and generic search routes.
- OpenAIRE/HAL failures and 429 responses must return discovery misses immediately; they must not block a paper or erase partial Find results.
- Find and Read must not import each other's implementation modules.
- Every repository PDF remains subject to the existing PDF format, minimum-text, and same-paper text identity gates.
- Do not change concurrency/cooldown defaults or add per-conference scripts.
- Add tests only to `tests/test_taste_contracts.py`.

---

### Task 1: Add a Read-owned structured repository adapter

**Files:**
- Create: `modules/reading/scripts/acquisition/repository_sources.py`
- Test: `tests/test_taste_contracts.py`

**Interfaces:**
- Consumes: a normal paper dictionary and `core.common` helpers `normalized_paper_title`, `paper_title_similarity`, `paper_author_family_tokens`, `response_receipt`, and `service_get`.
- Produces: `structured_repository_pdf_candidates(paper: dict[str, Any], *, request_get: Callable = service_get) -> list[dict[str, Any]]` where accepted rows contain `kind`, `pdf_url`, `accepted=True`, and `requires_pdf_text_identity_check=True`; failures are receipt rows with `accepted=False`.

- [ ] **Step 1: Write failing adapter tests**

Add `_load_reading_repository_sources()` next to the existing Reading loader helpers. Test these observable cases with a local fake response object:

```python
candidates = repository_sources.structured_repository_pdf_candidates(
    {
        "title": "Exact Paper Title",
        "authors": ["Ada Lovelace", "Alan Turing"],
        "doi": "10.1145/123.456",
        "metadata": {"openaire_repository_urls": ["https://repo.example/item/1"]},
    },
    request_get=fake_get,
)
assert any(item.get("kind") == "openaire_repository_pdf" and item.get("pdf_url") == "https://repo.example/files/paper.pdf" for item in candidates)
assert any(item.get("kind") == "hal_exact_title_pdf" and item.get("pdf_url") == "https://hal.science/hal-1/file/paper.pdf" for item in candidates)
assert all(item.get("requires_pdf_text_identity_check") is True for item in candidates if item.get("accepted"))
```

The HAL fake must also return a same-title record with a conflicting DOI and verify that no accepted candidate is produced for it. A 429 fake response must produce a rejected discovery row without sleeping or retrying.

- [ ] **Step 2: Run adapter tests and verify RED**

Run:

```bash
conda run -n taste pytest -q tests/test_taste_contracts.py -k 'structured_repository or hal_exact_title_pdf or openaire_repository_pdf'
```

Expected: failure because `acquisition.repository_sources` and its public function do not exist.

- [ ] **Step 3: Implement conservative OpenAIRE landing-page parsing**

For each unique URL from `paper.metadata.openaire_repository_urls`, make one bounded request and inspect only:

```python
selectors = (
    'meta[name="citation_pdf_url"]',
    'meta[name="eprints.document_url"]',
    'a[href$=".pdf"]',
    'a[href*="/bitstream/"]',
    'a[href*="/bitstreams/"]',
)
```

Resolve relative links with `urljoin`, deduplicate them, and emit candidates requiring PDF text identity. A non-HTML response, non-2xx status, exception, or 429 emits one rejected receipt and returns control immediately.

- [ ] **Step 4: Implement strict HAL exact-title lookup**

Query `https://api.archives-ouvertes.fr/search/` once with the quoted title and request `title_s,doiId_s,authFullName_s,fileMain_s,uri_s`. Accept a record only when:

```python
title_ok = normalized_paper_title(record_title) == normalized_paper_title(paper["title"])
doi_ok = not paper_doi or not record_doi or paper_doi == record_doi
author_ok = not expected_authors or bool(expected_authors & record_authors)
accepted = title_ok and doi_ok and author_ok and bool(file_url)
```

A present conflicting DOI always rejects. If both sides provide authors, overlap is mandatory. Use `fileMain_s` directly; if absent, fetch only the exact matched `uri_s` page and apply the same declared-PDF selectors. Do not cache misses or perform a broad search.

- [ ] **Step 5: Run adapter tests and verify GREEN**

Run:

```bash
conda run -n taste pytest -q tests/test_taste_contracts.py -k 'structured_repository or hal_exact_title_pdf or openaire_repository_pdf'
```

Expected: all selected tests pass with no real network request.

- [ ] **Step 6: Commit the isolated Read adapter**

```bash
git add modules/reading/scripts/acquisition/repository_sources.py tests/test_taste_contracts.py
git commit -m "feat: add structured repository PDF candidates"
```

---

### Task 2: Add bounded OpenAIRE enrichment to Find ACM metadata

**Files:**
- Modify: `modules/finding/scripts/flow/support.py:1777-2005`
- Modify: `modules/finding/scripts/flow/support.py:3927-3975`
- Test: `tests/test_taste_contracts.py:7690-7745`

**Interfaces:**
- Consumes: ACM-family paper dictionaries already handled by `enrich_acm_doi_with_indexed_abstracts`.
- Produces: `enrich_acm_doi_with_openaire(papers: list[dict], limit: int = 0) -> tuple[list[dict], dict[str, Any]]`, adding only `paper.metadata.openaire_repository_urls` and provenance/statistics when identity is verified.

- [ ] **Step 1: Write failing Find enrichment tests**

Use a fake OpenAIRE Graph response containing one exact DOI/title publication and one same-title conflicting DOI publication:

```python
enriched, stats = find_support.enrich_acm_doi_with_openaire([paper])

assert enriched[0]["metadata"]["openaire_repository_urls"] == ["https://repository.example/record/1"]
assert stats["matched"] == 1
assert stats["requests"] == 1
```

Add a 429 case asserting `stats["rate_limited"] is True`, only one request is made, the input list remains present, and no sleep occurs. Update `test_find_acm_live_defaults_use_targeted_fallbacks_not_full_venue_scan` to monkeypatch this new enricher so that test remains offline and deterministic.

- [ ] **Step 2: Run Find enrichment tests and verify RED**

Run:

```bash
conda run -n taste pytest -q tests/test_taste_contracts.py -k 'find_acm and openaire'
```

Expected: failure because the new enricher is absent.

- [ ] **Step 3: Implement exact OpenAIRE identity and URL extraction**

Batch at most five exact titles per request to `https://api.openaire.eu/graph/v3/research-products`, request only `type=publication`, and make no retries inside this adapter. For an input ACM DOI, require that DOI in the candidate `pids` or `instances.alternateIdentifiers` and require exact normalized title equality. Only when the input has no DOI may exact title plus author-family overlap substitute for DOI.

Extract repository URLs only from publication instances, remove DOI resolver URLs, preserve stable order, and never replace an existing title or DOI. Store provenance under the paper's existing `metadata.indexed_enrichment` list.

- [ ] **Step 4: Wire the enricher into existing ACM indexed enrichment**

Call it once from `enrich_acm_doi_with_indexed_abstracts`, controlled by `ACM_OPENAIRE_FALLBACK` with default enabled. Add its returned statistics under `stats["openaire"]`. A failure must leave the same `papers` list flowing to HAL, OpenAlex, Semantic Scholar, ChatPaper, and current PDF routes.

- [ ] **Step 5: Run Find enrichment tests and verify GREEN**

Run:

```bash
conda run -n taste pytest -q tests/test_taste_contracts.py -k 'find_acm and (openaire or targeted_fallbacks)'
```

Expected: all selected tests pass without network access.

- [ ] **Step 6: Commit the Find enrichment**

```bash
git add modules/finding/scripts/flow/support.py tests/test_taste_contracts.py
git commit -m "feat: enrich ACM metadata with OpenAIRE repositories"
```

---

### Task 3: Integrate structured repository candidates before broad search

**Files:**
- Modify: `modules/reading/scripts/pipeline/read_pipeline.py:100-125`
- Modify: `modules/reading/scripts/pipeline/read_pipeline.py:2985-3360`
- Test: `tests/test_taste_contracts.py`

**Interfaces:**
- Consumes: `structured_repository_pdf_candidates(paper)` from Task 1.
- Produces: repository candidates in `_pdf_candidates_for_reading`, with all accepted candidates retaining `requires_pdf_text_identity_check=True`.

- [ ] **Step 1: Write a failing route-order and fallback test**

Monkeypatch official and structured candidate providers and assert that the structured route appears after the official candidate and before a broad-search candidate:

```python
kinds = [item["kind"] for item in read_pipeline._pdf_candidates_for_reading(paper)]
assert kinds.index("conference_official_pdf") < kinds.index("hal_exact_title_pdf")
assert kinds.index("hal_exact_title_pdf") < kinds.index("search_result_pdf_requires_text_identity")
```

Add an exception case where the structured provider raises and assert the existing broad-search candidate is still returned.

- [ ] **Step 2: Run route tests and verify RED**

Run:

```bash
conda run -n taste pytest -q tests/test_taste_contracts.py -k 'structured_repository_route_order or structured_repository_failure_falls_through'
```

Expected: failure because the Read pipeline does not call the provider.

- [ ] **Step 3: Add the guarded adapter import and candidate insertion**

Import the adapter beside `conference_sources`, with the same conservative import fallback pattern used by that module. In `_pdf_candidates_for_reading`, call it after official/publisher/OpenReview candidates and before the first `_search_result_pdf_candidates` call:

```python
try:
    repository_candidates = structured_repository_pdf_candidates(paper)
except Exception as exc:
    repository_candidates = [{
        "kind": "structured_repository_discovery",
        "accepted": False,
        "reason": "structured_repository_lookup_failed",
        "error": type(exc).__name__,
    }]
for candidate in repository_candidates:
    if candidate.get("accepted") and candidate.get("pdf_url"):
        add(
            str(candidate.get("kind") or "structured_repository_pdf"),
            candidate.get("pdf_url"),
            repository_match=candidate,
            requires_pdf_text_identity_check=True,
        )
    else:
        discovery.append(candidate)
```

Do not short-circuit later channels when no repository candidate is accepted.

- [ ] **Step 4: Run route tests and related Reading acquisition tests**

Run:

```bash
conda run -n taste pytest -q tests/test_taste_contracts.py -k 'structured_repository or pdf_candidates_for_reading or reading_full_text or conference_source'
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the integration**

```bash
git add modules/reading/scripts/pipeline/read_pipeline.py tests/test_taste_contracts.py
git commit -m "feat: use repository candidates before broad PDF search"
```

---

### Task 4: Verify both subprojects together

**Files:**
- Verify only; no planned production edit.

**Interfaces:**
- Consumes: completed cache and repository changes.
- Produces: fresh regression evidence.

- [ ] **Step 1: Run syntax and whitespace checks**

```bash
python -m py_compile modules/reading/scripts/acquisition/repository_sources.py modules/reading/scripts/pipeline/read_pipeline.py modules/finding/scripts/flow/support.py
git diff --check
```

- [ ] **Step 2: Run all centralized contracts**

```bash
conda run -n taste pytest -q tests/test_taste_contracts.py
```

- [ ] **Step 3: Run the remaining repository test suite**

```bash
conda run -n taste pytest -q
```

- [ ] **Step 4: Inspect scope and cache/channel boundaries**

```bash
git status --short
git diff --stat HEAD~3..HEAD
git diff HEAD~3..HEAD -- modules/reading/scripts/pipeline/read_pipeline.py modules/reading/scripts/acquisition/repository_sources.py modules/finding/scripts/flow/support.py tests/test_taste_contracts.py
```

Confirm there are no changes to prompts, Markdown format, scoring, ranking, taskbar, Web UI, concurrency defaults, or per-conference adapters.
