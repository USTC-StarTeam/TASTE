from __future__ import annotations

import json
import os
from typing import Any

from auto_research.llm import LLMClient, fallback_score
from auto_research.models import AppConfig


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

    if _use_llm_category_select(config) and llm.enabled and interest and entries:
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
                selected = llm_selected[:max_categories]
                rejected = _build_rejected(entries, selected, data.get("rejected_categories"))
                fallback_used = False
                selection_mode = "llm_adaptive_category_select"
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
