from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from auto_research.llm import LLMClient, fallback_score
from auto_research.models import AppConfig
from auto_research.paths import ROOT, WORKFLOW_RUNTIME_DIR
from auto_research.storage import read_json, write_json


def _interest_text(config: AppConfig) -> str:
    return "\n".join(part for part in [config.research_interest, config.researcher_profile] if part).strip()


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
    return [
        {
            "name": entry["name"],
            "count": entry["count"],
            "sample_titles": entry["sample_titles"],
            "sample_keywords": entry["sample_keywords"],
        }
        for entry in entries
    ]


CATEGORY_SELECT_TEMPERATURE = 0.0
CATEGORY_SELECT_CACHE_SCHEMA_VERSION = "find_category_select_cache_v2"
CATEGORY_SELECT_CACHE_MAX_ENTRIES = 2000


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


def _project_cache_dir() -> Any | None:
    project = (os.environ.get("PROJECT_ID") or os.environ.get("DEFAULT_PROJECT_ID") or "").strip()
    if not project:
        return None
    root = Path(os.environ.get("WORKSPACE_ROOT") or ROOT).expanduser()
    return root / "projects" / project / "planning" / "finding" / "cache"


def _category_select_cache_enabled(config: AppConfig | None = None) -> bool:
    disabled = os.environ.get("DISABLE_FIND_CATEGORY_SELECT_CACHE")
    if disabled is not None and disabled.lower() in {"1", "true", "yes", "on"}:
        return False
    explicit = os.environ.get("USE_FIND_CATEGORY_SELECT_CACHE")
    if explicit is not None:
        return explicit.lower() in {"1", "true", "yes", "on", "force"}
    return _project_cache_dir() is not None


def _category_select_cache_path(config: AppConfig | None = None) -> Any | None:
    if not _category_select_cache_enabled(config):
        return None
    project_dir = _project_cache_dir()
    if project_dir is not None:
        return project_dir / "category_selection.json"
    return WORKFLOW_RUNTIME_DIR / "state" / "find_category_selection_cache.json"


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
        payload = read_json(path, {})
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _valid_cached_selection(cached: Any, entries: list[dict[str, Any]], max_categories: int) -> dict[str, Any] | None:
    if not isinstance(cached, dict):
        return None
    if cached.get("schema") != CATEGORY_SELECT_CACHE_SCHEMA_VERSION:
        return None
    valid_names = {entry["name"].lower(): entry["name"] for entry in entries}
    selected = _normalize_selected_rows(cached.get("selected_categories"), valid_names)
    if not selected:
        return None
    selected = selected[:max_categories]
    rejected = _build_rejected(entries, selected, cached.get("rejected_categories"))
    selected_names = {item["name"] for item in selected}
    selected_count = sum(int(entry.get("count") or 0) for entry in entries if entry.get("name") in selected_names)
    return {
        "selected_categories": selected,
        "rejected_categories": rejected,
        "selected_paper_count": selected_count,
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
    }
    if len(entries) > CATEGORY_SELECT_CACHE_MAX_ENTRIES:
        keys = list(entries.keys())[-CATEGORY_SELECT_CACHE_MAX_ENTRIES:]
        entries = {key: entries[key] for key in keys}
    payload = {
        "schema": CATEGORY_SELECT_CACHE_SCHEMA_VERSION,
        "entries": entries,
    }
    try:
        write_json(path, payload)
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



def _min_llm_category_recall(entries: list[dict[str, Any]], max_categories: int) -> int:
    if len(entries) < 6:
        return 0
    try:
        configured = int(os.environ.get("VENUE_CATEGORY_SELECT_MIN_CATEGORIES", "6") or 6)
    except Exception:
        configured = 6
    return max(0, min(max_categories, len(entries), configured))


def _supplement_selected_for_recall(
    selected: list[dict[str, str]],
    entries: list[dict[str, Any]],
    config: AppConfig,
    max_categories: int,
) -> list[dict[str, str]]:
    target = _min_llm_category_recall(entries, max_categories)
    if target <= 0 or len(selected) >= target:
        return selected[:max_categories]
    supplemented = [dict(item) for item in selected[:max_categories]]
    seen = {item["name"] for item in supplemented}
    fallback_rows = _fallback_select(entries, config, max_categories)
    for row in fallback_rows:
        name = row.get("name", "")
        if not name or name in seen:
            continue
        reason = row.get("reason") or "Deterministic high-recall category supplement."
        supplemented.append({"name": name, "reason": f"High-recall deterministic supplement after LLM category selection; {reason}"})
        seen.add(name)
        if len(supplemented) >= target or len(supplemented) >= max_categories:
            return supplemented
    for entry in sorted(entries, key=lambda row: (-int(row.get("count") or 0), str(row.get("name") or ""))):
        name = entry["name"]
        if name in seen:
            continue
        supplemented.append({"name": name, "reason": "High-recall deterministic supplement after LLM category selection."})
        seen.add(name)
        if len(supplemented) >= target or len(supplemented) >= max_categories:
            break
    return supplemented[:max_categories]


