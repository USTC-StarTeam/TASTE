from __future__ import annotations


# ---- category selection ----

# ---- category_select.py ----

import hashlib
import json
import os
from typing import Any

from finding_runtime import LLMClient, fallback_score
from finding_runtime import AppConfig
from finding_runtime import STATE_DIR
from finding_runtime import read_json_safely, write_json_cache


def _interest_text(config: AppConfig) -> str:
    return "\n".join(part for part in [config.research_topic, config.research_interest, config.researcher_profile] if part).strip()


def _category_entries(category_summary: dict[str, Any]) -> list[dict[str, Any]]:
    entries = category_summary.get("category_summary", [])
    if not isinstance(entries, list):
        return []
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        result.append({
            "name": name,
            "count": int(entry.get("count") or 0),
            "sample_titles": [str(item) for item in (entry.get("sample_titles") or [])[:5]],
            "sample_keywords": [str(item) for item in (entry.get("sample_keywords") or [])[:20]],
        })
    return result


def _compact_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Backward-compatible compact category view for support-module imports."""
    return [
        {
            "name": entry["name"],
            "sample_titles": entry["sample_titles"],
            "sample_keywords": entry["sample_keywords"],
        }
        for entry in entries
    ]


CATEGORY_SELECT_TEMPERATURE = 0.0
CATEGORY_SELECT_CACHE_SCHEMA_VERSION = "find_category_select_cache_v8_alias_ranking_with_fallback"
CATEGORY_SELECT_CACHE_MAX_ENTRIES = 2000
CATEGORY_SELECTION_PAPER_TARGET = 1000


def _use_llm_category_select(config: AppConfig | None = None) -> bool:
    disabled = os.environ.get("DISABLE_LLM_CATEGORY_SELECT")
    if disabled is not None and disabled.lower() in {"1", "true", "yes", "on"}:
        return False
    value = os.environ.get("USE_LLM_CATEGORY_SELECT")
    if value is not None:
        return value.lower() in {"1", "true", "yes", "on", "force"}
    provider = str(getattr(config, "provider", "") or "").lower()
    return provider not in {"", "mock"}


def _json_or_none(llm: LLMClient, prompt: str, *, temperature: float | None = None) -> Any | None:
    if not hasattr(llm, "json_or_none"):
        return None
    try:
        return llm.json_or_none(prompt, temperature=temperature)
    except TypeError:
        return llm.json_or_none(prompt)


def _category_json_or_error(
    llm: LLMClient,
    prompt: str,
    *,
    temperature: float | None = None,
    max_tokens: int = 4000,
) -> tuple[Any | None, str]:
    try:
        if hasattr(llm, "json_or_error"):
            try:
                result = llm.json_or_error(prompt, temperature=temperature, max_tokens=max_tokens)
            except TypeError:
                result = llm.json_or_error(prompt)
            if isinstance(result, dict) and result.get("ok") and isinstance(result.get("data"), dict):
                return result["data"], ""
            if isinstance(result, dict):
                return None, str(result.get("error") or "LLM did not return valid JSON.")
            return None, "LLM did not return a structured result."
        data = _json_or_none(llm, prompt, temperature=temperature)
        return (data, "") if isinstance(data, dict) else (None, "LLM did not return valid JSON.")
    except Exception as exc:
        return None, str(exc) or "LLM category ranking request failed."


def _category_select_cache_enabled(config: AppConfig | None = None) -> bool:
    disabled = os.environ.get("DISABLE_FIND_CATEGORY_SELECT_CACHE")
    if disabled is not None and disabled.lower() in {"1", "true", "yes", "on"}:
        return False
    explicit = os.environ.get("USE_FIND_CATEGORY_SELECT_CACHE")
    if explicit is not None:
        return explicit.lower() in {"1", "true", "yes", "on", "force"}
    return False


def _category_select_cache_path(config: AppConfig | None = None) -> Any | None:
    if not _category_select_cache_enabled(config):
        return None
    return STATE_DIR / "find_category_selection_cache.json"


def _normalized_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _llm_identity(llm: LLMClient) -> dict[str, Any]:
    summary = llm.summary() if hasattr(llm, "summary") else {}
    if not isinstance(summary, dict):
        summary = {}
    return {
        "role": str(summary.get("role") or getattr(llm, "role", "") or ""),
        "provider": str(summary.get("provider") or getattr(llm, "provider", "") or ""),
        "base_url": str(summary.get("base_url") or getattr(llm, "base_url", "") or ""),
        "model": str(summary.get("model") or getattr(llm, "model", "") or ""),
        "api_mode": str(summary.get("api_mode") or getattr(llm, "api_mode", "") or ""),
    }


def _cache_entries_fingerprint(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted = []
    for entry in entries:
        compacted.append({
            "name": _normalized_text(entry.get("name")),
            "count": int(entry.get("count") or 0),
            "sample_titles": [_normalized_text(item) for item in (entry.get("sample_titles") or [])[:5]],
            "sample_keywords": [_normalized_text(item) for item in (entry.get("sample_keywords") or [])[:20]],
        })
    return compacted


def _category_select_cache_key(
    category_summary: dict[str, Any],
    config: AppConfig,
    llm: LLMClient,
    entries: list[dict[str, Any]],
    max_categories: int,
) -> str:
    payload = {
        "schema": CATEGORY_SELECT_CACHE_SCHEMA_VERSION,
        "temperature": CATEGORY_SELECT_TEMPERATURE,
        "max_categories": int(max_categories),
        "interest": _normalized_text(_interest_text(config)),
        "venue_id": _normalized_text(category_summary.get("venue_id")),
        "venue": _normalized_text(category_summary.get("venue")),
        "year": _normalized_text(category_summary.get("year")),
        "paper_count": int(category_summary.get("paper_count") or 0),
        "llm": _llm_identity(llm),
        "entries": _cache_entries_fingerprint(entries),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_category_select_cache(config: AppConfig | None = None) -> dict[str, Any]:
    path = _category_select_cache_path(config)
    if path is None:
        return {}
    try:
        payload = read_json_safely(path, {})
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _valid_cached_selection(cached: Any, entries: list[dict[str, Any]], max_categories: int) -> dict[str, Any] | None:
    if not isinstance(cached, dict):
        return None
    if cached.get("schema") != CATEGORY_SELECT_CACHE_SCHEMA_VERSION:
        return None
    ranked_entries = entries[:max_categories]
    valid_names = {entry["name"].lower(): entry["name"] for entry in ranked_entries}
    ranked_categories, useful_through_rank, payload_error = _strict_category_ranking_payload(
        {
            "ranked_categories": cached.get("ranked_categories"),
            "useful_through_rank": cached.get("useful_through_rank"),
        },
        valid_names,
        len(ranked_entries),
    )
    if payload_error:
        return None
    selected, selected_count, useful_category_paper_count = _select_ranked_categories_until_target(
        ranked_entries,
        ranked_categories,
        useful_through_rank,
    )
    rejected = _build_rejected(entries, selected)
    return {
        "selected_categories": selected,
        "rejected_categories": rejected,
        "selected_paper_count": selected_count,
        "ranked_categories": ranked_categories,
        "useful_through_rank": useful_through_rank,
        "useful_category_paper_count": useful_category_paper_count,
    }


def _store_category_select_cache_entry(config: AppConfig, cache_key: str, selection: dict[str, Any]) -> None:
    path = _category_select_cache_path(config)
    if path is None or not cache_key:
        return
    payload = _load_category_select_cache(config)
    entries = payload.get("entries") if isinstance(payload.get("entries"), dict) else {}
    entries[cache_key] = {
        "schema": CATEGORY_SELECT_CACHE_SCHEMA_VERSION,
        "selected_categories": selection.get("selected_categories") or [],
        "rejected_categories": selection.get("rejected_categories") or [],
        "ranked_categories": selection.get("ranked_categories") or [],
        "useful_through_rank": selection.get("useful_through_rank"),
    }
    if len(entries) > CATEGORY_SELECT_CACHE_MAX_ENTRIES:
        keys = list(entries.keys())[-CATEGORY_SELECT_CACHE_MAX_ENTRIES:]
        entries = {key: entries[key] for key in keys}
    payload = {
        "schema": CATEGORY_SELECT_CACHE_SCHEMA_VERSION,
        "entries": entries,
    }
    try:
        write_json_cache(path, payload, merge_existing=True)
    except Exception:
        return


def _normalize_selected_rows(rows: Any, valid_names: dict[str, str]) -> list[dict[str, str]]:
    if not isinstance(rows, list):
        return []
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            name = row
            reason = ""
        elif isinstance(row, dict):
            name = str(row.get("name") or row.get("category") or "").strip()
            reason = str(row.get("reason") or "").strip()
        else:
            continue
        canonical = valid_names.get(name.lower())
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        selected.append({"name": canonical, "reason": reason})
    return selected


def _strict_category_ranking_payload(
    data: Any,
    valid_names: dict[str, str],
    expected_count: int,
) -> tuple[list[str], int, str]:
    required_keys = {"ranked_categories", "useful_through_rank"}
    if not isinstance(data, dict) or set(data) != required_keys:
        return [], 0, "LLM category ranking must contain exactly ranked_categories and useful_through_rank."
    rows = data.get("ranked_categories")
    if not isinstance(rows, list) or len(rows) != expected_count or any(type(row) is not str for row in rows):
        actual = len(rows) if isinstance(rows, list) else 0
        return [], 0, f"LLM returned an incomplete category ranking ({actual}/{expected_count}) or a non-string category."
    ranked = [row for row in rows]
    canonical_names = set(valid_names.values())
    if any(name != name.strip() or name not in canonical_names for name in ranked):
        return [], 0, "LLM category ranking must use exact category names without unknown names or extra whitespace."
    if len(set(ranked)) != expected_count or set(ranked) != canonical_names:
        return [], 0, "LLM category ranking must include every category exactly once without duplicates."
    useful_through_rank = data.get("useful_through_rank")
    if type(useful_through_rank) is not int or not 0 <= useful_through_rank <= expected_count:
        return [], 0, f"LLM useful_through_rank must be an integer from 0 to {expected_count}."
    return ranked, useful_through_rank, ""


def _category_alias_map(entries: list[dict[str, Any]]) -> dict[str, str]:
    return {f"c{index:03d}": entry["name"] for index, entry in enumerate(entries, 1)}


def _category_prompt_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aliases = _category_alias_map(entries)
    return [
        {
            "id": alias,
            "name": aliases[alias],
            "sample_titles": entry["sample_titles"],
            "sample_keywords": entry["sample_keywords"],
        }
        for alias, entry in zip(aliases, entries, strict=False)
    ]


def _strict_llm_category_ranking_payload(
    data: Any,
    entries: list[dict[str, Any]],
) -> tuple[list[str], int, str]:
    """Validate an LLM ranking while keeping canonical venue names code-owned.

    The preferred contract uses short request-local IDs. Exact-name responses are
    accepted for backward compatibility, with only unambiguous whitespace/case
    normalization; unknown or fuzzy names are never admitted.
    """
    if not isinstance(data, dict):
        return [], 0, "LLM category ranking must be a JSON object."
    useful_through_rank = data.get("useful_through_rank")
    if type(useful_through_rank) is not int or not 0 <= useful_through_rank <= len(entries):
        return [], 0, f"LLM useful_through_rank must be an integer from 0 to {len(entries)}."

    if set(data) == {"ranked_category_ids", "useful_through_rank"}:
        rows = data.get("ranked_category_ids")
        if not isinstance(rows, list) or len(rows) != len(entries):
            actual = len(rows) if isinstance(rows, list) else 0
            return [], 0, f"LLM returned an incomplete category ID ranking ({actual}/{len(entries)})."
        aliases = _category_alias_map(entries)
        normalized_ids: list[str] = []
        for row in rows:
            if isinstance(row, bool):
                return [], 0, "LLM category ranking contains a non-category ID."
            if isinstance(row, int):
                alias = f"c{row:03d}"
            elif isinstance(row, str):
                text = row.strip().lower()
                alias = f"c{int(text):03d}" if text.isdigit() else text
            else:
                return [], 0, "LLM category ranking contains a non-category ID."
            if alias not in aliases:
                return [], 0, "LLM category ranking contains an unknown category ID."
            normalized_ids.append(alias)
        if len(set(normalized_ids)) != len(entries) or set(normalized_ids) != set(aliases):
            return [], 0, "LLM category ranking must include every category ID exactly once."
        return [aliases[alias] for alias in normalized_ids], useful_through_rank, ""

    if set(data) == {"ranked_categories", "useful_through_rank"}:
        valid_names = {entry["name"].lower(): entry["name"] for entry in entries}
        ranked, cutoff, payload_error = _strict_category_ranking_payload(data, valid_names, len(entries))
        if not payload_error:
            return ranked, cutoff, ""
        rows = data.get("ranked_categories")
        if isinstance(rows, list) and len(rows) == len(entries) and all(type(row) is str for row in rows):
            normalized = [valid_names.get(row.strip().lower(), "") for row in rows]
            canonical_names = set(valid_names.values())
            if all(normalized) and len(set(normalized)) == len(entries) and set(normalized) == canonical_names:
                return normalized, useful_through_rank, ""
        return [], 0, payload_error

    return [], 0, "LLM category ranking must contain exactly ranked_category_ids and useful_through_rank."


def _deterministic_category_ranking(
    entries: list[dict[str, Any]],
    config: AppConfig,
) -> tuple[list[str], int]:
    """Return a complete, code-owned high-recall ranking when the LLM is unusable."""
    if not entries:
        return [], 0
    interest = _interest_text(config)
    if interest:
        scored: list[tuple[float, int, int, str]] = []
        for index, entry in enumerate(entries):
            text = " ".join([
                entry["name"],
                " ".join(entry.get("sample_titles") or []),
                " ".join(entry.get("sample_keywords") or []),
            ])
            scored.append((fallback_score(interest, text, ""), int(entry.get("count") or 0), -index, entry["name"]))
        scored.sort(reverse=True)
        ranked = [name for _score, _count, _index, name in scored]
        score_values = {score for score, _count, _index, _name in scored}
    else:
        ranked = [entry["name"] for entry in entries]
        score_values = set()
    try:
        recall_floor = int(os.environ.get("VENUE_CATEGORY_SELECT_MIN_CATEGORIES", "6") or 6)
    except (TypeError, ValueError):
        recall_floor = 6
    if not interest or len(score_values) <= 1:
        return ranked, len(ranked)
    return ranked, max(0, min(len(ranked), recall_floor))


def _select_ranked_categories_until_target(
    entries: list[dict[str, Any]],
    ranked_categories: list[str],
    useful_through_rank: int,
    target_papers: int = CATEGORY_SELECTION_PAPER_TARGET,
    ranking_source: str = "LLM",
) -> tuple[list[dict[str, str]], int, int]:
    count_by_name = {entry["name"]: max(0, int(entry.get("count") or 0)) for entry in entries}
    target = max(1, int(target_papers or CATEGORY_SELECTION_PAPER_TARGET))
    useful_limit = max(0, min(len(ranked_categories), int(useful_through_rank)))
    selected: list[dict[str, str]] = []
    cumulative = 0
    useful_category_paper_count = 0
    for rank, name in enumerate(ranked_categories, 1):
        if name not in count_by_name:
            continue
        is_useful = rank <= useful_limit
        if not is_useful and cumulative >= target:
            break
        count = count_by_name[name]
        cumulative += count
        if is_useful:
            useful_category_paper_count += count
        selected.append({
            "name": name,
            "reason": (
                f"{ranking_source} relevant/useful category rank {rank}; cumulative categorized papers={cumulative}."
                if is_useful
                else f"Minimum-{target} supplement after the {ranking_source} useful cutoff; category rank {rank}; cumulative categorized papers={cumulative}."
            ),
        })
    return selected, cumulative, useful_category_paper_count


def _category_selection_max(entries: list[dict[str, Any]], requested: int) -> int:
    if not entries:
        return 0
    if requested > 0:
        return max(1, min(requested, len(entries)))
    return len(entries)


def _build_rejected(entries: list[dict[str, Any]], selected: list[dict[str, str]], explicit_rejected: Any = None) -> list[dict[str, str]]:
    selected_names = {item["name"] for item in selected}
    valid_names = {entry["name"].lower(): entry["name"] for entry in entries}
    rejected = _normalize_selected_rows(explicit_rejected, valid_names)
    rejected_names = {item["name"] for item in rejected}
    for entry in entries:
        name = entry["name"]
        if name not in selected_names and name not in rejected_names:
            rejected.append({"name": name, "reason": "Not selected for the current research profile."})
    return rejected


def select_relevant_categories(
    category_summary: dict[str, Any],
    config: AppConfig,
    llm: LLMClient,
    max_categories: int = 0,
) -> dict[str, Any]:
    entries = _category_entries(category_summary)
    max_categories = _category_selection_max(entries, max_categories)
    ranked_entries = entries[:max_categories]
    interest = _interest_text(config)

    fallback_used = True
    ranked_categories: list[str] = []
    useful_through_rank = 0
    useful_category_paper_count = 0
    selected: list[dict[str, str]] = []
    selected_count = 0
    rejected: list[dict[str, str]] = []
    llm_error = ""
    selection_mode = "llm_useful_prefix_then_minimum_paper_target"
    ranking_source = "llm"
    cache_key = ""
    cache_eligible = bool(
        entries
        and interest
        and _use_llm_category_select(config)
    )
    use_llm_selection = bool(cache_eligible and getattr(llm, "enabled", False))

    if cache_eligible:
        cache_key = _category_select_cache_key(category_summary, config, llm, entries, max_categories)
        cache_payload = _load_category_select_cache(config)
        cache_entries = cache_payload.get("entries") if isinstance(cache_payload.get("entries"), dict) else {}
        cached_selection = _valid_cached_selection(cache_entries.get(cache_key), entries, max_categories)
        if cached_selection:
            selected = cached_selection["selected_categories"]
            rejected = cached_selection["rejected_categories"]
            ranked_categories = cached_selection["ranked_categories"]
            useful_through_rank = int(cached_selection["useful_through_rank"])
            useful_category_paper_count = int(cached_selection["useful_category_paper_count"])
            selected_count = int(cached_selection.get("selected_paper_count") or 0)
            fallback_used = False
            selection_mode = "llm_useful_prefix_then_minimum_paper_target"
            ranking_source = "llm_cache"

    if fallback_used and use_llm_selection:
        prompt = f"""
