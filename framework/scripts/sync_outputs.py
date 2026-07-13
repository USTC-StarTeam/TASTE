#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths, load_project_config
from literature_policy import now_utc, paper_sort_key, repo_sort_key, score_paper, score_repo_candidate

from taste_pythonpath import ensure_taste_pythonpath
ensure_taste_pythonpath(ROOT)
from auto_research.source_selection import canonical_source_selection, paper_source_allowed, source_enabled
from auto_research.paths import FINDING_RUNS_DIR, RUNS_DIR


STANDARD_ARTIFACTS = [
    "find.md", "source_status.md", "hf.md", "github.md",
    "find_results.json", "find_progress.json", "manifest.json", "selection.json",
    "venue_health_report.json", "category_scan_report.json", "title_filter_report.json",
    "arxiv_raw.json", "arxiv_prefiltered.json", "biorxiv.md", "biorxiv_raw.json",
    "biorxiv_prefiltered.json", "nature.md", "nature_raw.json", "nature_prefiltered.json",
    "science.md", "science_raw.json", "science_prefiltered.json",
]


def resolve_taste_run_dir(run_id: str, state: Any | None = None) -> Path | None:
    run_id = str(run_id or "").strip()
    if not run_id:
        return None
    candidates: list[Path] = []
    state = state if isinstance(state, dict) else {}
    state_run_id = str(state.get("taste_run_id") or state.get("run_id") or "").strip()
    state_run_dir = str(state.get("taste_run_dir") or state.get("run_dir") or "").strip()
    if state_run_id == run_id and state_run_dir:
        candidates.append(Path(state_run_dir))
    candidates.extend([
        FINDING_RUNS_DIR / run_id,
        RUNS_DIR / run_id,
    ])
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and (candidate / "find_results.json").exists():
            return candidate
    return None


def adopt_taste_find_run(paths: Any, state: Any, run_id: str) -> dict[str, Any]:
    run_dir = resolve_taste_run_dir(run_id, state)
    if run_dir is None:
        raise SystemExit(f"TASTE run not found or missing find_results.json: {run_id}")
    taste_dir = paths.planning / "finding"
    taste_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in STANDARD_ARTIFACTS:
        source = run_dir / name
        if source.exists():
            shutil.copyfile(source, taste_dir / name)
            copied.append(name)
    find_results = load_json(taste_dir / "find_results.json", {})
    progress = load_json(taste_dir / "find_progress.json", {})
    state_payload = dict(state) if isinstance(state, dict) else {}
    counts = state_payload.get("counts") if isinstance(state_payload.get("counts"), dict) else {}
    if isinstance(find_results, dict):
        strong_count = len(find_results.get("strong_recommendations") or find_results.get("articles") or [])
        counts.update({
            "strong_recommendations": strong_count,
            "evaluated_candidates": len(find_results.get("evaluated_candidates") or []),
            "title_candidates": len(find_results.get("title_candidates") or find_results.get("retrieval_candidates") or []),
            "raw_title_index": len(find_results.get("raw_title_index") or []),
        })
    state_payload.update({
        "project": paths.root.name,
        "taste_root": str(ROOT),
        "taste_run_id": run_id,
        "taste_run_dir": str(run_dir),
        "output_dir": str(taste_dir),
        "stage": "find_completed_current_web_run_adopted",
        "status": "find_completed_current_web_run_adopted",
        "counts": counts,
        "survey_stats": survey_stats_from_find(find_results, state_payload.get("survey_stats", {})) if isinstance(find_results, dict) else state_payload.get("survey_stats", {}),
        "adopted_run": {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "copied": copied,
            "progress_phase": progress.get("phase") if isinstance(progress, dict) else "",
            "guardrail": "Adopted completed Web/API Find artifacts only; downstream Read/Idea/Plan must be rebuilt from this adopted run before use.",
        },
    })
    save_json(paths.state / "finding_frontend.json", state_payload)
    nl = chr(10)
    summary = []
    summary.append("# Find Frontend" + nl + nl)
    summary.append("- status: find_completed_current_web_run_adopted" + nl)
    summary.append(f"- taste_run_id: {run_id}" + nl)
    summary.append(f"- taste_run_dir: {run_dir}" + nl)
    summary.append(f"- output_dir: {taste_dir}" + nl)
    summary.append(f"- copied: {len(copied)} artifacts" + nl)
    summary.append("- 使用边界: 已采用 Web/API Find 产物；下游 Read/Idea/Plan 由 TASTE 统一脚本基于该 run 重建。" + nl)
    (paths.planning / "finding_frontend.md").write_text("".join(summary), encoding="utf-8")
    return state_payload


