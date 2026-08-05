# Read Cache Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind reusable single-paper Read output to the current full text, source abstract, accepted fixed Chinese abstract, and Read quality policy without invalidating reusable full text.

**Architecture:** Keep the existing file-only fingerprint as the full-text cache identity. Add a separate Read fingerprint derived from that file identity plus the two accepted abstract inputs; publish and restore `read.md` only against the Read fingerprint and the exact quality policy version.

**Tech Stack:** Python 3.11, pathlib, hashlib, JSON manifests, pytest.

## Global Constraints

- Preserve Find's partial/limited-result semantics.
- A Read contract miss invalidates only Read/Claude artifacts; verified PDF and extracted full text remain reusable.
- Do not modify Claude prompts, Markdown output, scoring, ranking, taskbar, Web UI, or concurrency.
- Add tests only to the existing centralized `tests/test_taste_contracts.py`; do not create `modules/reading/tests`.

---

### Task 1: Make Read cache identity include accepted abstract inputs

**Files:**
- Modify: `modules/reading/scripts/pipeline/read_pipeline.py:4797-4820`
- Modify: `modules/reading/scripts/pipeline/read_pipeline.py:5041-5285`
- Test: `tests/test_taste_contracts.py:1528-1660`

**Interfaces:**
- Consumes: `_article_cache_content_fingerprints(cache_dir: Path) -> dict[str, str]` and `_article_quality_expectations(paper: dict | None) -> tuple[str, str, str]`.
- Produces: `_article_read_cache_fingerprints(cache_dir: Path, paper: dict) -> dict[str, str]` with `full_text_sha256`, `pdf_sha256`, `source_abstract_sha256`, `fixed_abstract_zh_sha256`, and the combined `content_revision`.

- [ ] **Step 1: Replace the legacy-backfill expectation with contract-miss tests**

Add focused tests showing that a cache published for one abstract cannot be restored with another abstract, while `_restore_article_full_text_cache` remains independently usable. The core assertion shape is:

```python
published = read_pipeline._article_read_cache_fingerprints(cache_dir, original_paper)
manifest.update({
    "read_content_revision": published["content_revision"],
    "source_abstract_sha256": published["source_abstract_sha256"],
    "fixed_abstract_zh_sha256": published["fixed_abstract_zh_sha256"],
    "read_quality_policy_version": read_pipeline.READING_CONTENT_QUALITY_POLICY_VERSION,
})

changed_paper = {**original_paper, "abstract": "A corrected source abstract."}
result = read_pipeline._restore_article_read_cache(
    item_dir, changed_paper, run_id=_reading_test_run_id(), paper_index=1
)

assert result == {}
assert not (cache_dir / "read.md").exists()
assert (cache_dir / "extracted" / "full_text.txt").is_file()
assert (cache_dir / "downloads" / "article.pdf").is_file()
assert json.loads((cache_dir / "manifest.json").read_text())["read_invalidation_reason"] == "read_input_fingerprint_mismatch"
```

Use parameterization for `abstract` and `abstract_zh` so each input is proven independently. Add separate cases for a missing fingerprint field and an old `read_quality_policy_version`; both must return `{}` and preserve the two full-text files.

- [ ] **Step 2: Run the new cache tests and verify RED**

Run:

```bash
conda run -n taste pytest -q tests/test_taste_contracts.py -k 'reading_article_cache and (abstract or policy or legacy)'
```

Expected: the new tests fail because the current restore path accepts/backfills legacy manifests and does not bind abstract hashes.

- [ ] **Step 3: Add the separate Read fingerprint helper**

Implement without changing `_article_cache_content_fingerprints`:

```python
def _text_sha256(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _article_read_cache_fingerprints(cache_dir: Path, paper: dict) -> dict[str, str]:
    file_fingerprints = _article_cache_content_fingerprints(cache_dir)
    _title, fixed_abstract_zh, source_abstract_en = _article_quality_expectations(paper)
    source_hash = _text_sha256(source_abstract_en)
    fixed_hash = _text_sha256(fixed_abstract_zh)
    revision_source = (
        f"full_text_sha256={file_fingerprints.get('full_text_sha256', '')}\n"
        f"pdf_sha256={file_fingerprints.get('pdf_sha256', '')}\n"
        f"source_abstract_sha256={source_hash}\n"
        f"fixed_abstract_zh_sha256={fixed_hash}\n"
    )
    return {
        "full_text_sha256": file_fingerprints.get("full_text_sha256", ""),
        "pdf_sha256": file_fingerprints.get("pdf_sha256", ""),
        "source_abstract_sha256": source_hash,
        "fixed_abstract_zh_sha256": fixed_hash,
        "content_revision": hashlib.sha256(revision_source.encode("ascii")).hexdigest(),
    }
```

- [ ] **Step 4: Publish and restore against the exact Read contract**

In `_publish_article_read_cache`, store the combined revision in `read_content_revision`, store both abstract hashes, and keep the file-only revision in `full_text_content_revision`.

In `_restore_article_read_cache`, before copying `read.md`, require all of:

```python
read_fingerprints = _article_read_cache_fingerprints(cache_dir, paper)
contract_ok = (
    manifest.get("read_quality_policy_version") == READING_CONTENT_QUALITY_POLICY_VERSION
    and manifest.get("read_content_revision") == read_fingerprints["content_revision"]
    and manifest.get("source_abstract_sha256") == read_fingerprints["source_abstract_sha256"]
    and manifest.get("fixed_abstract_zh_sha256") == read_fingerprints["fixed_abstract_zh_sha256"]
)
```

If false, call `_invalidate_article_read_artifacts(cache_dir)`, set `has_read_md` false and `read_invalidation_reason` to `read_quality_policy_mismatch` or `read_input_fingerprint_mismatch`, and return `{}`. Do not alter full-text paths or file fingerprints. Remove the old behavior that silently backfills a missing Read binding after accepting the cached Markdown.

- [ ] **Step 5: Run targeted tests and verify GREEN**

Run:

```bash
conda run -n taste pytest -q tests/test_taste_contracts.py -k 'reading_article_cache'
```

Expected: all selected tests pass, including existing full-text replacement and exact arXiv-version tests.

- [ ] **Step 6: Commit the cache-contract change**

```bash
git add modules/reading/scripts/pipeline/read_pipeline.py tests/test_taste_contracts.py
git commit -m "fix: bind read cache to current content contract"
```