You select venue categories for a targeted academic paper scan.

Research interest/profile:
{interest}

Venue: {category_summary.get("venue", "")} {category_summary.get("year", "")}

Available categories as JSON:
{json.dumps(_category_prompt_entries(ranked_entries), ensure_ascii=False)}

Return one JSON object and no Markdown or explanation. It must contain exactly:
- ranked_category_ids: a JSON array containing all {len(ranked_entries)} request-local cNNN IDs, ordered from most to least relevant.
- useful_through_rank: one integer from 0 to {len(ranked_entries)}.

Rules:
- Rank every available category from most to least relevant to the research profile.
- Set useful_through_rank to the inclusive rank of the last category that is relevant or useful for the research topic/profile. The first N categories are the complete useful prefix; categories after N are not relevant or useful. Use 0 only when no category is useful.
- Return only the short cNNN IDs. Category names are context owned by the caller and must not be copied into the output.
- Derive relevance from the current research interest/profile only; do not use a fixed global topic list.
- Judge relevance from the category name plus its sample titles and keywords, not from paper count or venue prestige.
- Include every category ID exactly once. Return {len(ranked_entries)} IDs and one integer useful_through_rank from 0 to {len(ranked_entries)}.
- Do not add scores, reasons, booleans, nested objects, extra keys, duplicate IDs, unknown IDs, or category names.
- Code always keeps the complete useful prefix. Only when that prefix contains fewer than {CATEGORY_SELECTION_PAPER_TARGET} categorized papers will code continue down the same ranking until the minimum is reached. Paper count must not change your relevance ranking or useful cutoff.
"""
        data, request_error = _category_json_or_error(
            llm,
            prompt,
            temperature=CATEGORY_SELECT_TEMPERATURE,
            max_tokens=max(2000, min(8000, len(ranked_entries) * 80)),
        )
        if isinstance(data, dict):
            llm_ranking, llm_useful_through_rank, payload_error = _strict_llm_category_ranking_payload(data, ranked_entries)
        else:
            llm_ranking, llm_useful_through_rank = [], 0
            payload_error = request_error or "LLM did not return valid JSON."
        repaired = False
        if payload_error:
            previous_response = json.dumps(data, ensure_ascii=False) if data is not None else "<no parsed JSON response>"
            repair_prompt = f"""
{prompt}