def sync_current_find_progress(state: Any, taste_dir: Path) -> dict[str, Any]:
    """Keep project planning/finding/find_progress.json bound to the current run."""
    if not isinstance(state, dict):
        return {"synced": False, "reason": "missing_taste_state"}
    run_id = str(state.get("taste_run_id") or state.get("run_id") or "").strip()
    candidates: list[Path] = []
    run_dir_text = str(state.get("taste_run_dir") or "").strip()
    if run_dir_text:
        candidates.append(Path(run_dir_text) / "find_progress.json")
    if run_id:
        candidates.extend([
            FINDING_RUNS_DIR / run_id / "find_progress.json",
            RUNS_DIR / run_id / "find_progress.json",
        ])
    target = taste_dir / "find_progress.json"
    for source in candidates:
        if not source.exists():
            continue
        payload = load_json(source, {})
        if run_id and isinstance(payload, dict) and str(payload.get("run_id") or "") not in {"", run_id}:
            continue
        existing = load_json(target, {})
        if source.resolve() != target.resolve() and (not isinstance(existing, dict) or existing.get("run_id") != payload.get("run_id") or existing.get("updated_at") != payload.get("updated_at") or existing.get("counts") != payload.get("counts")):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            return {"synced": True, "source": str(source), "target": str(target), "run_id": payload.get("run_id") if isinstance(payload, dict) else run_id}
        return {"synced": False, "reason": "already_current", "source": str(source), "target": str(target), "run_id": payload.get("run_id") if isinstance(payload, dict) else run_id}
    return {"synced": False, "reason": "current_run_progress_not_found", "run_id": run_id, "target": str(target)}


def load_json(path: Path, default: Any):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def normalize_taste_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    normalized = dict(state)
    if normalized and normalized.get("taste_root") != str(ROOT):
        normalized["taste_root"] = str(ROOT)
    return normalized


def normalize_article(row: dict[str, Any], cfg: dict[str, Any], index: int, reference_time: dt.datetime) -> dict[str, Any]:
    paper_id = str(row.get("id") or row.get("paper_id") or row.get('url') or row.get('title') or f"taste_{index}")
    authors = row.get("authors", [])
    if isinstance(authors, str):
        authors = [item.strip() for item in authors.split(",") if item.strip()]
    item = {
        "source": "finding",
        "paper_id": paper_id.replace("/", "_"),
        "entry_id": row.get("id") or row.get('url') or "",
        "title": row.get('title') or "Untitled TASTE item",
        "summary": row.get("abstract") or row.get("summary") or row.get("reason") or row.get("fit_explanation") or "",
        "published": row.get("published") or row.get("published_at") or row.get("year") or row.get("updated") or "",
        "updated": row.get("updated") or row.get("published") or row.get("year") or "",
        "authors": authors,
        "categories": row.get("categories", []) if isinstance(row.get("categories", []), list) else [],
        "pdf_url": row.get("pdf_url") or row.get("pdf") or "",
        "abs_url": row.get('url') or row.get("abs_url") or "",
        "citations": row.get("citations") or row.get("citationCount"),
        "influential_citations": row.get("influential_citations") or row.get("influentialCitationCount"),
        "tldr": row.get("tldr") or row.get("fit_explanation") or "",
        "venue": row.get("venue") or row.get("source_venue") or "",
        "journal": row.get("journal") or "",
        "query": "workflow feedback-driven discovery",
        "taste_score": row.get("score") or row.get("fit_score") or 0,
        "taste_reason": row.get("reason") or row.get("fit_explanation") or "",
        "taste_fit_score": row.get("fit_score"),
        "taste_diversity_score": row.get("diversity_score"),
        "taste_pool": row.get("taste_pool", ""),
        "taste_pool_role": row.get("taste_pool_role", ""),
        "topic_evidence": row.get("topic_evidence", ""),
        "evidence_tier": row.get("evidence_tier", ""),
        "weak_candidate_for_critique": bool(row.get("weak_candidate_for_critique", False)),
        "recommendation_note": row.get("recommendation_note", ""),
        "not_positive_support": bool(row.get("weak_candidate_for_critique", False)) or str(row.get("taste_pool_role", "")) in {"evaluated_candidate", "title_candidate", "critique_candidate"},
    }
    item.update(score_paper(item, cfg, reference_time=reference_time))
    if item.get("not_positive_support"):
        item["selection_bucket"] = "deprioritized"
        item["high_quality_recent"] = False
        item["top_tier_readiness"] = "weak"
        item["discovery_priority_score"] = min(float(item.get("discovery_priority_score") or 0), 0.0)
        item["idea_worthiness_score"] = min(float(item.get("idea_worthiness_score") or 0), 0.0)
        item["guardrail"] = "finding inspected this as a weak or boundary candidate; keep it visible for critique/search expansion only, not as a positive method anchor or manuscript conclusion source."
    if str(row.get("source", "")) == "taste_recoverable_fallback" or "recoverable fallback" in str(row.get("reason", "")).lower():
        item["selection_bucket"] = "deprioritized"
        item["discovery_priority_score"] = -100.0
        item["idea_worthiness_score"] = -100.0
        item["high_quality_recent"] = False
        item["not_scientific_evidence"] = True
        item["guardrail"] = "Operational fallback only; do not treat as a discovered paper or idea source."
    return item




