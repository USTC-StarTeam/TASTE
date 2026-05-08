from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from auto_research.llm import LLMClient, clamp_workers, fallback_score, keyword_category, parallel_json
from auto_research.markdown import paper_markdown
from auto_research.models import AppConfig, FindRequest
from auto_research.jobs import JobCancelled
from auto_research.storage import create_run_dir, redacted_config, sync_latest, update_manifest, write_json, write_text

from .catalog import catalog_by_id
from .sources import (
    enrich_with_semantic_scholar,
    fetch_arxiv,
    fetch_github_trending,
    fetch_huggingface,
    fetch_openreview_iclr_2026,
    fetch_selected_venue_details,
    fetch_venue_title_index,
)


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]
ProgressFn = Callable[[str, int, int, str], None]


def _raise_if_cancelled(should_cancel: CancelFn) -> None:
    if should_cancel():
        raise JobCancelled("Task cancelled by user.")


def _chunks(items: list[dict], size: int) -> list[list[dict]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def _dedupe_items(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for item in items:
        key = str(item.get("id") or item.get("url") or item.get("title") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(default)
        except (TypeError, ValueError):
            return 0.0


def _normalize_hit_directions(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
    return []


def _combined_score(fit_score: object, diversity_score: object) -> float:
    fit = max(0.0, min(10.0, _as_float(fit_score)))
    diversity = max(0.0, min(10.0, _as_float(diversity_score)))
    return round(fit * 0.75 + diversity * 0.25, 2)


def _apply_relevance_guard(item: dict) -> None:
    text = f"{item.get('category', '')} {item.get('reason', '')} {item.get('fit_explanation', '')}".lower()
    irrelevant_markers = ["不相关", "无关", "irrelevant", "not relevant", "unrelated"]
    if any(marker in text for marker in irrelevant_markers):
        item["fit_score"] = min(_as_float(item.get("fit_score")), 1.5)
        item["diversity_score"] = min(_as_float(item.get("diversity_score")), 1.0)
        item["score"] = min(_as_float(item.get("score")), 1.5)


def _scan_count(total: int, config: AppConfig) -> int:
    fraction = max(0.01, min(1.0, float(config.venue_title_scan_fraction or 1.0)))
    return max(1, min(total, int(total * fraction) or 1))


def _prefilter_titles(
    items: list[dict],
    config: AppConfig,
    llm: LLMClient,
    venue_name: str,
    log: LogFn,
    should_cancel: CancelFn,
    progress: ProgressFn = lambda *_args: None,
) -> list[dict]:
    if not items:
        return []
    interest = "\n".join(part for part in [config.research_interest, config.researcher_profile] if part).strip()
    scanned = items[: _scan_count(len(items), config)]
    by_id = {item.get("id", ""): item for item in scanned}
    for item in scanned:
        fallback = fallback_score(interest, item.get("title", ""), "")
        item["title_fit_score"] = fallback
        item["fit_score"] = fallback
        item["diversity_score"] = min(8.0, max(0.0, fallback - 1.0))
        item["score"] = _combined_score(item["fit_score"], item["diversity_score"])
        item["hit_directions"] = []
        item["title_reason"] = "Keyword/profile title prefilter."
        item["reason_source"] = "keyword/profile fallback"

    if llm.enabled and interest:
        selected: list[dict] = []
        batch_size = 10
        batches = _chunks(scanned, batch_size)
        prompts: list[str] = []
        for batch_index, batch in enumerate(batches, 1):
            _raise_if_cancelled(should_cancel)
            title_lines = "\n".join(f"- {item.get('id')}: {item.get('title')}" for item in batch)
            prompts.append(f"""
You are strictly filtering accepted paper titles before expensive abstract/PDF fetching.

Research interest/profile:
{interest}

Paper titles, batch {batch_index}/{len(batches)}:
{title_lines}

Return strict JSON:
{{"selected":[{{"id":"paper id","fit_score":0-10,"diversity_score":0-10,"hit_directions":["direction"],"category":"short category","reason":"one concise Chinese reason"}}]}}

Rules:
- Only select titles that clearly hit the research interest or researcher profile.
- Generic AI/ML titles are not enough. They must connect to the user's concrete methods, domains, or constraints.
- fit_score is the core match to the profile. Use strict scoring: 9-10 exceptional, 7-8 strong, 6 possible, <=5 weak.
- diversity_score only rewards hitting multiple real user directions or adding a complementary method/domain. It cannot rescue low fit.
- If a title is unrelated or merely broad, do not include it.
""")
        workers = clamp_workers(config.llm_concurrency, default=16, maximum=32)
        results = parallel_json(llm, prompts, workers)
        seen: set[str] = set()
        for batch_index, (batch, result) in enumerate(zip(batches, results, strict=False), 1):
            _raise_if_cancelled(should_cancel)
            data = result.get("data")
            appended = 0
            if isinstance(data, dict) and isinstance(data.get("selected"), list):
                for row in data["selected"]:
                    item = by_id.get(str(row.get("id") or ""))
                    if not item or item.get("id") in seen:
                        continue
                    item["fit_score"] = _as_float(row.get("fit_score"), _as_float(row.get("score"), item.get("fit_score") or 0))
                    item["diversity_score"] = _as_float(row.get("diversity_score"), item.get("diversity_score") or 0)
                    item["score"] = _combined_score(item["fit_score"], item["diversity_score"])
                    item["hit_directions"] = _normalize_hit_directions(row.get("hit_directions"))
                    item["category"] = str(row.get("category") or item.get("category") or "")
                    item["title_reason"] = str(row.get("reason") or item.get("title_reason") or "")
                    item["fit_explanation"] = item["title_reason"]
                    item["reason_source"] = "llm title filter"
                    _apply_relevance_guard(item)
                    if _as_float(item.get("fit_score")) >= 6:
                        selected.append(item)
                        seen.add(item.get("id", ""))
                        appended += 1
            log(f"{venue_name}: title batch {batch_index}/{len(batches)} appended {appended}; candidates={len(selected)}")
            progress("llm_title_filter", batch_index, len(batches), f"{venue_name}: title batch {batch_index}/{len(batches)}, candidates {len(selected)}, workers {workers}")
        if selected:
            selected.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
            log(f"{venue_name}: LLM title prefilter appended {len(selected)} candidates from {len(scanned)} scanned titles")
            return selected[: config.max_fetch_papers]

    ranked = sorted(scanned, key=lambda row: float(row.get("score") or 0), reverse=True)
    if interest:
        ranked = [item for item in ranked if float(item.get("fit_score") or 0) >= 6.0]
    log(f"{venue_name}: fallback title prefilter ranked {len(ranked)} titles")
    return ranked[: config.max_fetch_papers]


def _evaluate_items(
    items: list[dict],
    config: AppConfig,
    llm: LLMClient,
    source_name: str,
    log: LogFn,
    should_cancel: CancelFn = lambda: False,
    progress: ProgressFn = lambda *_args: None,
) -> list[dict]:
    evaluated: list[dict] = []
    interest = "\n".join(part for part in [config.research_interest, config.researcher_profile] if part).strip()
    prompts: list[str] = []
    prompt_batches: list[list[dict]] = []
    for index, item in enumerate(items, 1):
        _raise_if_cancelled(should_cancel)
        progress("final_ranking_prepare", index, len(items), f"Preparing {source_name}: {item.get('title', 'Untitled')[:80]}")
        title = item.get("title", "")
        abstract = item.get("abstract", "")
        if item.get("classification_source") != "official":
            item["category"] = keyword_category(title, abstract)
            item["classification_source"] = "llm_inferred"
        fallback = fallback_score(interest, title, abstract)
        item["fit_score"] = _as_float(item.get("fit_score"), fallback) or fallback
        item["diversity_score"] = _as_float(item.get("diversity_score"), max(0.0, fallback - 1.0))
        item["score"] = _combined_score(item["fit_score"], item["diversity_score"])
        item["hit_directions"] = _normalize_hit_directions(item.get("hit_directions"))
        item["fit_explanation"] = item.get("fit_explanation") or "Keyword/profile fallback fit estimate."
        item["reason"] = item.get("reason") or "Keyword/profile fallback ranking. Configure an LLM API key for model-based relevance scoring."
        item["reason_source"] = item.get("reason_source") or "keyword/profile fallback"
        _apply_relevance_guard(item)
        evaluated.append(item)
    if llm.enabled and interest:
        for batch_index, batch in enumerate(_chunks(evaluated, 10), 1):
            item_lines = "\n\n".join(
                f"ID: {item.get('id')}\nTitle: {item.get('title')}\nAbstract/Description: {(item.get('abstract') or '')[:2200]}"
                for item in batch
            )
            prompts.append(f"""
You are the final strict relevance judge for research recommendations.

Research interest/profile:
{interest}

Candidate items, batch {batch_index}:
{item_lines}

Return strict JSON:
{{"evaluations":[{{"id":"paper id","category":"short category","fit_score":0-10,"diversity_score":0-10,"hit_directions":["direction"],"fit_explanation":"Chinese explanation","reason":"4-8 Chinese sentences"}}]}}

Scoring rules:
- fit_score is the primary score for direct match to the research interest and researcher profile.
- Use a strict rubric: 9-10 is exceptional match, 7-8 is strong match, 6 is maybe useful, <=5 is weak or generic.
- diversity_score rewards hitting multiple real user directions, bridging methods/domains, or complementing the user's profile.
- diversity_score cannot compensate for weak fit. If fit_score < 6, the item should not be recommended.
- Generic AI relevance is not enough. Explain exactly which user directions are hit and how strongly.
- If the item is unrelated, privacy-only, or just broad AI with no profile match, assign fit_score <= 2 and say so.
- The reason must explain what the item studies, its method/contribution, why it matches, and which directions it supports.
""")
            prompt_batches.append(batch)
        workers = clamp_workers(config.llm_concurrency, default=16, maximum=32)
        results = parallel_json(llm, prompts, workers)
        for batch_index, (batch, result) in enumerate(zip(prompt_batches, results, strict=False), 1):
            by_id = {str(item.get("id")): item for item in batch}
            data = result.get("data")
            if isinstance(data, dict):
                rows = data.get("evaluations")
                if isinstance(rows, dict):
                    rows = [rows]
                if isinstance(rows, list):
                    for row in rows:
                        item = by_id.get(str(row.get("id") or ""))
                        if not item:
                            continue
                        item["category"] = str(row.get("category") or item.get("category") or "")
                        item["fit_score"] = _as_float(row.get("fit_score"), item.get("fit_score") or 0)
                        item["diversity_score"] = _as_float(row.get("diversity_score"), item.get("diversity_score") or 0)
                        item["score"] = _combined_score(item["fit_score"], item["diversity_score"])
                        item["hit_directions"] = _normalize_hit_directions(row.get("hit_directions"))
                        item["fit_explanation"] = str(row.get("fit_explanation") or item.get("fit_explanation") or "")
                        item["reason"] = str(row.get("reason") or item.get("reason") or "")
                        item["reason_source"] = "llm abstract evaluation"
                        if item.get("classification_source") != "official":
                            item["classification_source"] = "llm_inferred"
                        _apply_relevance_guard(item)
            progress("abstract_scoring", batch_index, len(prompt_batches), f"{source_name}: scored batch {batch_index}/{len(prompt_batches)} with {workers} workers")
    evaluated.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
    log(f"{source_name}: evaluated {len(evaluated)} items")
    return evaluated


def _recommended(items: list[dict], config: AppConfig) -> list[dict]:
    interest = "\n".join(part for part in [config.research_interest, config.researcher_profile] if part).strip()
    ranked = sorted(items, key=lambda row: float(row.get("score") or 0), reverse=True)
    if interest:
        filtered = [item for item in ranked if float(item.get("fit_score") or 0) >= 6]
        return filtered[: config.max_recommended_papers]
    return ranked[: config.max_recommended_papers]


def _screened_ranking(items: list[dict]) -> list[dict]:
    ranked = [item for item in items if float(item.get("fit_score") or 0) > 6]
    ranked.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
    return ranked


def _source_status(source: str, ok: bool, count: int, message: str, limited: bool = False) -> dict:
    return {"source": source, "ok": ok, "limited": limited, "count": count, "message": message}


def _status_markdown(statuses: list[dict], title: str = "Source Status") -> str:
    lines = [f"# {title}", ""]
    for item in statuses:
        state = "limited" if item.get("limited") else ("ok" if item.get("ok") else "failed")
        lines.extend([
            f"## {item.get('source', '')}",
            "",
            f"- **Status**: {state}",
            f"- **Count**: {item.get('count', 0)}",
            f"- **Message**: {item.get('message', '')}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def run_find(
    request: FindRequest,
    log: LogFn = print,
    should_cancel: CancelFn = lambda: False,
    progress: ProgressFn = lambda *_args: None,
) -> dict:
    config = request.config or AppConfig()
    run_id, run_dir = create_run_dir("find")
    write_json(run_dir / "config.json", redacted_config(config.model_dump()))
    write_json(run_dir / "selection.json", request.selection.model_dump())
    log(f"Created run {run_id}")

    llm = LLMClient(config, "find")
    catalog = catalog_by_id()
    venue_papers: list[dict] = []
    title_candidates: list[dict] = []
    evaluated_candidates: list[dict] = []
    source_status: list[dict] = []
    venue_health_report: list[dict] = []

    per_venue_limit = max(1, config.max_fetch_papers)
    title_scan_limit = max(per_venue_limit, config.venue_title_scan_limit)
    progress("venue_title_index", 0, max(1, len(request.selection.venue_ids)), "Starting venue title index fetch")
    for venue_index, venue_id in enumerate(request.selection.venue_ids, 1):
        _raise_if_cancelled(should_cancel)
        venue = catalog.get(venue_id)
        if not venue:
            log(f"Skipping unknown venue id: {venue_id}")
            continue

        if venue.get("classification_source") == "official":
            log(f"Fetching official venue data for {venue.get('name')}")
            titles, adapter = fetch_venue_title_index(venue, request.selection.years, per_venue_limit)
            log(f"{venue.get('name')}: fetched {len(titles)} papers via {adapter}")
            venue_health_report.append({"venue_id": venue_id, "venue": venue.get("name"), "years": request.selection.years, "adapter": adapter, "sample_count": len(titles), "ok": bool(titles), "error": "" if titles else "No papers fetched.", "suggested_fix": "" if titles else "Check OpenReview/DBLP venue id."})
            venue_papers.extend(titles)
            continue

        log(f"Fetching title index for {venue.get('name')} years {request.selection.years}")
        progress("venue_title_index", venue_index, len(request.selection.venue_ids), f"Fetching title index: {venue.get('name')}")
        title_index, adapter = fetch_venue_title_index(venue, request.selection.years, title_scan_limit)
        venue_health_report.append({"venue_id": venue_id, "venue": venue.get("name"), "years": request.selection.years, "adapter": adapter, "sample_count": len(title_index), "ok": bool(title_index), "error": "" if title_index else "No title index found.", "suggested_fix": "" if title_index else "High-priority venue may need a dedicated proceedings adapter."})
        if not title_index:
            log(f"{venue.get('name')}: no title index found via {adapter}")
            continue
        log(f"{venue.get('name')}: fetched {len(title_index)} candidate titles via {adapter}")
        selected_titles = _prefilter_titles(title_index, config, llm, venue.get("name", venue_id), log, should_cancel, progress)
        title_candidates.extend(selected_titles)
        _raise_if_cancelled(should_cancel)
        progress("detail_fetch", 0, max(1, len(selected_titles)), f"{venue.get('name')}: fetching selected paper details")
        detailed = fetch_selected_venue_details(selected_titles)
        progress("detail_fetch", len(selected_titles), max(1, len(selected_titles)), f"{venue.get('name')}: detail fetch complete")
        if any(not item.get("abstract") for item in detailed):
            progress("abstract_enrichment", 0, min(20, len(detailed)), f"{venue.get('name')}: enriching abstracts")
            detailed = enrich_with_semantic_scholar(detailed, limit=min(20, len(detailed)))
            progress("abstract_enrichment", min(20, len(detailed)), min(20, len(detailed)), f"{venue.get('name')}: abstract enrichment complete")
        venue_papers.extend(detailed)
    title_candidates = _dedupe_items(title_candidates)
    venue_papers = _dedupe_items(venue_papers)
    if request.selection.venue_ids:
        source_status.append(_source_status("venues", bool(venue_papers), len(venue_papers), "ok" if venue_papers else "No venue papers fetched. See venue_health_report.json."))

    evaluated_candidates = _evaluate_items(venue_papers, config, llm, "articles", log, should_cancel, progress)
    article_items = _recommended(evaluated_candidates, config)

    if request.selection.include_arxiv:
        _raise_if_cancelled(should_cancel)
        log(f"Fetching arXiv categories: {', '.join(config.arxiv_categories)}")
        progress("arxiv", 0, 1, "Fetching arXiv")
        arxiv_items, arxiv_status = fetch_arxiv(
            config.arxiv_categories,
            config.max_fetch_papers,
            config.arxiv_start_date,
            config.arxiv_end_date,
        )
        source_status.append(arxiv_status)
        arxiv_evaluated = _evaluate_items(arxiv_items, config, llm, "arxiv", log, should_cancel, progress)
        evaluated_candidates.extend(arxiv_evaluated)
        article_items.extend(arxiv_evaluated)
        article_items = _recommended(article_items, config)
        progress("arxiv", 1, 1, "arXiv complete")

    hf_items: list[dict] = []
    if request.selection.include_huggingface:
        _raise_if_cancelled(should_cancel)
        log("Fetching HuggingFace papers/models")
        progress("huggingface", 0, 1, "Fetching HuggingFace")
        hf_papers, hf_models, hf_status = fetch_huggingface(
            max_papers=max(1, config.max_recommended_papers),
            max_models=10,
            include_papers=config.hf_include_papers,
            include_models=config.hf_include_models,
            start_date=config.arxiv_start_date,
            end_date=config.arxiv_end_date,
        )
        source_status.append(hf_status)
        hf_items = _evaluate_items(hf_papers + hf_models, config, llm, "huggingface", log, should_cancel, progress)[: config.max_recommended_papers]
        progress("huggingface", 1, 1, "HuggingFace complete")

    github_items: list[dict] = []
    if request.selection.include_github:
        _raise_if_cancelled(should_cancel)
        log("Fetching GitHub trending repositories")
        progress("github", 0, 1, "Fetching GitHub")
        github_raw, github_status = fetch_github_trending(
            config.github_languages,
            config.github_since,
            config.max_recommended_papers,
            config.arxiv_start_date,
            config.arxiv_end_date,
        )
        source_status.append(github_status)
        github_items = _evaluate_items(
            github_raw,
            config,
            llm,
            "github",
            log,
            should_cancel,
            progress,
        )[: config.max_recommended_papers]
        progress("github", 1, 1, "GitHub complete")

    _raise_if_cancelled(should_cancel)

    artifacts = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "title_candidates": title_candidates,
        "evaluated_candidates": evaluated_candidates,
        "screened_ranking": _screened_ranking(evaluated_candidates),
        "articles": article_items,
        "huggingface": hf_items,
        "github": github_items,
        "source_status": source_status,
        "venue_health_report": venue_health_report,
    }
    write_json(run_dir / "find_results.json", artifacts)
    write_json(run_dir / "venue_health_report.json", {"run_id": run_id, "results": venue_health_report})

    article_md = paper_markdown(article_items, "Recommended Articles")
    hf_md = paper_markdown(hf_items, "HuggingFace Papers and Models")
    github_md = paper_markdown(github_items, "GitHub Trending Repositories")
    status_md = _status_markdown(source_status)
    write_text(run_dir / "article.md", article_md)
    write_text(run_dir / "hf.md", hf_md)
    write_text(run_dir / "github.md", github_md)
    write_text(run_dir / "source_status.md", status_md)
    sync_latest("auto_find", "article.md", run_dir / "article.md")
    sync_latest("auto_find", "hf.md", run_dir / "hf.md")
    sync_latest("auto_find", "github.md", run_dir / "github.md")
    sync_latest("auto_find", "source_status.md", run_dir / "source_status.md")
    update_manifest(run_dir, "find")
    log("Find stage complete")
    return artifacts