def _fallback_select(entries: list[dict[str, Any]], config: AppConfig, max_categories: int) -> list[dict[str, str]]:
    interest = _interest_text(config)
    if not entries:
        return []
    if not interest:
        return [{"name": entry["name"], "reason": "No research profile configured; keeping category for recall."} for entry in entries[:max_categories]]

    scored = []
    for entry in entries:
        text = " ".join([
            entry["name"],
            " ".join(entry.get("sample_titles") or []),
            " ".join(entry.get("sample_keywords") or []),
        ])
        score = fallback_score(interest, text, "")
        scored.append((score, entry))
    scored.sort(key=lambda item: (item[0], item[1].get("count", 0)), reverse=True)
    def row(score: float, entry: dict[str, Any], prefix: str) -> dict[str, str]:
        return {
            "name": entry["name"],
            "reason": f"{prefix}; adaptive_score={round(score, 3)}.",
        }

    return [
        row(score, entry, "High-recall adaptive profile/category fallback")
        for score, entry in scored[:max_categories]
    ]


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
    max_categories: int = 6,
) -> dict[str, Any]:
    entries = _category_entries(category_summary)
    valid_names = {entry["name"].lower(): entry["name"] for entry in entries}
    max_categories = max(1, min(max_categories, len(entries) or 1))
    interest = _interest_text(config)

    fallback_used = True
    selected = _fallback_select(entries, config, max_categories)
    rejected: list[dict[str, str]] = []
    llm_error = ""
    selection_mode = "deterministic_adaptive_profile_recall"
    cache_key = ""
    use_llm_selection = _use_llm_category_select(config) and llm.enabled and interest and entries

    if use_llm_selection:
        cache_key = _category_select_cache_key(category_summary, config, llm, entries, max_categories)
        cache_payload = _load_category_select_cache(config)
        cache_entries = cache_payload.get("entries") if isinstance(cache_payload.get("entries"), dict) else {}
        cached_selection = _valid_cached_selection(cache_entries.get(cache_key), entries, max_categories)
        if cached_selection:
            selected = cached_selection["selected_categories"]
            rejected = cached_selection["rejected_categories"]
            fallback_used = False
            selection_mode = "llm_adaptive_category_select"

    if fallback_used and use_llm_selection:
        prompt = f"""
You select venue categories for a targeted academic paper scan.

Research interest/profile:
{interest}

Venue: {category_summary.get("venue", "")} {category_summary.get("year", "")}
Total papers: {category_summary.get("paper_count", "")}

Available categories as JSON:
{json.dumps(_compact_entries(entries), ensure_ascii=False)}

Return strict JSON:
{{
  "selected_categories": [
    {{"name": "exact category name", "reason": "concise reason"}}
  ],
  "rejected_categories": [
    {{"name": "exact category name", "reason": "concise reason"}}
  ]
}}

Rules:
- Select categories that are likely to contain papers directly useful for the research profile.
- Use exact category names from the available categories list.
- Derive relevance from the current research interest/profile only; do not use a fixed global topic list.
- Prefer recall over precision at this category stage. The final paper-level LLM evidence gate will decide strong recommendations.
- Do not select a category only because it contains generic words like model, benchmark, AI, data, or theory; explain the concrete profile route it may contain.
- Include adjacent categories when the samples suggest they may hide relevant papers whose titles are not obvious.
- Usually select 2-{max_categories} categories unless the profile is very broad.
- Reasons should be brief and specific.
"""
        data = _json_or_none(llm, prompt, temperature=CATEGORY_SELECT_TEMPERATURE)
        if isinstance(data, dict):
            llm_selected = _normalize_selected_rows(data.get("selected_categories"), valid_names)
            if llm_selected:
                selected = _supplement_selected_for_recall(llm_selected[:max_categories], entries, config, max_categories)
                rejected = _build_rejected(entries, selected, data.get("rejected_categories"))
                fallback_used = False
                selection_mode = "llm_adaptive_category_select"
                if cache_key:
                    _store_category_select_cache_entry(config, cache_key, {
                        "selected_categories": selected,
                        "rejected_categories": rejected,
                    })
            else:
                llm_error = "LLM returned no valid selected_categories."
        else:
            llm_error = "LLM did not return valid JSON."

    if not rejected:
        rejected = _build_rejected(entries, selected)

    selected_names = {item["name"] for item in selected}
    selected_count = sum(entry["count"] for entry in entries if entry["name"] in selected_names)
    return {
        "venue_id": category_summary.get("venue_id", ""),
        "venue": category_summary.get("venue", ""),
        "year": category_summary.get("year", ""),
        "paper_count": category_summary.get("paper_count", 0),
        "category_count": len(entries),
        "selected_paper_count": selected_count,
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
        if str(paper.get("primary_area") or paper.get("category") or paper.get("track") or "") in selected_names
    ]