def is_positive_taste_row(row: dict[str, Any], pool_name: str, pool_role: str) -> bool:
    if row.get("not_positive_support") or row.get("weak_candidate_for_critique") or row.get("foundation_demoted_from_strong"):
        return False
    tier = str(row.get("evidence_tier") or "").lower()
    role = str(row.get("evidence_role") or "").lower()
    if tier in {"retrieval_only", "nethreshold_for_reading", "weak_or_boundary"}:
        return False
    if role in {"weak_or_boundary", "negative", "critique_only"}:
        return False
    return pool_name in {"strong_recommendations", "articles", "screened_ranking"} and pool_role in {"strong_recommendation", "strong_ranking"}



def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


def survey_stats_from_find(find_results: Any, fallback: Any) -> dict[str, Any]:
    stats = dict(fallback) if isinstance(fallback, dict) else {}
    if not isinstance(find_results, dict):
        return stats
    raw_title_index = find_results.get("raw_title_index") if isinstance(find_results.get("raw_title_index"), list) else []
    venue_rows = find_results.get("venue_health_report") if isinstance(find_results.get("venue_health_report"), list) else []
    source_rows = find_results.get("source_status") if isinstance(find_results.get("source_status"), list) else []
    category_rows = find_results.get("category_scan_report") if isinstance(find_results.get("category_scan_report"), list) else []
    title_rows = find_results.get("title_filter_report") if isinstance(find_results.get("title_filter_report"), list) else []
    raw_count = len(raw_title_index)
    if not raw_count:
        raw_count = sum(_safe_int((row if isinstance(row, dict) else {}).get("corpus_count") or (row if isinstance(row, dict) else {}).get("sample_count") or (row if isinstance(row, dict) else {}).get("raw_title_index_count"), 0) for row in venue_rows)
    if not raw_count:
        raw_count = sum(_safe_int((row if isinstance(row, dict) else {}).get("raw_title_index_count"), 0) for row in source_rows)
    if raw_count:
        stats["raw_title_index_papers"] = raw_count
        stats["venue_total_papers_available"] = raw_count
        stats["venue_corpus_audited_papers"] = raw_count
    if category_rows:
        stats["category_corpus_audited_papers"] = sum(_safe_int((row if isinstance(row, dict) else {}).get("corpus_audit_papers") or (row if isinstance(row, dict) else {}).get("total_papers"), 0) for row in category_rows)
        stats["venue_category_selected_papers"] = sum(_safe_int((row if isinstance(row, dict) else {}).get("selected_category_papers"), 0) for row in category_rows)
        stats["category_scan_reports"] = len(category_rows)
    if title_rows:
        stats["venue_title_filter_input_papers"] = sum(_safe_int((row if isinstance(row, dict) else {}).get("title_filter_input_papers"), 0) for row in title_rows)
        stats["venue_final_title_candidates"] = sum(_safe_int((row if isinstance(row, dict) else {}).get("final_title_candidates"), 0) for row in title_rows)
        stats["title_filter_reports"] = len(title_rows)
    evaluated = find_results.get("evaluated_candidates") if isinstance(find_results.get("evaluated_candidates"), list) else []
    if evaluated:
        stats["venue_detail_fetched_candidates"] = len(evaluated)
        stats["venue_evaluated_candidates"] = len(evaluated)
        stats["llm_scored_candidates"] = max(
            _safe_int(stats.get("llm_scored_candidates"), 0),
            sum(1 for row in evaluated if isinstance(row, dict) and str(row.get("reason_source") or "") == "llm abstract evaluation") or len(evaluated),
        )
    return stats


