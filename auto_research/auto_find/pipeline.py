from __future__ import annotations

import copy
from collections import Counter
from datetime import UTC, datetime
from math import ceil
from typing import Callable

from auto_research.llm import LLMClient, clamp_workers, fallback_score, keyword_category, parallel_json
from auto_research.markdown import paper_markdown
from auto_research.models import AppConfig, FindRequest
from auto_research.jobs import JobCancelled
from auto_research.storage import create_run_dir, redacted_config, sync_latest, update_manifest, write_json, write_text

from .catalog import catalog_by_id
from .category_select import filter_papers_by_selected_categories, select_relevant_categories
from .local_cache import load_cached_venue_year, write_venue_year_cache
from .local_rank import rank_papers_tfidf
from .profile_normalize import normalize_user_profile, profile_retrieval_text
from .quality import attach_quality_metadata_many
from .sources import (
    enrich_with_semantic_scholar,
    enrich_pmlr_details,
    fetch_arxiv,
    fetch_github_trending,
    fetch_huggingface,
    enrich_nature_details,
    enrich_science_details,
    fetch_nature_portfolio,
    fetch_science_family,
    fetch_selected_venue_details,
    fetch_venue_title_index_all,
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


def _quality_bonus_for_fit(item: dict) -> float:
    fit = _as_float(item.get("fit_score"))
    if fit < 6.0:
        return 0.0
    return max(0.0, min(0.4, _as_float(item.get("quality_bonus_available"))))


def _set_rank_score(item: dict) -> None:
    item["quality_bonus"] = _quality_bonus_for_fit(item)
    item["score"] = round(_combined_score(item.get("fit_score"), item.get("diversity_score")) + item["quality_bonus"], 2)


def _cap_diversity_for_fit(fit_score: object, diversity_score: object) -> float:
    fit = max(0.0, min(10.0, _as_float(fit_score)))
    diversity = max(0.0, min(10.0, _as_float(diversity_score)))
    if fit < 6.0:
        return min(diversity, 4.0)
    if fit < 7.0:
        return min(diversity, 6.0)
    if fit < 8.0:
        return min(diversity, 8.0)
    return diversity


def _apply_relevance_guard(item: dict) -> None:
    text = f"{item.get('category', '')} {item.get('reason', '')} {item.get('fit_explanation', '')}".lower()
    irrelevant_markers = ["不相关", "无关", "irrelevant", "not relevant", "unrelated"]
    if any(marker in text for marker in irrelevant_markers):
        item["fit_score"] = min(_as_float(item.get("fit_score")), 1.5)
        item["diversity_score"] = min(_as_float(item.get("diversity_score")), 1.0)
        item["quality_bonus"] = 0.0
        item["score"] = min(_as_float(item.get("score")), 1.5)
        item["filter2_decision"] = "reject"


def _scan_count(total: int, config: AppConfig) -> int:
    fraction = max(0.01, min(1.0, float(config.venue_title_scan_fraction or 1.0)))
    return max(1, min(total, int(total * fraction) or 1))


def _normalize_filter2_decision(value: object, fit_score: object) -> str:
    decision = str(value or "").strip().lower()
    aliases = {
        "accept": "keep",
        "accepted": "keep",
        "selected": "keep",
        "select": "keep",
        "maybe": "uncertain",
        "neutral": "uncertain",
        "plausible": "uncertain",
        "drop": "reject",
        "rejected": "reject",
        "irrelevant": "reject",
    }
    decision = aliases.get(decision, decision)
    if decision in {"keep", "uncertain", "reject"}:
        return decision
    fit = _as_float(fit_score)
    if fit >= 6.0:
        return "keep"
    if fit >= 4.0:
        return "uncertain"
    return "reject"


def _filter2_group_key(item: dict) -> tuple[str, int, str]:
    return (
        str(item.get("venue") or item.get("source") or ""),
        int(item.get("year") or 0),
        _paper_category(item) or str(item.get("metadata", {}).get("article_type") or "(uncategorized)"),
    )


def _apply_filter2_safety_cap(candidates: list[dict], limit: int | None, groups: list[dict], log: LogFn, source_name: str) -> list[dict]:
    kept = [item for item in candidates if item.get("filter2_decision") == "keep"]
    uncertain = [item for item in candidates if item.get("filter2_decision") == "uncertain"]
    kept.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
    uncertain.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
    budget = int(limit or 0)
    if budget <= 0:
        return kept + uncertain
    if len(kept) >= budget:
        log(f"{source_name}: Filter 2 safety cap kept {budget}/{len(kept)} strong title matches")
        return kept[:budget]

    remaining = budget - len(kept)
    if not groups:
        return kept + uncertain[:remaining]

    selected_uncertain: list[dict] = []
    uncertain_by_group: dict[tuple[str, int, str], list[dict]] = {}
    for item in uncertain:
        uncertain_by_group.setdefault(_filter2_group_key(item), []).append(item)
    group_keys = [
        (str(group["venue"]), int(group["year"]), str(group["category"]))
        for group in groups
    ]
    for key in group_keys:
        if len(selected_uncertain) >= remaining:
            break
        bucket = uncertain_by_group.get(key) or []
        if bucket:
            selected_uncertain.append(bucket.pop(0))
    if len(selected_uncertain) < remaining:
        selected_ids = {id(item) for item in selected_uncertain}
        for item in uncertain:
            if len(selected_uncertain) >= remaining:
                break
            if id(item) not in selected_ids:
                selected_uncertain.append(item)
                selected_ids.add(id(item))
    return kept + selected_uncertain


def _prefilter_titles(
    items: list[dict],
    config: AppConfig,
    llm: LLMClient,
    venue_name: str,
    log: LogFn,
    should_cancel: CancelFn,
    progress: ProgressFn = lambda *_args: None,
    *,
    dynamic_title_filter: bool = False,
    result_limit: int | None = None,
    scan_all: bool = False,
    title_filter_reports: list[dict] | None = None,
    filter2_traces: list[dict] | None = None,
) -> list[dict]:
    if not items:
        return []
    interest = "\n".join(part for part in [config.research_interest, config.researcher_profile] if part).strip()
    scanned = list(items if scan_all else items[: _scan_count(len(items), config)])
    title_groups = _title_filter_groups(scanned) if dynamic_title_filter else []
    group_by_id = {
        str(item.get("id") or ""): group
        for group in title_groups
        for item in group["items"]
    }
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
        item["filter2_decision"] = _normalize_filter2_decision(None, item["fit_score"])
        item["filter2_reason"] = item["title_reason"]
        group = group_by_id.get(str(item.get("id") or ""))
        if group:
            item["title_filter_context"] = {
                "venue": group["venue"],
                "year": group["year"],
                "category": group["category"],
                "category_size": group["category_size"],
                "venue_year_total": group["venue_year_total"],
                "category_ratio": group["category_ratio"],
                "strictness": group["policy"]["label"],
                "min_fit_score": group["policy"]["min_score"],
                "keep_ratio": group["policy"]["keep_ratio"],
            }

    if llm.enabled and interest:
        selected: list[dict] = []
        batch_size = 10
        if title_groups:
            batches_with_context = [
                (batch, _title_filter_prompt_context(group))
                for group in title_groups
                for batch in _chunks(group["items"], batch_size)
            ]
        else:
            batches_with_context = [(batch, "") for batch in _chunks(scanned, batch_size)]
        prompts: list[str] = []
        for batch_index, (batch, context) in enumerate(batches_with_context, 1):
            _raise_if_cancelled(should_cancel)
            title_lines = "\n".join(f"- {item.get('id')}: {item.get('title')}" for item in batch)
            context_block = f"\nBatch context:\n{context}\n" if context else ""
            prompts.append(f"""
You are allocating a title-level Filter 2 budget before expensive abstract/PDF fetching.

Research interest/profile:
{interest}
{context_block}

Paper titles, batch {batch_index}/{len(batches_with_context)}:
{title_lines}

Return strict JSON:
{{"decisions":[{{"id":"paper id","decision":"keep|uncertain|reject","fit_score":0-10,"diversity_score":0-10,"hit_directions":["direction"],"category":"short category","reason":"one concise Chinese reason"}}]}}

Rules:
- This is a cheap Filter 2 budget allocation step before expensive detail/abstract fetching.
- Use keep for titles that clearly hit the research interest or researcher profile.
- Use uncertain for plausible, neutral, underspecified, acronym-heavy, or broad titles that might become relevant after reading the abstract.
- Use reject only for titles that are clearly mismatched, unrelated, or outside the user's concrete methods, domains, or constraints.
- Generic AI/ML titles are not enough for keep, but may be uncertain if they could plausibly match after abstract review.
- fit_score is the core match to the profile. Use strict scoring: 9-10 exceptional, 7-8 strong, 6 possible, <=5 weak.
- diversity_score only rewards hitting multiple real user directions or adding a complementary method/domain. It cannot rescue low fit.
- Do not reject merely because the title lacks detail.
""")
        workers = clamp_workers(config.llm_concurrency, default=16, maximum=32)
        results = parallel_json(llm, prompts, workers)
        seen: set[str] = set()
        total_batches = len(batches_with_context)
        for batch_index, ((batch, _context), result) in enumerate(zip(batches_with_context, results, strict=False), 1):
            _raise_if_cancelled(should_cancel)
            data = result.get("data")
            if isinstance(data, dict):
                rows = data.get("decisions")
                if not isinstance(rows, list):
                    rows = data.get("selected")
                if isinstance(rows, dict):
                    rows = [rows]
            else:
                rows = None
            if isinstance(rows, list):
                for row in rows:
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
                    item["reason_source"] = "llm filter2"
                    item["filter2_decision"] = _normalize_filter2_decision(row.get("decision"), item.get("fit_score"))
                    item["filter2_reason"] = item["title_reason"]
                    group = group_by_id.get(str(item.get("id") or ""))
                    if group:
                        item["title_filter_context"] = {
                            "venue": group["venue"],
                            "year": group["year"],
                            "category": group["category"],
                            "category_size": group["category_size"],
                            "venue_year_total": group["venue_year_total"],
                            "category_ratio": group["category_ratio"],
                            "strictness": group["policy"]["label"],
                            "min_fit_score": group["policy"]["min_score"],
                            "keep_ratio": group["policy"]["keep_ratio"],
                        }
                    _apply_relevance_guard(item)
                    if item.get("filter2_decision") != "reject":
                        selected.append(item)
                        seen.add(item.get("id", ""))
            pct = round(batch_index / total_batches * 100)
            progress(
                "llm_title_filter",
                batch_index,
                total_batches,
                f"{venue_name}: title filtering {pct}% ({batch_index}/{total_batches} batches), candidates={len(selected)}",
            )
        if selected:
            selected.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
            decision_counts = Counter(str(item.get("filter2_decision") or "") for item in selected)
            log(
                f"{venue_name}: Filter 2 kept {len(selected)} non-rejected candidates from {len(scanned)} scanned titles "
                f"(keep={decision_counts.get('keep', 0)}, uncertain={decision_counts.get('uncertain', 0)})"
            )
            selected_before_prune = len(selected)
            limit = result_limit
            selected = _apply_filter2_safety_cap(selected, limit, title_groups if dynamic_title_filter else [], log, venue_name)
            pruned_count = len(selected)
            if limit and len(selected) >= limit and selected_before_prune > len(selected):
                log(f"{venue_name}: Filter 2 safety cap selected {len(selected)} of {selected_before_prune} non-rejected candidates")
            _append_title_filter_report(
                title_filter_reports,
                venue_name,
                scanned,
                title_groups,
                len(batches_with_context),
                selected_before_prune,
                pruned_count,
                len(selected),
                limit,
                "llm",
            )
            _append_filter2_trace(filter2_traces, venue_name, scanned, selected, limit)
            return selected

    ranked = sorted([item for item in scanned if item.get("filter2_decision") != "reject"], key=lambda row: float(row.get("score") or 0), reverse=True)
    log(f"{venue_name}: fallback Filter 2 kept {len(ranked)} non-rejected titles")
    selected_before_prune = len(ranked)
    limit = result_limit
    final = _apply_filter2_safety_cap(ranked, limit, title_groups if dynamic_title_filter else [], log, venue_name)
    pruned_count = len(final)
    _append_title_filter_report(
        title_filter_reports,
        venue_name,
        scanned,
        title_groups,
        len(_chunks(scanned, 10)),
        selected_before_prune,
        pruned_count,
        len(final),
        limit,
        "fallback",
    )
    _append_filter2_trace(filter2_traces, venue_name, scanned, final, limit)
    return final


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
        item["diversity_score"] = _cap_diversity_for_fit(
            item["fit_score"],
            _as_float(item.get("diversity_score"), max(0.0, fallback - 1.0)),
        )
        _set_rank_score(item)
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
{{"evaluations":[{{"id":"paper id","category":"short category","fit_score":0-10,"diversity_score":0-10,"hit_directions":["direction"],"fit_explanation":"one concise Chinese sentence","reason":"3-5 Chinese sentences"}}]}}

Scoring rules:
- Treat the standardized profile as the only user intent. Do not infer extra interests from venue prestige or general AI popularity.
- Do not adjust fit_score or diversity_score for conference level, presentation status, journal family, or journal rank; deterministic ranking code handles that separately.
- fit_score is the primary score for direct match to the profile, based on explicit evidence in title and abstract/description.
- Score bands for fit_score:
  - 10: exact center of the profile with clear method, domain, and goal match.
  - 9: excellent match to a core direction with a concrete contribution.
  - 8: strong match, but one profile dimension is less explicit or secondary.
  - 7: good adjacent match with a defensible bridge to the profile.
  - 6: minimally recommendable; requires at least one concrete profile match in the title or abstract/description.
  - 4-5: broad field overlap, weak adjacency, or missing key profile dimensions.
  - 3: keyword overlap without real alignment.
  - 0-2: unrelated, excluded, privacy-only, or generic AI with no profile match.
- Adjacent work may score 7-8 only if it offers a concrete method, dataset, benchmark, theory, evaluation setup, or domain mechanism that could advance a stated profile direction. The bridge must be explicit in the reason.
- If an item matches a hard exclusion and does not satisfy a stated exception, assign fit_score <= 2.
- diversity_score rewards multiple explicit user directions, method-domain bridges, or genuinely complementary angles.
- diversity_score cannot compensate for weak fit: if fit_score < 6, diversity_score must be <= 4; if fit_score is 6.x, diversity_score must be <= 6.
- hit_directions must be concrete phrases from the standardized profile or close paraphrases, not generic labels like AI or machine learning.
- Generic AI relevance is not enough. Explain exactly which user directions are hit and how strongly.
- If the evidence is title-only or abstract is missing, avoid scores above 7 unless the title is unambiguously central.
- Be score-stable across the batch: items with similar evidence and similar profile fit should receive similar scores.
- fit_explanation should briefly justify the score. The reason should cover topic, contribution, profile match, and any caveat without over-rationalizing.
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
                        item["diversity_score"] = _cap_diversity_for_fit(
                            item["fit_score"],
                            _as_float(row.get("diversity_score"), item.get("diversity_score") or 0),
                        )
                        _set_rank_score(item)
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


def _pmlr_detail_url(item: dict) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    source_records = metadata.get("source_records") if isinstance(metadata.get("source_records"), dict) else {}
    pmlr_record = source_records.get("pmlr") if isinstance(source_records.get("pmlr"), dict) else {}
    return str(item.get("url") or metadata.get("pmlr_url") or pmlr_record.get("url") or "")


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


def _paper_category(item: dict) -> str:
    return str(item.get("primary_area") or item.get("category") or item.get("track") or "")


def _title_filter_policy(category_ratio: float) -> dict:
    if category_ratio >= 0.40:
        return {
            "label": "heated",
            "min_score": 7.5,
            "keep_ratio": 0.08,
            "instruction": "This is a crowded/heated category for this venue-year. Be strict: select only titles with a direct, concrete match to the profile.",
        }
    if category_ratio >= 0.20:
        return {
            "label": "moderate",
            "min_score": 7.0,
            "keep_ratio": 0.12,
            "instruction": "This is a moderately crowded category for this venue-year. Select clear matches; avoid broad or weakly related titles.",
        }
    return {
        "label": "niche",
        "min_score": 6.0,
        "keep_ratio": 0.20,
        "instruction": "This is a smaller category for this venue-year. Keep niche matches when they directly support the profile.",
    }


def _title_filter_groups(items: list[dict]) -> list[dict]:
    venue_year_totals: dict[tuple[str, int], int] = {}
    buckets: dict[tuple[str, int, str], list[dict]] = {}
    order: list[tuple[str, int, str]] = []
    for item in items:
        venue = str(item.get("venue") or "")
        year = int(item.get("year") or 0)
        category = _paper_category(item) or "(uncategorized)"
        venue_year_totals[(venue, year)] = venue_year_totals.get((venue, year), 0) + 1
        key = (venue, year, category)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(item)

    groups: list[dict] = []
    for venue, year, category in order:
        group_items = buckets[(venue, year, category)]
        total = max(1, venue_year_totals.get((venue, year), len(group_items)))
        ratio = len(group_items) / total
        policy = _title_filter_policy(ratio)
        groups.append({
            "key": f"{venue}|{year}|{category}",
            "venue": venue,
            "year": year,
            "category": category,
            "items": group_items,
            "category_size": len(group_items),
            "venue_year_total": total,
            "category_ratio": ratio,
            "policy": policy,
        })
    return groups


def _title_filter_prompt_context(group: dict) -> str:
    ratio_pct = round(float(group["category_ratio"]) * 100, 1)
    return "\n".join([
        f"Venue/year/category: {group['venue']} {group['year']} / {group['category']}",
        f"Category share among category-filtered papers for this venue-year: {ratio_pct}% ({group['category_size']}/{group['venue_year_total']}).",
        f"Dynamic strictness: {group['policy']['label']}.",
        group["policy"]["instruction"],
    ])


def _dynamic_title_prune(selected: list[dict], groups: list[dict], log: LogFn, venue_name: str) -> list[dict]:
    if not selected or not groups:
        return selected
    policies = {group["key"]: group for group in groups}
    selected_by_group: dict[str, list[dict]] = {}
    for item in selected:
        key = f"{item.get('venue') or ''}|{int(item.get('year') or 0)}|{_paper_category(item) or '(uncategorized)'}"
        selected_by_group.setdefault(key, []).append(item)

    pruned: list[dict] = []
    for key, items in selected_by_group.items():
        group = policies.get(key)
        if not group:
            pruned.extend(items)
            continue
        policy = group["policy"]
        items.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
        max_keep = min(50, max(3, ceil(group["category_size"] * policy["keep_ratio"])))
        passed = [item for item in items if _as_float(item.get("fit_score")) >= policy["min_score"]]
        if not passed and items and _as_float(items[0].get("fit_score")) >= 6.0:
            passed = items[:1]
        kept = passed[:max_keep]
        pruned.extend(kept)
        group["title_selected_scored"] = len(items)
        group["after_dynamic_prune"] = len(kept)
        log(
            f"{venue_name}: dynamic title prune {group['year']} / {group['category']} "
            f"ratio={group['category_ratio']:.1%} strictness={policy['label']} "
            f"selected={len(items)} kept={len(kept)}"
        )
    pruned.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
    return pruned


def _append_title_filter_report(
    reports: list[dict] | None,
    venue_name: str,
    scanned: list[dict],
    groups: list[dict],
    batch_count: int,
    selected_before_prune: int,
    selected_after_prune: int,
    final_count: int,
    result_limit: int | None,
    mode: str,
) -> None:
    if reports is None:
        return
    group_rows = []
    selected_counts = Counter(
        (
            str(item.get("venue") or ""),
            int(item.get("year") or 0),
            _paper_category(item) or "(uncategorized)",
        )
        for item in scanned
    )
    for group in groups:
        group_rows.append({
            "venue": group["venue"],
            "year": group["year"],
            "category": group["category"],
            "category_filter_input_papers": selected_counts.get((group["venue"], group["year"], group["category"]), group["category_size"]),
            "venue_year_title_input_papers": group["venue_year_total"],
            "category_ratio": group["category_ratio"],
            "strictness": group["policy"]["label"],
            "min_fit_score": group["policy"]["min_score"],
            "keep_ratio": group["policy"]["keep_ratio"],
            "max_keep": min(50, max(3, ceil(group["category_size"] * group["policy"]["keep_ratio"]))),
            "llm_selected_scored": group.get("title_selected_scored", 0),
            "after_code_side_dynamic_pruning": group.get("after_dynamic_prune", 0),
        })
    reports.append({
        "venue": venue_name,
        "mode": mode,
        "title_filter_input_papers": len(scanned),
        "title_filter_batches": batch_count,
        "llm_selected_scored": selected_before_prune,
        "after_code_side_dynamic_pruning": selected_after_prune,
        "non_rejected_before_safety_cap": selected_before_prune,
        "after_safety_cap": selected_after_prune,
        "post_title_candidate_limit": result_limit,
        "final_title_candidates": final_count,
        "groups": group_rows,
    })


def _append_filter2_trace(
    traces: list[dict] | None,
    source_name: str,
    scanned: list[dict],
    selected: list[dict],
    result_limit: int | None,
) -> None:
    if traces is None:
        return
    selected_ids = {str(item.get("id") or "") for item in selected}
    traces.append({
        "source": source_name,
        "input_count": len(scanned),
        "survivor_count": len(selected),
        "safety_cap": result_limit,
        "items": [
            {
                "id": item.get("id", ""),
                "source": item.get("source", ""),
                "title": item.get("title", ""),
                "venue": item.get("venue", ""),
                "year": item.get("year", ""),
                "category": _paper_category(item),
                "filter2_decision": item.get("filter2_decision", ""),
                "filter2_selected": str(item.get("id") or "") in selected_ids,
                "decision_source": item.get("reason_source", ""),
                "fit_score": item.get("fit_score", 0),
                "diversity_score": item.get("diversity_score", 0),
                "score": item.get("score", 0),
                "reason": item.get("filter2_reason") or item.get("title_reason") or item.get("reason") or "",
                "included_after_safety_cap": str(item.get("id") or "") in selected_ids,
            }
            for item in scanned
        ],
    })


def _snapshot_items(items: list[dict]) -> list[dict]:
    return copy.deepcopy(items)


def _has_category_summary(local: dict) -> bool:
    entries = local.get("category_summary", {}).get("category_summary", [])
    return isinstance(entries, list) and bool(entries)


def _cached_or_fetched_venue_index(
    venue: dict,
    years: list[int],
    config: AppConfig,
    llm: LLMClient,
    log: LogFn,
) -> tuple[list[dict], list[dict], str, bool]:
    combined: list[dict] = []
    reports: list[dict] = []
    used_category_filter = False
    adapters: list[str] = []
    for year in years:
        local = load_cached_venue_year(venue, year)
        cache_status = "hit"
        if not local:
            log(f"{venue.get('name', '')} {year}: local cache miss; fetching full venue/year paper list")
            fetched, adapter = fetch_venue_title_index_all(venue, [year])
            adapters.append(adapter)
            if fetched:
                local = write_venue_year_cache(venue, year, fetched, adapter)
                cache_status = "built"
                log(f"{venue.get('name', '')} {year}: cached {len(fetched)} papers from {adapter}")
            else:
                reports.append({
                    "venue_id": venue.get("id", ""),
                    "venue": venue.get("name", ""),
                    "year": year,
                    "adapter": adapter,
                    "cache_status": "miss",
                    "total_papers": 0,
                    "selected_category_papers": 0,
                    "category_filter_skipped": True,
                    "skip_reason": "No papers fetched from available sources.",
                })
                log(f"{venue.get('name', '')} {year}: no papers fetched from available sources")
                continue
        else:
            adapter = str(local.get("source_adapter") or "local_database")
            adapters.append(adapter)
            log(f"{venue.get('name', '')} {year}: local cache hit with {local['paper_count']} papers ({adapter})")

        if _has_category_summary(local):
            selection = select_relevant_categories(local["category_summary"], config, llm)
            filtered = filter_papers_by_selected_categories(local["papers"], selection)
            used_category_filter = True
            selected_names = [item.get("name", "") for item in selection.get("selected_categories", [])]
            log(f"{venue.get('name', '')} {year}: category scan selected {len(filtered)}/{local['paper_count']} papers from {len(selected_names)} categories")
            combined.extend(filtered)
            reports.append({
                "venue_id": local["venue_id"],
                "venue": venue.get("name", ""),
                "year": year,
                "adapter": adapter,
                "cache_status": cache_status,
                "papers_path": local["papers_path"],
                "category_summary_path": local.get("category_summary_path", ""),
                "source_report_path": local.get("source_report_path", ""),
                "total_papers": local["paper_count"],
                "selected_category_papers": len(filtered),
                "selection": selection,
                "category_filter_skipped": False,
                "title_filter_input_papers": len(filtered),
            })
        else:
            combined.extend(local["papers"])
            reports.append({
                "venue_id": local["venue_id"],
                "venue": venue.get("name", ""),
                "year": year,
                "adapter": adapter,
                "cache_status": cache_status,
                "papers_path": local["papers_path"],
                "category_summary_path": local.get("category_summary_path", ""),
                "source_report_path": local.get("source_report_path", ""),
                "total_papers": local["paper_count"],
                "selected_category_papers": local["paper_count"],
                "selection": None,
                "category_filter_skipped": True,
                "skip_reason": "No category summary available; using full venue/year paper list for title filtering.",
                "title_filter_input_papers": local["paper_count"],
            })
            log(f"{venue.get('name', '')} {year}: no category summary; sending all {local['paper_count']} papers to title filtering")
    adapter_label = "local_cache:" + ",".join(dict.fromkeys(adapters or ["none"]))
    return combined, reports, adapter_label, used_category_filter


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
    stage0_profile, stage0_fallback_used, stage0_error = normalize_user_profile(config, llm)
    stage0_retrieval_text = profile_retrieval_text(stage0_profile)
    effective_config = config.model_copy(update={
        "research_interest": stage0_retrieval_text,
        "researcher_profile": "",
    })
    stage0_result = {
        "profile": stage0_profile,
        "retrieval_text": stage0_retrieval_text,
        "fallback_used": stage0_fallback_used,
        "llm_error": stage0_error,
    }
    write_json(run_dir / "stage0_profile.json", stage0_result)
    log("Stage 0 profile normalization complete")

    catalog = catalog_by_id()
    venue_papers: list[dict] = []
    title_candidates: list[dict] = []
    evaluated_candidates: list[dict] = []
    source_status: list[dict] = []
    venue_health_report: list[dict] = []
    category_scan_report: list[dict] = []
    title_filter_report: list[dict] = []
    venue_filter1_items: list[dict] = []
    filter2_trace: list[dict] = []
    filter2_survivors: list[dict] = []
    enriched_pre_filter3: list[dict] = []
    arxiv_raw_items: list[dict] = []
    arxiv_raw_snapshot: list[dict] = []
    arxiv_prefiltered_items: list[dict] = []
    arxiv_prefiltered_snapshot: list[dict] = []
    arxiv_prefilter_report: dict = {}
    nature_items: list[dict] = []
    nature_raw_items: list[dict] = []
    nature_raw_snapshot: list[dict] = []
    science_items: list[dict] = []
    science_raw_items: list[dict] = []
    science_raw_snapshot: list[dict] = []
    hf_raw_items: list[dict] = []
    hf_raw_snapshot: list[dict] = []
    github_raw_items: list[dict] = []
    github_raw_snapshot: list[dict] = []

    title_scan_limit = max(1, config.venue_title_scan_limit)
    progress("venue_title_index", 0, max(1, len(request.selection.venue_ids)), "Starting venue title index fetch")
    for venue_index, venue_id in enumerate(request.selection.venue_ids, 1):
        _raise_if_cancelled(should_cancel)
        venue = catalog.get(venue_id)
        if not venue:
            log(f"Skipping unknown venue id: {venue_id}")
            continue

        log(f"Loading full venue/year paper list for {venue.get('name')} years {request.selection.years}")
        progress("venue_title_index", venue_index, len(request.selection.venue_ids), f"Loading venue cache: {venue.get('name')}")
        title_index, reports, adapter, used_category_filter = _cached_or_fetched_venue_index(
            venue,
            request.selection.years,
            effective_config,
            llm,
            log,
        )
        category_scan_report.extend(reports)
        venue_health_report.append({
            "venue_id": venue_id,
            "venue": venue.get("name"),
            "years": request.selection.years,
            "adapter": adapter,
            "sample_count": len(title_index),
            "ok": bool(title_index),
            "error": "" if title_index else "No papers available in local cache or online sources.",
            "suggested_fix": "" if title_index else "Check venue source address or add a dedicated proceedings adapter.",
            "cache_enabled": True,
            "category_filter_used": used_category_filter,
        })
        if not title_index:
            log(f"{venue.get('name')}: no papers available after cache/source lookup via {adapter}")
            continue
        venue_filter1_items.extend(title_index)
        log(f"{venue.get('name')}: loaded {len(title_index)} title candidates via {adapter}")
        selected_titles = _prefilter_titles(
            title_index,
            effective_config,
            llm,
            venue.get("name", venue_id),
            log,
            should_cancel,
            progress,
            dynamic_title_filter=used_category_filter,
            result_limit=title_scan_limit,
            scan_all=True,
            title_filter_reports=title_filter_report,
            filter2_traces=filter2_trace,
        )
        title_candidates.extend(selected_titles)
        filter2_survivors.extend(_snapshot_items(selected_titles))
        _raise_if_cancelled(should_cancel)
        progress("detail_fetch", 0, max(1, len(selected_titles)), f"{venue.get('name')}: fetching selected paper details")
        detailed = fetch_selected_venue_details(selected_titles)
        progress("detail_fetch", len(selected_titles), max(1, len(selected_titles)), f"{venue.get('name')}: detail fetch complete")
        if any("proceedings.mlr.press" in _pmlr_detail_url(item) for item in detailed):
            progress("pmlr_detail_enrichment", 0, len(detailed), f"{venue.get('name')}: enriching PMLR details")
            detailed, pmlr_stats = enrich_pmlr_details(detailed)
            log(
                f"{venue.get('name')}: PMLR detail enrichment filled abstracts "
                f"{pmlr_stats['abstracts_filled']}/{pmlr_stats['attempted']}, "
                f"urls {pmlr_stats['urls_filled']}/{pmlr_stats['attempted']}, "
                f"pdfs {pmlr_stats['pdfs_filled']}/{pmlr_stats['attempted']}"
            )
            progress("pmlr_detail_enrichment", len(detailed), len(detailed), f"{venue.get('name')}: PMLR detail enrichment complete")
        if any(not item.get("abstract") for item in detailed):
            missing_before = sum(not item.get("abstract") for item in detailed[: min(20, len(detailed))])
            progress("abstract_enrichment", 0, min(20, len(detailed)), f"{venue.get('name')}: enriching abstracts")
            detailed = enrich_with_semantic_scholar(detailed, limit=min(20, len(detailed)))
            missing_after = sum(not item.get("abstract") for item in detailed[: min(20, len(detailed))])
            log(f"{venue.get('name')}: Semantic Scholar enrichment filled abstracts {missing_before - missing_after}/{missing_before}")
            progress("abstract_enrichment", min(20, len(detailed)), min(20, len(detailed)), f"{venue.get('name')}: abstract enrichment complete")
        detailed = attach_quality_metadata_many(detailed)
        venue_papers.extend(detailed)
        enriched_pre_filter3.extend(_snapshot_items(detailed))
    title_candidates = _dedupe_items(title_candidates)
    filter2_survivors = _dedupe_items(filter2_survivors)
    venue_papers = _dedupe_items(venue_papers)
    if request.selection.venue_ids:
        source_status.append(_source_status("venues", bool(venue_papers), len(venue_papers), "ok" if venue_papers else "No venue papers fetched. See venue_health_report.json."))

    evaluated_candidates = _evaluate_items(venue_papers, effective_config, llm, "articles", log, should_cancel, progress)
    article_items = _recommended(evaluated_candidates, config)

    if request.selection.include_nature:
        _raise_if_cancelled(should_cancel)
        log(f"Fetching Nature Portfolio journals: {', '.join(config.nature_journals)}")
        progress("nature", 0, 1, "Fetching Nature Portfolio")
        nature_raw_items, nature_status = fetch_nature_portfolio(
            config.nature_journals,
            config.nature_article_types,
            start_date=config.nature_start_date,
            end_date=config.nature_end_date,
            enrich_details=False,
        )
        nature_raw_snapshot = _snapshot_items(nature_raw_items)
        source_status.append(nature_status)
        nature_title_candidates = _prefilter_titles(
            nature_raw_items,
            effective_config,
            llm,
            "Nature Portfolio",
            log,
            should_cancel,
            progress,
            result_limit=config.nature_candidate_limit,
            scan_all=True,
            title_filter_reports=title_filter_report,
            filter2_traces=filter2_trace,
        )
        title_candidates.extend(nature_title_candidates)
        filter2_survivors.extend(_snapshot_items(nature_title_candidates))
        progress("nature_detail_enrichment", 0, max(1, len(nature_title_candidates)), "Nature Portfolio: enriching selected article details")
        nature_detailed_items, nature_detail_stats = enrich_nature_details(nature_title_candidates, limit=len(nature_title_candidates))
        nature_status["detail_enrichment"] = nature_detail_stats
        progress("nature_detail_enrichment", len(nature_title_candidates), max(1, len(nature_title_candidates)), "Nature Portfolio: detail enrichment complete")
        nature_detailed_items = attach_quality_metadata_many(nature_detailed_items)
        enriched_pre_filter3.extend(_snapshot_items(nature_detailed_items))
        nature_evaluated = _evaluate_items(nature_detailed_items, effective_config, llm, "nature", log, should_cancel, progress)
        nature_items = nature_evaluated[: config.max_recommended_papers]
        evaluated_candidates.extend(nature_evaluated)
        article_items.extend(nature_items)
        article_items = _recommended(article_items, config)
        progress("nature", 1, 1, "Nature Portfolio complete")

    if request.selection.include_science:
        _raise_if_cancelled(should_cancel)
        log(f"Fetching Science Family journals: {', '.join(config.science_journals)}")
        progress("science", 0, 1, "Fetching Science Family")
        science_raw_items, science_status = fetch_science_family(
            config.science_journals,
            config.science_article_types,
            start_date=config.science_start_date,
            end_date=config.science_end_date,
        )
        science_raw_snapshot = _snapshot_items(science_raw_items)
        source_status.append(science_status)
        science_title_candidates = _prefilter_titles(
            science_raw_items,
            effective_config,
            llm,
            "Science Family",
            log,
            should_cancel,
            progress,
            result_limit=config.science_candidate_limit,
            scan_all=True,
            title_filter_reports=title_filter_report,
            filter2_traces=filter2_trace,
        )
        title_candidates.extend(science_title_candidates)
        filter2_survivors.extend(_snapshot_items(science_title_candidates))
        progress("science_detail_enrichment", 0, max(1, len(science_title_candidates)), "Science Family: enriching selected article details")
        science_detailed_items, science_detail_stats = enrich_science_details(science_title_candidates, limit=len(science_title_candidates))
        science_status["detail_enrichment"] = science_detail_stats
        progress("science_detail_enrichment", len(science_title_candidates), max(1, len(science_title_candidates)), "Science Family: detail enrichment complete")
        science_detailed_items = attach_quality_metadata_many(science_detailed_items)
        enriched_pre_filter3.extend(_snapshot_items(science_detailed_items))
        science_evaluated = _evaluate_items(science_detailed_items, effective_config, llm, "science", log, should_cancel, progress)
        science_items = science_evaluated[: config.max_recommended_papers]
        evaluated_candidates.extend(science_evaluated)
        article_items.extend(science_items)
        article_items = _recommended(article_items, config)
        progress("science", 1, 1, "Science Family complete")

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
        arxiv_raw_items = arxiv_items
        arxiv_raw_snapshot = _snapshot_items(arxiv_raw_items)
        query_text = stage0_retrieval_text
        arxiv_prefiltered_items, arxiv_prefilter_report = rank_papers_tfidf(
            arxiv_items,
            query_text,
            per_category_limit=config.arxiv_llm_candidates_per_category,
            global_limit=config.arxiv_llm_candidate_limit,
        )
        arxiv_prefiltered_snapshot = _snapshot_items(arxiv_prefiltered_items)
        arxiv_status["raw_count"] = len(arxiv_raw_items)
        arxiv_status["prefiltered_count"] = len(arxiv_prefiltered_items)
        arxiv_status["prefilter"] = arxiv_prefilter_report
        log(f"arXiv: fetched {len(arxiv_raw_items)} raw records; TF-IDF shortlisted {len(arxiv_prefiltered_items)} for LLM scoring")
        source_status.append(arxiv_status)
        arxiv_evaluated = _evaluate_items(arxiv_prefiltered_items, effective_config, llm, "arxiv", log, should_cancel, progress)
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
        hf_raw_items = hf_papers + hf_models
        hf_raw_snapshot = _snapshot_items(hf_raw_items)
        source_status.append(hf_status)
        hf_items = _evaluate_items(hf_raw_items, effective_config, llm, "huggingface", log, should_cancel, progress)[: config.max_recommended_papers]
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
        github_raw_items = github_raw
        github_raw_snapshot = _snapshot_items(github_raw_items)
        source_status.append(github_status)
        github_items = _evaluate_items(
            github_raw_items,
            effective_config,
            llm,
            "github",
            log,
            should_cancel,
            progress,
        )[: config.max_recommended_papers]
        progress("github", 1, 1, "GitHub complete")

    _raise_if_cancelled(should_cancel)
    filter2_survivors = _dedupe_items(filter2_survivors)
    title_candidates = filter2_survivors

    artifacts = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "title_candidates": title_candidates,
        "evaluated_candidates": evaluated_candidates,
        "screened_ranking": _screened_ranking(evaluated_candidates),
        "articles": article_items,
        "nature": nature_items,
        "science": science_items,
        "huggingface": hf_items,
        "github": github_items,
        "stage0_profile": stage0_result,
        "source_status": source_status,
        "venue_health_report": venue_health_report,
        "category_scan_report": category_scan_report,
        "title_filter_report": title_filter_report,
        "venue_filter1": venue_filter1_items,
        "filter2_trace": filter2_trace,
        "filter2_survivors": filter2_survivors,
        "enriched_pre_filter3": enriched_pre_filter3,
        "arxiv_raw": arxiv_raw_snapshot,
        "arxiv_prefiltered": arxiv_prefiltered_snapshot,
        "arxiv_prefilter_report": arxiv_prefilter_report,
        "nature_raw": nature_raw_snapshot,
        "science_raw": science_raw_snapshot,
        "huggingface_raw": hf_raw_snapshot,
        "github_raw": github_raw_snapshot,
    }
    write_json(run_dir / "find_results.json", artifacts)
    write_json(run_dir / "venue_health_report.json", {"run_id": run_id, "results": venue_health_report})
    write_json(run_dir / "category_scan_report.json", {"run_id": run_id, "results": category_scan_report})
    write_json(run_dir / "title_filter_report.json", {"run_id": run_id, "results": title_filter_report})
    write_json(run_dir / "venue_filter1.json", {"run_id": run_id, "results": venue_filter1_items})
    write_json(run_dir / "filter2_trace.json", {"run_id": run_id, "results": filter2_trace})
    write_json(run_dir / "filter2_survivors.json", {"run_id": run_id, "results": filter2_survivors})
    write_json(run_dir / "enriched_pre_filter3.json", {"run_id": run_id, "results": enriched_pre_filter3})
    write_json(run_dir / "stage0_profile.json", stage0_result)
    write_json(run_dir / "arxiv_raw.json", {"run_id": run_id, "results": arxiv_raw_snapshot})
    write_json(run_dir / "arxiv_prefiltered.json", {"run_id": run_id, "results": arxiv_prefiltered_snapshot, "report": arxiv_prefilter_report})
    write_json(run_dir / "nature_raw.json", {"run_id": run_id, "results": nature_raw_snapshot})
    write_json(run_dir / "science_raw.json", {"run_id": run_id, "results": science_raw_snapshot})
    write_json(run_dir / "huggingface_raw.json", {"run_id": run_id, "results": hf_raw_snapshot})
    write_json(run_dir / "github_raw.json", {"run_id": run_id, "results": github_raw_snapshot})

    article_md = paper_markdown(article_items, "Recommended Articles")
    nature_md = paper_markdown(nature_items, "Nature Portfolio Articles")
    science_md = paper_markdown(science_items, "Science Family Articles")
    hf_md = paper_markdown(hf_items, "HuggingFace Papers and Models")
    github_md = paper_markdown(github_items, "GitHub Trending Repositories")
    status_md = _status_markdown(source_status)
    write_text(run_dir / "article.md", article_md)
    write_text(run_dir / "nature.md", nature_md)
    write_text(run_dir / "science.md", science_md)
    write_text(run_dir / "hf.md", hf_md)
    write_text(run_dir / "github.md", github_md)
    write_text(run_dir / "source_status.md", status_md)
    sync_latest("auto_find", "article.md", run_dir / "article.md")
    sync_latest("auto_find", "nature.md", run_dir / "nature.md")
    sync_latest("auto_find", "science.md", run_dir / "science.md")
    sync_latest("auto_find", "hf.md", run_dir / "hf.md")
    sync_latest("auto_find", "github.md", run_dir / "github.md")
    sync_latest("auto_find", "source_status.md", run_dir / "source_status.md")
    update_manifest(run_dir, "find")
    log("Find stage complete")
    return artifacts