The previous response failed validation: {payload_error}
Previous parsed response: {previous_response}

Correct the response now. Return only the two required keys, use every valid cNNN ID exactly once, and do not output category names.
"""
            repaired_data, repair_error = _category_json_or_error(
                llm,
                repair_prompt,
                temperature=CATEGORY_SELECT_TEMPERATURE,
                max_tokens=max(2000, min(8000, len(ranked_entries) * 80)),
            )
            if isinstance(repaired_data, dict):
                llm_ranking, llm_useful_through_rank, repaired_error = _strict_llm_category_ranking_payload(
                    repaired_data,
                    ranked_entries,
                )
            else:
                repaired_error = repair_error or "LLM did not return a corrected category ranking."
            if repaired_error:
                llm_error = f"{payload_error}; repair failed: {repaired_error}"
            else:
                payload_error = ""
                repaired = True
        if not payload_error:
            ranked_categories = llm_ranking
            useful_through_rank = llm_useful_through_rank
            selected, selected_count, useful_category_paper_count = _select_ranked_categories_until_target(
                ranked_entries,
                ranked_categories,
                useful_through_rank,
            )
            rejected = _build_rejected(entries, selected)
            fallback_used = False
            selection_mode = "llm_useful_prefix_then_minimum_paper_target"
            ranking_source = "llm_repair" if repaired else "llm"
            if cache_key:
                _store_category_select_cache_entry(config, cache_key, {
                    "selected_categories": selected,
                    "rejected_categories": rejected,
                    "ranked_categories": ranked_categories,
                    "useful_through_rank": useful_through_rank,
                })
        else:
            llm_error = llm_error or payload_error

    if fallback_used:
        if entries and not llm_error and not use_llm_selection:
            llm_error = "Find LLM category ranking was unavailable; used deterministic fallback."
        ranked_categories, useful_through_rank = _deterministic_category_ranking(ranked_entries, config)
        selected, selected_count, useful_category_paper_count = _select_ranked_categories_until_target(
            ranked_entries,
            ranked_categories,
            useful_through_rank,
            ranking_source="Deterministic fallback",
        )
        rejected = _build_rejected(entries, selected)
        selection_mode = "deterministic_adaptive_profile_recall"
        ranking_source = "deterministic_fallback"

    if not rejected:
        rejected = _build_rejected(entries, selected)

    selected_names = {item["name"] for item in selected}
    count_by_name = {entry["name"]: int(entry.get("count") or 0) for entry in entries}
    return {
        "venue_id": category_summary.get("venue_id", ""),
        "venue": category_summary.get("venue", ""),
        "year": category_summary.get("year", ""),
        "paper_count": category_summary.get("paper_count", 0),
        "category_count": len(entries),
        "selected_paper_count": selected_count,
        "category_selection_target_papers": CATEGORY_SELECTION_PAPER_TARGET,
        "category_selection_max": max_categories,
        "category_ranking_source": ranking_source,
        "ranked_categories": ranked_categories,
        "useful_through_rank": useful_through_rank,
        "useful_category_cutoff": ranked_categories[useful_through_rank - 1] if useful_through_rank else "",
        "useful_category_paper_count": useful_category_paper_count,
        "category_ranking": [
            {
                "rank": rank,
                "name": name,
                "paper_count": count_by_name.get(name, 0),
                "llm_relevant_or_useful": rank <= useful_through_rank,
                "minimum_target_supplement": name in selected_names and rank > useful_through_rank,
                "selected": name in selected_names,
            }
            for rank, name in enumerate(ranked_categories, 1)
        ],
        "selected_categories": selected,
        "rejected_categories": rejected,
        "fallback_used": fallback_used,
        "selection_mode": selection_mode,
        "llm_error": llm_error,
    }


def filter_papers_by_selected_categories(papers: list[dict[str, Any]], selection: dict[str, Any]) -> list[dict[str, Any]]:
    selected_names = {str(item.get("name") or "") for item in selection.get("selected_categories", []) if isinstance(item, dict)}
    if not selected_names:
        return []
    return [
        paper
        for paper in papers
        if str(paper.get("primary_area") or paper.get("category") or "").strip() in selected_names
    ]


# Backward-compatible dotted imports for callers that still use the old package layout.
def _register_compat_aliases(*aliases: str) -> None:
    import sys as _sys
    _module = _sys.modules.get(__name__)
    if _module is None:
        return
    globals().setdefault("__path__", [])
    for _alias in aliases:
        _sys.modules.setdefault(_alias, _module)

_register_compat_aliases('selection.category_select')