def normalize_repo(row: dict[str, Any], cfg: dict[str, Any], reference_time: dt.datetime) -> dict[str, Any] | None:
    url = str(row.get('url') or row.get("html_url") or "")
    name = str(row.get('name') or row.get("full_name") or url.rstrip("/").split("github.com/")[-1])
    if "github.com" not in url and "/" not in name:
        return None
    item = {
        "source": "finding_github",
        "name": name,
        "url": url or f"https://github.com/{name}",
        "summary": row.get("abstract") or row.get("description") or row.get("summary") or row.get("reason") or "",
        "task_fit": True,
        "recent_activity": bool(row.get("recent_activity", True)),
        "has_readme": bool(row.get("has_readme", False)),
        "has_license": bool(row.get("has_license", False)),
        "has_install": bool(row.get("has_install", False)),
        "has_entrypoint": bool(row.get("has_entrypoint", False)),
        "has_tests": bool(row.get("has_tests", False)),
        "has_dataset_docs": bool(row.get("has_dataset_docs", False)),
        "stars": row.get("stars") or row.get("stargazers_count") or 0,
        "forks": row.get("forks") or row.get("forks_count") or 0,
        "last_pushed_at": row.get("last_pushed_at") or row.get("pushed_at") or row.get("updated_at") or "",
        "topics": row.get("topics", []) if isinstance(row.get("topics", []), list) else [],
        "language": row.get("language") or "",
        "notes": "synced from finding GitHub/source output; requires local audit before selection",
        "updated_from": "finding_sync",
    }
    item.update(score_repo_candidate(item, cfg, reference_time=reference_time))
    item["score"] = item.get("repo_reuse_score", 0)
    return item


def extract_plans(taste_dir: Path) -> dict[str, Any]:
    plans_payload = load_json(taste_dir / "plans.json", {})
    run_id = str(plans_payload.get("run_id") or "") if isinstance(plans_payload, dict) else ""
    if "recoverable_fallback" in run_id:
        return {"source": "finding", "public_final_artifact": "plan.md", "plans_json_path": "", "plan_markdown_path": "", "guardrail": "Fallback finding plans are operational repair notes only and are not synced."}
    plan_md_path = taste_dir / "plan.md"
    return {
        "source": "finding",
        "public_final_artifact": "plan.md",
        "plans_json_path": str(taste_dir / "plans.json") if isinstance(plans_payload, dict) and plans_payload else "",
        "plan_markdown_path": str(plan_md_path) if plan_md_path.exists() else "",
        "selected_idea_id": plans_payload.get("selected_idea_id", "") if isinstance(plans_payload, dict) else "",
        "selected_plan_id": plans_payload.get("selected_plan_id", "") if isinstance(plans_payload, dict) else "",
        "artifact_policy": {
            "public_plan_body": "plan.md",
            "plans_json_role": "machine audit/control state; no Markdown body copy",
            "taste_plan_bridge_role": "lightweight paths and selection index only",
        },
        "guardrail": "Plan must be reconciled with TASTE repo/data/evidence checks before execution.",
    }


