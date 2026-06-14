#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths, load_project_config, management_python

from taste_pythonpath import ensure_taste_pythonpath
ensure_taste_pythonpath(ROOT)
from auto_research.source_selection import canonical_source_selection, paper_source_allowed, source_enabled


CLAIM_READY_POOLS = {"strong_recommendations", "articles"}
AUDIT_ONLY_POOLS = {"screened_ranking", "read_candidates", "triage_candidates", "audit_candidates"}
NON_CLAIM_POOLS = AUDIT_ONLY_POOLS | {"arxiv_prefiltered", "evaluated_candidates", "title_candidates", "critique_candidates"}


POOL_ORDER = {
    "strong_recommendations": 0,
    "articles": 1,
    "screened_ranking": 2,
    "read_candidates": 2,
    "triage_candidates": 3,
    "audit_candidates": 4,
    "arxiv_prefiltered": 5,
    "evaluated_candidates": 5,
    "title_candidates": 6,
    "critique_candidates": 7,
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_text(path: Path, limit: int = 12000) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except Exception:
        return ""


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def compact(text: Any, limit: int = 900) -> str:
    value = " ".join(str(text or "").replace("\r", "\n").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


def enabled_literature_source_count(selection: Any) -> int:
    if not isinstance(selection, dict):
        return 0
    count = len(selection.get("venue_ids") or []) if isinstance(selection.get("venue_ids"), list) else 0
    for key in [
        "include_arxiv",
        "include_biorxiv",
        "include_nature",
        "include_science",
        "include_huggingface",
        "include_github",
    ]:
        if selection.get(key):
            count += 1
    return count


def recommendation_target_from_progress(progress: Any, selection: Any, actual: int) -> dict[str, Any]:
    data = progress if isinstance(progress, dict) else {}
    source_count = enabled_literature_source_count(selection)
    target = safe_int(data.get("recommendation_target_count"), 0) or (source_count * 5 if source_count else 0)
    progress_actual = safe_int(data.get("strong_recommendation_count"), 0)
    actual = max(actual, progress_actual)
    # The Find stage now defines recommendations as the LLM title+abstract
    # ranked article pool. Strict evidence anchors are a downstream audit subset,
    # so they must not recreate the old artificial recommendation shortfall.
    reported_shortfall = safe_int(data.get("recommendation_shortfall"), 0)
    shortfall = reported_shortfall if target and actual <= 0 else 0
    status = "shortfall" if shortfall > 0 else "pass" if actual > 0 or target else "unknown"
    return {
        "actual": actual,
        "target": target,
        "shortfall": shortfall,
        "source_count": source_count,
        "status": status,
    }


def normalize_key(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def numeric(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def yeof(row: dict[str, Any]) -> int | None:
    for key in ["year", "published", "updated", "published_at"]:
        raw = str(row.get(key) or "")
        match = re.search(r"(20\d{2}|19\d{2})", raw)
        if match:
            return int(match.group(1))
    return None


def display_score_of(row: dict[str, Any]) -> float:
    """Human-facing paper score on the Find 1-10 scale."""
    candidates = [
        numeric(row.get("recommendation_score")),
        numeric(row.get("score")),
        numeric(row.get("llm_fit_score")),
        numeric(row.get("fit_score")),
    ]
    value = max(candidates)
    if value > 10.0:
        fallback = max(numeric(row.get("llm_fit_score")), numeric(row.get("fit_score")))
        value = fallback if fallback > 0 else 10.0
    return round(max(0.0, min(10.0, value)), 3)


def score_of(row: dict[str, Any], pool: str, rank: int) -> float:
    """Internal packet priority. This is not a human-facing Fit score."""
    base = {
        "strong_recommendations": 100.0,
        "articles": 100.0,
        "screened_ranking": 82.0,
        "read_candidates": 100.0,
        "triage_candidates": 78.0,
        "audit_candidates": 78.0,
        "arxiv_prefiltered": 72.0,
        "evaluated_candidates": 55.0,
        "title_candidates": 35.0,
        "critique_candidates": 20.0,
    }.get(pool, 0.0)
    raw = max(
        numeric(row.get("recommendation_score")),
        numeric(row.get("score")),
        numeric(row.get("llm_fit_score")),
        numeric(row.get("fit_score")),
        numeric(row.get("discovery_priority_score")),
        numeric(row.get("idea_worthiness_score")),
    )
    recency = 0.0
    year = yeof(row)
    if year:
        current = dt.datetime.now(dt.timezone.utc).year
        recency = max(0.0, 8.0 - max(0, current - year) * 1.5)
    return base + min(raw, 20.0) + recency - rank * 0.03


def extract_links(row: dict[str, Any]) -> list[str]:
    text_parts = [
        str(row.get("url") or ""),
        str(row.get("pdf_url") or ""),
        str(row.get("abs_url") or ""),
        str(row.get("abstract") or ""),
        str(row.get("summary") or ""),
        str(row.get("reason") or ""),
        str(row.get("tldr") or ""),
    ]
    links: list[str] = []
    for match in re.finditer(r"https?://[^\s\]\)\}\>,;]+", "\n".join(text_parts)):
        link = match.group(0).rstrip(".")
        if link not in links:
            links.append(link)
    return links[:8]


def code_links(row: dict[str, Any]) -> list[str]:
    links = extract_links(row)
    out = []
    for link in links:
        lowered = link.lower()
        if any(token in lowered for token in ["github.com", "gitlab", "4open.science", "anonymous.4open", "code", "repo"]):
            out.append(link)
    return out[:5]


def candidate_key(row: dict[str, Any]) -> str:
    title = normalize_key(str(row.get("title") or ""))
    url = normalize_key(str(row.get("url") or row.get("abs_url") or row.get("pdf_url") or ""))
    return title or url or normalize_key(str(row.get("id") or row.get("paper_id") or ""))


def is_non_positive_candidate(row: dict[str, Any]) -> bool:
    if row.get("not_positive_support") or row.get("weak_candidate_for_critique") or row.get("foundation_demoted_from_strong"):
        return True
    if row.get("retrieval_pool_only") or row.get("audit_only_candidate") is True and not row.get("claim_ready_anchor"):
        return True
    tier = str(row.get("evidence_tier") or "").lower()
    role = str(row.get("evidence_role") or "").lower()
    pool_role = str(row.get("taste_pool_role") or row.get("use_in_workflow") or "").lower()
    if tier == "strong_recommendation":
        return False
    if any(token in tier for token in ["nethreshold", "retrieval_only", "weak", "boundary", "critique"]):
        return True
    if role in {"weak_or_boundary", "retrieval_pool_only"}:
        return True
    if any(token in pool_role for token in ["retrieval", "audit-only", "critique", "boundary"]):
        return True
    return False


def compact_score(value: Any) -> float | str:
    if value is None or value == "":
        return ""
    score = numeric(value, -1.0)
    if score < 0:
        return ""
    return round(max(0.0, min(10.0, score)), 3)


def candidate_from_row(row: dict[str, Any], pool: str, rank: int) -> dict[str, Any] | None:
    title = str(row.get("title") or "").strip()
    if not title:
        return None
    links = extract_links(row)
    codes = code_links(row)
    pool_use = {
        "strong_recommendations": "claim-ready strong paper anchor to inspect first",
        "articles": "strong paper anchor to inspect first",
        "screened_ranking": "audit-only strict ranking candidate; not positive evidence unless also in articles/strong_recommendations",
        "read_candidates": "deep-reading input; mirrors the user-facing recommended-paper pool",
        "triage_candidates": "triage/audit candidate; not positive evidence unless also in articles/strong_recommendations",
        "audit_candidates": "audit candidate; not positive evidence unless also in articles/strong_recommendations",
        "arxiv_prefiltered": "recent prefiltered paper signal; verify before anchoring",
        "evaluated_candidates": "inspected candidate; use for boundary mapping or search expansion unless promoted",
        "title_candidates": "title-level candidate; do not treat as positive support before reading",
        "critique_candidates": "critique/boundary candidate; useful for negative examples and novelty pressure",
    }.get(pool, "literature signal")
    abstract = compact(row.get("abstract") or row.get("abstract_en") or row.get("summary") or "", 1400)
    abstract_zh = compact(row.get("abstract_zh") or "", 1400)
    abstract_lc = str(abstract or "").strip().lower()
    has_real_abstract = bool(len(abstract_lc) >= 80 and abstract_lc not in {
        "no abstract available", "no abstract available.", "abstract not available", "abstract not available."
    })
    if pool in CLAIM_READY_POOLS and not has_real_abstract:
        raise ValueError(
            f"Find invariant violation: {pool} row lacks a real abstract before LLM scoring: {title}"
        )
    non_positive = is_non_positive_candidate(row)
    claim_ready = pool in CLAIM_READY_POOLS and not non_positive
    audit_only = pool in NON_CLAIM_POOLS or non_positive
    not_positive = (not claim_ready) or audit_only or non_positive
    priority_score = round(score_of(row, pool, rank) - (60.0 if not_positive else 0.0), 3)
    return {
        "title": title,
        "id": row.get("id") or row.get("paper_id") or row.get("entry_id") or "",
        "authors": row.get("authors") if isinstance(row.get("authors"), str) else ", ".join(row.get("authors", [])[:8]) if isinstance(row.get("authors"), list) else "",
        "venue": row.get("venue") or row.get("track") or row.get("source") or "",
        "year": yeof(row),
        "source": row.get("source") or "",
        "category": row.get("category") or row.get("primary_area") or "",
        "url": row.get("url") or row.get("abs_url") or "",
        "pdf_url": row.get("pdf_url") or "",
        "code_links": codes,
        "has_code_signal": bool(codes) or "code" in str(row.get("abstract") or row.get("summary") or row.get("reason") or "").lower(),
        "pool": pool,
        "pool_rank": rank,
        "use_in_workflow": pool_use,
        "claim_ready_anchor": claim_ready,
        "positive_claim_evidence": claim_ready,
        "audit_only_candidate": audit_only,
        "not_positive_support": not_positive,
        "weak_candidate_for_critique": bool(row.get("weak_candidate_for_critique")) or not_positive,
        "foundation_demoted_from_strong": bool(row.get("foundation_demoted_from_strong")),
        "retrieval_pool_only": bool(row.get("retrieval_pool_only")),
        "evidence_tier": row.get("evidence_tier") or "",
        "evidence_role": row.get("evidence_role") or "",
        "source_evidence_tier": row.get("evidence_tier") or "",
        "score": display_score_of(row),
        "packet_priority_score": priority_score,
        "fit_score": compact_score(row.get("llm_fit_score") or row.get("fit_score")),
        "diversity_score": compact_score(row.get("llm_diversity_score") or row.get("diversity_score")),
        "recommendation_score": compact_score(row.get("recommendation_score") or row.get("score")),
        "stable_source_score": compact_score(row.get("stable_source_score")),
        "stable_rank_score": compact_score(row.get("stable_rank_score")),
        "topic_evidence": row.get("topic_evidence") or "",
        "topic_evidence_supported": row.get("topic_evidence_supported"),
        "matched_topic_route": row.get("matched_topic_route") or "",
        "hit_directions": row.get("hit_directions") if isinstance(row.get("hit_directions"), list) else [],
        "hit_directions_zh": row.get("hit_directions_zh") if isinstance(row.get("hit_directions_zh"), list) else [],
        "hit_directions_en": row.get("hit_directions_en") if isinstance(row.get("hit_directions_en"), list) else [],
        "reason": compact(row.get("reason") or row.get("reason_zh") or row.get("reason_en") or row.get("fit_explanation") or row.get("recommendation_note") or row.get("tldr") or "", 700),
        "reason_zh": compact(row.get("reason_zh") or "", 700),
        "reason_en": compact(row.get("reason_en") or "", 700),
        "fit_explanation": compact(row.get("fit_explanation") or row.get("fit_explanation_zh") or row.get("fit_explanation_en") or "", 900),
        "fit_explanation_zh": compact(row.get("fit_explanation_zh") or "", 900),
        "fit_explanation_en": compact(row.get("fit_explanation_en") or "", 900),
        "recommendation_note": compact(row.get("recommendation_note") or "", 700),
        "recommendation_note_zh": compact(row.get("recommendation_note_zh") or "", 700),
        "recommendation_note_en": compact(row.get("recommendation_note_en") or "", 700),
        "reader_instruction": compact(row.get("reader_instruction_zh") or row.get("reader_instruction") or row.get("reader_instruction_en") or "", 900),
        "reader_instruction_zh": compact(row.get("reader_instruction_zh") or "", 900),
        "reader_instruction_en": compact(row.get("reader_instruction_en") or "", 900),
        "abstract": abstract,
        "abstract_en": abstract,
        "abstract_zh": abstract_zh,
        "abstract_excerpt": abstract,
        "links": links,
    }


def extract_candidates(find_results: dict[str, Any], selection: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for pool in POOL_ORDER:
        for rank, row in enumerate(as_list(find_results.get(pool)), 1):
            if not isinstance(row, dict):
                continue
            if selection is not None and not paper_source_allowed(row, selection):
                continue
            item = candidate_from_row(row, pool, rank)
            if not item:
                continue
            key = candidate_key(row)
            old = by_key.get(key)
            if old is None:
                item["seen_pools"] = [pool]
                by_key[key] = item
                continue
            old.setdefault("seen_pools", [])
            if pool not in old["seen_pools"]:
                old["seen_pools"].append(pool)
            item_priority = numeric(item.get("packet_priority_score"), numeric(item.get("score")))
            old_priority = numeric(old.get("packet_priority_score"), numeric(old.get("score")))
            if (POOL_ORDER.get(pool, 99), -item_priority, rank) < (POOL_ORDER.get(str(old.get("pool")), 99), -old_priority, int(old.get("pool_rank") or 9999)):
                item["seen_pools"] = old["seen_pools"]
                by_key[key] = item
    return sorted(by_key.values(), key=lambda item: (-numeric(item.get("packet_priority_score"), numeric(item.get("score"))), POOL_ORDER.get(str(item.get("pool")), 99), str(item.get("title", "")).lower()))


def claim_ready_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in candidates if row.get("claim_ready_anchor") is True and not row.get("not_positive_support")]


def audit_only_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in candidates if row.get("audit_only_candidate") and not row.get("claim_ready_anchor")]


def summarize_repo_candidates(paths, selection: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if selection is not None and not source_enabled(selection, "github"):
        return []
    rows = load_json(paths.state / "repo_candidates.json", [])
    out: list[dict[str, Any]] = []
    for row in as_list(rows)[:12]:
        if not isinstance(row, dict):
            continue
        out.append({
            "name": row.get("name") or row.get("url") or "",
            "url": row.get("url") or "",
            "score": row.get("score", row.get("repo_reuse_score", "")),
            "bucket": row.get("repo_selection_bucket") or "",
            "why_use": compact(row.get("notes") or row.get("summary") or row.get("description") or "", 500),
            "guardrail": "Clone or run only after TASTE repo/data/env audit; a repo URL is not experiment evidence.",
        })
    return out


def load_artifact_file_map(paths, taste_dir: Path) -> list[dict[str, Any]]:
    rows = [
        ("planning/literature_tool_packet.md", paths.planning / "literature_tool_packet.md", "human/Claude-readable summary and commands", "Read this first before idea/base/experiment decisions."),
        ("state/literature_tool_packet.json", paths.state / "literature_tool_packet.json", "machine-readable TASTE literature tool packet", "Use for scripted checks and compact context."),
        ("planning/finding/find_results.json", taste_dir / "find_results.json", "complete TASTE discovery output", "Inspect when choosing nearest work, paper anchors, or search gaps."),
        ("planning/finding/article.md", taste_dir / "article.md", "recommended-paper list", "Read before selecting a base paper or claim novelty."),
        ("planning/finding/read_results.json", taste_dir / "read_results.json", "TASTE reading-stage structured output", "Use to decide what papers need deeper reading."),
        ("planning/finding/read.md", taste_dir / "read.md", "reading notes", "Use for human-readable paper comparisons."),
        ("planning/finding/ideas.json", taste_dir / "ideas.json", "TASTE idea-stage output", "Treat as idea seeds only; The workflow must verify via repo/data experiments."),
        ("planning/finding/idea.md", taste_dir / "idea.md", "idea notes", "Use as brainstorming input, not as evidence."),
        ("planning/finding/plans.json", taste_dir / "plans.json", "TASTE plan-stage output", "Reconcile with TASTE gates before execution."),
        ("planning/finding/plan.md", taste_dir / "plan.md", "plan notes", "Use to seed experiment plans after evidence checks."),
        ("planning/finding/category_scan_report.json", taste_dir / "category_scan_report.json", "venue/category scan coverage", "Verify whether a conference scan was broad enough."),
        ("planning/finding/title_filter_report.json", taste_dir / "title_filter_report.json", "title-filter decisions", "Inspect when candidate count seems too low."),
        ("planning/finding/arxiv_raw.json", taste_dir / "arxiv_raw.json", "raw arXiv results", "Use when recent arXiv coverage is questioned."),
        ("planning/finding/arxiv_prefiltered.json", taste_dir / "arxiv_prefiltered.json", "prefiltered arXiv results", "Use for targeted follow-up queries."),
        ("state/taste_literature_intermediates.json", paths.state / "taste_literature_intermediates.json", "synced TASTE intermediate index", "Use for candidate counts and raw artifact paths."),
        ("state/taste_sync.json", paths.state / "taste_sync.json", "sync result into TASTE discovery/ideas/repos", "Check what was imported into research state."),
        ("state/taste_local_database_update.json", paths.state / "taste_local_database_update.json", "local venue database refresh state", "Use to verify local conference caches before claiming full scan."),
    ]
    return [
        {
            "label": label,
            "path": str(path),
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else 0,
            "description": description,
            "how_to_use": how_to_use,
        }
        for label, path, description, how_to_use in rows
    ]


def source_filtered_rows(rows: Any, selection: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in as_list(rows):
        if not isinstance(row, dict):
            continue
        if selection is not None and not paper_source_allowed(row, selection):
            continue
        out.append(row)
    return out


def coverage_from(find_results: dict[str, Any], taste_state: dict[str, Any], intermediates: dict[str, Any], selection: dict[str, Any] | None = None) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    current_stats = find_results.get("survey_stats")
    diagnostics = find_results.get("diagnostics") if isinstance(find_results.get("diagnostics"), dict) else {}
    if not isinstance(current_stats, dict):
        current_stats = diagnostics.get("survey_stats") if isinstance(diagnostics.get("survey_stats"), dict) else {}
    if isinstance(current_stats, dict):
        stats.update({k: v for k, v in current_stats.items() if v not in ("", None)})
    for fallback in [taste_state.get("survey_stats"), intermediates.get("survey_stats")]:
        if not isinstance(fallback, dict):
            continue
        for key, value in fallback.items():
            if value in ("", None, 0, False):
                continue
            if stats.get(key) in ("", None, 0, False):
                stats[key] = value
    category_rows = as_list(find_results.get("category_scan_report"))
    title_rows = as_list(find_results.get("title_filter_report"))
    if category_rows:
        stats.setdefault("category_corpus_audited_papers", sum(int(row.get("corpus_audit_papers") or row.get("total_papers") or 0) for row in category_rows if isinstance(row, dict)))
        stats.setdefault("venue_category_selected_papers", sum(int(row.get("selected_category_papers") or 0) for row in category_rows if isinstance(row, dict)))
    if title_rows:
        stats.setdefault("venue_title_filter_input_papers", sum(int(row.get("title_filter_input_papers") or 0) for row in title_rows if isinstance(row, dict)))
        stats.setdefault("venue_final_title_candidates", sum(int(row.get("final_title_candidates") or 0) for row in title_rows if isinstance(row, dict)))
    stats["candidate_pool_counts"] = {
        pool: len(source_filtered_rows(find_results.get(pool), selection))
        for pool in POOL_ORDER
    }
    progress_counts = find_results.get("progress_counts") if isinstance(find_results.get("progress_counts"), dict) else {}
    raw_title_count = len(source_filtered_rows(find_results.get("raw_title_index"), selection)) or safe_int(progress_counts.get("raw_title_index"), 0)
    venue_health_rows = [row for row in as_list(find_results.get("venue_health_report")) if isinstance(row, dict)]
    if not raw_title_count:
        raw_title_count = sum(safe_int(row.get("corpus_count") or row.get("sample_count") or row.get("raw_title_index_count"), 0) for row in venue_health_rows)
    if not raw_title_count:
        raw_title_count = sum(safe_int(row.get("raw_title_index_count"), 0) for row in as_list(find_results.get("source_status")) if isinstance(row, dict))
    title_candidate_count = len(source_filtered_rows(find_results.get("title_candidates"), selection)) or safe_int(progress_counts.get("title_candidates"), 0)
    evaluated_count = len(source_filtered_rows(find_results.get("evaluated_candidates"), selection)) or safe_int(progress_counts.get("evaluated_candidates"), 0)
    if raw_title_count:
        stats["raw_title_index"] = raw_title_count
        stats["raw_title_index_papers"] = raw_title_count
        stats["venue_total_papers_available"] = raw_title_count
        stats["venue_corpus_audited_papers"] = raw_title_count
    if title_candidate_count:
        stats["venue_final_title_candidates"] = title_candidate_count
    if evaluated_count:
        stats["venue_detail_fetched_candidates"] = evaluated_count
        stats["venue_evaluated_candidates"] = evaluated_count
        stats["llm_scored_candidates"] = max(safe_int(stats.get("llm_scored_candidates"), 0), evaluated_count)
    if selection is not None and not source_enabled(selection, "arxiv"):
        for key in ["arxiv_raw_count", "arxiv_prefiltered_count", "arxiv_pages_fetched", "arxiv_deduped_count"]:
            stats[key] = 0
        stats["arxiv_full_scan"] = False
    source_rows = []
    for row in as_list(find_results.get("source_status")):
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or row.get("venue") or "")
        if selection is not None and source.lower() in {"arxiv", "huggingface", "hf", "github"} and not source_enabled(selection, source):
            continue
        source_rows.append(row)
    stats["source_limitations"] = [
        {
            "source": row.get("source") or row.get("venue") or "",
            "status": "limited" if row.get("limited") else "failed" if not row.get("ok", True) else "ok",
            "count": row.get("count") or row.get("sample_count") or 0,
            "message": compact(row.get("message") or row.get("error") or "", 280),
        }
        for row in source_rows
        if row.get("limited") or not row.get("ok", True)
    ][:12]
    stats["missing_venue_indexes"] = [
        {
            "venue": row.get("venue") or row.get("venue_id") or "",
            "years": row.get("years") or [],
            "reason": compact(row.get("error") or row.get("suggested_fix") or "No usable title index.", 220),
        }
        for row in venue_health_rows
        if not row.get("ok", False)
    ][:12]
    return stats


def current_blockers(paths) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path, key in [
        (paths.state / "blocker_action_plan.json", "actions"),
        (paths.state / "scientific_progress_gate.json", "blockers"),
        (paths.state / "reference_reproduction_gate.json", "blockers"),
    ]:
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            continue
        values = payload.get(key, [])
        if isinstance(values, list):
            for row in values[:8]:
                if isinstance(row, dict):
                    text = row.get("issue") or row.get("description") or row.get("action") or row.get("id") or ""
                else:
                    text = str(row)
                if text:
                    out.append({"source": str(path), "text": compact(text, 700)})
    return out[:12]


def suggested_queries(cfg: dict[str, Any], candidates: list[dict[str, Any]], blockers: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for row in cfg.get("queries", []) if isinstance(cfg.get("queries", []), list) else []:
        if isinstance(row, str) and row.strip():
            values.append(row.strip())
    categories = Counter(str(row.get("category") or "").strip() for row in candidates[:80] if str(row.get("category") or "").strip())
    for category, _ in categories.most_common(8):
        values.append(category)
    for row in candidates[:8]:
        title = str(row.get("title") or "").strip()
        if title:
            values.append(title)
            if row.get("has_code_signal"):
                values.append(title + " code")
    for row in blockers[:4]:
        text = str(row.get("text") or "")
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{3,}", text)
        if tokens:
            values.append(" ".join(tokens[:8]))
    out: list[str] = []
    seen = set()
    for value in values:
        value = compact(value, 180)
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
        if len(out) >= 20:
            break
    return out


def render_markdown(packet: dict[str, Any]) -> str:
    lines = ["# TASTE Literature Tool Packet\n\n"]
    summary = packet.get("summary", {})
    coverage = packet.get("coverage", {})
    lines.append(f"- status: {packet.get('status')}\n")
    lines.append(f"- generated_at: {packet.get('generated_at')}\n")
    lines.append(f"- strong_paper_anchors: {summary.get('strong_paper_anchors', 0)}\n")
    lines.append(f"- recommendation_target: {summary.get('recommendation_target_count', 0)}\n")
    lines.append(f"- recommendation_shortfall: {summary.get('recommendation_shortfall', 0)}\n")
    lines.append(f"- inspected_candidates: {summary.get('inspected_candidates', 0)}\n")
    lines.append(f"- base_work_candidates: {summary.get('base_work_candidates', 0)}\n")
    lines.append(f"- candidate_pool_counts: {json.dumps(coverage.get('candidate_pool_counts', {}), ensure_ascii=False)}\n")
    lines.append("\n## How Claude Code Should Use This\n")
    for item in packet.get("workflow", []):
        lines.append(f"- {item}\n")
    lines.append("\n## Commands\n")
    for item in packet.get("commands", []):
        lines.append(f"- `{item}`\n")
    lines.append("\n## Strong Papers To Inspect First\n")
    for row in packet.get("strong_papers", [])[:12]:
        lines.append(f"- {row.get('year') or ''} {row.get('venue') or ''}: {row.get('title')} | use: {row.get('use_in_workflow')}\n")
    if not packet.get("strong_papers"):
        lines.append("- none\n")
    lines.append("\n## Base Or Code Candidates\n")
    for row in packet.get("base_work_candidates", [])[:10]:
        code = "; ".join(row.get("code_links", [])[:2]) if isinstance(row.get("code_links"), list) else ""
        lines.append(f"- {row.get('title')} | code signal: {code or 'not explicit'}\n")
    if not packet.get("base_work_candidates"):
        lines.append("- none\n")
    lines.append("\n## Critique And Boundary Candidates\n")
    for row in packet.get("critique_candidates", [])[:8]:
        lines.append(f"- {row.get('title')} | use: {row.get('use_in_workflow')}\n")
    if not packet.get("critique_candidates"):
        lines.append("- none\n")
    lines.append("\n## Intermediate Files\n")
    for row in packet.get("intermediate_files", []):
        mark = "yes" if row.get("exists") else "no"
        lines.append(f"- {mark}: {row.get('label')} -> {row.get('path')} | {row.get('how_to_use')}\n")
    lines.append("\n## Guardrails\n")
    for item in packet.get("guardrails", []):
        lines.append(f"- {item}\n")
    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a compact Claude-callable packet from TASTE's literature survey artifacts.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    args = parser.parse_args()

    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    taste_state = load_json(paths.state / "finding_frontend.json", {})
    taste_sync = load_json(paths.state / "taste_sync.json", {})
    intermediates = load_json(paths.state / "taste_literature_intermediates.json", {})
    taste_dir = Path(taste_state.get("output_dir") or paths.planning / "finding") if isinstance(taste_state, dict) else paths.planning / "finding"
    find_progress = load_json(taste_dir / "find_progress.json", {})
    find_results = load_json(taste_dir / "find_results.json", {})
    if not isinstance(find_results, dict):
        find_results = {}
    if isinstance(find_progress, dict) and isinstance(find_progress.get("counts"), dict):
        find_results["progress_counts"] = find_progress.get("counts") or {}
    source_selection = canonical_source_selection(project_config_path=paths.config)
    candidates = extract_candidates(find_results, source_selection)
    strong = claim_ready_candidates(candidates)
    audit_only = audit_only_candidates(candidates)
    critique = [row for row in candidates if row.get("pool") in NON_CLAIM_POOLS]
    base_work = [row for row in strong if row.get("claim_ready_anchor") and not row.get("not_positive_support")]
    blockers = current_blockers(paths)
    target_gate = recommendation_target_from_progress(find_progress, source_selection, len(strong))
    if target_gate.get("shortfall"):
        status = "needs_targeted_survey"
        blockers.insert(0, {
            "source": str(taste_dir / "find_progress.json"),
            "text": f"Find ranked recommendations are unavailable or below the explicit Find target: {target_gate.get('actual')}/{target_gate.get('target')}; shortfall={target_gate.get('shortfall')}. Repair retrieval/detail/LLM scoring rather than padding unscored papers.",
        })
    else:
        status = "ready" if target_gate.get("actual") or strong else "needs_targeted_survey" if candidates else "missing_or_empty"
    filtered_pool_counts = {
        pool: len(source_filtered_rows(find_results.get(pool), source_selection))
        for pool in POOL_ORDER
    }
    pool_counts = {
        "strong_papers": len(strong),
        "claim_ready_strong_papers": len(strong),
        "audit_only_candidates": len(audit_only),
        "base_work_candidates": len(base_work),
        "critique_candidates": len(critique),
        "all_traceable_candidates": len(candidates),
        "strong_recommendations": filtered_pool_counts.get("strong_recommendations", 0),
        "articles": filtered_pool_counts.get("articles", 0),
        "screened_ranking": filtered_pool_counts.get("screened_ranking", 0),
        "read_candidates": filtered_pool_counts.get("read_candidates", 0),
        "triage_candidates": filtered_pool_counts.get("triage_candidates", 0),
        "audit_candidates": filtered_pool_counts.get("audit_candidates", 0),
        "evaluated_candidates": filtered_pool_counts.get("evaluated_candidates", 0),
        "title_candidates": filtered_pool_counts.get("title_candidates", 0),
        "arxiv_prefiltered": filtered_pool_counts.get("arxiv_prefiltered", 0),
    }
    run_id = str(find_results.get("run_id") or taste_state.get("taste_run_id") or taste_state.get("run_id") or "")
    packet = {
        "generated_at": now_iso(),
        "project": args.project,
        "venue": args.venue,
        "run_id": run_id,
        "source_run_id": run_id,
        "status": status,
        "summary": {
            "strong_paper_anchors": len(strong),
            "recommendation_target_count": target_gate.get("target", 0),
            "recommendation_shortfall": target_gate.get("shortfall", 0),
            "recommendation_source_count": target_gate.get("source_count", 0),
            "recommendation_gate_status": target_gate.get("status", ""),
            "inspected_candidates": len(candidates),
            "base_work_candidates": len(base_work),
            "critique_candidates": len(critique),
            "repos_synced": len(summarize_repo_candidates(paths, source_selection)),
            "taste_sync_status": taste_sync.get("status") if isinstance(taste_sync, dict) else "",
            "taste_stage": taste_state.get("stage") or taste_state.get("status") if isinstance(taste_state, dict) else "",
        },
        "coverage": coverage_from(find_results, taste_state if isinstance(taste_state, dict) else {}, intermediates if isinstance(intermediates, dict) else {}, source_selection),
        "candidate_layer_summary": {
            "human_summary_zh": (
                f"Find 已按 LLM 标题+摘要评分生成推荐论文 {target_gate.get('actual', 0)} 篇。"
                f"其中 {len(base_work)} 篇同时通过严格基底/claim 证据审计，可优先用于代码、数据、环境和复现实验路线评估。"
                f"本轮还保留 {len(candidates)} 个可追踪候选、{len(critique)} 个边界/反例候选，用于后续审计和扩展调研。"
            ),
            "human_summary_en": (
                f"Find produced {target_gate.get('actual', 0)} recommended papers from LLM title+abstract scoring. "
                f"{len(base_work)} of them also passed the stricter base/claim evidence audit for code, data, environment, and reproduction-route assessment. "
                f"The run also preserves {len(candidates)} traceable candidates and {len(critique)} critique/boundary candidates for audit and expansion."
            ),
            "recommendation_target": target_gate,
            "pool_counts": {**pool_counts, "recommendation_target_count": target_gate.get("target", 0), "recommendation_shortfall": target_gate.get("shortfall", 0)},
        },
        "workflow": [
            "If ranked recommendations are missing, repair literature discovery/detail fetching/LLM scoring; do not pad unscored papers.",
            "Before proposing or coding a new research direction, inspect this packet and at least one raw TASTE artifact that supports the decision.",
            "Use articles/strong_recommendations as the user-facing recommended-paper pool ranked by LLM title+abstract scoring.",
            "Use strict base/claim anchors inside that pool for nearest-work, baseline, dataset, protocol, and transformable-code route decisions; use screened_ranking/triage_candidates/audit_candidates and critique candidates for audit and expansion.",
            "If the packet is stale, empty, or too generic for the current blocker, run the literature tool with a targeted query and rebuild this packet.",
            "Feed selected paper/repo signals into TASTE base-work selection, environment reproduction, and experiment planning; do not use them as experiment-result evidence.",
        ],
        "commands": [
            f"{management_python()} modules/finding/scripts/build_literature_tool_packet.py --project {args.project}" + (f" --venue {args.venue}" if args.venue else ""),
            f"{management_python()} modules/finding/scripts/run_literature_tool.py --project {args.project} --query \"<targeted paper/work query>\" --fast-mode" + (f" --venue {args.venue}" if args.venue else ""),
            f"{management_python()} modules/finding/scripts/run_literature_tool.py --project {args.project} --query \"<conference or arXiv focus>\" --deep-survey" + (f" --venue {args.venue}" if args.venue else ""),
        ],
        "intermediate_files": load_artifact_file_map(paths, taste_dir),
        "strong_papers": strong[:20],
        "audit_only_candidates": audit_only[:20],
        "base_work_candidates": base_work[:20],
        "critique_candidates": critique[:20],
        "repo_candidates": summarize_repo_candidates(paths, source_selection),
        "suggested_followup_queries": suggested_queries(cfg, candidates, blockers),
        "current_blockers": blockers,
        "taste_sync": {
            "path": str(paths.state / "taste_sync.json"),
            "status": taste_sync.get("status") if isinstance(taste_sync, dict) else "",
            "counts": taste_sync.get("counts", {}) if isinstance(taste_sync, dict) else {},
        },
        "guardrails": [
            "Literature/tool outputs are research signals, not proof that an experiment worked.",
            "articles/strong_recommendations/read_candidates are the LLM-ranked recommended-paper/deep-reading pool; screened_ranking/triage_candidates/audit_candidates are audit pools.",
            "A missing ranked recommendation pool is a hard quality blocker; The workflow must repair discovery/detail fetching/LLM scoring instead of padding unscored papers.",
            "A paper with a URL or code hint still needs TASTE repo/data/env audit before it can become a base work.",
            "Claims in the final paper require local experiment artifacts, audit-ready metrics, logs, bad-case or counterexample evidence, and citation metadata.",
            "Do not expose the survey tool as a separate agent; it is an internal TASTE literature capability used by Claude Code and the trajectory system.",
        ],
    }
    save_json(paths.state / "literature_tool_packet.json", packet)
    paths.planning.mkdir(parents=True, exist_ok=True)
    (paths.planning / "literature_tool_packet.md").write_text(render_markdown(packet), encoding="utf-8")
    print(paths.planning / "literature_tool_packet.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