def render_report(payload: dict[str, Any]) -> str:
    lines = ["# TASTE Sync\n\n"]
    for key in ["generated_at", "project", "status", "taste_output_dir"]:
        lines.append(f"- {key}: {payload.get(key, '')}\n")
    counts = payload.get("counts", {})
    for key in ["papers_synced", "audit_candidates_retained", "repos_synced", "ideas_synced", "plans_synced"]:
        lines.append(f"- {key}: {counts.get(key, 0)}\n")
    if payload.get("reason"):
        lines.append(f"- reason: {payload['reason']}\n")
    stats = payload.get("survey_stats", {}) if isinstance(payload.get("survey_stats", {}), dict) else {}
    if stats:
        lines.append("\n## Survey Coverage\n")
        for key in ["deep_survey", "venue_total_papers_available", "venue_category_selected_papers", "venue_title_filter_input_papers", "venue_final_title_candidates", "arxiv_raw_count", "arxiv_prefiltered_count", "arxiv_pages_fetched"]:
            lines.append(f"- {key}: {stats.get(key, '')}\n")
    if payload.get("intermediate_state_file"):
        lines.append(f"- intermediate_state_file: {payload.get('intermediate_state_file')}\n")
    if payload.get("audit_candidate_file"):
        lines.append(f"- audit_candidate_file: {payload.get('audit_candidate_file')}\n")
    lines.append("\n## 使用边界\n")
    lines.append("- Outputs are upstream research signals. The workflow must still verify recency, topic fit, repo runnability, real data, bad-case slices, and evidence checks before manuscript conclusions or final-paper status.\n")
    lines.append("\n## Top Papers\n")
    for row in payload.get("top_papers", [])[:8]:
        lines.append(f"- {row.get('selection_bucket')} | score={row.get('discovery_priority_score')} | {row.get('title')}\n")
    if not payload.get("top_papers"):
        lines.append("- none\n")
    lines.append("\n## Top Repos\n")
    for row in payload.get("top_repos", [])[:8]:
        lines.append(f"- {row.get('repo_selection_bucket')} | score={row.get('repo_reuse_score')} | {row.get('name')} | {row.get('url')}\n")
    if not payload.get("top_repos"):
        lines.append("- none\n")
    return "".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync finding outputs into AutoScientist discovery, repo, idea, and plan state.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", default="", help="Adopt a completed Web/API Find run into this project before syncing.")
    parser.add_argument("--allow-empty", action="store_true")
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    source_selection = canonical_source_selection(project_config_path=paths.config)
    state = normalize_taste_state(load_json(paths.state / "finding_frontend.json", {}))
    if state:
        save_json(paths.state / "finding_frontend.json", state)
    if args.run_id:
        state = adopt_taste_find_run(paths, state, args.run_id)
    taste_dir = Path(state.get("output_dir") or paths.planning / "finding")
    taste_intermediates = {
        "find_results": str(taste_dir / "find_results.json"),
        "find_progress": str(taste_dir / "find_progress.json"),
        "category_scan_report": str(taste_dir / "category_scan_report.json"),
        "title_filter_report": str(taste_dir / "title_filter_report.json"),
        "arxiv_raw": str(taste_dir / "arxiv_raw.json"),
        "arxiv_prefiltered": str(taste_dir / "arxiv_prefiltered.json"),
        "read_md": str(taste_dir / "read.md"),
        "public_final_artifact": str(taste_dir / "read.md"),
        "read_results": str(taste_dir / "read_results.json"),
        "ideas": str(taste_dir / "ideas.json"),
        "plans": str(taste_dir / "plans.json"),
    }
    payload: dict[str, Any] = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "project": args.project,
        "status": "running",
        "taste_state": state,
        "taste_output_dir": str(taste_dir),
        "taste_intermediates": taste_intermediates,
        "survey_stats": state.get("survey_stats", {}) if isinstance(state, dict) else {},
        "counts": {"papers_synced": 0, "repos_synced": 0, "ideas_synced": 0, "plans_synced": 0},
    }
    save_json(paths.state / "taste_sync.json", payload)

    progress_sync = sync_current_find_progress(state, taste_dir)
    payload["find_progress_sync"] = progress_sync

    if not taste_dir.exists():
        payload.update({"status": "skipped_no_taste_output", "reason": "TASTE output directory does not exist yet."})
        save_json(paths.state / "taste_sync.json", payload)
        (paths.reports / "taste_sync.md").write_text(render_report(payload), encoding="utf-8")
        if not args.allow_empty:
            raise SystemExit(2)
        print(paths.reports / "taste_sync.md")
        return

    reference_time = now_utc()
    find_results = load_json(taste_dir / "find_results.json", {})
    payload["survey_stats"] = survey_stats_from_find(find_results, payload.get("survey_stats", {}))
    if isinstance(find_results, dict):
        articles = find_results.get("strong_recommendations", []) if isinstance(find_results.get("strong_recommendations", []), list) else []
        fallback_only = bool(articles) and all(str(row.get("source", "")) == "taste_recoverable_fallback" for row in articles if isinstance(row, dict))
        if str(find_results.get("run_id") or "") == "taste_recoverable_fallback" or fallback_only:
            payload.update({
                "status": "blocked_fallback_only",
                "reason": "TASTE output is recoverable fallback only; placeholders are not literature evidence and must not be synced into TASTE discovery, idea, or claim support pools.",
                "counts": {"papers_synced": 0, "repos_synced": 0, "ideas_synced": 0, "plans_synced": 0},
            })
            save_json(paths.state / "taste_sync.json", payload)
            (paths.reports / "taste_sync.md").write_text(render_report(payload), encoding="utf-8")
            print(paths.reports / "taste_sync.md")
            if not args.allow_empty:
                raise SystemExit(2)
            return
    raw_articles = []
    audit_articles = []
    if isinstance(find_results, dict):
        # Only strict positive pools enter the main TASTE discovery stream. Audit and
        # boundary pools stay available in taste_literature_audit_candidates.json so
        # they cannot be promoted by generic source/recency ranking.
        source_pools = [
            ("strong_recommendations", "strong_recommendation"),
            ("screened_ranking", "strong_ranking"),
            ("triage_candidates", "triage_candidate"),
            ("arxiv_prefiltered", "nethreshold_arxiv"),
            ("evaluated_candidates", "evaluated_candidate"),
            ("title_candidates", "title_candidate"),
            ("critique_candidates", "critique_candidate"),
        ]
        for pool_name, pool_role in source_pools:
            for row in find_results.get(pool_name, []) or []:
                if not isinstance(row, dict):
                    continue
                item = dict(row)
                item.setdefault("taste_pool", pool_name)
                item.setdefault("taste_pool_role", pool_role)
                if not paper_source_allowed(item, source_selection):
                    continue
                if is_positive_taste_row(item, pool_name, pool_role):
                    raw_articles.append(item)
                else:
                    item.setdefault("weak_candidate_for_critique", True)
                    item.setdefault("recommendation_note", "finding inspected this candidate but did not promote it as strong positive evidence; use for critique, boundary mapping, or search expansion only.")
                    audit_articles.append(item)
    papers: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(raw_articles, 1):
        if isinstance(row, dict):
            item = normalize_article(row, cfg, index, reference_time)
            item["selection_bucket"] = "recent_high_priority"
            item["high_quality_recent"] = True
            item["top_tier_readiness"] = "promising"
            item["discovery_priority_score"] = max(float(item.get("discovery_priority_score") or 0), float(item.get("taste_score") or 0))
            item["idea_worthiness_score"] = max(float(item.get("idea_worthiness_score") or 0), float(item.get("taste_score") or 0))
            key = str(item.get("paper_id") or item.get("title"))
            old = papers.get(key)
            if old is None or paper_sort_key(item) < paper_sort_key(old):
                papers[key] = item
    paper_items = sorted(papers.values(), key=paper_sort_key)
    audit_items = [normalize_article(row, cfg, index, reference_time) for index, row in enumerate(audit_articles, 1) if isinstance(row, dict)]
    if paper_items:
        ts = reference_time.strftime("%Y%m%dT%H%M%SZ")
        discover_file = paths.discover / f"{ts}_finding_synced.json"
        save_json(discover_file, {
            "generated_at": ts,
            "reference_time": reference_time.isoformat(),
            "project": args.project,
            "source": "finding",
            "query": "workflow feedback-driven discovery",
            "items": paper_items,
            "taste_state": state,
        })
        payload["counts"]["papers_synced"] = len(paper_items)
        payload["discover_file"] = str(discover_file)

    if isinstance(find_results, dict):
        save_json(paths.state / "taste_literature_intermediates.json", {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "project": args.project,
            "taste_output_dir": str(taste_dir),
            "survey_stats": payload.get("survey_stats", {}),
            "taste_intermediates": taste_intermediates,
            "candidate_pool_counts": {
                key: sum(1 for row in (find_results.get(key, []) or []) if isinstance(row, dict) and paper_source_allowed(row, source_selection))
                for key in ["strong_recommendations", "screened_ranking", "triage_candidates", "arxiv_prefiltered", "evaluated_candidates", "title_candidates", "critique_candidates"]
            },
            "synced_candidate_count": len(paper_items),
            "audit_candidate_count": len(audit_items),
            "synced_candidate_roles": sorted({str(row.get("taste_pool_role") or row.get("taste_pool") or "unknown") for row in raw_articles if isinstance(row, dict)}),
            "audit_candidate_roles": sorted({str(row.get("taste_pool_role") or row.get("taste_pool") or "unknown") for row in audit_articles if isinstance(row, dict)}),
            "audit_candidate_file": str(paths.state / "taste_literature_audit_candidates.json"),
            "category_scan_report": load_json(taste_dir / "category_scan_report.json", {}),
            "title_filter_report": load_json(taste_dir / "title_filter_report.json", {}),
            "arxiv_prefiltered_report": load_json(taste_dir / "arxiv_prefiltered.json", {}).get("report", {}) if isinstance(load_json(taste_dir / "arxiv_prefiltered.json", {}), dict) else {},
            "guardrail": "Downstream project agents and experiment iteration should use these as literature signals only; local repo/data/experiment evidence is still required before manuscript conclusions. Weak/critique candidates must not be cited as positive support.",
        })
        save_json(paths.state / "taste_literature_audit_candidates.json", {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "project": args.project,
            "source": "finding_audit_only",
            "items": audit_items,
            "guardrail": "Audit-only finding candidates are retained for debugging, critique, and search expansion; they must not drive main recommendations, ideas, or manuscript conclusions.",
        })
        payload["intermediate_state_file"] = str(paths.state / "taste_literature_intermediates.json")
        payload["audit_candidate_file"] = str(paths.state / "taste_literature_audit_candidates.json")
        payload["counts"]["audit_candidates_retained"] = len(audit_items)

    raw_github = []
    if source_enabled(source_selection, "github"):
        if isinstance(find_results, dict):
            raw_github.extend(find_results.get("github", []) or [])
        github_json = load_json(taste_dir / "github.json", {})
        if isinstance(github_json, dict):
            raw_github.extend(github_json.get("items", []) or github_json.get("repositories", []) or [])
    repo_items = [item for item in (normalize_repo(row, cfg, reference_time) for row in raw_github if isinstance(row, dict)) if item]
    if repo_items:
        registry_path = paths.state / "repo_candidates.json"
        existing = load_json(registry_path, [])
        by_name = {str(row.get('name') or row.get('url')): row for row in existing if isinstance(row, dict)}
        for item in repo_items:
            by_name[str(item.get("name") or item.get("url"))] = item
        merged = sorted(by_name.values(), key=repo_sort_key)
        save_json(registry_path, merged)
        payload["counts"]["repos_synced"] = len(repo_items)
        payload["top_repos"] = merged[:10]

    plans = extract_plans(taste_dir)
    if plans.get("plans_json_path") or plans.get("plan_markdown_path"):
        save_json(paths.state / "taste_plan_bridge.json", plans)
        payload["counts"]["plans_synced"] = 1

    payload["top_papers"] = paper_items[:10]
    payload.setdefault("top_repos", repo_items[:10])
    payload["status"] = "completed" if any(payload["counts"].values()) else "completed_no_usable_taste_items"
    if payload["status"] == "completed_no_usable_taste_items":
        payload['reason'] = "TASTE ran or timed out but no parseable paper/repo/idea/plan artifacts were available."
    save_json(paths.state / "taste_sync.json", payload)
    (paths.reports / "taste_sync.md").write_text(render_report(payload), encoding="utf-8")
    print(paths.reports / "taste_sync.md")


if __name__ == "__main__":
    main()
