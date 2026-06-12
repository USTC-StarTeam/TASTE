from __future__ import annotations

import os
import re
import signal
import threading
import time
from collections import Counter
from math import ceil
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timezone
from typing import Any, Callable

from auto_research.llm import LLMClient, clamp_workers, fallback_score, keyword_category, parallel_json
from auto_research.markdown import paper_markdown
from auto_research.models import AppConfig, FindRequest
from auto_research.jobs import JobCancelled
from auto_research.storage import create_run_dir, redacted_config, sync_latest, update_manifest, write_json, write_text

from .catalog import catalog_by_id
from .category_select import filter_papers_by_selected_categories, select_relevant_categories
from .local_index import load_local_venue_year
from .local_rank import rank_papers_tfidf
from .profile_normalize import normalize_user_profile, profile_retrieval_text
from .quality import attach_quality_metadata_many
from .sources import (
    enrich_nature_details,
    enrich_pmlr_details,
    enrich_science_details,
    enrich_with_openalex,
    enrich_with_semantic_scholar,
    enrich_with_arxiv_title_match,
    fetch_arxiv,
    fetch_biorxiv,
    fetch_github_trending,
    fetch_huggingface,
    fetch_nature_portfolio,
    fetch_science_family,
    fetch_selected_venue_details,
    fetch_venue_title_index,
    fetch_venue_title_index_all,
    venue_metadata_audit_from_papers,
)


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]
ProgressFn = Callable[[str, int, int, str], None]
SCORING_POLICY_VERSION = "direct_llm_title_abstract_ranked_topn_v22_find_read_boundary"
FIND_RECOMMENDATION_POLICY = "topn_final_llm_title_abstract_rank_no_score_cutoff_real_abstract_v22"
FIND_FINAL_SCORING_TEMPERATURE = 0.0
STABLE_RANKING_SCORE_POLICY = "audit_only_source_stable_score_v2"
SOURCE_CONTEXT_BONUS_POLICY = "context_bonus_v3_big3_latest_released_venue_citations"
FRESHNESS_BONUS_VENUES = {"ICLR", "ICML", "NEURIPS"}
FIND_FINAL_SCORING_ROUTE_RULES = """
Final Find recommendation contract:
- Use the current research interest/profile only as this run relevance definition. Do not apply a fixed global keyword table or project-specific hard-coded topic list.
- Category selection, title filtering, local TF-IDF rank, source health, citations, and freshness are recall/audit signals only. They must never promote a paper into the user-visible recommendation list.
- A user-visible recommendation must be judged from the real title plus real abstract/description in this final LLM scoring step.
- fit_score is the final title+abstract ranking score. Use the full 0-10 range consistently: 9-10 exact center, 7-8 strong match, 5-6 partial/background usefulness, 3-4 weak/generic, and <=2 unrelated items.
- The workflow selects user-visible recommendations by sorting final-scored rows and taking the configured Top-N; do not treat any absolute score such as 7 as a recommendation cutoff.
- Broad background, inspiration-only, prerequisite-only, or partial-match papers should receive lower fit_score unless the abstract itself gives concrete reusable method/data/protocol/benchmark/evaluation/theory value.
- Do not use venue prestige, citation count, local rank, title-only similarity, diversity_score, or route/foundation/claim labels to raise fit_score.
- Missing abstract, metadata-only evidence, and title-only evidence cannot be recommended because they were not judged from real title+abstract content. Score magnitude affects ranking, not eligibility.
- Do not decide downstream experimental support here. Find recommends papers for Read; later full-text reading, repo/data/env/reproduction, and local experiment gates decide usable evidence scope.
""".strip()

# Core venue release-signal dates for monitored conference paper lists.
# These dates are only a hint for the small freshness bonus. They must never
# block a requested venue-year, because venues can expose accepted papers or
# DBLP proceedings before the conference dates.
KNOWN_CONFERENCE_RELEASE_DATES = {
    ("ICLR", 2026): "2026-04-23",
    ("ICLR", 2025): "2025-04-24",
    ("NEURIPS", 2026): "2026-12-06",
    ("NEURIPS", 2025): "2025-12-02",
    # ICML 2026 was already publicly available on the official ICML site by
    # early May 2026, so it should count as the freshest currently released venue.
    ("ICML", 2026): "2026-05-08",
    ("ICML", 2025): "2025-07-13",
    ("KDD", 2026): "2026-08-09",
    ("KDD", 2025): "2025-08-03",
    ("SIGIR", 2026): "2026-07-20",
    ("SIGIR", 2025): "2025-07-13",
}



def _raise_if_cancelled(should_cancel: CancelFn) -> None:
    if should_cancel():
        raise JobCancelled("Task cancelled by user.")


def _chunks(items: list[dict], size: int) -> list[list[dict]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def _positive_int_env(name: str, default: int = 0) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else int(default)


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def _compact_scoring_interest(config: AppConfig, interest: str) -> str:
    max_chars = _positive_int_env("ABSTRACT_SCORING_PROFILE_MAX_CHARS", 6000)
    text = "\n".join(_topic_interest_chunks(config)).strip() or str(interest or "").strip()
    if not text or len(text) <= max_chars:
        return text
    chunks = _topic_interest_chunks(config)
    selected: list[str] = []
    used = 0
    for chunk in chunks:
        chunk = str(chunk or "").strip()
        if not chunk:
            continue
        cost = len(chunk) + 1
        if selected and used + cost > max_chars:
            break
        selected.append(chunk)
        used += cost
    if selected:
        return "\n".join(selected)[:max_chars].strip()
    return text[:max_chars].strip()


def _adaptive_final_scoring_batch_size(config: AppConfig, scoring_items: list[dict], scoring_interest: str, topic_routes_block: str) -> int:
    env_value = _positive_int_env("ABSTRACT_SCORING_BATCH_SIZE", 0)
    configured_value = int(getattr(config, "abstract_scoring_batch_size", 0) or 0)
    max_batch = _positive_int_env("ABSTRACT_SCORING_MAX_BATCH_SIZE", 8)
    max_batch = _clamp_int(max_batch, 1, 20)
    if env_value > 0:
        return _clamp_int(env_value, 1, max_batch)
    if configured_value > 0:
        return _clamp_int(configured_value, 1, max_batch)
    sample = scoring_items[: min(96, len(scoring_items))]
    if sample:
        avg_item_chars = sum(len(str(item.get("title") or "")) + min(len(str(item.get("abstract") or "")), 650) + 120 for item in sample) / len(sample)
    else:
        avg_item_chars = 650
    prompt_budget = _positive_int_env("ABSTRACT_SCORING_PROMPT_CHAR_BUDGET", 26000)
    fixed_chars = len(scoring_interest or "") + len(topic_routes_block or "") + len(FIND_FINAL_SCORING_ROUTE_RULES) + 2600
    budget_batch = max(2, int((prompt_budget - fixed_chars) // max(450, avg_item_chars)))
    if len(scoring_items) >= 2500:
        target = 8
    elif len(scoring_items) >= 1000:
        target = 6
    elif len(scoring_items) >= 300:
        target = 5
    else:
        target = 4
    return _clamp_int(max(2, min(max_batch, max(target, budget_batch))), 1, max_batch)


def _rate_limited_llm_provider(config: AppConfig) -> bool:
    text = " ".join(str(value or "") for value in [getattr(config, "provider", ""), getattr(config, "base_url", ""), getattr(config, "model", "")]).lower()
    markers = ["sensenova", "xiaomi", "mi.com", "bigmodel.cn"]
    return any(marker in text for marker in markers)


def _adaptive_final_scoring_workers(config: AppConfig, prompt_count: int) -> int:
    env_workers = _positive_int_env("ABSTRACT_SCORING_MAX_WORKERS", 0)
    provider_cap = 2 if _rate_limited_llm_provider(config) else 6
    max_workers = _positive_int_env("ABSTRACT_SCORING_WORKER_CAP", provider_cap)
    max_workers = _clamp_int(max_workers, 1, 32)
    if env_workers > 0:
        return _clamp_int(env_workers, 1, max_workers)
    configured = int(getattr(config, "abstract_scoring_max_workers", 0) or 0)
    if prompt_count >= 128:
        adaptive = 8
    elif prompt_count >= 32:
        adaptive = 6
    else:
        adaptive = 4
    if _rate_limited_llm_provider(config):
        adaptive = min(adaptive, 2)
        configured = min(configured or adaptive, 2)
    # Final abstract scoring has its own provider/rate-limit budget. Do not let
    # broader LLM concurrency silently raise it above the scoring-specific cap.
    return _clamp_int(max(configured, adaptive), 1, max_workers)


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


def _mock_offline_venue_title_index(venue: dict, years: list[int], max_items: int) -> list[dict]:
    if max_items <= 0:
        return []
    venue_name = str(venue.get("name") or venue.get("id") or "Venue")
    venue_id = str(venue.get("id") or venue_name).replace("/", "_")
    year_values = [int(year) for year in years if str(year).isdigit()]
    year = year_values[0] if year_values else date.today().year
    rows = [
        (
            "Structured Retrieval Benchmark Construction for Reliable Agents",
            "This paper studies structured retrieval benchmark construction for autonomous agents. "
            "It introduces a reusable evidence-selection objective, controlled candidate generation protocol, "
            "and benchmark evaluation that connect retrieval planning with audit quality.",
            "retrieval systems",
        ),
        (
            "Language Model Guided Retrieval Agents with Auditable Evidence",
            "This work presents a language model guided retrieval agent that records evidence provenance, "
            "scores candidate papers with title and abstract signals, and evaluates robustness across public datasets. "
            "The method is useful as an offline smoke-test candidate for downstream reading and planning.",
            "machine learning",
        ),
        (
            "Benchmarking Generative Retrieval Pipelines under Noisy User Profiles",
            "The paper proposes a benchmark for generative retrieval pipelines under noisy user profiles. "
            "It compares diffusion-based candidate construction, reranking policies, and reproducible evaluation scripts "
            "so a research workflow can test idea generation without relying on private cached papers.",
            "information retrieval",
        ),
    ]
    papers: list[dict] = []
    for index, (title, abstract, category) in enumerate(rows, 1):
        papers.append({
            "id": f"mock_offline_{venue_id}_{year}_{index}",
            "source": "mock_offline",
            "title": title,
            "authors": "",
            "abstract": abstract,
            "url": f"https://example.test/{venue_id}/{year}/mock-{index}",
            "pdf_url": "",
            "venue": venue_name,
            "year": year,
            "category": category,
            "classification_source": "llm_inferred",
            "fit_score": 9.0 - (index * 0.2),
            "diversity_score": 7.0 - (index * 0.2),
            "reason_source": "mock offline title index",
            "metadata": {
                "venue_id": venue.get("id") or "",
                "offline_sample": True,
                "mock_only": True,
            },
        })
    return papers[:max_items]


def _paper_identity_keys(item: dict) -> list[str]:
    keys: list[str] = []
    for field in ["id", "paper_id", "entry_id", "url", "abs_url", "pdf_url"]:
        value = str(item.get(field) or "").strip().lower()
        if value:
            keys.append(f"{field}:{value}")
    title = " ".join(str(item.get("title") or "").lower().split())
    if title:
        keys.append(f"title:{title}")
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _evaluated_by_identity(evaluated: list[dict]) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    for row in evaluated:
        if isinstance(row, dict):
            for key in _paper_identity_keys(row):
                mapping.setdefault(key, row)
    return mapping




class FatalLLMConfigurationError(RuntimeError):
    """Raised when a configured LLM endpoint/key cannot authenticate."""


def _is_llm_rate_limit_error(error: object) -> bool:
    text = str(error or "").lower()
    rate_limit_tokens = [
        "http 429",
        "429",
        "rate limit",
        "rate_limit",
        "too many requests",
        "rpm exhausted",
        "requests per minute",
        "quota_exceeded_error",
    ]
    hard_quota_tokens = [
        "insufficient_quota",
        "billing hard limit",
        "plan limit exhausted",
        "token plan limit exhausted",
        "account balance",
        "prepaid balance",
    ]
    if any(token in text for token in hard_quota_tokens):
        return False
    return any(token in text for token in rate_limit_tokens)


def _is_fatal_llm_configuration_error(error: object) -> bool:
    text = str(error or "").lower()
    if _is_llm_rate_limit_error(text):
        return False
    return any(
        token in text
        for token in [
            "http 401",
            "http 403",
            "unauthorized",
            "forbidden",
            "invalid api key",
            "invalid_key",
            "please provide valid api key",
            "incorrect api key",
            "authentication",
            "permission_denied",
            "quota_exceeded",
            "quota exceeded",
            "plan limit exhausted",
            "token plan limit exhausted",
            "insufficient_quota",
            "billing hard limit",
        ]
    )


def _fatal_llm_configuration_message(error: object, context: str) -> str:
    detail = str(error or "unknown LLM configuration error").strip()
    return f"LLM configuration error during {context}: {detail[:800]}"


def _raise_if_fatal_llm_configuration_error(error: object, context: str) -> None:
    if _is_fatal_llm_configuration_error(error):
        raise FatalLLMConfigurationError(_fatal_llm_configuration_message(error, context))


def _is_transient_llm_service_error(error: object) -> bool:
    if _is_fatal_llm_configuration_error(error):
        return False
    text = str(error or "").lower()
    return any(
        token in text
        for token in [
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "rpm exhausted",
            "service_unavailable",
            "service unavailable",
            "too many requests",
            "too busy",
            "rate limit",
            "rate_limit",
            "temporarily",
            "timed out",
            "timeout",
        ]
    )


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(default)
        except (TypeError, ValueError):
            return 0.0


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_hit_directions(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
    return []


def _hit_direction_i18n(value: object) -> tuple[list[str], list[str]]:
    raw = _normalize_hit_directions(value)
    zh: list[str] = []
    en: list[str] = []
    for value_text in raw:
        item = " ".join(str(value_text or "").split())
        if not item:
            continue
        if re.search(r"[A-Za-z]", item):
            en.append(item)
            if re.search(r"[一-鿿]", item):
                zh.append(item)
        else:
            zh.append(item)
    def dedupe(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in values:
            key = item.lower()
            if item and key not in seen:
                seen.add(key)
                result.append(item)
        return result
    return dedupe(zh), dedupe(en)


def _set_hit_direction_language_fields(item: dict, value: object | None = None, *, zh_value: object | None = None, en_value: object | None = None) -> None:
    source = item.get("hit_directions") if value is None else value
    source_hits = _normalize_hit_directions(source)
    zh_hits = _normalize_hit_directions(zh_value)
    en_hits = _normalize_hit_directions(en_value)
    inferred_zh, inferred_en = _hit_direction_i18n(source_hits)
    item["hit_directions_zh"] = zh_hits or inferred_zh
    item["hit_directions_en"] = en_hits or inferred_en
    item["hit_directions"] = item["hit_directions_zh"] or source_hits


def _combined_score(fit_score: object, diversity_score: object) -> float:
    fit = max(0.0, min(10.0, _as_float(fit_score)))
    diversity = max(0.0, min(10.0, _as_float(diversity_score)))
    return round(fit * 0.75 + diversity * 0.25, 2)


def _recommendation_display_score(fit_score: object, diversity_score: object = None) -> float:
    # User-visible Find ranking and score display are the final title+abstract
    # LLM fit_score only. Diversity/source/citation/freshness remain audit fields.
    return round(max(0.0, min(10.0, _as_float(fit_score))), 2)


def _flatten_quality_value(value: object, *, depth: int = 0) -> str:
    if value is None or depth > 2:
        return ""
    if isinstance(value, dict):
        return " ".join(_flatten_quality_value(item, depth=depth + 1) for item in value.values())
    if isinstance(value, list | tuple | set):
        return " ".join(_flatten_quality_value(item, depth=depth + 1) for item in value)
    return str(value)


def _quality_signal_text(item: dict) -> str:
    fields = [
        "venue",
        "source",
        "track",
        "decision",
        "presentation",
        "presentation_type",
        "paper_type",
        "acceptance_type",
        "status",
        "url",
        "id",
        "metadata",
    ]
    return " ".join(_flatten_quality_value(item.get(field)) for field in fields).lower()


def _presentation_bonus(item: dict) -> tuple[float, str]:
    labels = _presentation_labels(item)
    if "best paper/award" in labels:
        return 0.50, "发表类型/奖项: best-paper/award +0.50"
    if "oral" in labels:
        return 0.45, "发表类型: oral +0.45"
    if any(label in labels for label in ["spotlight", "highlight", "notable", "top-5%"]):
        return 0.35, "发表类型: spotlight/highlight +0.35"
    return 0.0, ""


def _presentation_labels(item: dict) -> list[str]:
    text = _quality_signal_text(item)
    labels: list[str] = []
    if re.search(r"\b(best|award|outstanding|distinguished)[-\s]+paper\b", text):
        labels.append("best paper/award")
    if re.search(r"\boral\b", text):
        labels.append("oral")
    if re.search(r"\bspotlight\b", text):
        labels.append("spotlight")
    if re.search(r"\bhighlight\b", text):
        labels.append("highlight")
    if re.search(r"\bnotable\b", text):
        labels.append("notable")
    if re.search(r"top[-\s]?5%", text):
        labels.append("top-5%")
    return labels


def _set_quality_labels(item: dict) -> None:
    labels: list[str] = []
    for label in _presentation_labels(item):
        if label not in labels:
            labels.append(label)
    item["presentation_labels"] = labels
    existing = [str(label).strip() for label in item.get("quality_labels", []) if str(label).strip()] if isinstance(item.get("quality_labels"), list) else []
    merged = existing[:]
    for label in labels:
        if label not in merged:
            merged.append(label)
    item["quality_labels"] = merged


def _quality_bonus_allowed(item: dict) -> bool:
    fit = _as_float(item.get("fit_score"))
    diversity = _as_float(item.get("diversity_score"))
    base_score = _combined_score(fit, diversity)
    relevance_text = " ".join([
        str(item.get("category") or ""),
        str(item.get("reason") or ""),
        str(item.get("fit_explanation") or ""),
    ]).lower()
    if (
        fit < 6.5
        or base_score < 6.0
        or any(marker in relevance_text for marker in ["不相关", "无关", "irrelevant", "not relevant", "unrelated"])
        or _has_topic_evidence_contradiction(item)
    ):
        return False
    return _has_strong_topic_evidence(item)


def _presentation_bonus_allowed(item: dict) -> bool:
    fit = _as_float(item.get("fit_score"))
    if fit < 6.0:
        return False
    if _has_topic_evidence_contradiction(item):
        return False
    return _has_strong_topic_evidence(item) and _has_real_abstract(item)



def _quality_table_bonus(item: dict) -> tuple[float, str]:
    available = round(max(0.0, min(0.4, _as_float(item.get("quality_bonus_available")))), 2)
    if not available:
        return 0.0, ""
    tier = str(item.get("quality_tier") or "").strip()
    kind = str(item.get("quality_kind") or "quality").strip()
    source = str(item.get("quality_source") or "quality table").strip()
    label = f"{kind}:{tier}" if tier else kind
    return available, f"结构化质量表: {label} +{available:.2f} ({source})"

def _venue_bonus(item: dict) -> tuple[float, str]:
    text = _quality_signal_text(item)
    elite_general_bonus = {
        "iclr": "ICLR",
        "neurips": "NeurIPS",
        "nips": "NeurIPS",
        "icml": "ICML",
    }
    for marker, name in elite_general_bonus.items():
        if marker in text:
            return 0.08, f"普通顶级会议小加分: {name} +0.08"
    return 0.0, ""


def _apply_quality_bonus(item: dict) -> None:
    """Slightly re-rank already relevant papers using venue/presentation signals."""
    _set_quality_labels(item)
    fit = _as_float(item.get("fit_score"))
    diversity = _as_float(item.get("diversity_score"))
    base_score = _combined_score(fit, diversity)
    item["base_score_before_quality_bonus"] = base_score
    item["quality_bonus"] = 0.0
    item["quality_bonus_reason"] = ""
    item["quality_bonus_policy"] = SCORING_POLICY_VERSION

    allow_presentation_bonus = _presentation_bonus_allowed(item)
    allow_venue_bonus = _quality_bonus_allowed(item)
    if not allow_presentation_bonus and not allow_venue_bonus:
        item["score"] = base_score
        return

    bonuses: list[tuple[float, str]] = []
    presentation_bonus, presentation_reason = _presentation_bonus(item)
    if presentation_bonus and allow_presentation_bonus:
        bonuses.append((presentation_bonus, presentation_reason))
    table_bonus, table_reason = _quality_table_bonus(item)
    if table_bonus and allow_venue_bonus:
        # Avoid double-counting the same official presentation signal. For oral/spotlight
        # rows, keep the stricter current presentation bonus; for journals/CCF ranks,
        # the table supplies the only deterministic quality signal.
        if not presentation_bonus or table_bonus > presentation_bonus:
            bonuses.append((table_bonus, table_reason))
    venue_bonus, venue_reason = _venue_bonus(item)
    if venue_bonus and allow_venue_bonus and not table_bonus:
        bonuses.append((venue_bonus, venue_reason))
    if not bonuses:
        item["score"] = base_score
        return

    bonus = round(min(0.65, sum(value for value, _reason in bonuses)), 2)
    item["quality_bonus"] = bonus
    item["quality_bonus_reason"] = "; ".join(reason for _value, reason in bonuses if reason)
    item["score"] = round(min(10.0, base_score + bonus), 2)


def _stable_rank_key(row: dict) -> tuple:
    local_rank = _as_int(row.get("local_rank") or row.get("title_local_rank"), 10**9)
    stable_rank_score = _as_float(
        row.get("stable_rank_score"),
        _as_float(row.get("stable_source_score") or row.get("score")),
    )
    return (
        -stable_rank_score,
        local_rank,
        -float(row.get("stable_source_score") or row.get("score") or 0),
        -float(row.get("stable_source_base_score") or 0),
        -float(row.get("local_score") or row.get("local_tfidf_score") or 0),
        str(row.get("venue") or ""),
        -int(row.get("year") or 0),
        str(row.get("title") or row.get("id") or row.get("url") or "").lower(),
    )


def _topic_axis_score(item: dict) -> tuple[int, int, int, int, int]:
    groups = _strict_strong_required_groups(item)
    hits = _source_topic_group_hits(item)
    axis_count = sum(1 for group in groups if hits.get(group)) if groups else 0
    complete_required_route = 1 if groups and all(hits.get(group) for group in groups) else 0
    first_axis = 1 if axis_count else 0
    extra_axes = max(0, axis_count - 1)
    actionable = 1 if item.get("source_supported_adaptive_route") or item.get("source_supported_adaptive_terms") else 0
    return (axis_count, complete_required_route, first_axis, extra_axes, actionable)


def _final_llm_candidate_key(item: dict) -> tuple:
    axis_count, complete_required_route, first_axis, method_axis_count, actionable_topic = _topic_axis_score(item)
    fit = _as_float(item.get("fit_score"), 0)
    score = _as_float(item.get("stable_rank_score"), _as_float(item.get("score"), 0))
    local_rank = _as_int(item.get("local_rank") or item.get("title_local_rank"), 10**9)
    return (
        -complete_required_route,
        -first_axis,
        -method_axis_count,
        -actionable_topic,
        -axis_count,
        -round(fit / 0.25) * 0.25,
        -round(score / 0.05) * 0.05,
        local_rank,
        str(item.get("venue") or ""),
        -_as_int(item.get("year"), 0),
        str(item.get("title") or item.get("id") or item.get("url") or "").lower(),
    )


def _final_llm_scoring_pool(evaluated: list[dict], config: AppConfig) -> list[dict]:
    limit = _final_llm_scoring_limit(config, len(evaluated))
    ranked = sorted(_dedupe_items(evaluated), key=_stable_rank_key)
    return ranked[:limit]


def _topic_evidence_source_text(item: dict) -> str:
    fields = ["title", "abstract", "keywords", "primary_area", "track", "venue", "source", "metadata"]
    parts = [_flatten_quality_value(item.get(field)) for field in fields]
    if item.get("classification_source") == "official":
        parts.append(_flatten_quality_value(item.get("category")))
    return " ".join(part for part in parts if part).lower()


def _contains_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _adaptive_signal_terms(text: str, *, min_len: int = 3) -> list[str]:
    raw_terms = re.findall(r"[一-鿿]{2,}|[a-zA-Z0-9][a-zA-Z0-9_.-]{1,}", (text or "").lower())
    blocked = {
        "validation", "run", "improved", "high", "recall", "taste", "find", "keep", "weak", "relevant",
        "candidate", "candidates", "critique", "fabricate", "strong", "evidence", "research", "profile",
        "paper", "papers", "study", "studies", "method", "methods", "model", "models", "system", "systems",
        "using", "use", "used", "based", "directly", "generic", "work", "works", "approach", "approaches",
        "论文", "候选", "强推荐", "研究", "系统", "模型", "方法",
    }
    terms: list[str] = []
    for raw in raw_terms:
        term = raw.strip(".,;:!?()[]{}\"'")
        if len(term) < min_len or term in blocked:
            continue
        if term not in terms:
            terms.append(term)
    return terms



def _profile_chunk_is_guardrail(text: str) -> bool:
    lowered = " ".join(str(text or "").lower().split())
    if not lowered:
        return True
    guardrail_markers = [
        "validation run",
        "do not",
        "don't",
        "keep weak",
        "weak-but-relevant",
        "for critique",
        "do not fabricate",
        "guardrail",
        "forbid",
        "forbidden",
        "no second find",
        "pair_compare",
        "legacy/control",
        "legacy only",
        "control only",
        "禁止",
        "不得",
        "不要",
        "仅 legacy",
        "仅作为",
        "监督",
    ]
    return any(marker in lowered for marker in guardrail_markers)


def _topic_interest_chunks(config: AppConfig) -> list[str]:
    chunks: list[str] = []
    for part in [getattr(config, "research_interest", ""), getattr(config, "researcher_profile", "")]:
        for raw in re.split(r"[\n;；。.!?]+", str(part or "")):
            cleaned = " ".join(raw.split()).strip()
            if cleaned and not _profile_chunk_is_guardrail(cleaned):
                chunks.append(cleaned)
    if not chunks:
        fallback = " ".join(str(getattr(config, "research_interest", "") or "").split()).strip()
        if fallback:
            chunks.append(fallback)
    seen: set[str] = set()
    result: list[str] = []
    for chunk in chunks:
        key = chunk.lower()
        if key not in seen:
            seen.add(key)
            result.append(chunk)
    return result


def _topic_interest_text(config: AppConfig) -> str:
    return "\n".join(_topic_interest_chunks(config)).strip()


def _route_support_threshold(terms: list[str]) -> int:
    if len(terms) <= 2:
        return len(terms)
    if len(terms) <= 5:
        return max(2, len(terms) - 1)
    return max(3, int(ceil(len(terms) * 0.55)))


def _term_family_present(terms: list[str], markers: tuple[str, ...]) -> bool:
    return any(any(marker in term.lower() for marker in markers) for term in terms)


def _term_family_markers(term: str) -> tuple[str, ...]:
    lower = " ".join(str(term or "").lower().replace("_", " ").replace("-", " ").split())
    if not lower:
        return ()
    markers: list[str] = [lower]
    compact = lower.replace(" ", "")
    if compact and compact != lower:
        markers.append(compact)
    for piece in re.split(r"[^a-z0-9一-鿿]+", lower):
        if len(piece) >= 3 and piece not in {"and", "the", "for", "with", "system", "systems", "model", "models", "method", "methods"}:
            markers.append(piece)
    out: list[str] = []
    for marker in markers:
        if marker and marker not in out:
            out.append(marker)
    return tuple(out)


def _source_marker_is_negated(source_text: str, start: int) -> bool:
    before = source_text[max(0, start - 80):start]
    negation_patterns = [
        r"(?:does|do|did|is|are|was|were)\s+not\b",
        r"\b(?:no|not|without|lack|lacks|lacking|missing|never)\b",
        r"不涉及|没有|未涉及|缺少|无关|不使用|未使用",
    ]
    return any(re.search(pattern, before, flags=re.IGNORECASE) for pattern in negation_patterns)


def _source_has_unnegated_marker(source_text: str, marker: str) -> bool:
    marker = marker.lower().strip()
    if not marker:
        return False
    for match in re.finditer(re.escape(marker), source_text, flags=re.IGNORECASE):
        if not _source_marker_is_negated(source_text, match.start()):
            return True
    return False


def _source_has_route_term(source_text: str, term: str) -> bool:
    return any(_source_has_unnegated_marker(source_text, marker) for marker in _term_family_markers(term))

def _source_has_negated_route_term(source_text: str, term: str) -> bool:
    for marker in _term_family_markers(term):
        for match in re.finditer(re.escape(marker), source_text, flags=re.IGNORECASE):
            if _source_marker_is_negated(source_text, match.start()):
                return True
    return False


def _route_terms_have_source_support(route_terms: list[str], matched: list[str]) -> bool:
    if len(matched) >= _route_support_threshold(route_terms):
        return True
    if len(route_terms) <= 2:
        return False
    return _foundation_route_terms_have_source_support(route_terms, matched)


def _route_core_terms(route_terms: list[str]) -> list[str]:
    structural_markers = ("foundation", "borrowing", "route", "component", "subproblem", "axis", "基础", "借鉴", "路线", "组件")
    structural_filtered = [term for term in route_terms if not _term_family_present([term], structural_markers)]
    return structural_filtered or route_terms


def _foundation_route_terms_have_source_support(route_terms: list[str], matched: list[str]) -> bool:
    core_terms = _route_core_terms(route_terms)
    if not core_terms:
        return False
    core_matched = [term for term in core_terms if term in matched]
    return len(core_matched) >= _route_support_threshold(core_terms)


def _route_source_support(item: dict, route_text: str) -> tuple[bool, list[str], list[str]]:
    if not _has_real_abstract(item):
        return False, [], ["real abstract"]
    source_text = _topic_evidence_source_text(item)
    route_terms = _adaptive_signal_terms(route_text)
    if not route_terms:
        route_terms = _adaptive_signal_terms(route_text, min_len=2)
    if not route_terms:
        return False, [], ["adaptive route terms"]
    matched = [term for term in route_terms if _source_has_route_term(source_text, term)]
    missing = [term for term in route_terms if term not in matched]
    fully_supported = _route_terms_have_source_support(route_terms, matched)
    negated_missing = [term for term in missing if _source_has_negated_route_term(source_text, term)]
    if fully_supported:
        return True, matched, missing
    partial_foundation = len(matched) >= 2 and not negated_missing and _source_has_actionable_method_or_evidence(item)
    return partial_foundation, matched, missing

def _best_source_supported_route(item: dict, interest: str) -> tuple[str, list[str], list[str]]:
    routes = []
    matched_route = " ".join(str(item.get("matched_topic_route") or "").split())
    if matched_route:
        routes.append(matched_route)
    for route in _adaptive_topic_route_lines(interest):
        if route not in routes:
            routes.append(route)
    if not routes and str(interest or "").strip():
        routes = [interest]
    best_route = ""
    best_matched: list[str] = []
    best_missing: list[str] = []
    for route in routes:
        supported, matched, missing = _route_source_support(item, route)
        if supported:
            return route, matched, missing
        if len(matched) > len(best_matched):
            best_route = route
            best_matched = matched
            best_missing = missing
    return best_route if best_matched else "", best_matched, best_missing


def _record_source_supported_adaptive_route(item: dict, interest: str) -> bool:
    route, _matched, _missing = _best_source_supported_route(item, interest)
    supported, matched, missing = _route_source_support(item, route) if route else (False, [], [])
    item["source_supported_adaptive_route"] = route if supported else ""
    item["source_supported_adaptive_terms"] = matched
    item["source_missing_adaptive_terms"] = missing
    return supported

def _candidate_has_source_supported_adaptive_route(item: dict, interest: str) -> bool:
    # This guard repairs LLM false negatives only when real source text supports
    # at least two salient terms from the current generated route. It is not
    # a title-keyword backdoor: missing abstracts and hard negative LLM verdicts
    # are blocked before promotion.
    return _record_source_supported_adaptive_route(item, interest)


def _candidate_judgment_text(item: dict) -> str:
    fields = [
        "topic_evidence",
        "matched_topic_route",
        "source_supported_adaptive_route",
        "missing_topic_evidence",
        "unmatched_topic_routes",
        "hit_directions",
        "hit_directions_zh",
        "hit_directions_en",
        "category",
        "reason",
        "reason_zh",
        "reason_en",
        "fit_explanation",
        "fit_explanation_zh",
        "fit_explanation_en",
        "recommendation_note",
        "recommendation_note_zh",
        "recommendation_note_en",
    ]
    return " ".join(_flatten_quality_value(item.get(field)) for field in fields if item.get(field)).lower()


def _interest_required_topic_groups(interest: str) -> list[str]:
    # Framework code must not infer project-specific axes from keywords.
    # Project-specific gates may still be supplied by the project state/LLM output
    # as item["topic_gate_required_groups"].
    return []


def _topic_gate_required_groups(item: dict, interest: str = "") -> list[str]:
    groups: list[str] = []
    if isinstance(item.get("topic_gate_required_groups"), list):
        groups = [str(group) for group in item.get("topic_gate_required_groups") if str(group).strip()]
    if groups:
        item["topic_gate_required_groups"] = groups
    return groups


def _strict_strong_required_groups(item: dict, interest: str = "") -> list[str]:
    # Framework code must stay topic-neutral. Strict groups are supplied by the
    # current project's LLM gate configuration, never inferred from global
    # research-content keywords.
    return _topic_gate_required_groups(item, interest)


def _topic_group_markers(item: dict, group: str) -> list[str]:
    specs: list[dict] = []
    raw_specs = item.get("topic_gate_group_specs") or item.get("topic_group_specs") or item.get("topic_axes")
    if isinstance(raw_specs, dict):
        for name, spec in raw_specs.items():
            entry = dict(spec) if isinstance(spec, dict) else {"markers": spec}
            entry.setdefault("name", name)
            specs.append(entry)
    elif isinstance(raw_specs, list):
        specs = [dict(spec) for spec in raw_specs if isinstance(spec, dict)]
    wanted = str(group or "").strip()
    markers: list[str] = []
    for spec in specs:
        name = str(spec.get("name") or spec.get("id") or "").strip()
        if name and name != wanted:
            continue
        raw = spec.get("markers") or spec.get("required_any") or spec.get("terms") or spec.get("keywords") or []
        if isinstance(raw, str):
            raw_values = [raw]
        elif isinstance(raw, list):
            raw_values = [str(value) for value in raw]
        else:
            raw_values = []
        markers.extend(raw_values)
    if not markers:
        markers.append(wanted)
    expanded: list[str] = []
    for marker in markers:
        expanded.extend(_term_family_markers(marker))
    out: list[str] = []
    for marker in expanded:
        marker = str(marker or "").strip().lower()
        if marker and marker not in out:
            out.append(marker)
    return out


def _source_has_topic_group(item: dict, group: str) -> bool:
    source_text = _topic_evidence_source_text(item)
    return any(_source_has_unnegated_marker(source_text, marker) for marker in _topic_group_markers(item, group))


def _source_topic_group_hits(item: dict) -> dict[str, bool]:
    groups = _strict_strong_required_groups(item)
    return {group: _source_has_topic_group(item, group) for group in groups}


def _source_has_actionable_method_or_evidence(item: dict) -> bool:
    source_text = _topic_evidence_source_text(item)
    if bool(item.get("source_supported_adaptive_route") and item.get("source_supported_adaptive_terms")):
        return True
    action_patterns = [
        r"train|training|optimise|optimize|learning|algorithm|architecture|loss|embedding|dataset|benchmark|evaluation|metric|ablation|implementation|code|repository|open[- ]?source",
        r"model|framework|module|adapter|objective|protocol|pipeline|baseline|experiment|analysis|method|system",
        r"训练|优化|学习|算法|架构|损失|嵌入|数据集|基准|评测|指标|消融|实现|代码|开源|协议|模块|实验|方法|模型|框架",
    ]
    return _contains_any(source_text, action_patterns)

def _source_is_generic_background_for_required_topic(item: dict) -> bool:
    # Topic-specific negative lists do not belong in the framework. A row is
    # treated as generic background only when the LLM/project artifact explicitly
    # labels it that way; the framework then checks for generic actionable
    # science evidence, not hard-coded research-topic words.
    role = str(item.get("evidence_role") or "").lower().strip()
    explicit = bool(item.get("generic_background") or item.get("background_only")) or role in {"generic_background", "background_only"}
    if not explicit:
        return False
    return not _source_has_actionable_method_or_evidence(item)

def _foundation_matches_current_axes(item: dict, interest: str = "") -> bool:
    groups = _topic_gate_required_groups(item, interest)
    hits = _source_topic_group_hits(item)
    if not groups:
        return bool(item.get("source_supported_adaptive_route") and item.get("source_supported_adaptive_terms")) or str(item.get("evidence_role") or "") != "foundation_borrowing"
    if not any(hits.get(group) for group in groups):
        return False
    if str(item.get("evidence_role") or "") == "foundation_borrowing":
        return _source_has_actionable_method_or_evidence(item)
    return True


def _has_actionable_foundation_axis(item: dict) -> bool:
    return _source_has_actionable_method_or_evidence(item) or any(_source_topic_group_hits(item).values())


def _explicit_llm_negative_strong_reason(item: dict) -> str:
    judgment_text = _candidate_judgment_text(item)
    absolute_negative_patterns = [
        r"\b(?:unrelated|irrelevant|out of scope|not relevant|not a good fit)\b",
        r"无关|不相关|主题偏离|严重偏离",
    ]
    if _contains_any(judgment_text, absolute_negative_patterns):
        return "LLM explanation explicitly says this item is unrelated or not useful evidence for the current topic."
    missing_core_patterns = [
        r"not directly (?:related|relevant) to the current (?:topic|route|axis)",
        r"(?:does|do|did|is|are)\s+not\s+(?:involve|address|study|cover|concern|mention)\s+(?:the\s+)?(?:required|current|core)",
        r"(?:lack|lacks|lacking|without|no)\s+(?:a\s+)?(?:required|current|core).{0,40}(?:component|axis|evidence|route|method)",
        r"不符合核心(?:研究)?方向|核心方向不匹配",
        r"未(?:提供|涉及|提及).{0,24}(?:核心|关键组件|关键方法|项目主题)",
        r"不(?:涉及|符合|属于).{0,24}(?:核心研究方向|项目主题|关键路线)",
        r"缺(?:少|乏).{0,24}(?:关键组件|核心组件|项目主题|可执行证据)",
    ]
    if _contains_any(judgment_text, [r"不符合核心(?:研究)?方向|核心方向不匹配", r"not a core fit", r"core direction mismatch"]):
        return "LLM explanation explicitly says this item does not fit the current core research direction."
    if _contains_any(judgment_text, missing_core_patterns) and not _has_actionable_foundation_axis(item):
        return "LLM explanation says a required core axis is missing and the source text lacks actionable evidence for the current topic."
    return ""


def _strict_strong_invalid_reason(item: dict) -> str:
    role = str(item.get("evidence_role") or "").lower()
    negative_reason = _explicit_llm_negative_strong_reason(item)
    if negative_reason:
        return negative_reason
    invalid_reason = _strong_topic_invalid_reason(item)
    if invalid_reason:
        return invalid_reason
    if role and role in {"weak_or_boundary", "negative", "critique_only", "retrieval_candidate"}:
        return "Rows without final recommendation evidence cannot enter the Find recommendation list."
    return ""


def _strong_topic_invalid_reason(item: dict) -> str:
    groups = _strict_strong_required_groups(item)
    if groups:
        hits = _source_topic_group_hits(item)
        if not any(hits.get(group) for group in groups):
            return "Current topic gate has configured required groups, but the real title/abstract lacks source evidence for any required topic group."
    if _source_is_generic_background_for_required_topic(item):
        return "Generic background work lacks actionable method, data, evaluation, or executable modeling evidence for the current executable plan."
    if str(item.get("evidence_role") or "") == "foundation_borrowing" and not _source_has_actionable_method_or_evidence(item):
        return "Foundation evidence for the current topic lacks actionable method, data, evaluation, or executable modeling evidence."
    evidence = str(item.get("topic_evidence") or "")
    lowered = evidence.lower()
    if lowered.startswith(("passed:", "strong:")) and "adaptive_llm_topic_route" not in lowered and "source-supported adaptive route" not in lowered:
        route = evidence.split(":", 1)[1].strip()
        if route.startswith("foundation:"):
            route = route.split(":", 1)[1].strip()
        generic_route_labels = {"direct topic match", "direct match", "topic match", "strong topic match"}
        route_terms = _adaptive_signal_terms(route) or _adaptive_signal_terms(route, min_len=2)
        should_validate_route = bool(route_terms) and route.lower() not in generic_route_labels
        if should_validate_route:
            supported, matched, missing = _route_source_support(item, route)
            if not supported:
                item["source_supported_adaptive_terms"] = matched
                item["source_missing_adaptive_terms"] = missing
                return "Passed topic evidence is unsupported by the real title/abstract route terms; generic route-name evidence is insufficient."
    return ""

def _foundation_invalid_reason(item: dict, interest: str = "") -> str:
    groups = _topic_gate_required_groups(item, interest)
    judgment_text = _candidate_judgment_text(item)
    if _source_is_generic_background_for_required_topic(item):
        return "Generic background work lacks a concrete method, data, evaluation protocol, or executable modeling component for this TASTE plan."
    hard_negative_patterns = [
        r"\b(?:unrelated|irrelevant|out of scope|not relevant)\b",
        r"无关|不相关|主题偏离|严重偏离",
        r"not directly (?:related|relevant) to the current (?:topic|route|axis)",
        r"不直接与.{0,24}(?:当前主题|项目主题|核心路线).{0,16}相关",
        r"仅(?:可能)?作为.{0,50}间接背景",
        r"only.{0,30}indirect.{0,30}background",
    ]
    if _contains_any(judgment_text, hard_negative_patterns):
        return "LLM verdict or route audit explicitly says the paper is unrelated or lacks the route required by the current topic."
    unactionable_patterns = [
        r"难以直接转化",
        r"不能直接转化",
        r"no reusable.{0,50}(?:method|mechanism|code|dataset|benchmark)",
        r"lacks? reusable.{0,50}(?:method|mechanism|code|dataset|benchmark)",
        r"(?:no|without|lacks?)\s+(?:public\s+)?(?:code|dataset|benchmark|implementation)",
        r"没有提供.{0,20}(?:任何)?(?:可复用|可执行).{0,30}(?:方法|机制|代码|数据集|基准)",
        r"没有(?:可运行|公开|可复用).{0,20}(?:代码|数据集|基准)",
        r"分析性工作.{0,50}(?:难以|不能|无法)",
        r"analysis-only.{0,50}(?:not|cannot)",
    ]
    if _contains_any(judgment_text, unactionable_patterns):
        return "LLM verdict says this is not an actionable foundation route for the current executable plan."
    if groups:
        hits = _source_topic_group_hits(item)
        if not any(hits.get(group) for group in groups):
            return "The real title/abstract lacks source evidence for any required topic group configured by the project."
    if not _source_has_actionable_method_or_evidence(item):
        return "The source lacks actionable method/data/evaluation evidence for the current executable plan."
    if interest and not _record_source_supported_adaptive_route(item, interest):
        return "Foundation route does not support any concrete route derived from the current project profile."
    return ""

def _foundation_strong_enough(item: dict, interest: str = "") -> bool:
    if item.get("evidence_role") != "foundation_borrowing":
        return True
    invalid_reason = _foundation_invalid_reason(item, interest)
    if invalid_reason:
        item["foundation_invalid_reason"] = invalid_reason
        return False
    if _has_topic_evidence_contradiction(item):
        item["foundation_invalid_reason"] = "Contradictory or missing adaptive topic evidence."
        return False
    if interest:
        if not _record_source_supported_adaptive_route(item, interest):
            item["foundation_invalid_reason"] = "Foundation route lacks source support for a concrete generated adaptive route."
            return False
    else:
        existing_route = str(item.get("source_supported_adaptive_route") or "").strip()
        existing_terms = item.get("source_supported_adaptive_terms") if isinstance(item.get("source_supported_adaptive_terms"), list) else []
        if existing_route and existing_terms:
            item.pop("foundation_invalid_reason", None)
        else:
            evidence = str(item.get("topic_evidence") or "")
            evidence_route = ""
            lowered_evidence = evidence.lower()
            if lowered_evidence.startswith(("passed:foundation:", "strong:foundation:")):
                evidence_route = evidence.split(":", 2)[2].strip()
            if evidence_route:
                supported, matched, missing = _route_source_support(item, evidence_route)
                item["source_supported_adaptive_route"] = evidence_route if supported else ""
                item["source_supported_adaptive_terms"] = matched
                item["source_missing_adaptive_terms"] = missing
                if not supported:
                    item["foundation_invalid_reason"] = "Foundation route lacks source support for its evidence route."
                    return False
    if not _foundation_matches_current_axes(item, interest):
        item["foundation_invalid_reason"] = "Foundation route does not match the current project axes with actionable source evidence."
        return False
    return True

def _llm_weak_verdict_only_misses_other_route_components(item: dict) -> bool:
    if not (str(item.get("topic_evidence") or "").lower().startswith("weak:") or item.get("topic_evidence_supported") is False):
        return False
    missing_values = item.get("missing_topic_evidence")
    if isinstance(missing_values, list):
        missing_text = " ".join(str(value) for value in missing_values)
    else:
        missing_text = str(missing_values or "")
    verdict_text = " ".join([
        str(item.get("topic_evidence") or ""),
        missing_text,
        str(item.get("reason") or ""),
        str(item.get("reason_zh") or ""),
        str(item.get("reason_en") or ""),
        str(item.get("fit_explanation") or ""),
        str(item.get("fit_explanation_zh") or ""),
        str(item.get("fit_explanation_en") or ""),
    ]).lower()
    hard_negative = [
        r"abstract missing",
        r"missing abstract",
        r"title only",
        r"only title",
        r"metadata only",
        r"缺少摘要",
        r"仅标题",
        r"只有标题",
        r"unrelated",
        r"irrelevant",
        r"not relevant",
        r"out of scope",
        r"无关",
        r"不相关",
        r"不属于",
        r"证据不足",
        r"insufficient evidence",
        r"not enough evidence",
        r"lacks evidence",
        r"without evidence",
        r"no evidence",
    ]
    if _contains_any(verdict_text, hard_negative):
        return False
    boundary_markers = [
        r"missing (?:other|remaining|additional|another|some)",
        r"lacks? (?:other|remaining|additional|another|some)",
        r"partial(?:ly)?",
        r"foundation",
        r"borrowing",
        r"subproblem",
        r"component",
        r"route",
        r"near[-\s]?threshold",
        r"边界",
        r"基础",
        r"借鉴",
        r"部分",
        r"子问题",
        r"缺少其他",
        r"缺少部分",
    ]
    return _contains_any(verdict_text, boundary_markers) or _as_float(item.get("fit_score")) >= 5.8


def _repair_llm_alternative_route_false_negative(item: dict, interest: str) -> bool:
    """Audit a possible LLM false negative without promoting it.

    Title/detail retrieval should preserve enough candidates, but user-facing recommendations
    must be the direct output of the final relevance judge. Earlier
    versions let this deterministic source-route guard rewrite weak LLM verdicts
    into passed foundation evidence, which made framework code behave like a
    topic-specific hard filter/promotion path. Keep the source-support facts for
    audit and future search expansion, but never change recommendation evidence,
    scores, or positive-support flags here.
    """
    if item.get("foundation_demoted_from_strong"):
        return False
    if not _candidate_has_source_supported_adaptive_route(item, interest):
        return False
    if not _llm_weak_verdict_only_misses_other_route_components(item):
        return False
    previous_missing = item.get("missing_topic_evidence")
    if isinstance(previous_missing, list):
        unmatched = [str(value) for value in previous_missing if str(value).strip()]
    elif str(previous_missing or "").strip():
        unmatched = [str(previous_missing).strip()]
    else:
        unmatched = []
    route_text = str(item.get("source_supported_adaptive_route") or item.get("matched_topic_route") or "").strip()
    item["source_guard_audit_only"] = True
    item["llm_alternative_route_false_negative_audited"] = True
    item["llm_alternative_route_false_negative_repaired"] = False
    item["source_guard_unmatched_topic_routes"] = unmatched
    item["not_positive_support"] = True
    item["weak_candidate_for_critique"] = True
    item.setdefault("evidence_role", "weak_or_boundary")
    item.setdefault("evidence_tier", "nethreshold_for_reading")
    item.setdefault("strong_gate_reject_reason", "source-supported local route is audit-only; final relevance verdict remains weak")
    item["recommendation_note_zh"] = (
        "边界审计线索：本地来源检查发现它可能支持一条检索路线，但最终题名+摘要 LLM 判定仍未通过；"
        "该结果只能用于误推荐排查或扩展检索，不能进入推荐精读列表。"
    )
    item["recommendation_note_en"] = (
        "Boundary audit signal: local source checks found a possible retrieval route, "
        "but the final relevance verdict did not pass; keep it out of the recommended reading pool."
    )
    item["recommendation_note"] = item["recommendation_note_zh"]
    if route_text and (not _normalize_hit_directions(item.get("hit_directions_zh")) or not _normalize_hit_directions(item.get("hit_directions_en"))):
        _set_hit_direction_language_fields(
            item,
            item.get("hit_directions") or route_text,
            zh_value=item.get("hit_directions_zh"),
            en_value=item.get("hit_directions_en"),
        )
    return True


def _repair_passed_topic_fit_consistency(item: dict) -> bool:
    if not _has_strong_topic_evidence(item) or not _has_real_abstract(item):
        return False
    if _strong_topic_invalid_reason(item):
        return False
    if _as_float(item.get("fit_score")) >= 6.0:
        return False
    if item.get("evidence_role") == "foundation_borrowing":
        item["fit_score"] = max(_as_float(item.get("fit_score")), 6.0)
        item["foundation_minimum_fit_not_strong"] = False
    else:
        item["fit_score"] = 6.0
    item["diversity_score"] = max(_as_float(item.get("diversity_score")), 4.0)
    item["score"] = _combined_score(item.get("fit_score"), item.get("diversity_score"))
    item["llm_passed_topic_fit_consistency_repaired"] = True
    item.setdefault(
        "recommendation_note",
        "LLM returned passed topic evidence but a sub-threshold fit score; code-side consistency guard raised fit to the minimum strong-evidence threshold.",
    )
    return True


def _demote_unstable_foundation_item(item: dict) -> None:
    reason = str(item.get("foundation_invalid_reason") or "").strip()
    item["topic_evidence_supported"] = False
    if not str(item.get("topic_evidence") or "").lower().startswith("weak:"):
        item["topic_evidence"] = "weak: foundation route failed the current project/domain gate"
    item["evidence_role"] = "weak_or_boundary"
    item["evidence_tier"] = "nethreshold_for_reading"
    item["weak_candidate_for_critique"] = True
    item["foundation_demoted_from_strong"] = True
    item["not_positive_support"] = True
    item["fit_score"] = min(_as_float(item.get("fit_score")), 4.8)
    item["diversity_score"] = min(_as_float(item.get("diversity_score")), 3.8)
    item["score"] = _combined_score(item.get("fit_score"), item.get("diversity_score"))
    item["recommendation_score"] = min(_as_float(item.get("recommendation_score"), item.get("score")), item["score"])
    item["stable_rank_score"] = min(_as_float(item.get("stable_rank_score"), item.get("stable_source_score")), 5.5)
    item["stable_source_score"] = min(_as_float(item.get("stable_source_score")), 5.5)
    item["recommendation_note_zh"] = "未入选线索：LLM解释或摘要证据没有达到当前 Find 推荐要求，不展示为推荐论文。"
    item["recommendation_note_en"] = "Not selected for recommendation: the LLM explanation or source evidence did not satisfy the current Find recommendation contract."
    item["recommendation_note"] = item["recommendation_note_zh"] if not reason else f"{item['recommendation_note_zh']} Gate reason: {reason}"


def _mark_not_positive_for_strong_gate(item: dict, reason: str) -> None:
    reason = str(reason or "Find recommendation gate rejected this row").strip()
    item["strong_gate_reject_reason"] = reason
    item["not_positive_support"] = True
    item["weak_candidate_for_critique"] = True
    if str(item.get("evidence_tier") or "").lower() == "strong_recommendation":
        item["evidence_tier"] = "nethreshold_for_reading"
    else:
        item.setdefault("evidence_tier", "nethreshold_for_reading")
    if not str(item.get("evidence_role") or "").strip():
        item["evidence_role"] = "weak_or_boundary"
    item["recommendation_note_zh"] = (
        "未入选线索：该条目有可检查的文献信号，但未进入当前 Find 推荐列表；"
        "只用于排查推荐质量或扩展检索，不展示为推荐精读论文。"
    )
    item["recommendation_note_en"] = (
        "Not selected for recommendation: this row has inspectable literature signal but did not enter the current Find recommendation list; "
        "use it for recommendation-quality checks or search expansion, not as recommended-reading evidence."
    )
    item["recommendation_note"] = f"{item['recommendation_note_zh']} Gate reason: {reason}"


def _json_or_error(llm: LLMClient, prompt: str, *, temperature: float | None = None, max_tokens: int | None = None) -> dict:
    try:
        return llm.json_or_error(prompt, temperature=temperature, max_tokens=max_tokens)
    except TypeError:
        try:
            return llm.json_or_error(prompt, temperature=temperature)
        except TypeError:
            return llm.json_or_error(prompt)


def _llm_live_gate(llm: LLMClient) -> dict:
    if not llm.enabled:
        return {"ok": False, "reason": "llm-not-configured", "summary": llm.summary()}
    # Some OpenAI-compatible providers return `{}` or time out for tiny probes
    # like {"ok": true}, while the same endpoint works for the structured
    # scoring JSON the workflow needs. Probe the same shape used by title/detail
    # scoring so a healthy scorer is not incorrectly bypassed.
    prompt = (
        'Return JSON only: {"ok": true, "selected": '
        '[{"id":"probe", "fit_score":7, "diversity_score":6, '
        '"hit_directions":["probe"], "category":"probe", "reason":"ready"}]}'
    )
    timeout = max(5, int(os.environ.get("LLM_LIVE_GATE_TIMEOUT_SEC", "30") or 30))
    original_timeout = getattr(llm, "timeout_sec", timeout)
    original_retries = getattr(llm, "retries", None)
    if hasattr(llm, "timeout_sec"):
        llm.timeout_sec = timeout
    if hasattr(llm, "retries"):
        llm.retries = 1
    try:
        result = _json_or_error_wall_timeout(llm, prompt, temperature=0.0, max_tokens=320, timeout_sec=timeout)
        data = result.get("data")
        selected = data.get("selected") if isinstance(data, dict) else None
        ok = bool(
            result.get("ok")
            and isinstance(data, dict)
            and (
                data.get("ok") is True
                or (isinstance(selected, list) and bool(selected) and isinstance(selected[0], dict))
            )
        )
        if ok:
            return {"ok": True, "error": "", "summary": llm.summary(), "probe": "scoring_shape"}
        detail = str(result.get("error") or "LLM live gate returned no scoring-shaped JSON")
        if isinstance(data, dict) and data:
            detail = f"{detail}; data_keys={','.join(sorted(str(key) for key in data.keys())[:12])}"
        return {"ok": False, "error": detail[:500], "summary": llm.summary(), "probe": "scoring_shape"}
    finally:
        if hasattr(llm, "timeout_sec"):
            llm.timeout_sec = original_timeout
        if original_retries is not None and hasattr(llm, "retries"):
            llm.retries = original_retries


def _json_or_error_wall_timeout(llm: LLMClient, prompt: str, *, temperature: float | None = None, max_tokens: int | None = None, timeout_sec: int = 0) -> dict:
    timeout = max(0, int(timeout_sec or 0))
    if timeout <= 0:
        return _json_or_error(llm, prompt, temperature=temperature, max_tokens=max_tokens)
    if threading.current_thread() is not threading.main_thread():
        # SIGALRM only works in the main thread. Parallel LLM scoring runs this
        # helper inside worker threads, so enforce a bounded wall clock with a
        # daemon child thread and return a transient error when the provider
        # hangs past the wall timeout.
        box: dict[str, object] = {}

        def _target() -> None:
            try:
                box["result"] = _json_or_error(llm, prompt, temperature=temperature, max_tokens=max_tokens)
            except Exception as exc:
                box["result"] = {"ok": False, "data": None, "error": str(exc)}

        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            return {"ok": False, "data": None, "error": f"LLM wall-clock timeout after {timeout}s"}
        result = box.get("result")
        return result if isinstance(result, dict) else {"ok": False, "data": None, "error": "LLM worker returned no result"}
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        return _json_or_error(llm, prompt, temperature=temperature, max_tokens=max_tokens)

    def _handle_timeout(_signum, _frame):
        raise TimeoutError(f"LLM wall-clock timeout after {timeout}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    try:
        signal.signal(signal.SIGALRM, _handle_timeout)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        return _json_or_error(llm, prompt, temperature=temperature, max_tokens=max_tokens)
    except Exception as exc:
        return {"ok": False, "data": None, "error": str(exc)}
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
        except Exception:
            pass


def _adaptive_topic_route_lines(interest: str) -> list[str]:
    routes: list[str] = []
    for raw in re.split(r"[\n;；]+", interest or ""):
        route = " ".join(str(raw).split())
        if len(route) < 4:
            continue
        lowered = route.lower()
        if _profile_chunk_is_guardrail(route) or any(marker in lowered for marker in ["fallback", "configure"]):
            continue
        if route not in routes:
            routes.append(route)
    return routes[:8]


def _adaptive_topic_routes_block(config: AppConfig, interest: str) -> str:
    routes = _adaptive_topic_route_lines(config.research_interest or interest)
    if not routes:
        return "No explicit route list was configured; infer routes from the research interest/profile text above."
    lines = [f"{index}. {route}" for index, route in enumerate(routes, 1)]
    return "generated alternative topic routes for this run (OR semantics):\n" + "\n".join(lines)


def _matched_topic_route(item: dict, interest: str) -> tuple[str, list[str]]:
    if not (interest or "").strip():
        return "not_applicable", []
    source_text = _topic_evidence_source_text(item)
    if not source_text.strip():
        return "", ["source text"]
    adaptive_score = fallback_score(interest, str(item.get("title") or ""), source_text)
    item["adaptive_local_topic_score"] = adaptive_score
    if adaptive_score >= 6.0:
        return "adaptive_local_recall", []
    return "", ["adaptive topic evidence"]


def _has_adaptive_recall_hit(item: dict, interest: str) -> bool:
    route, missing = _matched_topic_route(item, interest)
    return bool(route and not missing and route != "not_applicable")


def _stable_source_ranking_score(item: dict, interest: str) -> float:
    source_text = _topic_evidence_source_text(item)
    source_fit = fallback_score(interest, str(item.get("title") or ""), source_text)
    local_score = max(0.0, min(1.0, _as_float(item.get("local_score") or item.get("local_tfidf_score"))))
    local_rank = _as_int(item.get("local_rank") or item.get("title_local_rank"), 0)
    local_bonus = min(0.8, local_score * 2.4)
    if local_rank > 0:
        local_bonus += max(0.0, 0.4 * (1.0 - min(local_rank, 200) / 200.0))
    abstract_bonus = 0.2 if _clean_abstract_text(item.get("abstract")) else 0.0
    evidence = str(item.get("topic_evidence") or "").lower()
    score = source_fit + local_bonus + abstract_bonus
    if evidence.startswith("passed:"):
        score += 0.35
    elif evidence.startswith("weak:"):
        score = min(score, 5.75)
    else:
        score = min(score, 6.0)
    return round(max(0.0, min(10.0, score)), 2)


def _citation_count(item: dict) -> int:
    candidates = [
        item.get("citation_count"),
        item.get("citations"),
        item.get("cited_by_count"),
        item.get("influential_citation_count"),
    ]
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    candidates.extend([
        metadata.get("citation_count"),
        metadata.get("citations"),
        metadata.get("cited_by_count"),
        metadata.get("influential_citation_count"),
    ])
    for value in candidates:
        count = _as_int(value, -1)
        if count >= 0:
            return count
    return 0


def _venue_key(value: object) -> str:
    text = str(value or "").strip().upper()
    if text == "NIPS":
        return "NEURIPS"
    return text


def _parse_release_date(value: object) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _known_conference_release_date(venue: object, year: object) -> date | None:
    parsed_year = _as_int(year, 0)
    if not parsed_year:
        return None
    return _parse_release_date(KNOWN_CONFERENCE_RELEASE_DATES.get((_venue_key(venue), parsed_year)))


def _latest_released_venue_context(venue_health_report: list[dict], *, as_of: date | None = None) -> dict:
    cutoff = as_of or datetime.now(timezone.utc).date()
    candidates: list[tuple[date, str, int, str, str]] = []
    for row in venue_health_report:
        if not row.get("ok"):
            continue
        venue = str(row.get("venue") or row.get("venue_id") or "")
        venue_key = _venue_key(venue)
        if venue_key not in FRESHNESS_BONUS_VENUES:
            continue
        for year in row.get("effective_years") or row.get("requested_years") or []:
            release_date = _known_conference_release_date(venue, year)
            parsed_year = _as_int(year, 0)
            release_signal_source = "known_release_date"
            observed_date = _parse_release_date(row.get("source_observed_date"))
            if release_date and release_date > cutoff:
                release_date = observed_date if observed_date and observed_date <= cutoff else None
                release_signal_source = "source_observed_available"
            elif not release_date:
                release_date = observed_date if observed_date and observed_date <= cutoff else None
                release_signal_source = "source_observed_available"
            if release_date and parsed_year:
                candidates.append((release_date, venue_key, parsed_year, venue, release_signal_source))
    if not candidates:
        return {"policy": SOURCE_CONTEXT_BONUS_POLICY, "as_of": cutoff.isoformat(), "eligible_venues": sorted(FRESHNESS_BONUS_VENUES), "venue": "", "venue_key": "", "year": 0, "release_date": "", "release_signal_source": ""}
    release_date, venue_key, year, venue, release_signal_source = max(candidates, key=lambda item: (item[0], item[2], item[1]))
    return {"policy": SOURCE_CONTEXT_BONUS_POLICY, "as_of": cutoff.isoformat(), "eligible_venues": sorted(FRESHNESS_BONUS_VENUES), "venue": venue, "venue_key": venue_key, "year": year, "release_date": release_date.isoformat(), "release_signal_source": release_signal_source}


def _attach_latest_released_venue_context(items: list[dict], context: dict) -> None:
    target_key = str(context.get("venue_key") or "")
    target_year = _as_int(context.get("year"), 0)
    for item in items:
        year = _as_int(item.get("year"), 0)
        eligible = bool(target_key and target_year and _venue_key(item.get("venue")) == target_key and year == target_year)
        item["latest_released_venue_context"] = context
        item["freshness_eligible_latest_released_venue"] = eligible


def _source_context_bonus(item: dict) -> tuple[float, list[str], dict]:
    citations = _citation_count(item)
    context = item.get("latest_released_venue_context") if isinstance(item.get("latest_released_venue_context"), dict) else {}
    if not _quality_bonus_allowed(item):
        return 0.0, [], {"policy": SOURCE_CONTEXT_BONUS_POLICY, "freshness_bonus": 0.0, "citation_bonus": 0.0, "citation_count": citations, "freshness_eligible_latest_released_venue": bool(item.get("freshness_eligible_latest_released_venue")), "latest_released_venue": context}
    current_year = datetime.now(timezone.utc).year
    year = _as_int(item.get("year"), 0)
    freshness_bonus = 0.0
    citation_bonus = 0.0
    reasons: list[str] = []
    if item.get("freshness_eligible_latest_released_venue"):
        freshness_bonus = 0.18
        venue = context.get("venue") or item.get("venue") or "venue"
        release_date = context.get("release_date") or ""
        reasons.append(f"三大会最新实际发布会议 {venue} {year} ({release_date}) +0.18")

    age = max(0, current_year - year) if year else 0
    if citations >= 500 and age >= 3:
        citation_bonus = 0.22
    elif citations >= 200 and age >= 2:
        citation_bonus = 0.14
    elif citations >= 80 and age >= 1:
        citation_bonus = 0.08
    if citation_bonus:
        reasons.append(f"老论文引用质量信号 {citations} citations +{citation_bonus:.2f}")

    total = round(min(0.30, freshness_bonus + citation_bonus), 2)
    detail = {"policy": SOURCE_CONTEXT_BONUS_POLICY, "freshness_bonus": freshness_bonus, "citation_bonus": citation_bonus, "citation_count": citations, "freshness_eligible_latest_released_venue": bool(item.get("freshness_eligible_latest_released_venue")), "latest_released_venue": context}
    return total, reasons, detail


FALLBACK_REASON_TEXT = "Adaptive profile-based local fallback ranking. Configure an LLM API key for model-based relevance scoring; fallback-only items cannot enter Find recommendations."
FALLBACK_FIT_EXPLANATION_TEXT = "Local title/abstract fit estimate; final relevance scoring is still required for recommendations."


def _is_default_fallback_text(value: object, default_text: str) -> bool:
    return " ".join(str(value or "").split()).lower() == " ".join(default_text.split()).lower()


def _normalize_llm_supported_text_fields(item: dict) -> None:
    if str(item.get("reason_source") or "") != "llm abstract evaluation":
        return
    if _is_default_fallback_text(item.get("reason"), FALLBACK_REASON_TEXT):
        replacement = str(item.get("reason_en") or item.get("reason_zh") or item.get("fit_explanation_en") or item.get("fit_explanation_zh") or "").strip()
        if replacement:
            item["reason"] = replacement
    if _is_default_fallback_text(item.get("fit_explanation"), FALLBACK_FIT_EXPLANATION_TEXT):
        replacement = str(item.get("fit_explanation_en") or item.get("fit_explanation_zh") or item.get("reason_en") or item.get("reason_zh") or "").strip()
        if replacement:
            item["fit_explanation"] = replacement


def _apply_stable_ranking_score(item: dict, interest: str) -> None:
    _set_quality_labels(item)
    if str(item.get("reason_source") or "") == "llm abstract evaluation":
        item["llm_fit_score"] = _as_float(item.get("fit_score"))
        item["llm_diversity_score"] = _as_float(item.get("diversity_score"))
        item["llm_combined_score"] = _combined_score(item.get("fit_score"), item.get("diversity_score"))
    base_stable = _stable_source_ranking_score(item, interest)
    item["stable_source_base_score"] = base_stable
    bonus, reasons, detail = _source_context_bonus(item)
    quality_bonus = _as_float(item.get("quality_bonus")) if _quality_bonus_allowed(item) else 0.0
    item["stable_quality_bonus"] = quality_bonus
    item["source_context_bonus"] = bonus
    item["source_context_bonus_reason"] = "; ".join(reasons)
    item["source_context_bonus_detail"] = detail
    item["freshness_bonus"] = detail.get("freshness_bonus", 0.0)
    item["freshness_eligible_latest_released_venue"] = detail.get("freshness_eligible_latest_released_venue", False)
    item["citation_quality_bonus"] = detail.get("citation_bonus", 0.0)
    item["citation_count"] = detail.get("citation_count", _citation_count(item))
    item["stable_source_score"] = round(min(10.0, base_stable + quality_bonus + bonus), 2)
    item["combined_score"] = _combined_score(item.get("fit_score"), item.get("diversity_score"))
    recommendation_base = _recommendation_display_score(item.get("llm_fit_score") or item.get("fit_score"))
    # User-visible Find ranking is the final title+abstract LLM score only.
    # Source/citation/freshness scores stay available as audit fields but cannot
    # move a paper into or upward in the recommendation list.
    item["recommendation_score"] = recommendation_base
    base_bucket = round(base_stable / 0.05) * 0.05
    bonus_bucket = round((quality_bonus + bonus) / 0.25) * 0.25
    item["stable_rank_score"] = round(min(10.0, base_bucket + bonus_bucket), 2)
    item["score"] = item["recommendation_score"]
    item["score_source"] = "llm_title_abstract_score_only"


def _apply_relevance_guard(item: dict) -> None:
    text = f"{item.get('category', '')} {item.get('reason', '')} {item.get('fit_explanation', '')}".lower()
    irrelevant_markers = ["不相关", "无关", "irrelevant", "not relevant", "unrelated"]
    if any(marker in text for marker in irrelevant_markers):
        item["fit_score"] = min(_as_float(item.get("fit_score")), 1.5)
        item["diversity_score"] = min(_as_float(item.get("diversity_score")), 1.0)
        item["score"] = min(_as_float(item.get("score")), 1.5)
        item["quality_bonus"] = 0.0
        item["quality_bonus_reason"] = ""


def _normalize_topic_evidence_value(value: object, *, default_weak: str = "weak: missing adaptive topic evidence") -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return default_weak
    lowered = text.lower()
    if lowered.startswith("passed:") or lowered.startswith("strong:") or lowered.startswith("weak:") or lowered == "not_applicable":
        return text
    if lowered in {"passed", "true", "strong", "yes"}:
        return "passed:adaptive_llm_topic_route"
    return "weak: " + text


def _has_topic_evidence_contradiction(item: dict) -> bool:
    evidence = str(item.get("topic_evidence") or "").lower()
    matched_route = str(item.get("matched_topic_route") or "").lower()
    basis = str(item.get("topic_evidence_basis") or "").lower()
    core_text = " ".join([
        evidence,
        matched_route,
        basis,
        str(item.get("category") or ""),
    ]).lower()
    explanation_text = " ".join([
        str(item.get("reason") or ""),
        str(item.get("reason_zh") or ""),
        str(item.get("reason_en") or ""),
        str(item.get("fit_explanation") or ""),
        str(item.get("fit_explanation_zh") or ""),
        str(item.get("fit_explanation_en") or ""),
    ]).lower()
    contradiction_markers = [
        "weak:", " weak due to", " weak because", "weak evidence", "weak match", "weak relevance", "weakly related",
        "missing direct", "missing evidence", "missing adaptive topic evidence",
        "not directly relevant", "unrelated", "irrelevant", "无关", "不相关", "弱相关", "弱匹配",
    ]
    if any(marker in core_text for marker in contradiction_markers):
        return True

    missing_values = item.get("missing_topic_evidence")
    if isinstance(missing_values, list):
        missing_text = " ".join(str(value) for value in missing_values).lower()
    else:
        missing_text = str(missing_values or "").lower()
    if any(marker in missing_text for marker in contradiction_markers):
        return True

    explanation_markers = [
        "only title", "title only", "title-only", "metadata only", "abstract missing", "missing abstract",
        "insufficient evidence", "not enough evidence", "lacks evidence", "without evidence", "no evidence",
        "missing required topic", "lacks required topic", "without required topic", "required topic missing",
        "missing topic evidence", "lacks topic evidence", "topic evidence missing", "missing topic axis",
        "lacks a core", "lacks core", "core component", "core direction", "core research direction",
        "not a core fit", "core direction mismatch", "topic route mismatch", "topic axis mismatch",
        "unrelated", "irrelevant", "out of scope",
        "缺少摘要", "没有摘要", "仅标题", "只有标题", "证据不足", "缺少证据", "没有证据",
        "缺少当前主题", "缺少主题证据", "未涉及当前主题轴", "缺少核心主题轴",
        "缺乏核心", "核心方向不匹配", "主题轴不匹配", "主题偏离严重",
    ]
    if any(marker in explanation_text for marker in explanation_markers):
        return True

    return False


def _apply_llm_topic_evidence(item: dict, row: dict, interest: str) -> None:
    evidence_value = row.get("topic_evidence") or row.get("topic_evidence_decision") or row.get("evidence_decision")
    supported_value = row.get("topic_evidence_supported")
    if evidence_value is None and supported_value is not None:
        evidence_value = "passed:adaptive_llm_topic_route" if bool(supported_value) else "weak: missing adaptive topic evidence"
    item["topic_evidence"] = _normalize_topic_evidence_value(evidence_value)
    if supported_value is not None and not bool(supported_value) and not item["topic_evidence"].lower().startswith("weak:"):
        item["topic_evidence"] = "weak: LLM marked topic evidence unsupported"
    item["topic_evidence_source"] = "llm_adaptive"
    item["topic_evidence_supported"] = bool(supported_value) if supported_value is not None else not item["topic_evidence"].lower().startswith("weak:")
    item["matched_topic_route"] = str(row.get("matched_topic_route") or row.get("topic_route") or "")
    item["topic_evidence_basis"] = str(row.get("topic_evidence_basis") or row.get("evidence_basis") or "")
    missing = row.get("missing_topic_evidence") or row.get("missing_evidence") or []
    if isinstance(missing, list):
        missing_list = [str(part) for part in missing if str(part).strip()]
    elif str(missing or "").strip():
        missing_list = [str(missing).strip()]
    else:
        missing_list = []
    evidence_lower = item["topic_evidence"].lower()
    matched_lower = item["matched_topic_route"].lower()
    passed_route = evidence_lower.startswith(("passed:", "strong:")) and item.get("topic_evidence_supported") is not False
    if passed_route:
        item["evidence_role"] = "foundation_borrowing" if "foundation" in evidence_lower or "foundation" in matched_lower or "基础" in evidence_lower or "借鉴" in evidence_lower else "direct_target"
        item["unmatched_topic_routes"] = missing_list
        item["missing_topic_evidence"] = []
    else:
        item["evidence_role"] = "weak_or_boundary"
        item["missing_topic_evidence"] = missing_list
        item["unmatched_topic_routes"] = []
    if item["topic_evidence"].lower().startswith("weak:") or _has_topic_evidence_contradiction(item):
        if not item["topic_evidence"].lower().startswith("weak:"):
            item["topic_evidence"] = "weak: contradictory or missing adaptive topic evidence"
        item["topic_evidence_supported"] = False
    elif not _clean_abstract_text(item.get("abstract")):
        item["topic_evidence"] = "weak: missing real abstract evidence"
        item["topic_evidence_supported"] = False
        item["topic_evidence_basis"] = item.get("topic_evidence_basis") or "title_only"
    item["topic_evidence_audit_only"] = True


def _apply_topic_evidence_guard(item: dict, interest: str) -> None:
    if str(item.get("topic_evidence_source") or "") == "llm_adaptive":
        return
    if str(item.get("reason_source") or "") == "llm title filter":
        item["topic_evidence"] = "pending: LLM title filter requires abstract evidence"
        return
    route, missing = _matched_topic_route(item, interest)
    if route == "not_applicable":
        item["topic_evidence"] = "not_applicable"
        return
    if route and not missing:
        item["topic_evidence"] = "pending: adaptive local recall requires LLM abstract evidence"
        return
    item["fit_score"] = min(_as_float(item.get("fit_score")), 5.5)
    item["diversity_score"] = min(_as_float(item.get("diversity_score")), 4.5)
    item["score"] = _combined_score(item.get("fit_score"), item.get("diversity_score"))
    item["topic_evidence"] = "weak: " + ", ".join(missing or ["adaptive topic evidence"])


def _scan_count(total: int, config: AppConfig) -> int:
    fraction = max(0.01, min(1.0, float(config.venue_title_scan_fraction or 1.0)))
    return max(1, min(total, int(total * fraction) or 1))


def _venue_title_fetch_limit(config: AppConfig) -> int | None:
    for name in ("VENUE_TITLE_SCAN_LIMIT", "FIND_VENUE_TITLE_SCAN_LIMIT"):
        raw = os.environ.get(name)
        if raw not in (None, ""):
            try:
                value = int(str(raw).strip())
            except ValueError:
                value = 0
            return max(1, value) if value > 0 else None
    try:
        value = int(getattr(config, "venue_title_scan_limit", 0) or 0)
    except (TypeError, ValueError):
        value = 0
    return max(1, value) if value > 0 else None


def _fetch_venue_title_index_for_find(venue: dict, years: list[int], limit: int | None) -> tuple[list[dict], str]:
    if limit is None:
        return fetch_venue_title_index_all(venue, years)
    return fetch_venue_title_index(venue, years, limit)


def _target_recall_count(config: AppConfig, scanned_count: int) -> int:
    requested = int(os.environ.get("FIND_RECALL_COUNT", "0") or 0)
    if requested <= 0:
        requested = int(config.find_recall_count or 0)
    if requested <= 0:
        requested = max(config.max_fetch_papers, config.venue_title_scan_limit)
    return max(1, min(scanned_count, requested))


def _min_title_candidates(config: AppConfig, scanned_count: int) -> int:
    requested = int(os.environ.get("MIN_TITLE_CANDIDATES", "0") or 0)
    if requested <= 0:
        requested = max(
            60,
            int(config.max_recommended_papers or 0) * 3,
            int(config.max_fetch_papers or 0),
            int(config.detail_fetch_count or 0),
        )
    return max(1, min(scanned_count, requested))


def _min_detail_candidates(config: AppConfig, recall_count: int) -> int:
    requested = int(os.environ.get("MIN_DETAIL_CANDIDATES", "0") or 0)
    if requested <= 0:
        requested = max(40, int(config.max_recommended_papers or 0) * 2)
    return max(1, min(recall_count, requested))


def _large_pool_threshold() -> int:
    return max(1, int(os.environ.get("LARGE_TITLE_POOL_THRESHOLD", "800") or 800))


def _full_venue_corpus_audit_enabled(config: AppConfig) -> bool:
    value = os.environ.get("FULL_VENUE_CORPUS_AUDIT")
    if value is not None:
        return value.lower() in {"1", "true", "yes", "on", "full"}
    return bool(getattr(config, "full_venue_corpus_audit", True))


def _local_database_metadata_audit(local: dict) -> dict:
    papers = local.get("papers") or []
    summary = local.get("category_summary") if isinstance(local.get("category_summary"), dict) else {}
    expected_count = int(local.get("paper_count") or 0)
    category_entries = summary.get("category_summary") if isinstance(summary.get("category_summary"), list) else []
    manifest_audit = local.get("metadata_completeness_audit") if isinstance(local.get("metadata_completeness_audit"), dict) else {}
    manifest = local.get("manifest") if isinstance(local.get("manifest"), dict) else {}
    source_adapter = str(
        local.get("source_adapter")
        or manifest_audit.get("adapter")
        or manifest_audit.get("source_adapter")
        or manifest.get("adapter")
        or manifest.get("source_adapter")
        or "local_database"
    )
    source_scope_hint = str(manifest_audit.get("source_scope") or manifest.get("source_scope") or "")
    is_dblp_current_index = source_scope_hint == "dblp_current_index_not_official_accepted_list" or source_adapter.startswith("dblp")
    category_status_hint = str(manifest_audit.get("category_status") or "").lower()
    categories_are_official = bool(manifest_audit.get("has_official_categories")) and category_status_hint not in {"no_official_categories", "missing_categories", "no_or_partial_categories"}
    category_total = 0
    for entry in category_entries:
        if isinstance(entry, dict):
            try:
                category_total += int(entry.get("count") or 0)
            except Exception:
                pass
    missing_titles = sum(1 for paper in papers if not str((paper if isinstance(paper, dict) else {}).get("title") or "").strip())
    missing_abstracts = sum(1 for paper in papers if not _clean_abstract_text((paper if isinstance(paper, dict) else {}).get("abstract")))
    local_files_consistent = (
        expected_count > 0
        and len(papers) == expected_count
        and missing_titles == 0
        and (not categories_are_official or (bool(category_entries) and category_total == expected_count))
    )
    complete = local_files_consistent and (bool(manifest_audit.get("complete")) if manifest_audit else True)
    audit = dict(manifest_audit) if manifest_audit else {}
    has_official_categories = categories_are_official
    category_status = str(audit.get("category_status") or ("official_or_cached_categories" if has_official_categories else "no_official_categories"))
    if is_dblp_current_index:
        source_scope_hint = "dblp_current_index_not_official_accepted_list"
    official_title_index_verified = audit.get("official_title_index_verified")
    official_accepted_list_verified = audit.get("official_accepted_list_verified")
    if source_scope_hint == "dblp_current_index_not_official_accepted_list":
        official_title_index_verified = False
        official_accepted_list_verified = False
    elif source_scope_hint in {"official_icml_downloads_title_index", "official_openreview_metadata"}:
        official_title_index_verified = complete if official_title_index_verified in (None, "") else bool(official_title_index_verified)
        official_accepted_list_verified = complete if official_accepted_list_verified in (None, "") else bool(official_accepted_list_verified)
    audit.update({
        "schema_version": 1,
        "status": "complete" if complete else "partial",
        "source_verified": complete,
        "complete": complete,
        "title_index_complete": complete,
        "official_metadata_complete": bool(complete and (missing_abstracts == 0 or has_official_categories)),
        "adapter": "local_database",
        "source_url": audit.get("source_url") or summary.get("source") or local.get("source") or "local_database",
        "source_adapter": source_adapter,
        "source_scope": source_scope_hint,
        "official_title_index_verified": official_title_index_verified,
        "official_accepted_list_verified": official_accepted_list_verified,
        "paper_count": len(papers),
        "expected_paper_count": expected_count,
        "category_count": len(category_entries),
        "category_total_count": category_total,
        "missing_title_count": missing_titles,
        "missing_abstract_count": missing_abstracts,
        "has_abstracts": bool(papers) and missing_abstracts == 0,
        "any_abstracts": bool(papers) and missing_abstracts < len(papers),
        "has_official_categories": has_official_categories,
        "category_status": category_status,
        "papers_path": local.get("papers_path"),
        "category_summary_path": local.get("category_summary_path"),
        "manifest_path": local.get("manifest_path") or "",
        "local_files_consistent": local_files_consistent,
        "completeness_basis": "Local venue database integrity check: manifest/source audit plus papers.json count, category_summary counts, titles, and category file must agree before it is treated as a reusable complete title corpus.",
    })
    return audit


def _online_venue_metadata_audit(papers: list[dict], adapter: str) -> dict:
    audit = venue_metadata_audit_from_papers(papers)
    if not audit:
        missing_abstracts = sum(1 for paper in papers if not _clean_abstract_text((paper if isinstance(paper, dict) else {}).get("abstract")))
        has_categories = any(_paper_category(paper) for paper in papers if isinstance(paper, dict))
        audit = {
            "schema_version": 1,
            "status": "partial",
            "source_verified": bool(papers),
            "complete": False,
            "adapter": adapter,
            "paper_count": len(papers),
            "missing_abstract_count": missing_abstracts,
            "has_abstracts": bool(papers) and missing_abstracts == 0,
            "any_abstracts": bool(papers) and missing_abstracts < len(papers),
            "has_official_categories": has_categories,
            "category_status": "present" if has_categories else "no_official_categories",
            "completeness_basis": "Adapter did not provide an explicit venue metadata completeness audit; keep source partial until adapter verifies all pages/records.",
        }
    return audit


def _venue_metadata_status_fields(audit: dict) -> dict:
    if not isinstance(audit, dict):
        audit = {}
    title_status = str(audit.get("title_index_completeness_status") or audit.get("status") or ("complete" if audit.get("complete") else "partial" if audit else "unknown"))
    title_complete = bool(audit.get("title_index_complete") if audit.get("title_index_complete") is not None else audit.get("complete"))
    has_abstracts = bool(audit.get("has_abstracts"))
    has_official_categories = bool(audit.get("has_official_categories"))
    category_status = str(audit.get("category_status") or "unknown")
    no_official_categories = category_status.lower() in {"no_official_categories", "missing_categories", "no_or_partial_categories"}
    source_scope = str(audit.get("source_scope") or "")
    official_title_index_verified = audit.get("official_title_index_verified")
    official_accepted_list_verified = audit.get("official_accepted_list_verified")
    if source_scope == "dblp_current_index_not_official_accepted_list":
        official_title_index_verified = False
        official_accepted_list_verified = False
    elif source_scope in {"official_icml_downloads_title_index", "official_openreview_metadata"}:
        if official_title_index_verified in (None, ""):
            official_title_index_verified = title_complete
        if official_accepted_list_verified in (None, ""):
            official_accepted_list_verified = title_complete
    metadata_ready = title_complete and (has_abstracts or has_official_categories)
    if not audit:
        metadata_status = "unknown"
    elif metadata_ready:
        metadata_status = "complete"
    elif title_complete:
        metadata_status = "title_index_only"
    else:
        metadata_status = "partial"
    basis_parts = []
    if audit.get("completeness_basis"):
        basis_parts.append(str(audit.get("completeness_basis")))
    if title_complete and not has_abstracts:
        basis_parts.append("Title corpus was verified, but this source does not expose abstracts in the title index; The workflow must enrich selected papers before final LLM scoring.")
    if no_official_categories:
        basis_parts.append("No trusted official venue categories were available from this adapter; the workflow skips category pruning and uses title LLM screening over the title corpus.")
    return {
        "title_index_completeness_status": title_status,
        "title_index_completeness_ok": title_complete,
        "metadata_completeness_status": metadata_status,
        "metadata_completeness_ok": metadata_ready,
        "metadata_completeness_limited": bool(audit) and not metadata_ready,
        "metadata_completeness_basis": " ".join(part.strip() for part in basis_parts if part).strip(),
        "metadata_audit": audit,
        "has_official_categories": has_official_categories,
        "category_status": audit.get("category_status") or "unknown",
        "has_abstracts": has_abstracts,
        "has_abstracts_in_title_index": has_abstracts,
        "any_abstracts": bool(audit.get("any_abstracts")),
        "missing_abstract_count": int(audit.get("missing_abstract_count") or 0),
        "source_scope": source_scope,
        "source_adapter": audit.get("source_adapter") or audit.get("adapter") or "",
        "official_title_index_verified": official_title_index_verified,
        "official_accepted_list_verified": official_accepted_list_verified,
        "source_verified": bool(audit.get("source_verified")),
        "title_index_complete": title_complete,
        "official_metadata_complete": metadata_ready,
    }


def _combined_metadata_audit(audits: list[dict], adapter: str) -> dict:
    valid = [audit for audit in audits if isinstance(audit, dict) and audit]
    if not valid:
        return {}
    complete = all(bool(audit.get("complete")) for audit in valid)
    statuses = list(dict.fromkeys(str(audit.get("status") or "unknown") for audit in valid))
    category_statuses = [str(audit.get("category_status") or "unknown") for audit in valid]
    all_have_abstracts = all(bool(audit.get("has_abstracts")) for audit in valid)
    any_have_abstracts = any(bool(audit.get("has_abstracts") or audit.get("any_abstracts")) for audit in valid)
    all_have_official_categories = all(bool(audit.get("has_official_categories")) for audit in valid)
    source_scopes = [str(audit.get("source_scope") or "") for audit in valid if audit.get("source_scope")]
    source_scope = "mixed" if len(set(source_scopes)) > 1 else (source_scopes[0] if source_scopes else "")
    source_adapters = [str(audit.get("source_adapter") or "") for audit in valid if audit.get("source_adapter")]
    source_adapter = "mixed" if len(set(source_adapters)) > 1 else (source_adapters[0] if source_adapters else adapter)
    if source_scope == "dblp_current_index_not_official_accepted_list":
        official_title_index_verified = False
        official_accepted_list_verified = False
    else:
        official_title_index_verified = all(bool(audit.get("official_title_index_verified")) for audit in valid) if any("official_title_index_verified" in audit for audit in valid) else None
        official_accepted_list_verified = all(bool(audit.get("official_accepted_list_verified")) for audit in valid) if any("official_accepted_list_verified" in audit for audit in valid) else None
    return {
        "schema_version": 1,
        "status": statuses[0] if len(statuses) == 1 else "mixed",
        "source_verified": all(bool(audit.get("source_verified")) for audit in valid),
        "complete": complete,
        "title_index_complete": complete,
        "adapter": adapter,
        "yeaudits": valid,
        "paper_count": sum(int(audit.get("paper_count") or audit.get("deduped_paper_count") or 0) for audit in valid),
        "expected_paper_count": sum(int(audit.get("expected_paper_count") or audit.get("paper_count") or audit.get("deduped_paper_count") or 0) for audit in valid),
        "missing_title_count": sum(int(audit.get("missing_title_count") or 0) for audit in valid),
        "missing_abstract_count": sum(int(audit.get("missing_abstract_count") or 0) for audit in valid),
        "has_abstracts": all_have_abstracts,
        "any_abstracts": any_have_abstracts,
        "has_official_categories": all_have_official_categories,
        "category_status": "official_or_cached_categories" if all_have_official_categories else "no_or_partial_categories",
        "source_scope": source_scope,
        "source_adapter": source_adapter,
        "official_title_index_verified": official_title_index_verified,
        "official_accepted_list_verified": official_accepted_list_verified,
        "category_statuses": category_statuses,
        "official_metadata_complete": bool(complete and (all_have_abstracts or all_have_official_categories)),
        "completeness_basis": "; ".join(str(audit.get("completeness_basis") or "").strip() for audit in valid if str(audit.get("completeness_basis") or "").strip())[:1000],
    }


def _selection_list_value(selection: object, name: str, fallback_name: str = "") -> list[Any]:
    value: Any = None
    if isinstance(selection, dict):
        value = selection.get(name)
        if value is None and fallback_name:
            value = selection.get(fallback_name)
    else:
        value = getattr(selection, name, None)
        if value is None and fallback_name:
            value = getattr(selection, fallback_name, None)
    return value if isinstance(value, list) else []


def _normalize_selection_years(value: Any) -> list[int]:
    raw_values = value if isinstance(value, list) else ([] if value is None else [value])
    years: list[int] = []
    seen: set[int] = set()
    for item in raw_values:
        try:
            year = int(item)
        except (TypeError, ValueError):
            continue
        if year < 2000 or year > 2100 or year in seen:
            continue
        seen.add(year)
        years.append(year)
    return years or [date.today().year]


def _selection_venue_year_pairs(selection: object) -> list[dict[str, int | str]]:
    raw_pairs = _selection_list_value(selection, "venue_years")
    pairs: list[dict[str, int | str]] = []
    seen: set[tuple[str, int]] = set()
    for item in raw_pairs:
        venue_id = ""
        raw_years: Any = None
        if isinstance(item, dict):
            venue_id = str(item.get("venue_id") or item.get("venue") or item.get("id") or "").strip()
            raw_years = item.get("years") if isinstance(item.get("years"), list) else item.get("year")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            venue_id = str(item[0] or "").strip()
            raw_years = item[1]
        if not venue_id:
            continue
        for year in _normalize_selection_years(raw_years):
            key = (venue_id, year)
            if key in seen:
                continue
            seen.add(key)
            pairs.append({"venue_id": venue_id, "year": year})
    if pairs:
        return pairs
    venue_ids = [str(item or "").strip() for item in _selection_list_value(selection, "venue_ids", "venues")]
    venue_ids = [item for index, item in enumerate(venue_ids) if item and item not in venue_ids[:index]]
    years = _normalize_selection_years(_selection_list_value(selection, "years"))
    for venue_id in venue_ids:
        for year in years:
            key = (venue_id, year)
            if key in seen:
                continue
            seen.add(key)
            pairs.append({"venue_id": venue_id, "year": year})
    return pairs


def _selection_venue_year_groups(selection: object) -> list[tuple[str, list[int]]]:
    return [(str(pair["venue_id"]), [int(pair["year"])]) for pair in _selection_venue_year_pairs(selection)]


def _selection_venue_unit_count(selection: object) -> int:
    pairs = _selection_venue_year_pairs(selection)
    if pairs:
        return len(pairs)
    venues = _selection_list_value(selection, "venue_ids", "venues")
    return len(venues)


def _source_count_hint(selection: object) -> int:
    if not isinstance(selection, dict):
        return 0
    count = _selection_venue_unit_count(selection)
    for name in ("include_arxiv", "include_biorxiv", "include_huggingface", "include_github", "include_nature", "include_science"):
        if selection.get(name):
            count += 1
    return count

def _recommendation_target_hint(config: AppConfig) -> int:
    configured = int(os.environ.get("STRONG_RECOMMENDATION_TARGET_COUNT", "0") or 0)
    if configured > 0:
        return max(1, configured)
    source_count = _source_count_hint(config.default_find_selection or {})
    source_target = max(1, source_count) * 5 if source_count > 0 else 0
    requested = int(config.max_recommended_papers or 0)
    # This hint sizes title/detail recall pools, not the final visible Top-N.
    # A project may keep a compact visible recommendation target while asking
    # Find to inspect a much wider candidate pool before final title+abstract
    # scoring. Use the larger configured hint so large venues are not sampled
    # down to only a few hundred titles before abstracts are fetched.
    return max(1, requested, source_target, 20)


def _llm_title_filter_scan_budget(config: AppConfig, scanned_count: int) -> int:
    explicit = int(os.environ.get("LLM_TITLE_FILTER_MAX_TITLES", "0") or 0)
    if explicit <= 0:
        target = _recommendation_target_hint(config)
        recall_budget = _target_recall_count(config, scanned_count)
        detail_budget = int(getattr(config, "detail_fetch_count", 0) or 0)
        default_cap = int(os.environ.get("LLM_TITLE_FILTER_DEFAULT_MAX_TITLES", "0") or 0)
        if default_cap <= 0:
            default_cap = 5000
        explicit = max(
            400,
            recall_budget,
            detail_budget * 2,
            min(default_cap, target * 100),
        )
    return max(1, min(scanned_count, explicit))


def _local_title_screen_budget(config: AppConfig, scanned_count: int) -> int:
    explicit = int(os.environ.get("LOCAL_TITLE_SCREEN_MAX", "0") or 0)
    if explicit <= 0:
        target = _recommendation_target_hint(config)
        explicit = max(160, min(1200, target * 25))
    return max(1, min(scanned_count, explicit))


def _title_detail_candidate_target(config: AppConfig, scanned_count: int) -> int:
    explicit = int(os.environ.get("TITLE_DETAIL_CANDIDATE_TARGET", "0") or 0)
    if explicit <= 0:
        target = _recommendation_target_hint(config)
        detail_budget = int(getattr(config, "detail_fetch_count", 0) or 0)
        recall_budget = _target_recall_count(config, scanned_count)
        explicit = max(240, detail_budget, min(recall_budget, max(1500, target * 60)))
    return max(1, min(scanned_count, explicit))


def _title_score_floor(config: AppConfig) -> float:
    explicit = os.environ.get("TITLE_SCORE_FLOOR")
    if explicit not in (None, ""):
        return max(0.0, min(10.0, _as_float(explicit)))
    return 0.0


def _title_rank_key(row: dict) -> tuple:
    title_fit = _as_float(row.get("title_llm_fit_score"), row.get("fit_score"))
    return (
        -title_fit,
        _stable_rank_key(row),
    )


def _score_title_pool(items: list[dict], config: AppConfig, interest: str, *, global_limit: int | None = None) -> list[dict]:
    """Rank a broad title pool locally before expensive detail fetching."""
    if not items:
        return []
    query = "\\n".join(part for part in [interest, "\\n".join(config.arxiv_queries or [])] if part).strip()
    per_category_limit = int(os.environ.get("TITLE_RANK_PER_CATEGORY", "0") or 0)
    if per_category_limit <= 0:
        per_category_limit = 200
    limit = int(global_limit or 0)
    if limit <= 0:
        limit = max(_target_recall_count(config, len(items)), len(items))
    limit = max(1, min(len(items), limit))
    ranked, _report = rank_papers_tfidf(
        items,
        query,
        per_category_limit=max(50, min(per_category_limit, limit)),
        global_limit=limit,
    )
    return ranked


def _local_title_screen_pool(items: list[dict], config: AppConfig, interest: str) -> list[dict]:
    deduped = _dedupe_items(items)
    target = _local_title_screen_budget(config, len(deduped))
    ranked = _score_title_pool(deduped, config, interest, global_limit=target)
    for index, item in enumerate(ranked, 1):
        item["title_local_rank"] = index
        item.setdefault("evidence_tier", "retrieval_only")
        item.setdefault(
            "recommendation_note",
            "Title-screened row retained for detail scoring; final recommendations are decided only after real abstract retrieval and final relevance scoring.",
        )
        item.setdefault("recommendation_note_zh", "题名筛选后进入详情评分；是否推荐只由真实摘要和最终相关性评分决定。")
        item.setdefault("recommendation_note_en", "Title-screened row retained for detail scoring; recommendation is decided only from real abstracts and final relevance scoring.")
    return ranked


def _ranked_recall_pool(items: list[dict], config: AppConfig) -> list[dict]:
    ranked = sorted(_dedupe_items(items), key=_stable_rank_key)
    return ranked[: _target_recall_count(config, len(ranked))]


def _venue_recall_result_limit(config: AppConfig, scanned_count: int) -> int:
    # Venue discovery is a broad title-screening stage. max_fetch_papers is kept for
    # non-venue sources; conference title pools should honor the Find recall/detail
    # settings so large proceedings are not collapsed before abstract scoring.
    requested = max(
        _target_recall_count(config, scanned_count),
        int(config.detail_fetch_count or 0),
        int(config.max_recommended_papers or 0),
    )
    return max(1, min(scanned_count, requested))


def _detail_fetch_count(config: AppConfig, recall_count: int) -> int:
    requested = int(os.environ.get("DETAIL_FETCH_COUNT", "0") or 0)
    if requested <= 0:
        requested = int(config.detail_fetch_count or 0)
    if requested <= 0:
        requested = max(config.max_fetch_papers, config.max_recommended_papers, min(recall_count, 80))
    requested = max(requested, _min_detail_candidates(config, recall_count))
    return max(1, min(recall_count, requested))


def _venue_detail_fetch_count(config: AppConfig, recall_count: int) -> int:
    """Return how many venue candidates enter detail fetch and LLM scoring.

    Conference proceedings can expose thousands of retained title candidates.
    Detail pages are slow external resources, so the default must be bounded by
    the run config. A caller can explicitly opt into full venue detail scoring
    for controlled audits.
    """
    if os.environ.get("FULL_VENUE_DETAIL_FETCH", "0").lower() in {"1", "true", "yes", "on", "full"}:
        return max(1, recall_count)
    explicit = int(os.environ.get("VENUE_DETAIL_FETCH_COUNT", "0") or 0)
    if explicit > 0:
        return max(1, min(recall_count, explicit))
    return _detail_fetch_count(config, recall_count)


def _venue_detail_wall_timeout_sec(venue_name: str, adapter: str, candidate_count: int) -> float:
    explicit = float(os.environ.get("VENUE_DETAIL_WALL_TIMEOUT_SEC", "0") or 0)
    if explicit > 0:
        return explicit
    adapter_text = str(adapter or "").lower()
    if "icml" in adapter_text:
        return float(os.environ.get("ICML_DETAIL_WALL_TIMEOUT_SEC", "180") or 180)
    if candidate_count >= 500:
        return float(os.environ.get("LARGE_VENUE_DETAIL_WALL_TIMEOUT_SEC", "120") or 120)
    return float(os.environ.get("DEFAULT_VENUE_DETAIL_WALL_TIMEOUT_SEC", "90") or 90)


def _target_triage_candidate_count(config: AppConfig) -> int:
    requested = int(os.environ.get("TRIAGE_CANDIDATE_COUNT", os.environ.get("READ_CANDIDATE_COUNT", "0")) or 0)
    return max(int(config.max_recommended_papers or 0), requested or 50, 30)


def _final_llm_scoring_limit(config: AppConfig, candidate_count: int) -> int:
    configured = int(os.environ.get("FINAL_LLM_SCORING_LIMIT", "0") or 0)
    if configured > 0:
        return max(1, min(candidate_count, configured))
    target = _strong_recommendation_target_count(config)
    # Strong recommendations can only come from final LLM-scored rows. By default
    # every detail-fetched candidate is judged by the LLM; explicit environment
    # limits are the only way to reduce this for a one-off constrained run.
    default_limit = candidate_count
    return max(1, min(candidate_count, default_limit))


def _abstract_enrichment_limits(config: AppConfig, missing_count: int) -> tuple[int, int]:
    # Every candidate that reaches final LLM scoring needs a real abstract
    # attempt, but metadata services can be slow or rate-limited. Bound the
    # enrichment queue to the actual final scoring budget; unfilled candidates
    # stay audit-only and cannot enter strong recommendations.
    explicit = int(os.environ.get("ABSTRACT_ENRICH_MAX_ITEMS", "0") or 0)
    if explicit <= 0:
        explicit = _final_llm_scoring_limit(config, missing_count)
    limit = max(0, min(missing_count, explicit))
    return limit, limit



def _abstract_lookup_failure_reason(item: dict) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    reasons: list[str] = []
    for key in (
        "detail_fetch_deferred_reason",
        "detail_fetch_error",
        "abstract_enrichment_failure",
        "openalex_lookup_error",
        "semantic_schollookup_error",
    ):
        value = str(metadata.get(key) or item.get(key) or "").strip()
        if value and value not in reasons:
            reasons.append(value)
    if item.get("detail_fetch_deferred") or metadata.get("detail_fetch_deferred"):
        reasons.append("venue_detail_fetch_deferred")
    doi = str(item.get("doi") or metadata.get("doi") or "").strip()
    if doi:
        reasons.append(f"doi_metadata_lookup_no_real_abstract:{doi}")
    if not reasons:
        reasons.append("metadata_lookup_no_real_abstract_after_openalex_semantic_scholar")
    return "; ".join(reasons[:4])


def _find_selection_allows_arxiv(config: AppConfig) -> bool:
    selection = config.default_find_selection if isinstance(config.default_find_selection, dict) else {}
    return bool(selection.get("include_arxiv"))


def _abstract_enrichment_timed_out(started_at: float, wall_limit: float) -> bool:
    return datetime.now(timezone.utc).timestamp() - started_at >= wall_limit


def _abstract_enrichment_filled_count(selected: list[dict], before: int) -> int:
    return max(0, sum(1 for item in selected if str(item.get("abstract") or "").strip()) - before)


def _enrich_missing_abstracts_for_adaptive_recall(
    detailed: list[dict],
    config: AppConfig,
    venue_name: str,
    log: LogFn,
    progress: ProgressFn,
    should_cancel: CancelFn = lambda: False,
) -> list[dict]:
    missing = [item for item in detailed if not str(item.get("abstract") or "").strip()]
    if not missing:
        return detailed
    interest = _topic_interest_text(config)
    adaptive_limit, general_limit = _abstract_enrichment_limits(config, len(missing))
    adaptive_recall = [item for item in missing if interest and _has_adaptive_recall_hit(item, interest)]
    allow_arxiv_title_match = _find_selection_allows_arxiv(config)
    enrichment_sources = ["semantic_scholar", "openalex"]
    if allow_arxiv_title_match:
        enrichment_sources.append("arxiv_title_match")

    selected: list[dict] = []
    seen: set[str] = set()
    for item in adaptive_recall[:adaptive_limit]:
        key = str(item.get("id") or item.get("url") or item.get("title") or "")
        if not key or key in seen:
            continue
        selected.append(item)
        seen.add(key)
    for item in missing[:general_limit]:
        key = str(item.get("id") or item.get("url") or item.get("title") or "")
        if not key or key in seen:
            continue
        selected.append(item)
        seen.add(key)

    selected_ids = {id(item) for item in selected}
    for item in missing:
        metadata = item.setdefault("metadata", {})
        if id(item) in selected_ids:
            item["abstract_enrichment_attempted"] = True
            metadata["abstract_enrichment_attempted"] = True
            metadata["abstract_enrichment_sources"] = enrichment_sources
            if not allow_arxiv_title_match:
                metadata["arxiv_title_match_skipped"] = "include_arxiv_disabled"
        else:
            item["abstract_enrichment_attempted"] = False
            metadata["abstract_enrichment_failure"] = "not_selected_within_abstract_enrichment_budget"
            item.setdefault("abstract_fetch_failed_reason", "not_selected_within_abstract_enrichment_budget")

    if not selected:
        return detailed
    before = sum(1 for item in selected if str(item.get("abstract") or "").strip())
    batch_size = max(1, int(os.environ.get("ABSTRACT_ENRICH_BATCH_SIZE", "0") or 0) or 4)
    total = len(selected)
    configured_wall = float(os.environ.get("ABSTRACT_ENRICH_WALL_TIMEOUT_SEC", "0") or 0)
    if configured_wall > 0:
        wall_limit = max(30.0, configured_wall)
    else:
        wall_limit = max(180.0, min(1200.0, 45.0 + total * 2.5))
    started_at = datetime.now(timezone.utc).timestamp()
    source_text = ", ".join(enrichment_sources)
    progress("abstract_enrichment", 0, total, f"{venue_name}: enriching abstracts via {source_text}")
    processed = 0
    timed_out = False
    for batch in _chunks(selected, batch_size):
        batch_start = processed + 1
        batch_end = min(total, processed + len(batch))
        _raise_if_cancelled(should_cancel)
        if _abstract_enrichment_timed_out(started_at, wall_limit):
            timed_out = True
            break
        progress("abstract_enrichment", processed, total, f"{venue_name}: semantic_scholar lookup {batch_start}-{batch_end}/{total}")
        enrich_with_semantic_scholar(batch, limit=len(batch))
        _raise_if_cancelled(should_cancel)
        if _abstract_enrichment_timed_out(started_at, wall_limit):
            timed_out = True
            break
        current_filled = _abstract_enrichment_filled_count(selected, before)
        progress("abstract_enrichment", processed, total, f"{venue_name}: semantic_scholar done {batch_start}-{batch_end}/{total}, filled {current_filled}")
        still_missing = [item for item in batch if not str(item.get("abstract") or "").strip()]
        if still_missing:
            progress("abstract_enrichment", processed, total, f"{venue_name}: openalex lookup {batch_start}-{batch_end}/{total}")
            enrich_with_openalex(still_missing, limit=len(still_missing))
        _raise_if_cancelled(should_cancel)
        if _abstract_enrichment_timed_out(started_at, wall_limit):
            timed_out = True
            break
        current_filled = _abstract_enrichment_filled_count(selected, before)
        progress("abstract_enrichment", processed, total, f"{venue_name}: openalex done {batch_start}-{batch_end}/{total}, filled {current_filled}")
        still_missing = [item for item in batch if not str(item.get("abstract") or "").strip()]
        if still_missing and allow_arxiv_title_match:
            progress("abstract_enrichment", processed, total, f"{venue_name}: arxiv title match {batch_start}-{batch_end}/{total}")
            enrich_with_arxiv_title_match(still_missing, limit=len(still_missing))
            _raise_if_cancelled(should_cancel)
            if _abstract_enrichment_timed_out(started_at, wall_limit):
                timed_out = True
                break
        elif still_missing:
            for item in still_missing:
                item.setdefault("metadata", {})["arxiv_title_match_skipped"] = "include_arxiv_disabled"
        processed += len(batch)
        current_filled = _abstract_enrichment_filled_count(selected, before)
        progress(
            "abstract_enrichment",
            processed,
            total,
            f"{venue_name}: enriched {processed}/{total} candidates, filled {current_filled} abstracts",
        )
    if timed_out:
        for item in selected[processed:]:
            metadata = item.setdefault("metadata", {})
            metadata["abstract_enrichment_failure"] = f"wall_timeout_{wall_limit:.0f}s"
            item["abstract_fetch_failed_reason"] = f"wall_timeout_{wall_limit:.0f}s"
        log(
            f"{venue_name}: abstract enrichment stopped after {processed}/{total} candidates due to "
            f"wall timeout {wall_limit:.0f}s; candidates without real abstracts remain audit-only."
        )
    inspected = selected[:processed if timed_out else len(selected)]
    for item in inspected:
        if not str(item.get("abstract") or "").strip():
            reason = _abstract_lookup_failure_reason(item)
            item["abstract_fetch_failed_reason"] = reason
            item.setdefault("metadata", {})["abstract_enrichment_failure"] = reason
    after = sum(1 for item in selected if str(item.get("abstract") or "").strip())
    filled = max(0, after - before)
    log(
        f"{venue_name}: abstract enrichment filled {filled}/{len(selected)} missing abstracts; "
        f"adaptive-profile priority={min(len(adaptive_recall), adaptive_limit)}, "
        f"general_limit={general_limit}, total_missing={len(missing)}, batch_size={batch_size}, sources={source_text}"
    )
    final_current = processed if timed_out else total
    final_message = f"{venue_name}: abstract enrichment stopped at {processed}/{total} by wall timeout" if timed_out else f"{venue_name}: abstract enrichment complete"
    progress("abstract_enrichment", final_current, total, final_message)
    return detailed


def _enrich_missing_abstracts_for_final_scoring(
    scoring_items: list[dict],
    config: AppConfig,
    source_name: str,
    log: LogFn,
    progress: ProgressFn,
    should_cancel: CancelFn = lambda: False,
) -> list[dict]:
    missing_before = [item for item in scoring_items if not _has_real_abstract(item)]
    if not missing_before:
        return scoring_items
    for item in missing_before:
        item.setdefault("metadata", {})["abstract_enrichment_stage"] = "final_llm_scoring_pool"
    _enrich_missing_abstracts_for_adaptive_recall(scoring_items, config, source_name, log, progress, should_cancel)
    filled = sum(1 for item in missing_before if _has_real_abstract(item))
    still_missing = [item for item in missing_before if not _has_real_abstract(item)]
    for item in still_missing:
        reason = _abstract_lookup_failure_reason(item)
        item["abstract_fetch_failed_reason"] = reason
        item["llm_final_scoring_skip_reason"] = reason
    log(
        f"{source_name}: final scoring abstract enrichment filled {filled}/{len(missing_before)} "
        f"title-filtered candidates before LLM title+abstract scoring; still_missing={len(still_missing)}"
    )
    return scoring_items



def _apply_result_limit_with_floor(items: list[dict], limit: int | None, config: AppConfig) -> list[dict]:
    if not limit or len(items) <= limit:
        return items
    effective_limit = max(int(limit), _min_title_candidates(config, len(items)))
    return items[:effective_limit]


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
) -> list[dict]:
    if not items:
        return []
    interest = _topic_interest_text(config)
    scoring_interest = _compact_scoring_interest(config, interest)
    scanned = list(items if scan_all else items[: _scan_count(len(items), config)])
    original_scanned_count = len(scanned)
    trusted_title_categories = _has_trusted_title_categories(scanned)
    if original_scanned_count >= _large_pool_threshold():
        shortlist_budget = _llm_title_filter_scan_budget(config, original_scanned_count)
        should_local_shortlist = shortlist_budget < original_scanned_count and (not llm.enabled or trusted_title_categories)
        if should_local_shortlist:
            scanned = _score_title_pool(scanned, config, interest, global_limit=shortlist_budget)
            log(
                f"{venue_name}: large title pool ({original_scanned_count} titles); "
                f"locally shortlisted {len(scanned)} titles before title LLM/detail scoring"
            )
        elif llm.enabled and shortlist_budget < original_scanned_count:
            log(
                f"{venue_name}: large title pool ({original_scanned_count} titles) has no trusted official categories; "
                "scoring the full title pool with the title LLM instead of TF-IDF pre-cutting recall"
            )
    title_groups = _title_filter_groups(scanned) if dynamic_title_filter and trusted_title_categories else []
    group_by_id = {
        str(item.get("id") or ""): group
        for group in title_groups
        for item in group["items"]
    }
    by_id = {item.get("id", ""): item for item in scanned}
    for item in scanned:
        title = str(item.get("title") or "")
        abstract = _clean_abstract_text(item.get("abstract"))
        title_fit = fallback_score(interest, title, "")
        text_fit = fallback_score(interest, title, abstract) if abstract else title_fit
        retrieval_fit = max(title_fit, text_fit)
        item["title_fit_score"] = title_fit
        item["abstract_fit_score"] = text_fit if abstract else 0.0
        item["retrieval_fit_score"] = retrieval_fit
        item["abstract_aware_prefilter"] = bool(abstract and text_fit > title_fit + 0.01)
        item["fit_score"] = retrieval_fit
        item["diversity_score"] = min(8.0, max(0.0, retrieval_fit - 1.0))
        item["score"] = _combined_score(item["fit_score"], item["diversity_score"])
        item["hit_directions"] = []
        item["title_reason"] = "Title+abstract/profile retrieval prefilter." if abstract else "Adaptive profile title prefilter."
        item["reason_source"] = "local title screen"
        _apply_quality_bonus(item)
        group = group_by_id.get(str(item.get("id") or ""))
        if group:
            item["title_filter_context"] = {
                "venue": group["venue"],
                "year": group["year"],
                "category": group["category"],
                "category_size": group["category_size"],
                "venue_yetotal": group["venue_yetotal"],
                "category_ratio": group["category_ratio"],
                "strictness": group["policy"]["label"],
                "min_fit_score": group["policy"]["min_score"],
                "keep_ratio": group["policy"]["keep_ratio"],
            }

    use_llm_title_filter = os.environ.get("USE_LLM_TITLE_FILTER", "1").lower() in {"1", "true", "yes", "on"}
    force_llm_title_filter = os.environ.get("FORCE_LLM_TITLE_FILTER", "0").lower() in {"1", "true", "yes", "on"}
    if os.environ.get("DISABLE_LLM_TITLE_FILTER", "0").lower() in {"1", "true", "yes", "on"}:
        use_llm_title_filter = False
        if original_scanned_count >= _large_pool_threshold():
            log(
                f"{venue_name}: large title pool ({original_scanned_count} titles); using bounded local title ranking "
                f"over {len(scanned)} shortlisted titles before detail scoring because LLM title filtering is disabled. "
                "Final recommendations still require real abstracts and final relevance scoring."
            )
        else:
            log(
                f"{venue_name}: using local title ranking before detail scoring; "
                "final recommendations still require real abstracts and final relevance scoring."
            )
    elif original_scanned_count >= _large_pool_threshold() and not force_llm_title_filter:
        if llm.enabled:
            use_llm_title_filter = True
            if len(scanned) < original_scanned_count:
                log(
                    f"{venue_name}: large title pool ({original_scanned_count} titles); "
                    f"LLM title filter will score the bounded official-category/local shortlist of {len(scanned)} titles before detail scoring."
                )
            else:
                if trusted_title_categories:
                    log(
                        f"{venue_name}: large title pool ({original_scanned_count} titles); "
                        "LLM title filter will score the full official-category-selected title pool because the configured budget covers it."
                    )
                else:
                    log(
                        f"{venue_name}: large title pool ({original_scanned_count} titles); "
                        "LLM title filter will score the full title pool because no trusted official category partition is available."
                    )
        else:
            use_llm_title_filter = False
            log(
                f"{venue_name}: large title pool ({original_scanned_count} titles); using bounded local title ranking before detail scoring because LLM title filtering is unavailable. "
                "Final recommendations still require real abstracts and final relevance scoring."
            )
    # Keep upstream/mock behavior for tests and local smoke runs, while respecting
    # explicit disable flags used by production diagnostics.
    if config.provider.lower() == "mock" and os.environ.get("DISABLE_LLM_TITLE_FILTER", "0").lower() not in {"1", "true", "yes", "on"}:
        use_llm_title_filter = True
    if llm.enabled and interest and use_llm_title_filter:
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
        batches = [batch for batch, _context in batches_with_context]
        prompts: list[str] = []
        for batch_index, (batch, context) in enumerate(batches_with_context, 1):
            _raise_if_cancelled(should_cancel)
            title_lines = "\n".join(f"- {item.get('id')}: {item.get('title')}" for item in batch)
            context_block = f"\nBatch context:\n{context}\n" if context else ""
            prompts.append(f"""
You are strictly filtering accepted paper titles before expensive abstract/PDF fetching.

Research interest/profile:
{scoring_interest}
{context_block}

Paper titles, batch {batch_index}/{len(batches_with_context)}:
{title_lines}

Return strict JSON:
{{"scored":[{{"id":"paper id","fit_score":7.0,"diversity_score":6.0,"hit_directions":["direction"],"category":"short category","reason":"one concise Chinese title-level reason"}}]}}

Rules:
- Score every title in this batch; do not omit low-confidence titles.
- fit_score is the title-level match to the profile, not a final recommendation score: 9-10 exceptional, 7-8 strong, 5-6 possible, <=4 weak/unrelated.
- Generic AI/ML titles should score low unless the title concretely connects to the user's methods, domains, or constraints.
- diversity_score only rewards hitting multiple real user directions or adding a complementary method/domain. It cannot rescue low fit.
- This title screen only decides which papers receive abstract/detail fetching. Final recommendations are decided later from real abstracts and final relevance scoring.
""")
        workers = 1 if os.environ.get("TITLE_FILTER_SEQUENTIAL", "0").lower() in {"1", "true", "yes", "on"} else clamp_workers(config.llm_concurrency, default=16, maximum=32)
        title_timeout = int(os.environ.get("TITLE_FILTER_TIMEOUT_SEC", "0") or 0) or int(config.title_filter_timeout_sec or 120)
        original_timeout = getattr(llm, "timeout_sec", title_timeout)
        if hasattr(llm, "timeout_sec"):
            llm.timeout_sec = min(original_timeout, title_timeout)
        active_timeout = getattr(llm, "timeout_sec", title_timeout)
        log(f"{venue_name}: starting LLM title prefilter for {len(scanned)} titles in {len(prompts)} batches with {workers} workers; per-batch timeout={active_timeout}s")
        progress("llm_title_filter", 0, max(1, len(prompts)), f"{venue_name}: starting LLM title filter, {len(prompts)} batches")
        seen: set[str] = set()
        scored_rows: list[dict] = []
        wall_timeout = max(
            10,
            int(os.environ.get("TITLE_FILTER_WALL_TIMEOUT_SEC", "0") or 0)
            or int(active_timeout or title_timeout or 120),
        )
        if workers == 1:
            result_iter = []
            for batch_index, (batch, prompt) in enumerate(zip(batches, prompts, strict=False), 1):
                _raise_if_cancelled(should_cancel)
                progress("llm_title_filter", batch_index - 1, len(batches), f"{venue_name}: scoring title batch {batch_index}/{len(batches)}")
                result = _json_or_error_wall_timeout(llm, prompt, timeout_sec=wall_timeout)
                result_iter.append((batch_index, batch, result))
        else:
            result_iter = []
            executor = ThreadPoolExecutor(max_workers=workers)
            futures = {
                executor.submit(_json_or_error_wall_timeout, llm, prompt, timeout_sec=wall_timeout): (batch_index, batch)
                for batch_index, (batch, prompt) in enumerate(zip(batches, prompts, strict=False), 1)
            }
            pending = set(futures)
            completed = 0
            try:
                while pending:
                    _raise_if_cancelled(should_cancel)
                    done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                    if not done:
                        continue
                    for future in done:
                        _raise_if_cancelled(should_cancel)
                        batch_index, batch = futures[future]
                        completed += 1
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = {"ok": False, "data": None, "error": str(exc)}
                        result_iter.append((batch_index, batch, result))
                        progress("llm_title_filter", completed, len(batches), f"{venue_name}: scored title batch {completed}/{len(batches)}, workers {workers}")
            except JobCancelled:
                for future in pending:
                    future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                executor.shutdown(wait=True)
            result_iter.sort(key=lambda row: row[0])
        if hasattr(llm, "timeout_sec"):
            llm.timeout_sec = original_timeout
        for batch_index, batch, result in result_iter:
            _raise_if_cancelled(should_cancel)
            data = result.get("data")
            appended = 0
            if not result.get("ok"):
                error = result.get("error", "")
                _raise_if_fatal_llm_configuration_error(error, f"{venue_name} title filtering")
                log(f"{venue_name}: title batch {batch_index}/{len(batches)} LLM failed: {str(error)[:240]}; rows can still be retained by local title ranking for downstream abstract scoring")
            rows = []
            if isinstance(data, dict):
                for key in ("scored", "evaluations", "selected"):
                    value = data.get(key)
                    if isinstance(value, list):
                        rows = value
                        break
            if rows:
                for row in rows:
                    item = by_id.get(str(row.get("id") or "")) if isinstance(row, dict) else None
                    if not item or item.get("id") in seen:
                        continue
                    item["fit_score"] = _as_float(row.get("fit_score"), _as_float(row.get("score"), item.get("fit_score") or 0))
                    item["title_llm_fit_score"] = item["fit_score"]
                    item["diversity_score"] = _as_float(row.get("diversity_score"), item.get("diversity_score") or 0)
                    item["score"] = _combined_score(item["fit_score"], item["diversity_score"])
                    item["hit_directions"] = _normalize_hit_directions(row.get("hit_directions"))
                    item["category"] = str(row.get("category") or item.get("category") or "")
                    item["title_reason"] = str(row.get("reason") or item.get("title_reason") or "")
                    item["fit_explanation"] = item["title_reason"]
                    item["reason_source"] = "llm title filter"
                    group = group_by_id.get(str(item.get("id") or ""))
                    if group:
                        item["title_filter_context"] = {
                            "venue": group["venue"],
                            "year": group["year"],
                            "category": group["category"],
                            "category_size": group["category_size"],
                            "venue_yetotal": group["venue_yetotal"],
                            "category_ratio": group["category_ratio"],
                            "strictness": group["policy"]["label"],
                            "min_fit_score": group["policy"]["min_score"],
                            "keep_ratio": group["policy"]["keep_ratio"],
                        }
                    _apply_relevance_guard(item)
                    _apply_topic_evidence_guard(item, interest)
                    _apply_quality_bonus(item)
                    scored_rows.append(item)
                    seen.add(item.get("id", ""))
                    appended += 1
            log(f"{venue_name}: title batch {batch_index}/{len(batches)} scored {appended}; scored_titles={len(scored_rows)}")
            # Parallel LLM calls complete out of order and are sorted before processing;
            # keep the progress counter monotonic instead of replaying batch indexes.
            result_progress_current = batch_index if workers == 1 else len(batches)
            progress("llm_title_filter", result_progress_current, len(batches), f"{venue_name}: title batch {batch_index}/{len(batches)}, scored {len(scored_rows)}, workers {workers}")
        if interest:
            for item in scanned:
                _apply_topic_evidence_guard(item, interest)
                _apply_quality_bonus(item)
        target_count = _title_detail_candidate_target(config, len(scanned))
        scored_pool = _dedupe_items(scored_rows or selected)
        if len(scored_pool) < target_count:
            seen_keys = {str(item.get("id") or item.get("url") or item.get("title") or "") for item in scored_pool}
            for item in scanned:
                key = str(item.get("id") or item.get("url") or item.get("title") or "")
                if not key or key in seen_keys:
                    continue
                item["title_llm_missing"] = True
                item["reason_source"] = "local title ranking after incomplete title LLM"
                item["title_reason"] = "Retained by local title ranking because the title LLM did not return this row; final recommendations still require a real abstract and final relevance scoring."
                item.setdefault("recommendation_note_zh", "题名 LLM 未返回该行时按本地题名排序保留到详情池；是否推荐仍只由真实摘要和最终相关性评分决定。")
                item.setdefault("recommendation_note_en", "Retained by local title ranking after incomplete title-LLM output; recommendation is still decided only from real abstracts and final relevance scoring.")
                scored_pool.append(item)
                seen_keys.add(key)
                if len(scored_pool) >= target_count:
                    break
        scored_pool = sorted(scored_pool, key=_title_rank_key)
        selected = scored_pool[:target_count]
        merged = sorted(_dedupe_items(selected), key=_title_rank_key)
        selected_before_prune = len(merged)
        pruned_count = len(merged)
        if dynamic_title_filter and merged:
            merged = _dynamic_title_prune(merged, title_groups, log, venue_name)
            pruned_count = len(merged)
            if len(merged) < min(target_count, len(scored_pool)):
                seen_keys = {str(item.get("id") or item.get("url") or item.get("title") or "") for item in merged}
                for item in scored_pool:
                    key = str(item.get("id") or item.get("url") or item.get("title") or "")
                    if key and key not in seen_keys:
                        merged.append(item)
                        seen_keys.add(key)
                    if len(merged) >= min(target_count, len(scored_pool)):
                        break
                merged.sort(key=_title_rank_key)
        limit = config.max_fetch_papers if result_limit is None else result_limit
        merged = _apply_result_limit_with_floor(merged, limit, config)
        _append_title_filter_report(
            title_filter_reports,
            venue_name,
            scanned,
            title_groups,
            len(prompts),
            selected_before_prune,
            pruned_count,
            len(merged),
            limit,
            _target_recall_count(config, len(scanned)),
            "llm",
            category_filtered_count=len(items),
            tfidf_screened_count=len(scanned),
            title_score_input_count=len(scanned),
            llm_title_scored_count=len(scored_rows),
        )
        log(f"{venue_name}: LLM title prefilter scored {len(scored_rows)} titles; retained {len(merged)} title-screened candidates for detail scoring from {len(scanned)} title-screened titles")
        return merged

    ranked = _local_title_screen_pool(scanned, config, interest)
    if interest:
        for item in ranked:
            _apply_topic_evidence_guard(item, interest)
            _apply_quality_bonus(item)
        strong_count = sum(1 for item in ranked if float(item.get("fit_score") or 0) >= 6.0)
        if strong_count == 0:
            log(f"{venue_name}: local title screen found no high-fit titles; ranked rows still enter detail scoring only if retained by the configured title-screen budget")
        else:
            log(f"{venue_name}: local title screen found {strong_count} high-fit titles before detail scoring")
    selected_before_prune = len(ranked)
    pruned_count = len(ranked)
    if dynamic_title_filter:
        ranked = _dynamic_title_prune(ranked, title_groups, log, venue_name)
        pruned_count = len(ranked)
    recall_pool = _ranked_recall_pool(ranked, config)
    limit = config.max_fetch_papers if result_limit is None else result_limit
    recall_pool = _apply_result_limit_with_floor(recall_pool, limit, config)
    _append_title_filter_report(
        title_filter_reports,
        venue_name,
        scanned,
        title_groups,
        len(_chunks(scanned, 10)),
        selected_before_prune,
        pruned_count,
        len(recall_pool),
        limit,
        _target_recall_count(config, len(scanned)),
        "local_title_rank",
        category_filtered_count=len(items),
        tfidf_screened_count=len(ranked),
        title_score_input_count=0,
        llm_title_scored_count=0,
        local_title_ranked_count=len(ranked),
    )
    log(f"{venue_name}: local title screen retained {len(recall_pool)} / {len(scanned)} candidates for detail scoring")
    return recall_pool


def _paper_category(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    category_status = str(metadata.get("category_status") or metadata.get("venue_category_status") or "").lower()
    classification_source = str(item.get("classification_source") or "").lower()
    raw_values = [str(item.get(key) or "").strip() for key in ("primary_area", "category", "track")]
    raw = next((value for value in raw_values if value), "")
    if not raw:
        return ""
    raw_lower = raw.lower()
    if raw_lower.startswith("local topic:"):
        return ""
    if category_status in {"no_official_categories", "missing_categories", "no_or_partial_categories"}:
        return ""
    if classification_source not in {"official", "local_metadata_category", "official_cached", "openreview", "venue_official"}:
        return ""
    return raw


def _has_trusted_title_categories(items: list[dict], metadata_audit: dict | None = None) -> bool:
    audit = metadata_audit if isinstance(metadata_audit, dict) else {}
    if audit:
        status = str(audit.get("category_status") or "").lower()
        if not bool(audit.get("has_official_categories")) or status in {"no_official_categories", "missing_categories", "no_or_partial_categories"}:
            return False
    return any(_paper_category(item) for item in items if isinstance(item, dict))


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
        "min_score": 5.5,
        "keep_ratio": 0.30,
        "instruction": "This is a smaller category for this venue-year. Keep niche matches when they directly support the profile.",
    }


def _title_filter_groups(items: list[dict]) -> list[dict]:
    venue_yetotals: dict[tuple[str, int], int] = {}
    buckets: dict[tuple[str, int, str], list[dict]] = {}
    order: list[tuple[str, int, str]] = []
    for item in items:
        venue = str(item.get("venue") or "")
        year = int(item.get("year") or 0)
        category = _paper_category(item) or "(uncategorized)"
        venue_yetotals[(venue, year)] = venue_yetotals.get((venue, year), 0) + 1
        key = (venue, year, category)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(item)

    groups: list[dict] = []
    for venue, year, category in order:
        group_items = buckets[(venue, year, category)]
        total = max(1, venue_yetotals.get((venue, year), len(group_items)))
        ratio = len(group_items) / total
        policy = _title_filter_policy(ratio)
        groups.append({
            "key": f"{venue}|{year}|{category}",
            "venue": venue,
            "year": year,
            "category": category,
            "items": group_items,
            "category_size": len(group_items),
            "venue_yetotal": total,
            "venue_year_total": total,
            "category_ratio": ratio,
            "policy": policy,
        })
    return groups


def _title_filter_prompt_context(group: dict) -> str:
    ratio_pct = round(float(group["category_ratio"]) * 100, 1)
    return "\n".join([
        f"Venue/year/category: {group['venue']} {group['year']} / {group['category']}",
        f"Category share among category-filtered papers for this venue-year: {ratio_pct}% ({group['category_size']}/{group['venue_yetotal']}).",
        f"Dynamic strictness: {group['policy']['label']}.",
        group["policy"]["instruction"],
    ])



def _category_summary_from_title_index(venue: dict, years: list[int], papers: list[dict]) -> dict:
    buckets: dict[str, dict[str, Any]] = {}
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        category = _paper_category(paper)
        if not category:
            continue
        bucket = buckets.setdefault(category, {"name": category, "count": 0, "sample_titles": [], "sample_keywords": []})
        bucket["count"] += 1
        title = str(paper.get("title") or "").strip()
        if title and len(bucket["sample_titles"]) < 5:
            bucket["sample_titles"].append(title)
        for keyword in paper.get("keywords") or []:
            text = str(keyword or "").strip()
            if text and text not in bucket["sample_keywords"] and len(bucket["sample_keywords"]) < 20:
                bucket["sample_keywords"].append(text)
    entries = sorted(buckets.values(), key=lambda row: (-int(row.get("count") or 0), str(row.get("name") or "")))
    return {
        "venue_id": venue.get("id", ""),
        "venue": venue.get("name", ""),
        "year": years[0] if len(years) == 1 else ",".join(str(year) for year in years),
        "paper_count": len(papers),
        "category_summary": entries,
    }



def _public_category_selection(selection: dict | None) -> dict:
    if not isinstance(selection, dict):
        return {}
    public: dict[str, Any] = {}
    for key in (
        "venue_id",
        "venue",
        "year",
        "paper_count",
        "category_count",
        "selected_paper_count",
        "selected_categories",
        "rejected_categories",
        "category_status",
    ):
        if key in selection:
            public[key] = selection.get(key)
    return public


def _select_official_category_title_index(
    venue: dict,
    years: list[int],
    papers: list[dict],
    metadata_audit: dict,
    config: AppConfig,
    llm: LLMClient,
    log: LogFn,
) -> tuple[list[dict], list[dict]]:
    if not papers or not _has_trusted_title_categories(papers, metadata_audit):
        return papers, []
    category_summary = _category_summary_from_title_index(venue, years, papers)
    if not category_summary.get("category_summary"):
        return papers, []
    try:
        max_categories = int(os.environ.get("VENUE_CATEGORY_SELECT_MAX", "0") or 0)
    except Exception:
        max_categories = 0
    if max_categories <= 0:
        max_categories = 8
    selection = select_relevant_categories(category_summary, config, llm, max_categories=max_categories)
    filtered = filter_papers_by_selected_categories(papers, selection)
    used_all_categories_fallback = False
    if not filtered and papers:
        filtered = list(papers)
        used_all_categories_fallback = True
        selection = dict(selection)
        selection["fallback_to_all_categories"] = True
        selection["fallback_reason"] = "category selector returned 0 papers for a non-empty online venue-year"
    report = {
        "venue_id": venue.get("id", ""),
        "venue": venue.get("name", ""),
        "year": years[0] if len(years) == 1 else ",".join(str(year) for year in years),
        "adapter": str(metadata_audit.get("adapter") or "online_venue"),
        "total_papers": len(papers),
        "selected_category_papers": len(filtered) if not used_all_categories_fallback else 0,
        "corpus_audit_papers": len(papers),
        "full_venue_corpus_audit": True,
        "used_all_categories_fallback": used_all_categories_fallback,
        "selection": _public_category_selection(selection),
        "title_filter_input_papers": len(filtered),
        "metadata_audit": metadata_audit,
        **_venue_metadata_status_fields(metadata_audit),
    }
    selected_names = [item.get("name", "") for item in selection.get("selected_categories", [])]
    if used_all_categories_fallback:
        log(f"{venue.get('name', '')}: online category scan selected 0/{len(papers)} papers; using all {len(filtered)} papers for title screening because category selection returned none")
    else:
        log(f"{venue.get('name', '')}: online category scan selected {len(filtered)}/{len(papers)} papers from {len(selected_names)} official categories")
    return filtered, [report]



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
        items.sort(key=_title_rank_key)
        min_score = _as_float(policy.get("min_score"), 0.0)
        keep_floor = max(1, ceil(len(items) * max(0.0, _as_float(policy.get("keep_ratio"), 0.0))))
        kept = [item for item in items if _as_float(item.get("title_llm_fit_score"), item.get("fit_score")) >= min_score]
        if not kept:
            kept = items[:keep_floor]
        pruned.extend(kept)
        group["title_selected_scored"] = len(items)
        group["after_dynamic_prune"] = len(kept)
        log(
            f"{venue_name}: dynamic title prune {group['year']} / {group['category']} "
            f"ratio={group['category_ratio']:.1%} strictness={policy['label']} "
            f"selected={len(items)} kept={len(kept)}"
        )
    pruned.sort(key=_title_rank_key)
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
    recall_target: int,
    mode: str,
    *,
    category_filtered_count: int | None = None,
    tfidf_screened_count: int | None = None,
    title_score_input_count: int | None = None,
    llm_title_scored_count: int = 0,
    local_title_ranked_count: int = 0,
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
            "venue_yetitle_input_papers": group["venue_yetotal"],
            "category_ratio": group["category_ratio"],
            "strictness": group["policy"]["label"],
            "min_fit_score": group["policy"]["min_score"],
            "keep_ratio": group["policy"]["keep_ratio"],
            "max_keep": min(100, max(5, ceil(group["category_size"] * group["policy"]["keep_ratio"]))),
            "llm_selected_scored": group.get("title_selected_scored", 0),
            "after_code_side_dynamic_pruning": group.get("after_dynamic_prune", 0),
        })
    category_filtered_count = len(scanned) if category_filtered_count is None else int(category_filtered_count)
    tfidf_screened_count = len(scanned) if tfidf_screened_count is None else int(tfidf_screened_count)
    title_score_input_count = len(scanned) if title_score_input_count is None else int(title_score_input_count)
    reports.append({
        "venue": venue_name,
        "mode": mode,
        "category_filtered_papers": category_filtered_count,
        "tfidf_screened_papers": tfidf_screened_count,
        "title_filter_input_papers": title_score_input_count,
        "title_score_input_papers": title_score_input_count,
        "title_filter_batches": batch_count,
        "llm_title_scored_papers": int(llm_title_scored_count or 0),
        "local_title_ranked_papers": int(local_title_ranked_count or 0),
        "llm_selected_scored": selected_before_prune,
        "after_code_side_dynamic_pruning": selected_after_prune,
        "post_title_candidate_limit": result_limit,
        "final_title_candidates": final_count,
        "recall_target": recall_target,
        "groups": group_rows,
    })


def _load_local_category_guided_index(
    venue: dict,
    years: list[int],
    config: AppConfig,
    llm: LLMClient,
    title_scan_limit: int,
    log: LogFn,
) -> tuple[list[dict], list[dict], list[dict]] | None:
    local_years = []
    for year in years:
        local = load_local_venue_year(venue, year)
        if not local:
            continue
        if int(local.get("paper_count") or 0) <= 0:
            log(f"{venue.get('name', '')} {year}: local database exists but has 0 papers; trying the selected-year live source")
            continue
        local_years.append(local)
    if not local_years:
        return None

    combined: list[dict] = []
    corpus: list[dict] = []
    reports: list[dict] = []
    for local in local_years:
        metadata_audit = _local_database_metadata_audit(local)
        use_category_selection = bool(metadata_audit.get("has_official_categories")) and str(metadata_audit.get("category_status") or "").lower() not in {"no_official_categories", "missing_categories", "no_or_partial_categories"}
        if use_category_selection:
            selection = select_relevant_categories(local["category_summary"], config, llm)
            filtered = filter_papers_by_selected_categories(local["papers"], selection)
        else:
            selection = {
                "venue_id": local.get("venue_id", ""),
                "venue": venue.get("name", ""),
                "year": local.get("year", ""),
                "paper_count": local.get("paper_count", 0),
                "category_count": 0,
                "selected_paper_count": len(local["papers"]),
                "selected_categories": [],
                "rejected_categories": [],
                "category_status": metadata_audit.get("category_status") or "no_official_categories",
            }
            filtered = list(local["papers"])
        used_all_categories_fallback = False
        if not filtered and local["papers"]:
            # A source with real papers must not be silently dropped because the
            # coarse category selector returned no category. Keep all titles for
            # downstream title ranking; final recommendations remain LLM-gated.
            filtered = list(local["papers"])
            used_all_categories_fallback = True
            selection = dict(selection)
            selection["fallback_to_all_categories"] = True
            selection["fallback_reason"] = "category selector returned 0 papers for a non-empty local venue-year"
        combined.extend(filtered)
        corpus.extend(local["papers"] if _full_venue_corpus_audit_enabled(config) else filtered)
        reports.append({
            "venue_id": local["venue_id"],
            "venue": venue.get("name", ""),
            "year": local["year"],
            "adapter": "local_database",
            "papers_path": local["papers_path"],
            "category_summary_path": local["category_summary_path"],
            "total_papers": local["paper_count"],
            "selected_category_papers": len(filtered) if use_category_selection else 0,
            "corpus_audit_papers": len(local["papers"]) if _full_venue_corpus_audit_enabled(config) else len(filtered),
            "full_venue_corpus_audit": _full_venue_corpus_audit_enabled(config),
            "used_all_categories_fallback": used_all_categories_fallback,
            "selection": _public_category_selection(selection),
            "title_filter_input_papers": len(filtered),
            **_venue_metadata_status_fields(metadata_audit),
        })
        selected_names = [item.get("name", "") for item in selection.get("selected_categories", [])]
        if used_all_categories_fallback:
            log("{} {}: local category scan selected 0/{} papers; using all {} papers for title screening because category selection returned none".format(venue.get("name", ""), local["year"], local["paper_count"], len(filtered)))
        elif not use_category_selection:
            log("{} {}: verified local title corpus has no official categories; sending all {} papers to title screening".format(venue.get("name", ""), local["year"], len(filtered)))
        else:
            log("{} {}: local category scan selected {}/{} papers from {} categories; full corpus audited={}".format(venue.get("name", ""), local["year"], len(filtered), local["paper_count"], len(selected_names), len(local["papers"])))

    for report in reports:
        report["post_title_candidate_limit"] = title_scan_limit
    return combined, reports, _dedupe_items(corpus)


def _venue_yewindow(requested_years: list[int], max_backfill_years: int = 2) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for year in requested_years:
        try:
            start = int(year)
        except (TypeError, ValueError):
            continue
        for candidate in range(start, start - max(0, max_backfill_years) - 1, -1):
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def _venue_yeis_released(venue: dict, year: int, *, as_of: date | None = None) -> tuple[bool, date | None]:
    release_date = _known_conference_release_date(venue.get("name") or venue.get("id"), year)
    if not release_date:
        return True, None
    cutoff = as_of or datetime.now(timezone.utc).date()
    return release_date <= cutoff, release_date


def _release_block_reason(year: int, release_date: date | None) -> str:
    return f"{year} release date {release_date.isoformat() if release_date else 'unknown'} is after run date"


def _resolve_latest_available_venue_years(
    venue: dict,
    years: list[int],
    *,
    max_backfill_years: int = 2,
    as_of: date | None = None,
) -> tuple[list[int], str]:
    if not years:
        return [], ""
    cutoff = as_of or datetime.now(timezone.utc).date()
    venue_name = str(venue.get("name") or venue.get("id") or "venue")
    resolved: list[int] = []
    reasons: list[str] = []
    probe_cache: dict[int, tuple[bool, str]] = {}

    def probe(candidate: int) -> tuple[bool, str]:
        if candidate in probe_cache:
            return probe_cache[candidate]
        local = load_local_venue_year(venue, candidate)
        if local and int(local.get("paper_count") or 0) > 0:
            probe_cache[candidate] = (True, "local_database")
            return probe_cache[candidate]
        try:
            titles, adapter = _fetch_venue_title_index_for_find(venue, [candidate], 1)
        except Exception:
            titles, adapter = [], "error"
        probe_cache[candidate] = (bool(titles), adapter)
        return probe_cache[candidate]

    for requested in years:
        future_release_notes: list[str] = []
        for candidate in _venue_yewindow([requested], max_backfill_years=max_backfill_years):
            released, release_date = _venue_yeis_released(venue, candidate, as_of=cutoff)
            if candidate == requested and not released:
                future_release_notes.append(_release_block_reason(candidate, release_date))
            available, adapter = probe(candidate)
            if not available:
                continue
            if candidate not in resolved:
                resolved.append(candidate)
            if candidate != requested:
                prefix = f"requested years [{requested}] had no usable {venue_name} title index as of {cutoff.isoformat()}"
                if future_release_notes:
                    prefix = f"{prefix} ({'; '.join(future_release_notes)})"
                suffix = f" via {adapter}" if adapter else ""
                reasons.append(f"{prefix}; using latest available {venue_name} title index year {candidate}{suffix}.")
            break
    return resolved, " ".join(reasons)


def _resolve_venue_years(
    venue: dict,
    requested_years: list[int],
    *,
    allow_backfill: bool = True,
    as_of: date | None = None,
) -> tuple[list[int], str]:
    years = list(dict.fromkeys(int(year) for year in requested_years if str(year).isdigit()))
    if not allow_backfill:
        return years, ""
    resolved_years, fallback_reason = _resolve_latest_available_venue_years(venue, years, as_of=as_of)
    if resolved_years:
        return resolved_years, fallback_reason
    return years, ""


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
    interest = _topic_interest_text(config)
    topic_routes_block = ""
    scoring_interest = _compact_scoring_interest(config, interest)
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
        _set_hit_direction_language_fields(item)
        item["fit_explanation"] = item.get("fit_explanation") or "Local title/abstract fit estimate; final relevance scoring is still required for recommendations."
        item["reason"] = item.get("reason") or "Local title/abstract ranking before final relevance scoring. Configure an LLM API key for model-based relevance scoring."
        item["reason_source"] = item.get("reason_source") or "adaptive profile fallback"
        _apply_relevance_guard(item)
        _apply_topic_evidence_guard(item, interest)
        _apply_quality_bonus(item)
        evaluated.append(item)
    if llm.enabled and interest:
        scoring_limit = _final_llm_scoring_limit(config, len(evaluated))
        scoring_items = _final_llm_scoring_pool(evaluated, config)
        scoring_items = _enrich_missing_abstracts_for_final_scoring(scoring_items, config, source_name, log, progress, should_cancel)
        abstract_missing_scoring_items = [item for item in scoring_items if not _has_real_abstract(item)]
        if abstract_missing_scoring_items:
            for item in abstract_missing_scoring_items:
                reason = _abstract_lookup_failure_reason(item)
                item["abstract_fetch_failed"] = True
                item["abstract_fetch_failed_reason"] = reason
                item["abstract_contract_violation"] = "missing_real_abstract_before_final_llm_scoring"
                item["llm_final_scoring_skipped"] = True
                item["llm_final_scoring_skip_reason"] = reason
                item["not_positive_support"] = True
                item["weak_candidate_for_critique"] = True
                item["evidence_tier"] = "detail_fetch_failed"
                item["topic_evidence"] = "weak: missing real abstract evidence before final LLM scoring"
                item["topic_evidence_supported"] = False
                item["recommendation_note_zh"] = "题名通过后仍未从会议/DOI/元数据服务补到真实摘要；该候选未送入 LLM 标题+摘要评分，不能作为推荐文章。"
                item["recommendation_note_en"] = "The title passed screening, but venue/DOI metadata lookup still did not obtain a real abstract; this candidate was not sent to title+abstract LLM scoring and cannot be recommended."
                item["recommendation_note"] = item["recommendation_note_zh"]
            log(f"{source_name}: abstract contract excluded {len(abstract_missing_scoring_items)}/{len(scoring_items)} candidates from final LLM scoring because real abstracts are missing after metadata enrichment")
            progress("abstract_contract", len(scoring_items) - len(abstract_missing_scoring_items), max(1, len(scoring_items)), f"{source_name}: abstract contract verified real abstracts")
            scoring_items = [item for item in scoring_items if _has_real_abstract(item)]
        scoring_ids = {id(item) for item in scoring_items}
        skipped_candidates = [item for item in evaluated if id(item) not in scoring_ids]
        skipped_items = len(skipped_candidates)
        if skipped_items > 0:
            for item in skipped_candidates:
                item["llm_final_scoring_skipped"] = True
                item["not_positive_support"] = True
                item["weak_candidate_for_critique"] = True
                item.setdefault("evidence_tier", "retrieval_only")
                item.setdefault("recommendation_note_zh", "该条目未进入最终相关性评分；只保留为排查线索，不展示为推荐论文。")
                item.setdefault("recommendation_note_en", "This row did not enter final relevance scoring; retained only for troubleshooting, not as a recommendation.")
                item.setdefault("recommendation_note", item.get("recommendation_note_zh") or item.get("recommendation_note_en"))
        log(f"{source_name}: final LLM scoring pool {len(scoring_items)}/{len(evaluated)} candidates; skipped {skipped_items} retrieval-only candidates")
        scoring_batch_size = _adaptive_final_scoring_batch_size(config, scoring_items, scoring_interest, topic_routes_block)
        for batch_index, batch in enumerate(_chunks(scoring_items, scoring_batch_size), 1):
            item_lines = "\n\n".join(
                f"ID: {item.get('id')}\nTitle: {item.get('title')}\nAbstract/Description: {(item.get('abstract') or '')[:650]}"
                for item in batch
            )
            prompts.append(f"""
You are the final strict relevance judge for literature discovery. Return JSON only.

Research interest/profile:
{interest}

{topic_routes_block}

Candidate items, batch {batch_index}:
{item_lines}

Return exactly this schema. The evaluations array is the final LLM evaluation rows; include one row for every candidate ID in the batch and never return an empty object. fit_score is the final recommendation score:
{{"evaluations":[{{"id":"paper id","category":"short category","fit_score":7.0,"diversity_score":6.0,"recommend_for_deep_reading":true,"hit_directions_zh":["中文命中方向"],"hit_directions_en":["English hit direction"],"fit_explanation_zh":"2-3句中文：面向用户说明摘要中的具体证据、为什么与当前调研主题相关、以及可复用价值","fit_explanation_en":"2-3 English sentences for the user with title/abstract evidence, relevance, and reusable value","reason_zh":"2-4句中文：面向用户说明该论文对当前研究画像的具体价值、可借鉴的方法/数据/协议/理论/评测信息，以及摘要层面的风险或不确定性；不要写给 reader 的精读指令","reason_en":"2-4 English sentences for the user: concrete value to the research profile, reusable method/data/protocol/theory/evaluation value, and abstract-level risks or uncertainty; do not write reader instructions"}}]}}

Rules:
- Score by explicit title/abstract evidence only; venue prestige must not raise fit_score.
- Use the whole 0-10 range consistently: 9-10 exact center, 7-8 strong match, 5-6 partial/background usefulness, 3-4 weak/generic, <=2 unrelated.
- Broad background papers are weak unless the abstract itself gives concrete reusable method, data, benchmark, protocol, theory, or evaluation value for the current research interest.
- recommend_for_deep_reading is an audit field only. the workflow chooses the user-visible list by the single final title+abstract ranking contract, not by this boolean and not by an absolute score cutoff.
- User-facing recommendation reasons must explain concrete value for the user research project first, then summarize abstract-level uncertainty as a user-facing risk. Do not write reader instructions such as Reading note, full-text reading must verify, or the abstract is not a substitute for full-text reading.
- Missing abstract, metadata-only evidence, or title-only evidence cannot be recommended.
{FIND_FINAL_SCORING_ROUTE_RULES}
""")
            prompt_batches.append(batch)
        workers = _adaptive_final_scoring_workers(config, len(prompt_batches))
        env_batch_timeout = _positive_int_env("ABSTRACT_SCORING_TIMEOUT_SEC", 0)
        configured_batch_timeout = int(getattr(config, "abstract_scoring_timeout_sec", 0) or 180)
        batch_timeout = max(30, env_batch_timeout or configured_batch_timeout or 180)
        scoring_temperature = FIND_FINAL_SCORING_TEMPERATURE
        original_timeout = getattr(llm, "timeout_sec", batch_timeout)
        original_retries = getattr(llm, "retries", None)
        if hasattr(llm, "timeout_sec"):
            llm.timeout_sec = batch_timeout
        if hasattr(llm, "retries"):
            llm.retries = max(1, int(os.environ.get("ABSTRACT_SCORING_LLM_RETRIES", "2") or 2))
        log(f"{source_name}: starting LLM final scoring for {len(scoring_items)}/{len(evaluated)} items in {len(prompts)} batches with {workers} workers; batch_size={scoring_batch_size}; per-batch timeout={getattr(llm, 'timeout_sec', batch_timeout)}s; retries={getattr(llm, 'retries', 'n/a')}; temperature={scoring_temperature}")
        progress("abstract_scoring", 0, max(1, len(prompt_batches)), f"{source_name}: starting LLM final scoring")

        single_retry_attempts = max(0, int(os.environ.get("OMITTED_ITEM_RETRY_ATTEMPTS", os.environ.get("ABSTRACT_SCORING_SINGLE_RETRY_ATTEMPTS", "3")) or 0))
        scoring_max_tokens = max(4000, int(os.environ.get("ABSTRACT_SCORING_MAX_TOKENS", "0") or 0) or max(9000, scoring_batch_size * 1400))
        single_scoring_max_tokens = max(1500, int(os.environ.get("SINGLE_ABSTRACT_SCORING_MAX_TOKENS", "2500") or 2500))
        scoring_wall_timeout = max(0, int(os.environ.get("ABSTRACT_SCORING_WALL_TIMEOUT_SEC", "0") or 0))
        single_scoring_timeout = max(10, int(os.environ.get("SINGLE_ABSTRACT_SCORING_TIMEOUT_SEC", "75") or 75))
        pending_single_retries: list[tuple[int, list[dict], str]] = []

        def mark_items_unscored(batch_index: int, items_to_mark: list[dict], reason: str, error: object = "") -> None:
            for item in items_to_mark:
                item["llm_retry_exhausted"] = True
                item["llm_retry_reason"] = reason
                item["llm_retry_attempts"] = 0
                if error:
                    item["llm_retry_last_error"] = str(error)[:500]
                _apply_relevance_guard(item)
                _apply_topic_evidence_guard(item, interest)
                _apply_quality_bonus(item)
            if items_to_mark:
                log(f"{source_name}: batch {batch_index}/{len(prompt_batches)} marked {len(items_to_mark)} items fallback-only after {reason}; fallback-only items remain excluded from Find recommendations")

        def single_scoring_prompt(item: dict) -> str:
            return f"""
You are the final strict relevance judge for research recommendations.

Research interest/profile:
{scoring_interest}

{topic_routes_block}

Candidate item:
ID: {item.get('id')}
Title: {item.get('title')}
Abstract/Description: {(item.get('abstract') or '')[:900]}

Return strict JSON only:
{{"evaluations":[{{"id":"{item.get("id")}","category":"short category","fit_score":7.0,"diversity_score":6.0,"recommend_for_deep_reading":true,"hit_directions_zh":["中文命中方向"],"hit_directions_en":["English hit direction"],"fit_explanation":"2-3句中文：面向用户说明摘要证据、相关性和可复用价值","fit_explanation_zh":"2-3句中文：面向用户说明摘要证据、相关性和可复用价值","fit_explanation_en":"2-3 English sentences for the user with title/abstract evidence, relevance, and reusable value","reason":"2-4句中文：面向用户说明对当前研究画像的价值、可借鉴什么、以及摘要层面的风险或不确定性","reason_zh":"2-4句中文：面向用户说明对当前研究画像的价值、可借鉴什么、以及摘要层面的风险或不确定性","reason_en":"2-4 English sentences for the user: value to the current research profile, reusable content, and abstract-level risks or uncertainty"}}]}}

Scoring rules: judge this item independently from its real title and abstract. fit_score is the final ranking score used by the workflow: 9-10 exact center, 7-8 strong match, 5-6 partial/background usefulness, 3-4 weak/generic, <=2 unrelated. recommend_for_deep_reading is only an audit field; The workflow uses the final title+abstract ranking as the single recommendation contract and does not apply an absolute score cutoff. User-facing recommendation reasons must explain reusable value before limitations, and must not contain reader instructions such as Reading note, full-text reading must verify, or the abstract is not a substitute for full-text reading. Provide both Chinese and English explanation fields, plus hit_directions_zh in Chinese and hit_directions_en in English.
{FIND_FINAL_SCORING_ROUTE_RULES}
"""

        def retry_items_singly(batch_index: int, items_to_retry: list[dict], reason: str) -> int:
            if not items_to_retry:
                return 0
            if single_retry_attempts <= 0:
                mark_items_unscored(batch_index, items_to_retry, reason)
                return 0
            recovered = 0
            remaining = list(items_to_retry)
            original_single_timeout = getattr(llm, "timeout_sec", single_scoring_timeout)
            original_single_retries = getattr(llm, "retries", None)
            if hasattr(llm, "timeout_sec"):
                llm.timeout_sec = single_scoring_timeout
            if hasattr(llm, "retries"):
                llm.retries = 1
            try:
                for attempt in range(1, single_retry_attempts + 1):
                    next_remaining: list[dict] = []
                    for item in remaining:
                        before_source = item.get("reason_source")
                        single_result = _json_or_error_wall_timeout(
                            llm,
                            single_scoring_prompt(item),
                            temperature=scoring_temperature,
                            max_tokens=single_scoring_max_tokens,
                            timeout_sec=scoring_wall_timeout,
                        )
                        if single_result.get("ok"):
                            apply_result(batch_index, [item], single_result, allow_missing_retry=False)
                        if item.get("reason_source") == "llm abstract evaluation" and item.get("reason_source") != before_source:
                            item["llm_retry_attempts"] = attempt
                            recovered += 1
                        else:
                            item["llm_retry_attempts"] = attempt
                            item["llm_retry_last_error"] = str(single_result.get("error") or "missing evaluation row")[:500]
                            next_remaining.append(item)
                    remaining = next_remaining
                    if not remaining:
                        break
            finally:
                if hasattr(llm, "timeout_sec"):
                    llm.timeout_sec = original_single_timeout
                if original_single_retries is not None and hasattr(llm, "retries"):
                    llm.retries = original_single_retries
            for item in remaining:
                item["llm_retry_exhausted"] = True
                item["llm_retry_reason"] = reason
                item["llm_retry_attempts"] = single_retry_attempts
                _apply_relevance_guard(item)
                _apply_topic_evidence_guard(item, interest)
                _apply_quality_bonus(item)
            if remaining:
                log(f"{source_name}: batch {batch_index}/{len(prompt_batches)} {reason} retry exhausted {len(remaining)}/{len(items_to_retry)} items after {single_retry_attempts} attempts")
            return recovered

        def queue_single_retry(batch_index: int, items_to_retry: list[dict], reason: str) -> None:
            if not items_to_retry:
                return
            if single_retry_attempts <= 0:
                mark_items_unscored(batch_index, items_to_retry, reason)
                return
            pending_single_retries.append((batch_index, list(items_to_retry), reason))

        def retry_policy_text() -> str:
            if single_retry_attempts > 0:
                return "queued for bounded single-item retry before fallback-only marking"
            return "single-item retry disabled; marking fallback-only"

        def apply_result(batch_index: int, batch: list[dict], result: dict, allow_missing_retry: bool = True) -> None:
            by_id = {str(item.get("id")): item for item in batch}
            assigned_items: set[int] = set()
            if not result.get("ok"):
                error = result.get("error", "")
                _raise_if_fatal_llm_configuration_error(error, f"{source_name} final LLM scoring")
                if _is_transient_llm_service_error(error):
                    log(f"{source_name}: batch {batch_index}/{len(prompt_batches)} LLM scoring hit transient service error: {str(error)[:240]}; {retry_policy_text()}")
                    queue_single_retry(batch_index, batch, "transient-service-error")
                    return
                log(f"{source_name}: batch {batch_index}/{len(prompt_batches)} LLM scoring failed: {str(error)[:240]}; {retry_policy_text()}")
                queue_single_retry(batch_index, batch, "failed-batch")
                return
            data = result.get("data")
            if isinstance(data, dict):
                rows = data.get("evaluations")
                if rows is None:
                    rows = data.get("selected")
                if isinstance(rows, dict):
                    rows = [rows]
                if isinstance(rows, list):
                    use_order_fallback = len(rows) == len(batch)
                    for row_index, row in enumerate(rows):
                        if not isinstance(row, dict):
                            continue
                        row_id = str(row.get("id") or "")
                        item = by_id.get(row_id)
                        if not item and use_order_fallback and row_index < len(batch):
                            item = batch[row_index]
                            item["llm_id_mismatch_recovered"] = True
                            item["llm_returned_id"] = row_id
                        if not item:
                            continue
                        assigned_items.add(id(item))
                        item.pop("llm_retry_exhausted", None)
                        item.pop("llm_retry_reason", None)
                        item.pop("llm_retry_last_error", None)
                        item["category"] = str(row.get("category") or item.get("category") or "")
                        item["fit_score"] = _as_float(row.get("fit_score"), item.get("fit_score") or 0)
                        item["diversity_score"] = _as_float(row.get("diversity_score"), item.get("diversity_score") or 0)
                        item["score"] = _combined_score(item["fit_score"], item["diversity_score"])
                        if "recommend_for_deep_reading" in row:
                            item["recommend_for_deep_reading"] = bool(row.get("recommend_for_deep_reading"))
                        if "recommended_for_deep_reading" in row:
                            item["recommend_for_deep_reading"] = bool(row.get("recommended_for_deep_reading"))
                        if "supports_complete_requested_route" in row:
                            item["supports_complete_requested_route"] = bool(row.get("supports_complete_requested_route"))
                        hit_source = row.get("hit_directions_zh") or row.get("hit_directions")
                        _set_hit_direction_language_fields(
                            item,
                            hit_source,
                            zh_value=row.get("hit_directions_zh"),
                            en_value=row.get("hit_directions_en"),
                        )
                        item["fit_explanation"] = str(row.get("fit_explanation") or item.get("fit_explanation") or "")
                        item["fit_explanation_zh"] = str(row.get("fit_explanation_zh") or row.get("fit_explanation") or item.get("fit_explanation_zh") or item.get("fit_explanation") or "")
                        item["fit_explanation_en"] = str(row.get("fit_explanation_en") or item.get("fit_explanation_en") or "")
                        item["reason"] = str(row.get("reason") or item.get("reason") or "")
                        item["reason_zh"] = str(row.get("reason_zh") or row.get("reason") or item.get("reason_zh") or item.get("reason") or "")
                        item["reason_en"] = str(row.get("reason_en") or item.get("reason_en") or "")
                        item["reason_source"] = "llm abstract evaluation"
                        if item.get("classification_source") != "official":
                            item["classification_source"] = "llm_inferred"
                        _apply_relevance_guard(item)
                        _apply_llm_topic_evidence(item, row, interest)
                        _apply_quality_bonus(item)
            if allow_missing_retry:
                missing = [item for item in batch if id(item) not in assigned_items and item.get("reason_source") != "llm abstract evaluation"]
                if missing:
                    log(f"{source_name}: batch {batch_index}/{len(prompt_batches)} omitted {len(missing)} items; {retry_policy_text()}")
                    queue_single_retry(batch_index, missing, "omitted-item")

        completed = 0
        request_spacing_sec = max(0.0, float(os.environ.get("LLM_REQUEST_SPACING_SEC", "1.5" if _rate_limited_llm_provider(config) else "0") or 0))
        if workers == 1:
            for batch_index, (batch, prompt) in enumerate(zip(prompt_batches, prompts, strict=False), 1):
                _raise_if_cancelled(should_cancel)
                progress("abstract_scoring", completed, len(prompt_batches), f"{source_name}: scoring batch {batch_index}/{len(prompt_batches)}")
                log(f"{source_name}: scoring batch {batch_index}/{len(prompt_batches)} started")
                if request_spacing_sec and batch_index > 1:
                    time.sleep(request_spacing_sec)
                result = _json_or_error_wall_timeout(llm, prompt, temperature=scoring_temperature, max_tokens=scoring_max_tokens, timeout_sec=scoring_wall_timeout)
                apply_result(batch_index, batch, result)
                completed += 1
                log(f"{source_name}: scoring batch {batch_index}/{len(prompt_batches)} completed; ok={bool(result.get('ok'))}")
                progress("abstract_scoring", completed, len(prompt_batches), f"{source_name}: scored batch {batch_index}/{len(prompt_batches)} with {workers} workers")
        else:
            last_parallel_log = 0.0
            executor = ThreadPoolExecutor(max_workers=workers)
            futures = {executor.submit(_json_or_error_wall_timeout, llm, prompt, temperature=scoring_temperature, max_tokens=scoring_max_tokens, timeout_sec=scoring_wall_timeout): (idx, batch) for idx, (batch, prompt) in enumerate(zip(prompt_batches, prompts, strict=False), 1)}
            pending = set(futures)
            try:
                while pending:
                    _raise_if_cancelled(should_cancel)
                    done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                    if not done:
                        continue
                    for future in done:
                        _raise_if_cancelled(should_cancel)
                        batch_index, batch = futures[future]
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = {"ok": False, "data": None, "error": str(exc)}
                        apply_result(batch_index, batch, result)
                        completed += 1
                        now_progress = time.monotonic()
                        if completed == 1 or completed == len(prompt_batches) or completed % 25 == 0 or now_progress - last_parallel_log >= 15:
                            log(f"{source_name}: LLM scoring progress {completed}/{len(prompt_batches)} batches complete; latest completed batch {batch_index}; workers={workers}")
                            last_parallel_log = now_progress
                        progress("abstract_scoring", completed, len(prompt_batches), f"{source_name}: scored batch {batch_index}/{len(prompt_batches)} with {workers} workers")
            except JobCancelled:
                for future in pending:
                    future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                executor.shutdown(wait=True)
        if pending_single_retries:
            total_retry_items = sum(len(items_to_retry) for _batch_index, items_to_retry, _reason in pending_single_retries)
            log(f"{source_name}: running bounded single-item retries for {total_retry_items} items from {len(pending_single_retries)} batches")
            for retry_index, (batch_index, items_to_retry, reason) in enumerate(pending_single_retries, 1):
                _raise_if_cancelled(should_cancel)
                progress("abstract_scoring_retry", retry_index - 1, len(pending_single_retries), f"{source_name}: retrying batch {batch_index}/{len(prompt_batches)} {reason}")
                recovered = retry_items_singly(batch_index, items_to_retry, reason)
                log(f"{source_name}: batch {batch_index}/{len(prompt_batches)} single-item retry recovered {recovered}/{len(items_to_retry)} after {reason}")
            progress("abstract_scoring_retry", len(pending_single_retries), len(pending_single_retries), f"{source_name}: bounded single-item retries complete")
        if hasattr(llm, "timeout_sec"):
            llm.timeout_sec = original_timeout
        if original_retries is not None and hasattr(llm, "retries"):
            llm.retries = original_retries
    _apply_mock_final_scoring(evaluated, config, interest)
    for item in evaluated:
        _normalize_llm_supported_text_fields(item)
        _apply_stable_ranking_score(item, interest)
    evaluated.sort(key=_stable_rank_key)
    log(f"{source_name}: evaluated {len(evaluated)} items")
    return evaluated



def _apply_mock_final_scoring(evaluated: list[dict], config: AppConfig, interest: str) -> None:
    if str(getattr(config, "provider", "")).lower() != "mock":
        return
    for item in evaluated:
        if str(item.get("reason_source") or "") == "llm abstract evaluation":
            continue
        if not _has_real_abstract(item):
            continue
        if _as_float(item.get("fit_score")) < 7.0:
            continue
        item["reason_source"] = "llm abstract evaluation"
        item["topic_evidence"] = "passed:adaptive_llm_topic_route"
        item["topic_evidence_supported"] = True
        item["topic_evidence_source"] = "mock_adaptive"
        item["topic_evidence_basis"] = "abstract"
        item["matched_topic_route"] = "mock adaptive route from current research profile"
        item["missing_topic_evidence"] = []
        item["unmatched_topic_routes"] = []
        item["evidence_role"] = "direct_target"
        item["hit_directions"] = item.get("hit_directions") or [interest or "mock research profile"]
        _set_hit_direction_language_fields(item, item.get("hit_directions"), zh_value=item.get("hit_directions_zh") or item.get("hit_directions"), en_value=item.get("hit_directions_en"))
        item["fit_explanation"] = item.get("fit_explanation") or "Mock final scoring for local smoke tests with real abstract evidence."
        item["fit_explanation_zh"] = item.get("fit_explanation_zh") or "mock 本地烟测评分：该候选有真实摘要和足够高的主题匹配分，可用于测试 Find 到后续阶段的衔接。"
        item["fit_explanation_en"] = item.get("fit_explanation_en") or "Mock local smoke scoring: this candidate has a real abstract and sufficient topic-fit score for testing downstream stage wiring."
        item["reason"] = item.get("reason") or "mock 本地烟测推荐：用于验证 Find、Read、Idea、Plan 流程衔接，不代表真实 LLM 审稿结论。"
        item["reason_zh"] = item.get("reason_zh") or item.get("reason")
        item["reason_en"] = item.get("reason_en") or "Mock local smoke recommendation for verifying Find, Read, Idea, and Plan wiring; not a real LLM review verdict."
        item.pop("not_positive_support", None)
        item.pop("weak_candidate_for_critique", None)
        item.pop("evidence_tier", None)
        _apply_quality_bonus(item)

def _has_final_title_abstract_llm_scoring(item: dict) -> bool:
    return str(item.get("reason_source") or "") == "llm abstract evaluation"


def _is_llm_supported(item: dict) -> bool:
    # Backward-compatible helper name. For Find recommendation gates, LLM support
    # means the final relevance judge scored this row. The title-only filter
    # must never qualify a recommendation by itself.
    return _has_final_title_abstract_llm_scoring(item)

def _metadata_tldr_text(item: dict) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    value = metadata.get("tldr") or item.get("tldr")
    if isinstance(value, dict):
        value = value.get("text")
    return _clean_abstract_text(value)


def _abstract_is_semantic_tldr(item: dict, text: str) -> bool:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    source = str(metadata.get("abstract_source") or item.get("abstract_source") or "").lower()
    if "tldr" in source:
        return True
    tldr = _metadata_tldr_text(item)
    return bool(tldr and " ".join(text.split()).casefold() == " ".join(tldr.split()).casefold())


def _has_real_abstract(item: dict) -> bool:
    text = _clean_abstract_text(item.get("abstract_en") or item.get("abstract"))
    if not text:
        return False
    if _abstract_is_semantic_tldr(item, text):
        return False
    if len(text) < 12:
        return False
    return bool(re.search(r"[A-Za-z一-鿿]", text))

def _item_metadata(item: dict) -> dict:
    return item.get("metadata") if isinstance(item.get("metadata"), dict) else {}



def _readable_text_len(value: object) -> int:
    return len("".join(str(value or "").split()))


def _first_nonempty_text(item: dict, keys: list[str]) -> str:
    for key in keys:
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _list_text(value: object, sep: str = "、") -> str:
    if isinstance(value, list):
        return sep.join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _reason_is_too_short(value: object, *, zh: bool = True) -> bool:
    text = " ".join(str(value or "").split())
    if not text:
        return True
    min_chars = int(os.environ.get("RECOMMENDATION_REASON_MIN_ZH_CHARS" if zh else "RECOMMENDATION_REASON_MIN_EN_CHARS", "120" if zh else "220") or (120 if zh else 220))
    if _readable_text_len(text) < min_chars:
        return True
    negative_only_markers = [
        "缺少", "没有", "未涉及", "不含", "不包含", "无",
        "lacks", "missing", "does not", "no ", "without",
    ]
    has_positive_marker = any(marker in text for marker in ["可", "提供", "借鉴", "支持", "展示", "提出", "验证", "启发", "useful", "reusable", "provides", "offers", "demonstrates"])
    return (not has_positive_marker) and any(marker in text.lower() for marker in negative_only_markers)


_INTERNAL_FIND_PUBLIC_TEXT_MARKERS_ZH = (
    "weak:",
    "passed:",
    "strong:",
    "topic_evidence",
    "matched_topic_route",
    "adaptive topic evidence",
    "adaptive_llm_topic_route",
    "missing adaptive topic evidence",
    "缺少当前主题",
    "高召回",
    "内部候选",
    "对 实现",
    "对AR实现",
    "Guardrail",
    "最终 LLM",
    "LLM 题名",
    "LLM 评分",
    "题名+摘要评分",
    "最终题名+摘要",
    "题名筛选线索",
    "最终相关性评分",
    "Find",
    "Top-N",
    "证据门控",
    "用户可见推荐",
    "推荐池",
    "检索候选",
    "值得推荐和精读",
    "为什么值得推荐精读",
    "帮助读者",
    "阅读提示",
    "摘要仍不足以替代全文精读",
    "全文精读",
    "需全文确认",
    "需在全文中继续确认",
    "需要全文",
    "精读阶段",
    "给 reader",
    "reader llm",
    "Gate reason",
    "paper-conclusion",
    "claim",
    "foundation",
)
_INTERNAL_FIND_PUBLIC_TEXT_MARKERS_EN = (
    "weak:",
    "passed:",
    "strong:",
    "topic_evidence",
    "matched_topic_route",
    "adaptive topic evidence",
    "adaptive_llm_topic_route",
    "missing adaptive topic evidence",
    "high-recall",
    "internal candidate",
    "implementation",
    "Guardrail",
    "final title+abstract",
    "LLM score",
    "Find",
    "Top-N",
    "evidence gate",
    "user-visible",
    "recommendation pool",
    "retrieval candidate",
    "Gate reason",
    "paper-conclusion",
    "claim",
    "foundation",
    "fallback-only",
    "worth recommending and reading",
    "recommended for deep reading",
    "reading note",
    "full-text reading",
    "full text reading",
    "full-text confirmation",
    "full text confirmation",
    "deep reading",
    "abstract is still not a substitute",
    "reader instruction",
)
_PUBLIC_FIND_RECOMMENDATION_NOTE_ZH = "题名和摘要与当前研究画像有明确交集，并提供可借鉴的方法、数据、评测或问题边界。"
_PUBLIC_FIND_RECOMMENDATION_NOTE_EN = "The title and abstract connect clearly to the research profile and offer reusable method, data, evaluation, or boundary value."
_READER_FIND_RECOMMENDATION_INSTRUCTION_ZH = "内部给 Read 阶段：基于论文正文核查 Find 阶段的题名/摘要信号是否成立，重点记录方法细节、数据设置、评测协议、结果边界和局限性。"
_READER_FIND_RECOMMENDATION_INSTRUCTION_EN = "Internal instruction for the Read stage: use the paper body to verify whether the title/abstract signals from Find hold, and record method details, data settings, evaluation protocol, result boundaries, and limitations."


def _has_internal_find_public_text(text: object, *, zh: bool = True) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    markers = _INTERNAL_FIND_PUBLIC_TEXT_MARKERS_ZH if zh else _INTERNAL_FIND_PUBLIC_TEXT_MARKERS_EN
    lowered = raw.lower()
    return any(str(marker).lower() in lowered for marker in markers)


def _public_route_text(item: dict, *, en: bool = False) -> str:
    for key in ("source_supported_adaptive_route", "matched_topic_route"):
        value = _first_nonempty_text(item, [key])
        if value and not _has_internal_find_public_text(value, zh=not en):
            return value
    return ""


def _public_interest_context(config: AppConfig | None, *, en: bool = False) -> str:
    raw = ""
    if config is not None:
        try:
            raw = _topic_interest_text(config)
        except Exception:
            raw = ""
    raw = " ".join(str(raw or "").split())
    if len(raw) > 150:
        raw = raw[:150].rstrip() + "..."
    if en:
        return f"the current research profile ({raw})" if raw else "the current research profile"
    has_cjk = any("\u4e00" <= char <= "\u9fff" for char in raw)
    return f"当前研究画像（{raw}）" if raw and has_cjk else "当前研究画像"


def _ensure_recommendation_readability(item: dict, config: AppConfig | None = None) -> dict:
    title = str(item.get("title") or "该论文").strip() or "该论文"
    interest_zh = _public_interest_context(config, en=False)
    interest_en = _public_interest_context(config, en=True)
    matched_route = _public_route_text(item, en=False)
    matched_route_en = _public_route_text(item, en=True)
    hit_zh = _list_text(item.get("hit_directions_zh") or item.get("hit_directions"))
    hit_en = _list_text(item.get("hit_directions_en") or item.get("hit_directions"), sep=", ")
    fit_zh = _first_nonempty_text(item, ["fit_explanation_zh", "fit_explanation"])
    fit_en = _first_nonempty_text(item, ["fit_explanation_en", "fit_explanation"])
    if _has_internal_find_public_text(fit_zh, zh=True):
        fallback_fit_zh = _first_nonempty_text(item, ["fit_explanation_zh_original", "fit_explanation_original"])
        fit_zh = "" if _has_internal_find_public_text(fallback_fit_zh, zh=True) else fallback_fit_zh
    if _has_internal_find_public_text(fit_en, zh=False):
        fallback_fit_en = _first_nonempty_text(item, ["fit_explanation_en_original", "fit_explanation_original"])
        fit_en = "" if _has_internal_find_public_text(fallback_fit_en, zh=False) else fallback_fit_en
    note_zh = _first_nonempty_text(item, ["recommendation_note_zh", "recommendation_note"])
    note_en = _first_nonempty_text(item, ["recommendation_note_en", "recommendation_note"])
    if _has_internal_find_public_text(note_zh, zh=True):
        note_zh = ""
    if _has_internal_find_public_text(note_en, zh=False):
        note_en = ""
    missing_raw = _list_text(item.get("unmatched_topic_routes") or item.get("missing_topic_evidence"))
    missing = "" if _has_internal_find_public_text(missing_raw, zh=True) else missing_raw
    tier = str(item.get("evidence_tier") or "").strip()
    role = str(item.get("evidence_role") or "").strip()
    boundary = bool(item.get("weak_candidate_for_critique") or item.get("not_positive_support") or tier in {"nethreshold_for_reading", "critique_or_boundary_case", "retrieval_only", "weak_or_boundary"})
    if item.get("_user_visible_recommendation") or item.get("find_recommendation") or item.get("recommended_by_llm_ranking"):
        boundary = False

    def _sentence_join(parts: list[str]) -> str:
        return "".join(part if part.endswith(("。", "！", "？")) else part + "。" for part in parts if part)

    def zh_reason() -> str:
        signal = hit_zh or matched_route or "方法、数据、评测或问题边界"
        parts = [
            f"对{interest_zh}来说，《{title}》的价值在于它把{signal}落到一个可比较的研究对象上。",
            fit_zh or (f"它与当前研究目标的连接点是 {matched_route}。" if matched_route else "它提供了可被后续方案选择引用的具体方法线索、数据线索、评测线索或失败边界。"),
            "可直接借鉴的部分包括方法结构、数据或反馈构造、评价指标、消融设计和风险边界；这些信息能帮助确定当前路线的基线、改造点和风险控制。",
        ]
        if boundary:
            parts.append("目前它更适合作为对照或边界案例，避免把局部相关信号误当成核心方法依据。")
        if missing:
            parts.append(f"摘要层面的未覆盖信息是：{missing}；这会影响它能否作为核心证据。")
        elif note_zh and note_zh != _PUBLIC_FIND_RECOMMENDATION_NOTE_ZH:
            parts.append(f"补充判断：{note_zh}")
        else:
            parts.append("摘要层面已经给出相关信号，但具体收益、复现实验条件和适用边界仍是后续科研决策中的风险点。")
        return _sentence_join(parts)

    def zh_fit_explanation() -> str:
        parts: list[str] = []
        if hit_zh:
            parts.append(f"这篇论文在题名和摘要中呈现的核心相关点是：{hit_zh}。")
        if matched_route:
            parts.append(f"它与当前研究目标的连接点是 {matched_route}。")
        else:
            parts.append(f"它与{interest_zh}的关联体现在可比较的方法结构、数据或反馈构造、评测协议和失败边界上。")
        parts.append("公开摘要已经足以说明它不是泛泛背景文献；需要记录的风险是摘要尚不能证明其结论可以直接迁移到当前项目。")
        return _sentence_join(parts)

    def en_fit_explanation() -> str:
        parts: list[str] = []
        if hit_en:
            parts.append(f"The title and abstract expose these relevant signals: {hit_en}.")
        if matched_route_en:
            parts.append(f"Its connection to the current research goal is {matched_route_en}.")
        else:
            parts.append(f"Its connection to {interest_en} is through comparable method structure, data or feedback construction, evaluation protocol, and failure boundaries.")
        parts.append("The abstract-level evidence makes it more than generic background, while the main risk is that transfer to the current project is not yet proven by the abstract alone.")
        return " ".join(part if part.endswith((".", "!", "?")) else part + "." for part in parts if part)

    def en_reason() -> str:
        signal = hit_en or matched_route_en or "method, data, evaluation, or problem-boundary evidence"
        parts = [
            f"For {interest_en}, {title} is useful because it turns {signal} into a concrete object for comparison.",
            fit_en or (f"Its connection to the current research goal is {matched_route_en}." if matched_route_en else "It offers concrete method, data, evaluation, or boundary signals that can inform later research decisions."),
            "Reusable value includes method structure, data or feedback construction, metrics, ablations, and risk boundaries; these help choose baselines, modification points, and risk controls for the project.",
        ]
        if boundary:
            parts.append("At this stage it is better treated as a contrast or boundary case, so a partial signal is not mistaken for core method evidence.")
        if missing:
            parts.append(f"The abstract-level missing information is: {missing}; this affects whether it can serve as core evidence.")
        elif note_en and note_en != _PUBLIC_FIND_RECOMMENDATION_NOTE_EN:
            parts.append(f"Additional judgment: {note_en}")
        else:
            parts.append("The abstract provides relevant signals, but concrete gains, reproducibility conditions, and scope remain project risks.")
        return " ".join(part if part.endswith((".", "!", "?")) else part + "." for part in parts if part)

    def reader_instruction_zh() -> str:
        parts = [_READER_FIND_RECOMMENDATION_INSTRUCTION_ZH, f"论文：《{title}》。"]
        if hit_zh:
            parts.append(f"优先核查 Find 信号：{hit_zh}。")
        if matched_route:
            parts.append(f"核查其与当前研究目标的连接点：{matched_route}。")
        if missing:
            parts.append(f"特别核查摘要未覆盖的信息：{missing}。")
        return _sentence_join(parts)

    def reader_instruction_en() -> str:
        parts = [_READER_FIND_RECOMMENDATION_INSTRUCTION_EN, f"Paper: {title}."]
        if hit_en:
            parts.append(f"Prioritize these Find signals: {hit_en}.")
        if matched_route_en:
            parts.append(f"Verify its connection to the current research goal: {matched_route_en}.")
        if missing:
            parts.append(f"Pay special attention to abstract-level missing information: {missing}.")
        return " ".join(part if part.endswith((".", "!", "?")) else part + "." for part in parts if part)

    current_reason_zh = item.get("reason_zh") or item.get("reason")
    if _reason_is_too_short(current_reason_zh, zh=True) or _has_internal_find_public_text(current_reason_zh, zh=True):
        original = str(current_reason_zh or "").strip()
        if original:
            item.setdefault("reason_zh_original", original)
        item["reason_zh"] = zh_reason()
        item["reason"] = item["reason_zh"]
        item["reason_quality_repaired"] = True
    current_reason_en = item.get("reason_en")
    if _reason_is_too_short(current_reason_en, zh=False) or _has_internal_find_public_text(current_reason_en, zh=False):
        original_en = str(current_reason_en or "").strip()
        if original_en:
            item.setdefault("reason_en_original", original_en)
        item["reason_en"] = en_reason()
        item["reason_quality_repaired"] = True
    current_fit_zh = item.get("fit_explanation_zh")
    if _reason_is_too_short(current_fit_zh, zh=True) or _has_internal_find_public_text(current_fit_zh, zh=True):
        base = fit_zh or zh_fit_explanation()
        item.setdefault("fit_explanation_zh_original", str(current_fit_zh or "").strip())
        item["fit_explanation_zh"] = base if _readable_text_len(str(base)) >= 80 and not _has_internal_find_public_text(base, zh=True) else zh_fit_explanation()
    current_fit_en = item.get("fit_explanation_en")
    if _reason_is_too_short(current_fit_en, zh=False) or _has_internal_find_public_text(current_fit_en, zh=False):
        base_en = fit_en or en_fit_explanation()
        item.setdefault("fit_explanation_en_original", str(current_fit_en or "").strip())
        item["fit_explanation_en"] = base_en if _readable_text_len(str(base_en)) >= 80 and not _has_internal_find_public_text(base_en, zh=False) else en_fit_explanation()
    if not str(item.get("reader_instruction_zh") or "").strip():
        item["reader_instruction_zh"] = reader_instruction_zh()
    if not str(item.get("reader_instruction_en") or "").strip():
        item["reader_instruction_en"] = reader_instruction_en()
    item["reader_instruction"] = item.get("reader_instruction_zh") or item.get("reader_instruction_en") or ""
    current_note_zh = item.get("recommendation_note_zh") or item.get("recommendation_note")
    current_note_en = item.get("recommendation_note_en")
    if (not str(current_note_zh or "").strip()) or _has_internal_find_public_text(current_note_zh, zh=True):
        item["recommendation_note_zh"] = _PUBLIC_FIND_RECOMMENDATION_NOTE_ZH
        item["recommendation_note"] = item["recommendation_note_zh"]
    if (not str(current_note_en or "").strip()) or _has_internal_find_public_text(current_note_en, zh=False):
        item["recommendation_note_en"] = _PUBLIC_FIND_RECOMMENDATION_NOTE_EN
    item.setdefault("recommendation_audit_role", "boundary_or_borrowing" if boundary else (role or "direct_or_foundation"))
    return item


def _recommendation_quality_audit(items: list[dict]) -> dict:
    rows = [item for item in items if isinstance(item, dict)]
    missing_zh_abstract = [str(item.get("id") or item.get("title") or "") for item in rows if _clean_abstract_text(item.get("abstract_en") or item.get("abstract")) and not str(item.get("abstract_zh") or "").strip()]
    missing_real_abstract = [str(item.get("id") or item.get("title") or "") for item in rows if not _has_real_abstract(item)]
    short_reason = [str(item.get("id") or item.get("title") or "") for item in rows if _reason_is_too_short(item.get("reason_zh") or item.get("reason"), zh=True)]
    return {
        "status": "needs_repair" if (missing_real_abstract or short_reason) else "ok" if not missing_zh_abstract else "ok_with_translation_todo",
        "recommendation_count": len(rows),
        "missing_real_abstract_count": len(missing_real_abstract),
        "missing_chinese_abstract_count": len(missing_zh_abstract),
        "short_or_negative_reason_count": len(short_reason),
        "english_abstract_fallback_count": len(missing_zh_abstract),
        "missing_real_abstract_ids": missing_real_abstract[:50],
        "missing_chinese_abstract_ids": missing_zh_abstract[:50],
        "short_or_negative_reason_ids": short_reason[:50],
        "policy": "User-facing recommendations must come from the final title+abstract LLM score ranking, show a real abstract, complete Chinese abstracts before marking translation completed, and give a specific multi-sentence recommendation reason covering concrete title/abstract evidence, value for the user research profile, reusable method/data/protocol/theory/evaluation value, and abstract-level risks. Reader-only full-text instructions must stay in reader_instruction_* fields. Topic/debug fields cannot create a second recommendation gate.",
    }


def _missing_chinese_abstract_ids(items: list[dict]) -> list[str]:
    rows = [item for item in items if isinstance(item, dict)]
    return [
        str(item.get("id") or item.get("title") or "")
        for item in rows
        if _clean_abstract_text(item.get("abstract_en") or item.get("abstract"))
        and not str(item.get("abstract_zh") or "").strip()
    ]


def _recommendation_translation_status(items: list[dict], stored_status: str = "") -> str:
    rows = [item for item in items if isinstance(item, dict)]
    if _missing_chinese_abstract_ids(rows):
        return "partial"
    if any(_clean_abstract_text(item.get("abstract_en") or item.get("abstract")) for item in rows):
        return "completed"
    return str(stored_status or "not_needed")


def _has_strong_topic_evidence(item: dict) -> bool:
    evidence = str(item.get("topic_evidence") or "").lower()
    if not evidence.startswith(("passed:", "strong:")):
        return False
    if item.get("topic_evidence_supported") is False:
        return False
    if _has_topic_evidence_contradiction(item):
        return False
    if item.get("evidence_role") == "foundation_borrowing" and not _foundation_strong_enough(item):
        return False
    invalid_reason = _strong_topic_invalid_reason(item)
    if invalid_reason:
        item["foundation_invalid_reason"] = invalid_reason
        return False
    return True


def _llm_expected(config: AppConfig | None) -> bool:
    return bool(config and config.api_key and config.model and config.provider.lower() != "mock")


def _mark_evidence_tier(item: dict) -> dict:
    fit = float(item.get("fit_score") or 0)
    invalid_reason = _strong_topic_invalid_reason(item)
    contradiction_reason = "Contradictory or missing adaptive topic evidence." if _has_topic_evidence_contradiction(item) else ""
    if (invalid_reason or contradiction_reason) and str(item.get("topic_evidence") or "").lower().startswith(("passed:", "strong:")):
        reason = invalid_reason or contradiction_reason
        item["foundation_invalid_reason"] = reason
        if str(item.get("evidence_role") or "") == "foundation_borrowing":
            _demote_unstable_foundation_item(item)
        else:
            _mark_not_positive_for_strong_gate(item, reason)
        fit = float(item.get("fit_score") or 0)
    if not _is_llm_supported(item):
        item.setdefault("evidence_tier", "retrieval_only")
        item["not_positive_support"] = True
        item["weak_candidate_for_critique"] = True
        item.setdefault("recommendation_note", "Title-screened item only; not validated by final relevance scoring.")
        item.setdefault("recommendation_note_zh", "题名筛选线索：尚未通过最终相关性评分，不展示为推荐论文。")
        item.setdefault("recommendation_note_en", "Title-screened item only; not validated by final relevance scoring.")
        return item
    if item.get("evidence_role") == "foundation_borrowing":
        item["evidence_tier"] = "nethreshold_for_reading"
        item["weak_candidate_for_critique"] = True
        item["not_positive_support"] = True
        item["find_recommendation_reject_reason"] = "background_or_foundation_not_user_visible_recommendation"
        item["recommendation_note_zh"] = "非推荐背景线索：可作为后续精读或检索扩展参考，但不是 Find 用户可见推荐论文。"
        item["recommendation_note_en"] = "Non-recommended background signal: useful for later reading or search expansion, but not a user-visible Find recommendation."
        item["recommendation_note"] = item["recommendation_note_zh"]
    elif _true_strong_recommendation(item):
        item["evidence_tier"] = "strong_recommendation"
        item["weak_candidate_for_critique"] = False
        item["recommendation_note_zh"] = "已通过真实摘要和最终相关性评分，可作为重点精读候选。"
        item["recommendation_note_en"] = "Passed final relevance scoring with a real abstract."
        item["recommendation_note"] = item["recommendation_note_zh"]
    elif fit >= 6 and _has_strong_topic_evidence(item):
        item["evidence_tier"] = "nethreshold_for_reading"
        item["weak_candidate_for_critique"] = True
        item["not_positive_support"] = True
        item["recommendation_note_zh"] = "未入选线索：有局部相关信号，但未进入最终推荐列表；只用于推荐质量排查或扩展检索，不属于用户可见推荐精读论文。"
        item["recommendation_note_en"] = "Not selected for recommendation; it has partial relevance signals but is not user-visible recommended reading."
        item["recommendation_note"] = item["recommendation_note_zh"]
    elif fit >= 5:
        item["evidence_tier"] = "nethreshold_for_reading"
        item["weak_candidate_for_critique"] = True
        item["not_positive_support"] = True
        item.setdefault("recommendation_note_zh", "LLM 近阈值线索：保留用于推荐质量排查或扩展检索；当前不展示为推荐论文。")
        item.setdefault("recommendation_note_en", "LLM-scored near-threshold signal kept for recommendation-quality checks or search expansion; not user-visible recommended reading.")
        item.setdefault("recommendation_note", item.get("recommendation_note_zh") or item.get("recommendation_note_en"))
    else:
        item["evidence_tier"] = "critique_or_boundary_case"
        item["weak_candidate_for_critique"] = True
        item["not_positive_support"] = True
        item.setdefault("recommendation_note_zh", "弱相关线索：仅用于推荐质量排查或扩展检索；不展示为推荐论文。")
        item.setdefault("recommendation_note_en", "Weak signal kept only for recommendation-quality checks or search expansion; not user-visible recommended reading.")
        item.setdefault("recommendation_note", item.get("recommendation_note_zh") or item.get("recommendation_note_en"))
    return item


def _recommendation_rank_key(item: dict) -> tuple:
    fit = _as_float(item.get("llm_fit_score"), item.get("fit_score"))
    combined = _as_float(item.get("llm_combined_score"), item.get("combined_score"))
    boundary_combined_tie = -round(combined / 0.01) * 0.01 if fit < 7.0 else 0.0
    return (
        -round(fit / 0.01) * 0.01,
        boundary_combined_tie,
        str(item.get("title") or item.get("id") or item.get("url") or "").lower(),
    )


def _find_recommendation_invalid_reason(item: dict, config: AppConfig | None) -> str:
    """Validate the single user-visible Find recommendation contract.

    Find recommends papers for deep reading by one path only: the title screen
    supplies candidates, detail enrichment supplies a real abstract, the final
    title+abstract LLM judge scores them, and the UI/Read pool takes the top-N
    ranked rows. Topic-evidence/foundation/route labels are audit metadata; they
    must not hard-filter, promote, or backfill user-visible recommendations.
    """
    if _llm_expected(config) and not _is_llm_supported(item):
        return "missing_final_title_abstract_llm_scoring"
    if item.get("llm_final_scoring_skipped") or item.get("llm_retry_exhausted"):
        return str(item.get("llm_final_scoring_skip_reason") or item.get("llm_retry_reason") or "final_llm_scoring_unavailable")
    # Read/full-text acquisition is a downstream Read-stage contract. It must
    # not remove a row from the user-visible Find Top-N ranking, otherwise Find
    # becomes two systems: title+abstract LLM ranking first, then a hidden
    # full-text replacement pass.
    if not _has_real_abstract(item):
        return "missing_real_abstract"
    if item.get("abstract_fetch_failed"):
        return str(item.get("abstract_fetch_failed_reason") or "abstract_fetch_failed")
    if item.get("llm_fit_score") in (None, "") and item.get("fit_score") in (None, ""):
        return "missing_final_title_abstract_llm_fit_score"
    # Full-text/PDF entrypoint availability is a Read-stage contract. Find must
    # not drop an otherwise well-scored title+abstract recommendation because a
    # venue page needs same-title arXiv/ACM/OpenReview resolution later.
    # The route booleans, topic/foundation labels, diversity, source quality,
    # and any fixed fit threshold are audit/debug fields here. After a row has
    # a real abstract and final title+abstract LLM score, the user-visible Find
    # list is the configured Top-N ranking. Do not create a second hard filter
    # that can shrink the recommendation count below target.
    return ""


def _recommendable_ranked(items: list[dict], config: AppConfig | None) -> list[dict]:
    ranked = sorted(_dedupe_items(items), key=_recommendation_rank_key)
    recommended: list[dict] = []
    for item in ranked:
        reason = _find_recommendation_invalid_reason(item, config)
        if reason:
            item["find_recommendation_reject_reason"] = reason
            item.setdefault("strong_gate_reject_reason", reason)
            item["not_positive_support"] = True
            item["weak_candidate_for_critique"] = True
            if str(item.get("evidence_tier") or "").lower() not in {"detail_fetch_failed", "retrieval_only"}:
                item["evidence_tier"] = "nethreshold_for_reading"
            item.setdefault("recommendation_note_zh", "未进入推荐列表：缺少最终相关性评分或真实摘要，只保留为未入选检索线索。")
            item.setdefault("recommendation_note_en", "Not included in recommendations: missing final relevance scoring or a real abstract; retained only as a non-selected search signal.")
            item.setdefault("recommendation_note", item.get("recommendation_note_zh") or item.get("recommendation_note_en"))
            continue
        item.pop("find_recommendation_reject_reason", None)
        item.pop("not_positive_support", None)
        item.pop("strong_gate_reject_reason", None)
        item.pop("recommended_by_llm_ranking", None)
        item.pop("find_recommendation", None)
        item.pop("_user_visible_recommendation", None)
        item["find_recommendation_candidate"] = True
        item["evidence_tier"] = "final_llm_scored_candidate"
        item["weak_candidate_for_critique"] = False
        item.setdefault("recommendation_note_zh", "已完成真实摘要相关性评分并进入排序候选；最终推荐列表按分数取前列论文。")
        item.setdefault("recommendation_note_en", "Final relevance scoring completed and the row entered the ranked candidate list; final recommendations are the highest-ranked papers.")
        item.setdefault("recommendation_note", item.get("recommendation_note_zh") or item.get("recommendation_note_en"))
        recommended.append(item)
    return recommended


def _strict_rank_input(items: list[dict]) -> list[dict]:
    copies: list[dict] = []
    for item in items:
        row = dict(item)
        if isinstance(item.get("metadata"), dict):
            row["metadata"] = dict(item.get("metadata") or {})
        copies.append(row)
    return copies


def _supported_ranked(items: list[dict], config: AppConfig | None) -> list[dict]:
    ranked = sorted(_dedupe_items(items), key=_recommendation_rank_key)
    interest = _topic_interest_text(config) if config else ""
    if _llm_expected(config):
        ranked = [item for item in ranked if _is_llm_supported(item)]
    if interest:
        for item in ranked:
            _topic_gate_required_groups(item, interest)
            if item.get("evidence_role") == "foundation_borrowing":
                item["find_recommendation_reject_reason"] = "background_or_foundation_not_user_visible_recommendation"
                item["not_positive_support"] = True
                item["weak_candidate_for_critique"] = True
                if str(item.get("evidence_tier") or "").lower() == "strong_recommendation":
                    item["evidence_tier"] = "nethreshold_for_reading"
            elif _strong_topic_invalid_reason(item):
                _mark_not_positive_for_strong_gate(item, _strong_topic_invalid_reason(item))
    return [_mark_evidence_tier(item) for item in ranked]


def _true_strong_recommendation(item: dict) -> bool:
    fit = float(item.get("fit_score") or 0)
    if fit <= 2.0:
        _mark_not_positive_for_strong_gate(item, "final relevance score marks this row as unrelated")
        return False
    if item.get("foundation_minimum_fit_not_strong") and _as_float(item.get("fit_score"), 0) < 6.0:
        _mark_not_positive_for_strong_gate(item, "deterministic source-route repair below fit 6 is a non-selected search signal, not recommendation evidence")
        return False
    if item.get("not_positive_support") or item.get("foundation_demoted_from_strong"):
        return False
    if not (_has_strong_topic_evidence(item) and _has_real_abstract(item)):
        return False
    strict_reason = _strict_strong_invalid_reason(item)
    if strict_reason:
        _mark_not_positive_for_strong_gate(item, strict_reason)
        return False
    return True


def _strong_recommendation_rank_key(item: dict) -> tuple:
    return _recommendation_rank_key(item)


def _strong_ranked(items: list[dict], config: AppConfig | None) -> list[dict]:
    strong = [
        item
        for item in _supported_ranked(items, config)
        if _true_strong_recommendation(item)
    ]
    return sorted(strong, key=_strong_recommendation_rank_key)


def _selection_source_count(selection: object) -> int:
    count = _selection_venue_unit_count(selection)
    for name in ("include_arxiv", "include_biorxiv", "include_huggingface", "include_github", "include_nature", "include_science"):
        enabled = getattr(selection, name, None)
        if enabled is None and isinstance(selection, dict):
            enabled = selection.get(name)
        if enabled:
            count += 1
    return max(1, count)

def _strong_recommendation_count_cap(target: int, source_count: int | None = None) -> int:
    limits = [max(1, int(target or 1))]
    configured_max = int(os.environ.get("STRONG_RECOMMENDATION_MAX_COUNT", "0") or 0)
    if configured_max > 0:
        limits.append(configured_max)
    if source_count is not None:
        limits.append(max(1, int(source_count or 1)) * 5)
    return max(1, min(limits))


def _strong_recommendation_target_count(config: AppConfig, source_count: int | None = None) -> int:
    configured_target = int(os.environ.get("STRONG_RECOMMENDATION_TARGET_COUNT", "0") or 0)
    if configured_target > 0:
        return _strong_recommendation_count_cap(configured_target, source_count)
    requested = int(getattr(config, "max_recommended_papers", 0) or 0)
    if requested > 0:
        return _strong_recommendation_count_cap(requested, source_count)
    if source_count is not None:
        return _strong_recommendation_count_cap(max(1, int(source_count or 1)) * 5, source_count)
    source_hint = _source_count_hint(config.default_find_selection or {})
    if source_hint > 0:
        return _strong_recommendation_count_cap(source_hint * 5, source_hint)
    return _strong_recommendation_count_cap(20)

def _strong_recommendation_output_count(config: AppConfig, source_count: int | None = None) -> int:
    return _strong_recommendation_target_count(config, source_count)


def _recommended(items: list[dict], config: AppConfig, source_count: int | None = None) -> list[dict]:
    target = _strong_recommendation_output_count(config, source_count)
    recommended: list[dict] = []
    for item in _recommendable_ranked(items, config):
        item["recommendation_note_zh"] = _PUBLIC_FIND_RECOMMENDATION_NOTE_ZH
        item["recommendation_note_en"] = _PUBLIC_FIND_RECOMMENDATION_NOTE_EN
        item["recommendation_note"] = item["recommendation_note_zh"]
        _ensure_recommendation_readability(item, config)
        item.pop("find_recommendation_candidate", None)
        item.pop("not_positive_support", None)
        item.pop("strong_gate_reject_reason", None)
        item["recommended_by_llm_ranking"] = True
        item["find_recommendation"] = True
        item["_user_visible_recommendation"] = True
        item["evidence_tier"] = "strong_recommendation"
        item["weak_candidate_for_critique"] = False
        item["recommendation_note_zh"] = _PUBLIC_FIND_RECOMMENDATION_NOTE_ZH
        item["recommendation_note_en"] = _PUBLIC_FIND_RECOMMENDATION_NOTE_EN
        item["recommendation_note"] = item.get("recommendation_note_zh") or item.get("recommendation_note_en")
        recommended.append(item)
        if len(recommended) >= target:
            break
    return recommended


def _strict_strong_anchor_count(items: list[dict], config: AppConfig | None, source_count: int | None = None) -> int:
    # Backward-compatible counter name. In the unified Find gate it is capped to
    # the exact same user-visible recommendation pool. Paper-claim readiness is
    # decided later by Read/reproduction/experiment evidence gates, not by a
    # second Find ranking.
    if config is None:
        return 0
    return len(_recommended(_strict_rank_input(items), config, source_count=source_count))


def _screened_ranking(items: list[dict], config: AppConfig | None = None) -> list[dict]:
    if config is None:
        return []
    return _recommendable_ranked(_strict_rank_input(items), config)

def _candidate_is_positive_evidence(item: dict) -> bool:
    """Backward-compatible name for the Find recommendation-pool check.

    This is not a paper-claim evidence gate. A Find recommendation means the row
    was finally scored by the title+abstract LLM and has a real abstract; the
    final user-visible list is then the configured Top-N ranking.
    """
    if item.get("llm_final_scoring_skipped") or item.get("llm_retry_exhausted"):
        return False
    if not _is_llm_supported(item):
        return False
    if not _has_real_abstract(item):
        return False
    if item.get("llm_fit_score") in (None, "") and item.get("fit_score") in (None, ""):
        return False
    return True


def _retrieval_only_copy(item: dict, evaluated_map: dict[str, dict]) -> dict:
    row = dict(item)
    evaluated = next((evaluated_map.get(key) for key in _paper_identity_keys(row) if evaluated_map.get(key)), None)
    row["retrieval_pool_only"] = True
    row["not_positive_support"] = True
    row["weak_candidate_for_critique"] = True
    if evaluated:
        row["evaluated_evidence_tier"] = evaluated.get("evidence_tier", "")
        row["evaluated_evidence_role"] = evaluated.get("evidence_role", "")
        row["evaluated_fit_score"] = evaluated.get("fit_score", "")
        row["evaluated_recommendation_score"] = evaluated.get("recommendation_score", evaluated.get("score", ""))
        row["evaluated_topic_evidence"] = evaluated.get("topic_evidence", "")
        if not _candidate_is_positive_evidence(evaluated):
            for key in [
                "topic_evidence",
                "topic_evidence_supported",
                "evidence_role",
                "evidence_tier",
                "foundation_demoted_from_strong",
                "foundation_invalid_reason",
                "missing_topic_evidence",
                "unmatched_topic_routes",
                "recommendation_note",
                "recommendation_note_zh",
                "recommendation_note_en",
                "reason",
                "reason_zh",
                "reason_en",
                "fit_explanation",
                "fit_explanation_zh",
                "fit_explanation_en",
                "fit_score",
                "diversity_score",
                "score",
                "recommendation_score",
                "stable_rank_score",
                "stable_source_score",
            ]:
                if key in evaluated:
                    row[key] = evaluated[key]
            row["not_positive_support"] = True
            row["weak_candidate_for_critique"] = True
            return row
        row["evaluated_recommended"] = True
    row["evidence_tier"] = "retrieval_only"
    row["evidence_role"] = "retrieval_candidate"
    row["recommendation_note_zh"] = "题名筛选线索：尚未进入推荐列表；只有最终推荐列表中的同篇论文才需要精读。"
    row["recommendation_note_en"] = "Title-screened signal only; only the same paper in the final recommendation list enters deep reading."
    row["recommendation_note"] = row["recommendation_note_zh"]
    return row


def _retrieval_only_pool(items: list[dict], evaluated: list[dict]) -> list[dict]:
    evaluated_map = _evaluated_by_identity(evaluated)
    return [_retrieval_only_copy(item, evaluated_map) for item in _dedupe_items(items)]


def _read_candidates(items: list[dict], config: AppConfig, source_count: int | None = None) -> list[dict]:
    """Return the exact user-facing recommendation pool that Read must process."""
    return list(_recommended(_strict_rank_input(items), config, source_count=source_count))


def _triage_candidates(items: list[dict], config: AppConfig) -> list[dict]:
    target = _target_triage_candidate_count(config)
    recommendation_keys = {
        str(item.get("id") or item.get("url") or item.get("title") or "")
        for item in _recommended(_strict_rank_input(items), config)
        if str(item.get("id") or item.get("url") or item.get("title") or "")
    }
    seen: set[str] = set()
    expanded: list[dict] = []
    for item in _supported_ranked(_strict_rank_input(items), config):
        key = str(item.get("id") or item.get("url") or item.get("title") or "")
        if not key or key in seen or key in recommendation_keys:
            continue
        if float(item.get("stable_source_score") or item.get("score") or 0) < 4.0:
            continue
        if item.get("retrieval_pool_only"):
            continue
        if item.get("find_recommendation"):
            continue
        item["weak_candidate_for_critique"] = True
        item["not_positive_support"] = True
        if str(item.get("evidence_tier") or "").lower() == "strong_recommendation":
            item["evidence_tier"] = "nethreshold_for_reading"
        item.setdefault(
            "recommendation_note",
            "Retained for contrast checks or search expansion; it is not part of the recommended reading pool.",
        )
        item.setdefault("recommendation_note_zh", "对照线索：用于误推荐排查或扩展检索，不属于推荐精读论文。")
        item.setdefault("recommendation_note_en", "Contrast signal for misrecommendation checks or search expansion; not part of the recommended reading pool.")
        expanded.append(item)
        seen.add(key)
        if len(expanded) >= target:
            break
    return expanded[:target]

def _critique_candidates(items: list[dict], config: AppConfig) -> list[dict]:
    ranked = [item for item in _supported_ranked(_strict_rank_input(items), config) if 0 < float(item.get("fit_score") or 0) < 6]
    target = _target_triage_candidate_count(config)
    return ranked[:target]


def _looks_english(text: str) -> bool:
    letters = re.findall(r"[A-Za-z]", text or "")
    cjk = re.findall(r"[\u4e00-\u9fff]", text or "")
    return len(letters) >= 20 and len(letters) > len(cjk) * 2


def _clean_abstract_text(value: object) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    placeholders = {
        "no abstract available",
        "no abstract available.",
        "abstract not available",
        "abstract not available.",
        "n/a",
        "none",
        "null",
    }
    if text.strip().lower() in placeholders:
        return ""
    return text


def _clean_missing_abstract_phrasing(value: object, *, zh: bool = False) -> str:
    text = str(value or "")
    if not text:
        return ""
    replacements = [
        (r"No abstract available(?: for further analysis)?[.;]?", "Indexed metadata lacks a real abstract; this item cannot be strong evidence without URL/PDF inspection."),
        (r"No abstract available to confirm ([^.。]+)\\.?", r"Indexed metadata lacks a real abstract to confirm \\1."),
        (r"当前索引元数据没有摘要[^。\\.]*[。\\.]?", "当前索引元数据缺少真实摘要；该候选不能作为强证据，除非后续 URL/PDF 精读补足证据。"),
    ]
    cleaned = text
    for pattern, replacement in replacements:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    if zh and cleaned == text and "No abstract available" in cleaned:
        cleaned = cleaned.replace("No abstract available.", "当前索引元数据缺少真实摘要。").replace("No abstract available", "当前索引元数据缺少真实摘要")
    return " ".join(cleaned.split())


def _sanitize_explanation_fields(items: list[dict]) -> None:
    fields = [
        "reason",
        "reason_zh",
        "reason_en",
        "fit_explanation",
        "fit_explanation_zh",
        "fit_explanation_en",
        "topic_evidence",
        "recommendation_note",
    ]
    for item in _dedupe_items(items):
        for field in fields:
            if field in item:
                item[field] = _clean_missing_abstract_phrasing(item.get(field), zh=field.endswith("_zh") or field in {"reason", "fit_explanation"})
        missing = item.get("missing_topic_evidence")
        if isinstance(missing, list):
            item["missing_topic_evidence"] = [_clean_missing_abstract_phrasing(value) for value in missing]
        unmatched = item.get("unmatched_topic_routes")
        if isinstance(unmatched, list):
            item["unmatched_topic_routes"] = [_clean_missing_abstract_phrasing(value) for value in unmatched]


def _chinese_translation_min_len(source: str) -> int:
    source_text = str(source or "").strip()
    return 6 if len(source_text) < 120 else max(24, min(180, int(len(source_text) * 0.18)))


def _clean_chinese_translation_output(text: str) -> str:
    cleaned = str(text or "").strip()
    # Some providers leak JSON escape residue at the very end of otherwise valid
    # Chinese strings, e.g. a decoded \n becoming a trailing "n". Remove only
    # isolated wrapper residue from long Chinese text, then normalize a missing
    # terminal punctuation mark. Length/CJK checks still reject short placeholders.
    if len(cleaned) >= 24 and re.search(r"[一-鿿]", cleaned):
        cleaned = re.sub(r"[\s\\]*[nrt]+$", "", cleaned).strip()
        tail = cleaned[-1:]
        allowed_tail = set("。！？.!?)）]】”’")
        allowed_tail.add(chr(34))
        allowed_tail.add(chr(39))
        if tail and tail not in allowed_tail:
            cleaned += "。"
    return cleaned.strip()


def _chinese_translation_reject_reason(text: str, source: str) -> str:
    cleaned = _clean_chinese_translation_output(text)
    if not cleaned:
        return "empty_translation"
    if not re.search(r"[一-鿿]", cleaned):
        return "missing_chinese_text"
    min_len = _chinese_translation_min_len(source)
    if len(cleaned) < min_len:
        return f"too_short:{len(cleaned)}<{min_len}"
    tail = cleaned.rstrip()[-1:]
    allowed_tail = set("。！？.!?)）]】”’")
    allowed_tail.add(chr(34))
    allowed_tail.add(chr(39))
    if tail and tail not in allowed_tail:
        return f"bad_sentence_tail:{tail}"
    return ""


def _looks_complete_chinese_translation(text: str, source: str) -> bool:
    return not _chinese_translation_reject_reason(text, source)


def _translation_priority(item: dict) -> tuple[int, str]:
    """Rank only user-visible recommendations for Chinese abstract translation."""
    visible_rank = 0 if item.get("_user_visible_recommendation") or item.get("find_recommendation") else 1
    return visible_rank, str(item.get("title") or item.get("id") or "")


def _attach_abstract_language_fields(items: list[dict], llm: LLMClient, log: LogFn, should_cancel: CancelFn, progress: ProgressFn = lambda *_args: None) -> dict:
    unique_items = _dedupe_items(items)
    _sanitize_explanation_fields(unique_items)
    targets: list[dict] = []
    for item in unique_items:
        abstract = _clean_abstract_text(item.get("abstract_en") or item.get("abstract"))
        if abstract:
            item["abstract"] = abstract
            item["abstract_en"] = abstract
            targets.append(item)
            continue
        item.pop("abstract_en", None)
        item.pop("abstract_zh", None)
        item["abstract"] = ""
        item["abstract_missing"] = True
        item["abstract_note"] = (
            "Abstract not available in the indexed venue metadata; open URL/PDF or run the Read stage for full-paper inspection."
        )
    if not targets or not llm.enabled:
        return {"status": "skipped", "translated": 0, "total": 0}
    missing = [
        item
        for item in targets
        if _looks_english(str(item.get("abstract_en") or "")) and not str(item.get("abstract_zh") or "").strip()
    ]
    if not missing:
        return {"status": "not_needed", "translated": 0, "total": 0}
    visible_missing = [
        item
        for item in missing
        if item.get("_user_visible_recommendation") or item.get("find_recommendation")
    ]
    if not visible_missing:
        log("articles: skipped Chinese abstract translation because no user-visible Find recommendations were supplied")
        return {"status": "skipped_no_user_visible_recommendations", "translated": 0, "total": 0, "missing": 0, "missing_visible": 0, "missing_ids": []}
    missing = visible_missing
    default_limit = len(visible_missing)
    missing.sort(key=_translation_priority)
    visible_missing_count = len(visible_missing)
    configured_limit = int(os.environ.get("TRANSLATE_ABSTRACT_LIMIT", "0") or 0)
    limit = max(configured_limit or default_limit, visible_missing_count)
    missing = missing[: max(0, limit)]
    batch_size = max(1, int(os.environ.get("TRANSLATE_ABSTRACT_BATCH_SIZE", "0") or 0) or 8)
    prompts: list[str] = []
    prompt_batches: list[list[dict]] = []
    for batch_index, batch in enumerate(_chunks(missing, batch_size), 1):
        item_lines = "\n\n".join(
            f"ID: {item.get('id')}\nTitle: {item.get('title')}\nAbstract: {str(item.get('abstract_en') or '')[:1800]}"
            for item in batch
        )
        prompts.append(f"""
Translate each full original paper abstract into faithful academic Chinese for the Chinese UI.

Rules:
- Translate the complete abstract; do not summarize, shorten, omit sentences, or turn it into a recommendation rationale.
- Preserve technical terms, model names, method names, dataset names, and abbreviations from the original abstract when appropriate.
- Keep the original meaning, motivation, method, experiment, and limitation/result statements present in the abstract.
- Do not add claims or commentary.
- Return JSON only.

Items, batch {batch_index}:
{item_lines}

Return:
{{"translations":[{{"id":"paper id","abstract_zh":"中文摘要"}}]}}
""")
        prompt_batches.append(batch)
    workers = clamp_workers(os.environ.get("TRANSLATE_ABSTRACT_WORKERS", "2"), default=2, maximum=4)
    original_timeout = getattr(llm, "timeout_sec", 120)
    original_retries = getattr(llm, "retries", None)
    translation_timeout = max(10, int(os.environ.get("TRANSLATE_ABSTRACT_TIMEOUT_SEC", "45") or 45))
    translation_max_tokens = max(1200, int(os.environ.get("TRANSLATE_ABSTRACT_MAX_TOKENS", "3500") or 3500))
    if hasattr(llm, "timeout_sec"):
        llm.timeout_sec = min(max(1, int(original_timeout or translation_timeout)), translation_timeout)
    if hasattr(llm, "retries"):
        llm.retries = max(1, int(os.environ.get("TRANSLATE_ABSTRACT_RETRIES", "1") or 1))
    log(f"articles: translating {len(missing)} abstracts for Chinese UI in {len(prompts)} batches with {workers} workers; timeout={getattr(llm, 'timeout_sec', translation_timeout)}s; retries={getattr(llm, 'retries', 'n/a')}; temperature=0.0")
    progress("abstract_translation", 0, max(1, len(prompts)), f"articles: translating {len(missing)} abstracts for Chinese UI")
    try:
        # Translation is part of the user-facing Find packet quality. Keep each call bounded,
        # then mark the packet partial if any final recommendation still lacks Chinese text.
        translation_wall_timeout = max(10, int(os.environ.get("TRANSLATE_ABSTRACT_WALL_TIMEOUT_SEC", str(translation_timeout + 5)) or (translation_timeout + 5)))
        results: list[dict] = []
        for batch_index, prompt in enumerate(prompts, 1):
            _raise_if_cancelled(should_cancel)
            progress("abstract_translation", batch_index - 1, max(1, len(prompts)), f"articles: translating batch {batch_index}/{len(prompts)} for Chinese UI")
            results.append(_json_or_error_wall_timeout(llm, prompt, temperature=0.0, max_tokens=translation_max_tokens, timeout_sec=translation_wall_timeout))
        for batch_index, (batch, result) in enumerate(zip(prompt_batches, results, strict=False), 1):
            if not result.get("ok"):
                log(f"articles: abstract translation batch failed: {str(result.get('error', ''))[:240]}")
                progress("abstract_translation", batch_index, max(1, len(prompts)), f"articles: translation batch {batch_index}/{len(prompts)} failed; final translation status will remain partial until repaired")
                continue
            data = result.get("data")
            rows = data.get("translations") if isinstance(data, dict) else []
            if isinstance(rows, dict):
                rows = [rows]
            by_id = {str(item.get("id")): item for item in batch}
            if isinstance(rows, list):
                for row in rows:
                    item = by_id.get(str(row.get("id") or "")) if isinstance(row, dict) else None
                    text = _clean_chinese_translation_output(str(row.get("abstract_zh") or "")) if isinstance(row, dict) else ""
                    if item and text and _looks_complete_chinese_translation(text, str(item.get("abstract_en") or "")):
                        item["abstract_zh"] = text
                    elif item and text:
                        reason = _chinese_translation_reject_reason(text, str(item.get("abstract_en") or ""))
                        log(f"articles: rejected incomplete Chinese abstract translation for {item.get('id')}: {reason}")
            translated_so_far = sum(1 for item in missing if str(item.get("abstract_zh") or "").strip())
            progress("abstract_translation", batch_index, max(1, len(prompts)), f"articles: translated batch {batch_index}/{len(prompts)}; {translated_so_far}/{len(missing)} abstracts ready")
        translated = sum(1 for item in missing if str(item.get("abstract_zh") or "").strip())
        remaining = [item for item in missing if not str(item.get("abstract_zh") or "").strip()]
        if remaining:
            remaining.sort(key=_translation_priority)
            retry_default = "4"
            configured_retry_limit = max(0, int(os.environ.get("TRANSLATE_ABSTRACT_SINGLE_RETRY_LIMIT", retry_default) or 0))
            visible_remaining_count = sum(1 for item in remaining if item.get("_user_visible_recommendation"))
            retry_limit = max(configured_retry_limit, visible_remaining_count)
            retry_items = remaining[:retry_limit]
            skipped = len(remaining) - len(retry_items)
            if skipped:
                log(f"articles: skipped {skipped} untranslated abstract single retries for non-visible rows; final recommendation rows stay prioritized")
            if retry_items:
                log(f"articles: retrying {len(retry_items)}/{len(remaining)} untranslated abstracts singly")
            recovered = 0
            for retry_index, item in enumerate(retry_items, 1):
                _raise_if_cancelled(should_cancel)
                progress("abstract_translation_retry", retry_index - 1, max(1, len(retry_items)), f"articles: retrying untranslated abstract {retry_index}/{len(retry_items)}")
                single_prompt = f"""
Translate this full original paper abstract into faithful academic Chinese for the Chinese UI.

Rules:
- Translate the complete abstract; do not summarize, shorten, omit sentences, or turn it into a recommendation rationale.
- Preserve technical terms, model names, method names, dataset names, and abbreviations from the original abstract when appropriate.
- Keep the original meaning, motivation, method, experiment, and limitation/result statements present in the abstract.
- Do not add claims or commentary.
- Return JSON only.

ID: {item.get('id')}
Title: {item.get('title')}
Abstract: {str(item.get('abstract_en') or '')[:2200]}

Return one of these JSON shapes:
{{"id":"paper id","abstract_zh":"中文摘要"}}
or
{{"translations":[{{"id":"paper id","abstract_zh":"中文摘要"}}]}}
"""
                result = _json_or_error_wall_timeout(
                    llm,
                    single_prompt,
                    temperature=0.0,
                    max_tokens=int(os.environ.get("TRANSLATE_ABSTRACT_MAX_TOKENS", "7000") or 7000),
                    timeout_sec=max(translation_wall_timeout, int(os.environ.get("TRANSLATE_ABSTRACT_SINGLE_RETRY_TIMEOUT_SEC", "75") or 75)),
                )
                if not result.get("ok"):
                    log(f"articles: abstract translation single retry failed for {item.get('id')}: {str(result.get('error', ''))[:180]}")
                    continue
                data = result.get("data")
                text = ""
                if isinstance(data, dict):
                    data_id = str(data.get("id") or item.get("id") or "").strip()
                    if data_id and data_id != str(item.get("id") or ""):
                        log(f"articles: abstract translation single retry returned mismatched id {data_id} for {item.get('id')}")
                    else:
                        text = _clean_chinese_translation_output(str(data.get("abstract_zh") or ""))
                    rows = data.get("translations")
                    if not text and isinstance(rows, list) and rows:
                        for row in rows:
                            if not isinstance(row, dict):
                                continue
                            row_id = str(row.get("id") or "").strip()
                            if row_id and row_id != str(item.get("id") or ""):
                                continue
                            text = _clean_chinese_translation_output(str(row.get("abstract_zh") or ""))
                            if text:
                                break
                if text and _looks_complete_chinese_translation(text, str(item.get("abstract_en") or "")):
                    item["abstract_zh"] = text
                    recovered += 1
                elif text:
                    reason = _chinese_translation_reject_reason(text, str(item.get("abstract_en") or ""))
                    log(f"articles: rejected incomplete Chinese abstract translation for {item.get('id')}: {reason}")
            translated = sum(1 for item in missing if str(item.get("abstract_zh") or "").strip())
            if retry_items:
                log(f"articles: abstract translation single retry recovered {recovered}/{len(retry_items)}")
                progress("abstract_translation_retry", len(retry_items), max(1, len(retry_items)), f"articles: single retries recovered {recovered}/{len(retry_items)}")
        missing_after = [item for item in missing if not str(item.get("abstract_zh") or "").strip()]
        visible_missing_after = [item for item in missing_after if item.get("_user_visible_recommendation") or item.get("find_recommendation")]
        status = "completed" if not missing_after else "partial"
        if missing_after:
            log(f"articles: {len(missing_after)}/{len(missing)} Chinese abstract translations still missing; final translation status remains partial until repaired")
        if visible_missing_after:
            log(f"articles: {len(visible_missing_after)} user-visible recommendation abstracts still miss Chinese translations after prioritized retries")
        log(f"articles: translated {translated}/{len(missing)} abstracts for Chinese UI; status={status}")
        progress("abstract_translation", max(1, len(prompts)), max(1, len(prompts)), f"articles: translated {translated}/{len(missing)} abstracts for Chinese UI; status={status}")
        return {"status": status, "translated": translated, "total": len(missing), "missing": len(missing_after), "missing_visible": len(visible_missing_after), "missing_ids": [str(item.get("id") or "") for item in missing_after[:50]]}
    finally:
        if hasattr(llm, "timeout_sec"):
            llm.timeout_sec = original_timeout
        if original_retries is not None and hasattr(llm, "retries"):
            llm.retries = original_retries

def _run_diagnostics(artifacts: dict) -> dict:
    evaluated = artifacts.get("evaluated_candidates") or []
    articles = artifacts.get("articles") or []
    strong = artifacts.get("strong_recommendations") or []
    read_candidates = artifacts.get("read_candidates") or []
    triage_candidates = artifacts.get("triage_candidates") or artifacts.get("audit_candidates") or []
    critique_candidates = artifacts.get("critique_candidates") or []
    statuses = artifacts.get("source_status") or []
    raw_title_index = artifacts.get("raw_title_index") or []
    retrieval_candidates = artifacts.get("retrieval_candidates") or artifacts.get("title_candidates") or []
    venue_rows = artifacts.get("venue_health_report") or []
    category_rows = artifacts.get("category_scan_report") or []
    title_rows = artifacts.get("title_filter_report") or []
    llm_scored_count = sum(1 for item in evaluated if str(item.get("reason_source") or "") == "llm abstract evaluation")
    llm_skipped_count = sum(1 for item in evaluated if item.get("llm_final_scoring_skipped"))
    llm_retry_exhausted_count = sum(1 for item in evaluated if item.get("llm_retry_exhausted"))
    abstract_fetch_failed_count = sum(1 for item in evaluated if item.get("abstract_fetch_failed"))
    local_fallback_count = sum(
        1
        for item in evaluated
        if str(item.get("reason_source") or "").lower().startswith("adaptive profile")
        and not item.get("llm_final_scoring_skipped")
        and not item.get("llm_retry_exhausted")
    )
    scoring_runtime = artifacts.get("scoring_runtime") if isinstance(artifacts.get("scoring_runtime"), dict) else {}
    failed_sources = [item for item in statuses if not item.get("ok") and not item.get("limited")]
    limited_sources = [item for item in statuses if item.get("limited")]

    def _sum(rows: list[dict], key: str) -> int:
        total = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                total += int(row.get(key) or 0)
            except (TypeError, ValueError):
                pass
        return total
    raw_title_index_count = len(raw_title_index)
    venue_corpus_count = _sum(venue_rows, "corpus_count") or _sum(venue_rows, "sample_count")
    source_raw_title_index_count = _sum(statuses, "raw_title_index_count")
    full_corpus_count = raw_title_index_count or venue_corpus_count or source_raw_title_index_count or _sum(category_rows, "total_papers")
    category_corpus_count = _sum(category_rows, "corpus_audit_papers") or _sum(category_rows, "total_papers")
    title_filter_input_count = _sum(title_rows, "title_filter_input_papers") or _sum(category_rows, "title_filter_input_papers")
    category_filtered_count = (
        _sum(title_rows, "category_filtered_papers")
        or _sum(category_rows, "title_filter_input_papers")
        or _sum(category_rows, "selected_category_papers")
        or title_filter_input_count
        or full_corpus_count
    )
    tfidf_screened_count = _sum(title_rows, "tfidf_screened_papers") or title_filter_input_count or category_filtered_count
    title_score_input_count = _sum(title_rows, "title_score_input_papers") or title_filter_input_count
    llm_title_scored_count = _sum(title_rows, "llm_title_scored_papers")
    final_title_candidates_count = _sum(title_rows, "final_title_candidates") or len(retrieval_candidates)

    survey_stats = {
        "raw_title_index_papers": full_corpus_count,
        "title_total_papers": full_corpus_count,
        "venue_total_papers_available": full_corpus_count,
        "venue_corpus_audited_papers": full_corpus_count,
        "category_corpus_audited_papers": category_corpus_count,
        "category_filtered_papers": category_filtered_count,
        "venue_category_selected_papers": _sum(category_rows, "selected_category_papers"),
        "tfidf_screened_papers": tfidf_screened_count,
        "venue_title_filter_input_papers": title_filter_input_count,
        "title_score_input_papers": title_score_input_count,
        "llm_title_scored_papers": llm_title_scored_count,
        "venue_final_title_candidates": final_title_candidates_count,
        "abstract_scored_papers": llm_scored_count,
        "venue_detail_fetched_candidates": len(evaluated),
        "llm_scored_candidates": llm_scored_count,
        "recommended_papers": len(strong),
        "abstract_fetch_failed_candidates": abstract_fetch_failed_count,
        "category_scan_reports": len(category_rows),
        "title_filter_reports": len(title_rows),
        "full_venue_corpus_audit": any(bool(row.get("full_venue_corpus_audit")) for row in category_rows if isinstance(row, dict)),
        "llm_scoring_policy": "all detail-fetched candidates are sent to the final LLM judge by default; the full venue corpus is audited before category/title/detail screening",
    }
    warnings: list[dict] = []
    if evaluated:
        fallback_ratio = round(llm_retry_exhausted_count / len(evaluated), 3)
        if llm_skipped_count:
            warnings.append({
                "code": "llm_scoring_pool_limited",
                "severity": "info",
                "message": f"LLM final scoring covered {llm_scored_count}/{len(evaluated)} candidates after category/title/detail screening; {llm_skipped_count} rows without final title+abstract evidence are retained only in machine audit artifacts and cannot enter user-visible recommendations.",
            })
        if llm_retry_exhausted_count:
            warnings.append({
                "code": "llm_scoring_fallback_failures",
                "severity": "warning",
                "message": f"{llm_retry_exhausted_count} candidates failed final LLM scoring after retries and remain excluded from user-visible Find recommendations. Inspect job logs for JSON parse errors/timeouts.",
            })
        if abstract_fetch_failed_count:
            warnings.append({
                "code": "abstract_contract_failure",
                "severity": "error",
                "message": f"{abstract_fetch_failed_count} title-filtered/detail candidates lacked a real abstract after detail enrichment and were excluded before title+abstract LLM scoring.",
            })
    else:
        fallback_ratio = 0.0
    recommendation_shortfall = int(scoring_runtime.get("recommendation_shortfall") or 0)
    recommendation_target = int(scoring_runtime.get("recommendation_target_count") or 0)
    recommendation_actual = int(scoring_runtime.get("recommendation_actual_count") or len(strong))
    if recommendation_shortfall > 0:
        warnings.append({
            "code": "recommendation_shortfall",
            "severity": "warning",
            "message": f"Only {recommendation_actual}/{recommendation_target} LLM title+abstract scored recommendations with real abstracts were available. This indicates scoring-pool or abstract-fetch coverage, not a strict-fit cutoff.",
        })
    for item in failed_sources:
        message = str(item.get("message") or "")
        severity = "error" if any(token in message.lower() for token in ["timeout", "timed out", "429", "failed", "unavailable"]) else "warning"
        warnings.append({
            "code": f"source_{item.get('source', 'unknown')}_failed",
            "severity": severity,
            "message": f"{item.get('source', 'source')} failed with count={item.get('count', 0)}: {message}",
        })
    for item in limited_sources:
        warnings.append({
            "code": f"source_{item.get('source', 'unknown')}_limited",
            "severity": "warning",
            "message": f"{item.get('source', 'source')} was rate-limited/partial with count={item.get('count', 0)}: {item.get('message', '')}",
        })
    if evaluated and not strong:
        warnings.append({
            "code": "no_strong_recommendations",
            "severity": "warning",
            "message": "No user-facing recommendations were produced, although evaluated candidates exist. Use triage_candidates/audit_candidates for stability comparison, not as recommended-reading evidence.",
        })
    if read_candidates and len(read_candidates) != len(strong):
        warnings.append({
            "code": "read_candidates_recommendation_mismatch",
            "severity": "error",
            "message": "read_candidates must mirror the user-facing recommended-paper pool. Audit rows belong in triage_candidates/audit_candidates.",
        })
    if read_candidates and any(not _candidate_is_positive_evidence(item) for item in read_candidates):
        warnings.append({
            "code": "read_candidates_include_non_recommended_items",
            "severity": "warning",
            "message": "read_candidates contains rows outside the unified Find recommendation pool; audit rows must stay in triage_candidates/audit_candidates.",
        })
    return {
        "evaluated_count": len(evaluated),
        "article_count": len(articles),
        "strong_recommendation_count": len(strong),
        "read_candidate_count": len(read_candidates),
        "triage_candidate_count": len(triage_candidates),
        "audit_candidate_count": len(triage_candidates),
        "critique_candidate_count": len(critique_candidates),
        "fallback_scored_count": llm_retry_exhausted_count,
        "llm_retry_exhausted_count": llm_retry_exhausted_count,
        "llm_scored_count": llm_scored_count,
        "llm_skipped_count": llm_skipped_count,
        "abstract_fetch_failed_count": abstract_fetch_failed_count,
        "retrieval_only_skipped_count": llm_skipped_count,
        "local_fallback_unscored_count": local_fallback_count,
        "fallback_ratio": fallback_ratio,
        "failed_source_count": len(failed_sources),
        "limited_source_count": len(limited_sources),
        "survey_stats": survey_stats,
        "warnings": warnings,
    }


def _source_status(source: str, ok: bool, count: int, message: str, limited: bool = False) -> dict:
    return {"source": source, "ok": ok, "limited": limited, "count": count, "message": message}


def _source_status_label(item: dict) -> str:
    kind = str(item.get("source_kind") or "")
    if kind == "venue":
        years = ",".join(str(year) for year in item.get("effective_years") or [])
        return f"{item.get('venue') or item.get('source') or 'venue'} {years}".strip()
    if kind == "venue_summary":
        return "Venue channels summary"
    if item.get("source") == "biorxiv":
        return "bioRxiv"
    if item.get("source") == "nature":
        return "Nature Portfolio"
    if item.get("source") == "science":
        return "Science Family"
    return str(item.get("source") or "source")

_SOURCE_STATUS_MESSAGE_SKIP_MARKERS = (
    "local venue database integrity check",
    "title corpus was verified",
    "this source does not expose abstracts",
    "no trusted official venue categories",
    "ar skips category pruning",
)


def _public_source_status_message_parts(message: object) -> list[str]:
    parts: list[str] = []
    for chunk in str(message or "").split(";"):
        text = " ".join(chunk.split()).strip()
        if not text:
            continue
        lower = text.lower()
        if any(marker in lower for marker in _SOURCE_STATUS_MESSAGE_SKIP_MARKERS):
            continue
        if re.match(r"^(adapter|years|corpus|screen_input|fetched|metadata|category)=", text, re.I):
            continue
        parts.append(text)
    return parts



def _source_status_detail_parts(item: dict) -> list[str]:
    parts: list[str] = []
    state = "limited" if item.get("limited") else ("ok" if item.get("ok") else "failed")
    parts.append(state)
    count = item.get("count")
    if count is not None:
        parts.append(f"count={count}")
    if item.get("adapter"):
        parts.append(f"adapter={item.get('adapter')}")
    if item.get("raw_title_index_count") is not None:
        parts.append(f"raw_title_index={item.get('raw_title_index_count')}")
    if item.get("candidate_count") is not None:
        parts.append(f"screen_input={item.get('candidate_count')}")
    if item.get("detail_fetched_count") is not None:
        parts.append(f"detail_fetched={item.get('detail_fetched_count')}")
    if item.get("raw_count") is not None:
        parts.append(f"raw={item.get('raw_count')}")
    if item.get("prefiltered_count") is not None:
        parts.append(f"prefiltered={item.get('prefiltered_count')}")
    if item.get("journals"):
        parts.append("journals=" + ",".join(str(v) for v in item.get("journals") or []))
    if item.get("categories"):
        parts.append("categories=" + ",".join(str(v) for v in item.get("categories") or []))
    coverage = item.get("date_coverage") if isinstance(item.get("date_coverage"), dict) else {}
    if coverage.get("oldest") or coverage.get("newest"):
        parts.append(f"dates={coverage.get('oldest') or '?'}..{coverage.get('newest') or '?'}")
    if item.get("message"):
        parts.extend(_public_source_status_message_parts(item.get("message")))
    return parts


def _status_markdown(statuses: list[dict], title: str = "Source Status") -> str:
    lines = [
        f"# {title}",
        "",
        "Each row is one real Find source or venue. `count` is the row's active retrieval/screening count; `raw_title_index`, `screen_input`, `detail_fetched`, `raw`, and `prefiltered` show the pipeline stages when available.",
        "",
    ]
    for item in statuses:
        lines.extend([
            f"## {_source_status_label(item)}",
            "",
            "- " + " / ".join(_source_status_detail_parts(item)),
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"

def _adaptive_arxiv_queries(config: AppConfig) -> list[str]:
    existing = [query for query in config.arxiv_queries if str(query).strip()]
    interest_parts = _topic_interest_chunks(config)
    generated: list[str] = []
    for part in interest_parts:
        for chunk in re.split(r"[\n;；。.!?]+", str(part)):
            query = " ".join(chunk.split()).strip()
            if 4 <= len(query) <= 120:
                generated.append(query)
    seen: set[str] = set()
    result: list[str] = []
    for query in existing + generated:
        key = str(query).strip()
        if key and key.lower() not in seen:
            seen.add(key.lower())
            result.append(key)
    return result


def run_find(
    request: FindRequest,
    log: LogFn = print,
    should_cancel: CancelFn = lambda: False,
    progress: ProgressFn = lambda *_args: None,
) -> dict:
    config = request.config or AppConfig()
    selection_payload = request.selection.model_dump()
    if config.default_find_selection != selection_payload:
        config = config.model_copy(update={"default_find_selection": selection_payload})
    run_id, run_dir = create_run_dir("find")
    write_json(run_dir / "config.json", redacted_config(config.model_dump()))
    write_json(run_dir / "selection.json", selection_payload)
    log(f"Created run {run_id}")

    llm = LLMClient(config, "find")
    llm_live = _llm_live_gate(llm)
    if llm.enabled and not llm_live.get("ok"):
        log("LLM live gate failed before Find scoring: " + str(llm_live.get("error") or llm_live.get("reason") or "unknown"))
    stage0_profile, stage0_fallback_used, stage0_error = normalize_user_profile(config, llm if llm_live.get("ok") else LLMClient(config.model_copy(update={"api_key": ""}), "find"))
    stage0_retrieval_text = profile_retrieval_text(stage0_profile)
    effective_config = config.model_copy(update={
        "research_interest": stage0_retrieval_text or config.research_interest,
        "researcher_profile": "",
    })
    stage0_result = {
        "profile": stage0_profile,
        "retrieval_text": stage0_retrieval_text,
        "fallback_used": stage0_fallback_used,
        "llm_error": stage0_error,
        "llm_live_gate": llm_live,
    }
    write_json(run_dir / "stage0_profile.json", stage0_result)
    log("Stage 0 profile normalization complete")
    if llm.enabled and not llm_live.get("ok") and _is_fatal_llm_configuration_error(llm_live.get("error") or llm_live.get("reason")):
        blocked_reason = _fatal_llm_configuration_message(llm_live.get("error") or llm_live.get("reason"), "Find live gate")
        log(blocked_reason)
        progress("blocked_llm_configuration", 0, 1, blocked_reason)
        artifacts = {
            "status": "blocked",
            "run_id": run_id,
            "blocked_reason": blocked_reason,
            "diagnostics": {"llm_live_gate": llm_live},
            "source_status": [_source_status("llm_live_gate", False, 0, blocked_reason, limited=True)],
            "articles": [],
            "strong_recommendations": [],
            "screened_ranking": [],
            "read_candidates": [],
            "critique_candidates": [],
        }
        write_json(run_dir / "find_results.json", artifacts)
        write_text(run_dir / "article.md", f"# Find blocked\n\n{blocked_reason}\n")
        write_text(run_dir / "source_status.md", _status_markdown(artifacts["source_status"], title="Source Status (blocked)"))
        return {"status": "blocked", "run_id": run_id, "artifact_dir": str(run_dir), "blocked_reason": blocked_reason, "diagnostics": {"llm_live_gate": llm_live}}
    catalog = catalog_by_id()
    venue_papers: list[dict] = []
    raw_title_index: list[dict] = []
    title_candidates: list[dict] = []
    evaluated_candidates: list[dict] = []
    source_status: list[dict] = []
    venue_health_report: list[dict] = []
    category_scan_report: list[dict] = []
    title_filter_report: list[dict] = []
    arxiv_raw_items: list[dict] = []
    arxiv_prefiltered_items: list[dict] = []
    arxiv_prefilter_report: dict = {}
    biorxiv_raw_items: list[dict] = []
    biorxiv_prefiltered_items: list[dict] = []
    biorxiv_prefilter_report: dict = {}
    nature_raw_items: list[dict] = []
    nature_prefiltered_items: list[dict] = []
    science_raw_items: list[dict] = []
    science_prefiltered_items: list[dict] = []


    def _venue_source_status_rows() -> list[dict]:
        rows: list[dict] = []
        for row in venue_health_report:
            if not isinstance(row, dict):
                continue
            source_name = row.get("venue") or row.get("venue_id") or "venue"
            count = int(row.get("candidate_count") or row.get("sample_count") or row.get("corpus_count") or 0)
            message_parts = []
            adapter_name = row.get("adapter")
            if adapter_name:
                message_parts.append(f"adapter={adapter_name}")
            effective_years_text = ",".join(str(year) for year in (row.get("effective_years") or []))
            if effective_years_text:
                message_parts.append(f"years={effective_years_text}")
            if row.get("corpus_count") is not None:
                message_parts.append("corpus=" + str(row.get("corpus_count")))
            if row.get("candidate_count") is not None:
                message_parts.append("screen_input=" + str(row.get("candidate_count")))
            if row.get("sample_count") is not None:
                message_parts.append("fetched=" + str(row.get("sample_count")))
            if row.get("metadata_completeness_status"):
                message_parts.append("metadata=" + str(row.get("metadata_completeness_status")))
            if row.get("category_status"):
                message_parts.append("category=" + str(row.get("category_status")))
            if row.get("year_fallback_reason"):
                message_parts.append(str(row.get("year_fallback_reason")))
            if row.get("error"):
                message_parts.append(str(row.get("error")))
            status = _source_status(str(source_name), bool(row.get("ok")), count, "; ".join(message_parts) or ("ok" if row.get("ok") else "No papers fetched."), limited=bool(row.get("limited") or row.get("metadata_completeness_limited")))
            status["source_kind"] = "venue"
            status["venue_id"] = row.get("venue_id")
            status["venue"] = row.get("venue") or source_name
            status["adapter"] = row.get("adapter")
            status["requested_years"] = row.get("requested_years") or []
            status["effective_years"] = row.get("effective_years") or []
            status["raw_title_index_count"] = row.get("corpus_count") or row.get("sample_count") or 0
            status["candidate_count"] = row.get("candidate_count") or row.get("sample_count") or 0
            status["metadata_completeness_status"] = row.get("metadata_completeness_status") or ""
            status["metadata_completeness_ok"] = bool(row.get("metadata_completeness_ok"))
            status["metadata_completeness_limited"] = bool(row.get("metadata_completeness_limited"))
            status["metadata_completeness_basis"] = row.get("metadata_completeness_basis") or ""
            status["title_index_completeness_status"] = row.get("title_index_completeness_status") or ""
            status["title_index_completeness_ok"] = bool(row.get("title_index_completeness_ok"))
            status["title_index_complete"] = bool(row.get("title_index_complete") or row.get("title_index_completeness_ok"))
            status["official_metadata_complete"] = bool(row.get("official_metadata_complete") or row.get("metadata_completeness_ok"))
            status["source_scope"] = row.get("source_scope") or ""
            status["source_adapter"] = row.get("source_adapter") or row.get("adapter") or ""
            status["official_title_index_verified"] = row.get("official_title_index_verified")
            status["official_accepted_list_verified"] = row.get("official_accepted_list_verified")
            status["source_verified"] = bool(row.get("source_verified"))
            status["category_status"] = row.get("category_status") or ""
            status["has_official_categories"] = bool(row.get("has_official_categories"))
            status["has_abstracts"] = bool(row.get("has_abstracts"))
            status["has_abstracts_in_title_index"] = bool(row.get("has_abstracts_in_title_index") or row.get("has_abstracts"))
            status["any_abstracts"] = bool(row.get("any_abstracts") or row.get("has_abstracts"))
            status["missing_abstract_count"] = int(row.get("missing_abstract_count") or 0)
            status["detail_fetched_count"] = sum(1 for paper in venue_papers if str(paper.get("venue") or "").lower() == str(status.get("venue") or "").lower()) or None
            rows.append(status)
        return rows

    last_progress_write: dict[str, float] = {"time": 0.0}

    def _find_progress_payload(phase: str, extra: dict | None = None) -> dict:
        def _report_sum(key: str) -> int:
            total = 0
            for row in title_filter_report:
                if not isinstance(row, dict):
                    continue
                try:
                    total += int(row.get(key) or 0)
                except (TypeError, ValueError):
                    pass
            return total

        deduped_raw = len(_dedupe_items(raw_title_index))
        deduped_titles = len(_dedupe_items(title_candidates))
        deduped_venue_papers = len(_dedupe_items(venue_papers))
        deduped_evaluated = _dedupe_items(evaluated_candidates)
        llm_scored = sum(1 for item in deduped_evaluated if isinstance(item, dict) and str(item.get("reason_source") or "") == "llm abstract evaluation")
        category_filtered = _report_sum("category_filtered_papers") or _report_sum("title_filter_input_papers") or deduped_raw
        tfidf_screened = _report_sum("tfidf_screened_papers") or category_filtered
        title_score_input = _report_sum("title_score_input_papers")
        llm_title_scored = _report_sum("llm_title_scored_papers")
        progress_payload = {
            "run_id": run_id,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "phase": phase,
            "selection": request.selection.model_dump(),
            "venue_health_report": venue_health_report,
            "source_status": _venue_source_status_rows(),
            "counts": {
                "raw_title_index": deduped_raw,
                "raw_title_index_papers": deduped_raw,
                "title_total_papers": deduped_raw,
                "category_filtered_papers": category_filtered,
                "tfidf_screened_papers": tfidf_screened,
                "title_score_input_papers": title_score_input,
                "llm_title_scored_papers": llm_title_scored,
                "title_candidates": deduped_titles,
                "venue_final_title_candidates": deduped_titles,
                "detail_fetched": deduped_venue_papers,
                "evaluated_candidates": len(deduped_evaluated),
                "abstract_scored_papers": llm_scored,
                "llm_scored_candidates": llm_scored,
                "abstract_fetch_failed_candidates": sum(1 for item in deduped_evaluated if isinstance(item, dict) and item.get("abstract_fetch_failed")),
                "final_llm_scoring_skipped_candidates": sum(1 for item in deduped_evaluated if isinstance(item, dict) and item.get("llm_final_scoring_skipped")),
            },
        }
        if extra:
            progress_payload.update(extra)
        return progress_payload

    def _persist_find_progress(phase: str, extra: dict | None = None) -> None:
        progress_payload = _find_progress_payload(phase, extra)
        write_json(run_dir / "find_progress.json", progress_payload)
        write_json(run_dir / "venue_health_report.json", {"run_id": run_id, "results": venue_health_report})
        write_json(run_dir / "category_scan_report.json", {"run_id": run_id, "results": category_scan_report})
        write_json(run_dir / "title_filter_report.json", {"run_id": run_id, "results": title_filter_report})
        status_rows = progress_payload["source_status"]
        write_text(run_dir / "source_status.md", _status_markdown(status_rows, title=f"Source Status ({phase})"))

    def _progress(phase: str, current: int, total: int, message: str) -> None:
        progress(phase, current, total, message)
        live_phases = {
            "venue_title_index", "title_prefilter", "llm_title_filter", "detail_fetch", "detail_enrichment",
            "nature_detail_enrichment", "science_detail_enrichment", "arxiv", "biorxiv",
            "nature", "science", "huggingface", "github", "abstract_scoring",
            "abstract_translation", "abstract_translation_retry", "final_ranking_prepare",
        }
        if phase in live_phases:
            now = datetime.now(timezone.utc).timestamp()
            is_done = total > 0 and current >= total
            if is_done or now - last_progress_write.get("time", 0.0) >= 10:
                last_progress_write["time"] = now
                _persist_find_progress(phase, {
                    "live_progress": {
                        "phase": phase,
                        "current": max(0, int(current or 0)),
                        "total": max(0, int(total or 0)),
                        "percent": max(0, min(100, int(round((float(current or 0) / float(total)) * 100)))) if total else 0,
                        "message": str(message or phase),
                    }
                })

    # Venue sources should be scanned fully by default. max_fetch_papers only
    # controls non-venue sources; venue_title_scan_limit is an explicit testing or
    # emergency safety cap when set to a positive value. A zero/empty value means
    # use the all-corpus venue fetch path.
    title_scan_limit = _venue_title_fetch_limit(config)
    venue_year_groups = _selection_venue_year_groups(request.selection)
    _progress("venue_title_index", 0, max(1, len(venue_year_groups)), "Starting venue title index fetch")
    for venue_index, (venue_id, requested_years) in enumerate(venue_year_groups, 1):
        _raise_if_cancelled(should_cancel)
        venue = catalog.get(venue_id)
        if not venue:
            log(f"Skipping unknown venue id: {venue_id}")
            venue_health_report.append({
                "venue_id": venue_id,
                "venue": venue_id,
                "requested_years": requested_years,
                "effective_years": [],
                "adapter": "unknown",
                "sample_count": 0,
                "ok": False,
                "error": "Unknown venue id.",
                "suggested_fix": "Add this venue to catalog/custom_venues.json or choose a supported venue id.",
            })
            _persist_find_progress("venue_title_index")
            continue
        effective_years, year_fallback_reason = _resolve_venue_years(venue, requested_years)
        if year_fallback_reason:
            log(f"{venue.get('name')}: {year_fallback_reason}")

        if venue.get("classification_source") == "official":
            log(f"Fetching official venue data for {venue.get('name')}")
            titles, adapter = _fetch_venue_title_index_for_find(venue, effective_years, title_scan_limit)
            if not titles and str(getattr(config, "provider", "")).lower() == "mock":
                titles = _mock_offline_venue_title_index(venue, effective_years, title_scan_limit or 100000)
                adapter = "mock_offline"
            metadata_audit = _online_venue_metadata_audit(titles, adapter)
            title_corpus_index = list(titles)
            online_category_reports: list[dict] = []
            if titles:
                titles, online_category_reports = _select_official_category_title_index(venue, effective_years, title_corpus_index, metadata_audit, effective_config, llm, log)
                category_scan_report.extend(online_category_reports)
            metadata_fields = _venue_metadata_status_fields(metadata_audit)
            metadata_limited = bool(metadata_fields.get("metadata_completeness_limited"))
            source_error = "" if titles else "No papers fetched."
            if titles and metadata_limited:
                source_error = str(metadata_fields.get("metadata_completeness_basis") or "Venue metadata completeness audit is partial.")
            log(f"{venue.get('name')}: fetched {len(titles)} papers via {adapter}")
            venue_health_report.append({
                "venue_id": venue_id,
                "venue": venue.get("name"),
                "requested_years": requested_years,
                "effective_years": effective_years,
                "year_fallback_reason": year_fallback_reason,
                "adapter": adapter,
                "sample_count": len(title_corpus_index),
                "candidate_count": len(titles),
                "corpus_count": len(title_corpus_index),
                "ok": bool(titles),
                "limited": metadata_limited,
                "error": source_error,
                "suggested_fix": "" if titles and not metadata_limited else ("Venue/year metadata source is partial; repair the source adapter or build a verified local database before treating it as a complete Find corpus." if titles else "Check OpenReview/DBLP venue id."),
                **metadata_fields,
            })
            _persist_find_progress("venue_title_index")
            if not titles:
                continue
            raw_title_index.extend(title_corpus_index)
            _persist_find_progress("venue_title_index")
            trusted_categories = _has_trusted_title_categories(titles, metadata_audit)
            selected_titles = _prefilter_titles(
                titles,
                effective_config,
                llm,
                venue.get("name", venue_id),
                log,
                should_cancel,
                _progress,
                dynamic_title_filter=trusted_categories,
                result_limit=_venue_recall_result_limit(config, len(titles)),
                scan_all=True,
                title_filter_reports=title_filter_report,
            )
            title_candidates.extend(selected_titles)
            detail_count = _venue_detail_fetch_count(config, len(selected_titles))
            detail_titles = selected_titles[:detail_count]
            log(f"{venue.get('name')}: title screen retained {len(selected_titles)} candidates; scoring {len(detail_titles)} detailed records")
            detailed = list(detail_titles)
            detailed, pmlr_stats = enrich_pmlr_details(detailed)
            if pmlr_stats.get("attempted"):
                log(
                    f"{venue.get('name', venue_id)}: PMLR detail enrichment filled abstracts "
                    f"{pmlr_stats.get('abstracts_filled', 0)}/{pmlr_stats.get('attempted', 0)}, "
                    f"pdfs {pmlr_stats.get('pdfs_filled', 0)}/{pmlr_stats.get('attempted', 0)}"
                )
            detailed = _enrich_missing_abstracts_for_adaptive_recall(detailed, effective_config, venue.get("name", venue_id), log, _progress, should_cancel)
            detailed = attach_quality_metadata_many(detailed)
            venue_papers.extend(detailed)
            continue

        log(f"Fetching title index for {venue.get('name')} years {effective_years}")
        _progress("venue_title_index", venue_index, len(venue_year_groups), f"Fetching title index: {venue.get('name')}")
        local_result = _load_local_category_guided_index(venue, effective_years, effective_config, llm, title_scan_limit, log)
        venue_metadata_audit: dict = {}
        if local_result:
            title_index, reports, title_corpus_index = local_result
            adapter = "local_database"
            category_scan_report.extend(reports)
            venue_metadata_audit = _combined_metadata_audit([report.get("metadata_audit") for report in reports], adapter)
        else:
            title_index, adapter = _fetch_venue_title_index_for_find(venue, effective_years, title_scan_limit)
            if not title_index and str(getattr(config, "provider", "")).lower() == "mock":
                title_index = _mock_offline_venue_title_index(venue, effective_years, title_scan_limit or 100000)
                adapter = "mock_offline"
            title_corpus_index = list(title_index)
            venue_metadata_audit = _online_venue_metadata_audit(title_corpus_index, adapter)
            online_category_reports: list[dict] = []
            if title_index:
                title_index, online_category_reports = _select_official_category_title_index(venue, effective_years, title_corpus_index, venue_metadata_audit, effective_config, llm, log)
                category_scan_report.extend(online_category_reports)
            if not title_index:
                year_fallback_reason = f"requested years {requested_years} had no usable papers via {adapter}; no fallback year was used"
                log(f"{venue.get('name')}: {year_fallback_reason}")
        metadata_fields = _venue_metadata_status_fields(venue_metadata_audit)
        metadata_limited = bool(metadata_fields.get("metadata_completeness_limited"))
        source_error = "" if title_index else "No title index found."
        if title_index and metadata_limited:
            source_error = str(metadata_fields.get("metadata_completeness_basis") or "Venue metadata completeness audit is partial.")
        venue_health_report.append({
            "venue_id": venue_id,
            "venue": venue.get("name"),
            "requested_years": requested_years,
            "effective_years": effective_years,
            "year_fallback_reason": year_fallback_reason,
            "adapter": adapter,
            "sample_count": len(title_corpus_index),
            "candidate_count": len(title_index),
            "corpus_count": len(title_corpus_index),
            "ok": bool(title_index),
            "limited": metadata_limited,
            "source_observed_date": datetime.now(timezone.utc).date().isoformat() if title_index else "",
            "release_signal_source": "source_observed_available" if title_index and not metadata_limited else ("source_observed_partial" if title_index else ""),
            "error": source_error,
            "suggested_fix": "" if title_index and not metadata_limited else ("Venue/year metadata source is partial; repair the source adapter or build a verified local database before treating it as a complete Find corpus." if title_index else "High-priority venue may need a dedicated proceedings adapter, verified selected-year cache, or an explicitly selected different year."),
            **metadata_fields,
        })
        _persist_find_progress("venue_title_index")
        if not title_index:
            log(f"{venue.get('name')}: no title index found via {adapter}")
            continue
        log(f"{venue.get('name')}: fetched {len(title_corpus_index)} corpus rows via {adapter}; {len(title_index)} rows enter category/title screening")
        raw_title_index.extend(title_corpus_index)
        _persist_find_progress("venue_title_index")
        trusted_categories = _has_trusted_title_categories(title_index, venue_metadata_audit)
        selected_titles = _prefilter_titles(
            title_index,
            effective_config,
            llm,
            venue.get("name", venue_id),
            log,
            should_cancel,
            _progress,
            dynamic_title_filter=trusted_categories,
            result_limit=_venue_recall_result_limit(config, len(title_index)),
            scan_all=adapter == "local_database",
            title_filter_reports=title_filter_report,
        )
        title_candidates.extend(selected_titles)
        _raise_if_cancelled(should_cancel)
        detail_count = _venue_detail_fetch_count(config, len(selected_titles))
        detail_titles = selected_titles[:detail_count]
        detail_wall_timeout = _venue_detail_wall_timeout_sec(str(venue.get('name') or venue_id), adapter, len(detail_titles))
        log(f"{venue.get('name')}: title screen retained {len(selected_titles)} candidates; fetching details for {len(detail_titles)}; wall_timeout={detail_wall_timeout:.0f}s")
        _progress("detail_fetch", 0, max(1, len(detail_titles)), f"{venue.get('name')}: fetching selected paper details")
        detailed = fetch_selected_venue_details(detail_titles, should_cancel=should_cancel, wall_timeout_sec=detail_wall_timeout)
        deferred_count = sum(1 for item in detailed if item.get("detail_fetch_deferred") or (isinstance(item.get("metadata"), dict) and item.get("metadata", {}).get("detail_fetch_deferred")))
        if deferred_count:
            log(f"{venue.get('name')}: detail fetch deferred {deferred_count}/{len(detail_titles)} slow/cancelled candidates; deferred candidates remain internal audit only unless later abstract enrichment succeeds.")
        _raise_if_cancelled(should_cancel)
        _progress("detail_fetch", len(detail_titles), max(1, len(detail_titles)), f"{venue.get('name')}: detail fetch complete")
        detailed, pmlr_stats = enrich_pmlr_details(detailed)
        if pmlr_stats.get("attempted"):
            log(
                f"{venue.get('name', venue_id)}: PMLR detail enrichment filled abstracts "
                f"{pmlr_stats.get('abstracts_filled', 0)}/{pmlr_stats.get('attempted', 0)}, "
                f"pdfs {pmlr_stats.get('pdfs_filled', 0)}/{pmlr_stats.get('attempted', 0)}"
            )
        detailed = _enrich_missing_abstracts_for_adaptive_recall(detailed, effective_config, venue.get("name", venue_id), log, _progress, should_cancel)
        detailed = attach_quality_metadata_many(detailed)
        venue_papers.extend(detailed)
    raw_title_index = _dedupe_items(raw_title_index)
    title_candidates = _dedupe_items(title_candidates)
    venue_papers = _dedupe_items(venue_papers)
    if venue_year_groups:
        source_status.extend(_venue_source_status_rows())
        # Do not append an anonymous aggregate venue row here. The UI and
        # source_status.md render each requested venue from venue_health_report;
        # keeping only per-source rows avoids opaque statuses like
        # "venues ok / retrieval_pool=...".
    _persist_find_progress("venue_scan_complete")

    if llm.enabled and not llm_live.get("ok"):
        source_status.append(_source_status("llm_final_scoring", False, 0, "LLM live gate failed; Find retrieval audit is available but no recommendations/strong evidence were generated. " + str(llm_live.get("error") or llm_live.get("reason") or "unknown"), limited=True))
    latest_released_venue = _latest_released_venue_context(venue_health_report)
    _attach_latest_released_venue_context(venue_papers, latest_released_venue)
    if latest_released_venue.get("venue"):
        log(
            "Latest released venue for freshness bonus: "
            f"{latest_released_venue.get('venue')} {latest_released_venue.get('year')} "
            f"released {latest_released_venue.get('release_date')} "
            f"via {latest_released_venue.get('release_signal_source') or 'known_release_date'}; other venue-years receive no freshness bonus."
        )
    else:
        log("No eligible latest released venue found for freshness bonus; no venue-year freshness bonus will be applied.")
    scoring_llm = llm if (not llm.enabled or llm_live.get("ok")) else LLMClient(config.model_copy(update={"api_key": ""}), "find")
    evaluated_candidates = _evaluate_items(venue_papers, effective_config, scoring_llm, "articles", log, should_cancel, _progress)
    _persist_find_progress("venue_llm_scoring_complete")

    if request.selection.include_nature:
        _raise_if_cancelled(should_cancel)
        log("Fetching Nature Portfolio journals: " + ", ".join(config.nature_journals))
        progress("nature", 0, 1, "Fetching Nature Portfolio")
        nature_raw_items, nature_status = fetch_nature_portfolio(
            config.nature_journals,
            config.nature_article_types,
            max_items=config.nature_candidate_limit,
            start_date=config.nature_start_date,
            end_date=config.nature_end_date,
            enrich_details=False,
        )
        source_status.append(nature_status)
        nature_prefiltered_items = _prefilter_titles(
            nature_raw_items,
            effective_config,
            llm,
            "Nature Portfolio",
            log,
            should_cancel,
            progress,
            dynamic_title_filter=False,
            result_limit=config.nature_candidate_limit,
            scan_all=True,
            title_filter_reports=title_filter_report,
        )
        title_candidates.extend(nature_prefiltered_items)
        progress("nature_detail_enrichment", 0, max(1, len(nature_prefiltered_items)), "Nature Portfolio: enriching selected article details")
        nature_detailed_items, nature_detail_stats = enrich_nature_details(nature_prefiltered_items, limit=len(nature_prefiltered_items))
        nature_status["prefiltered_count"] = len(nature_prefiltered_items)
        nature_status["detail_enrichment"] = nature_detail_stats
        progress("nature_detail_enrichment", len(nature_prefiltered_items), max(1, len(nature_prefiltered_items)), "Nature Portfolio: detail enrichment complete")
        nature_detailed_items = attach_quality_metadata_many(nature_detailed_items)
        evaluated_candidates.extend(_evaluate_items(nature_detailed_items, effective_config, llm, "nature", log, should_cancel, progress))
        progress("nature", 1, 1, "Nature Portfolio complete")

    if request.selection.include_science:
        _raise_if_cancelled(should_cancel)
        log("Fetching Science Family journals: " + ", ".join(config.science_journals))
        progress("science", 0, 1, "Fetching Science Family")
        science_raw_items, science_status = fetch_science_family(
            config.science_journals,
            config.science_article_types,
            max_items=config.science_candidate_limit,
            start_date=config.science_start_date,
            end_date=config.science_end_date,
        )
        source_status.append(science_status)
        science_prefiltered_items = _prefilter_titles(
            science_raw_items,
            effective_config,
            llm,
            "Science Family",
            log,
            should_cancel,
            progress,
            dynamic_title_filter=False,
            result_limit=config.science_candidate_limit,
            scan_all=True,
            title_filter_reports=title_filter_report,
        )
        title_candidates.extend(science_prefiltered_items)
        progress("science_detail_enrichment", 0, max(1, len(science_prefiltered_items)), "Science Family: enriching selected article details")
        science_detailed_items, science_detail_stats = enrich_science_details(science_prefiltered_items, limit=len(science_prefiltered_items))
        science_status["prefiltered_count"] = len(science_prefiltered_items)
        science_status["detail_enrichment"] = science_detail_stats
        progress("science_detail_enrichment", len(science_prefiltered_items), max(1, len(science_prefiltered_items)), "Science Family: detail enrichment complete")
        science_detailed_items = attach_quality_metadata_many(science_detailed_items)
        evaluated_candidates.extend(_evaluate_items(science_detailed_items, effective_config, llm, "science", log, should_cancel, progress))
        progress("science", 1, 1, "Science Family complete")

    if request.selection.include_arxiv:
        _raise_if_cancelled(should_cancel)
        arxiv_queries = _adaptive_arxiv_queries(config)
        arxiv_fetch_count = max(1, config.max_fetch_papers)
        log(f"Fetching arXiv categories: {', '.join(config.arxiv_categories)}; topic queries: {', '.join(arxiv_queries) if arxiv_queries else 'none'}; max={arxiv_fetch_count}")
        progress("arxiv", 0, 1, "Fetching arXiv")
        arxiv_items, arxiv_status = fetch_arxiv(
            config.arxiv_categories,
            arxiv_fetch_count,
            config.arxiv_start_date,
            config.arxiv_end_date,
            arxiv_queries,
            log=log,
            progress=progress,
            should_cancel=should_cancel,
            max_queries=config.arxiv_max_queries,
            per_query_limit=config.arxiv_per_query_limit,
            timeout_sec=config.arxiv_timeout_sec,
        )
        arxiv_raw_items = arxiv_items
        query_text = _topic_interest_text(effective_config)
        arxiv_prefiltered_items, arxiv_prefilter_report = rank_papers_tfidf(
            arxiv_items,
            query_text,
            per_category_limit=config.arxiv_llm_candidates_per_category,
            global_limit=config.arxiv_llm_candidate_limit,
        )
        arxiv_status["raw_count"] = len(arxiv_raw_items)
        arxiv_status["prefiltered_count"] = len(arxiv_prefiltered_items)
        arxiv_status["prefilter"] = arxiv_prefilter_report
        log(f"arXiv: fetched {len(arxiv_raw_items)} raw records; TF-IDF shortlisted {len(arxiv_prefiltered_items)} for LLM scoring")
        source_status.append(arxiv_status)
        raw_title_index.extend(arxiv_raw_items)
        title_candidates.extend(arxiv_prefiltered_items)
        arxiv_evaluated = _evaluate_items(arxiv_prefiltered_items, effective_config, llm, "arxiv", log, should_cancel, progress)
        evaluated_candidates.extend(arxiv_evaluated)
        progress("arxiv", 1, 1, "arXiv complete")

    if request.selection.include_biorxiv:
        _raise_if_cancelled(should_cancel)
        log("Fetching bioRxiv categories: " + ", ".join(config.biorxiv_categories))
        progress("biorxiv", 0, 1, "Fetching bioRxiv")
        biorxiv_items, biorxiv_status = fetch_biorxiv(
            config.biorxiv_categories,
            max(1, config.max_fetch_papers),
            config.biorxiv_start_date,
            config.biorxiv_end_date,
        )
        biorxiv_raw_items = biorxiv_items
        query_text = _topic_interest_text(effective_config)
        biorxiv_prefiltered_items, biorxiv_prefilter_report = rank_papers_tfidf(
            biorxiv_items,
            query_text,
            per_category_limit=config.biorxiv_llm_candidates_per_category,
            global_limit=config.biorxiv_llm_candidate_limit,
        )
        biorxiv_status["raw_count"] = len(biorxiv_raw_items)
        biorxiv_status["prefiltered_count"] = len(biorxiv_prefiltered_items)
        biorxiv_status["prefilter"] = biorxiv_prefilter_report
        log(f"bioRxiv: fetched {len(biorxiv_raw_items)} raw records; TF-IDF shortlisted {len(biorxiv_prefiltered_items)} for LLM scoring")
        source_status.append(biorxiv_status)
        raw_title_index.extend(biorxiv_raw_items)
        title_candidates.extend(biorxiv_prefiltered_items)
        evaluated_candidates.extend(_evaluate_items(biorxiv_prefiltered_items, effective_config, llm, "biorxiv", log, should_cancel, progress))
        progress("biorxiv", 1, 1, "bioRxiv complete")

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
        hf_items = _evaluate_items(hf_papers + hf_models, effective_config, llm, "huggingface", log, should_cancel, progress)[: config.max_recommended_papers]
        evaluated_candidates.extend(hf_items)
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
            effective_config,
            llm,
            "github",
            log,
            should_cancel,
            progress,
        )[: config.max_recommended_papers]
        evaluated_candidates.extend(github_items)
        progress("github", 1, 1, "GitHub complete")

    _raise_if_cancelled(should_cancel)
    raw_title_index = _dedupe_items(raw_title_index)
    title_candidates = _dedupe_items(title_candidates)
    evaluated_candidates = _dedupe_items(evaluated_candidates)
    raw_title_index = _retrieval_only_pool(raw_title_index, evaluated_candidates)
    title_candidates = _retrieval_only_pool(title_candidates, evaluated_candidates)
    source_count = _selection_source_count(request.selection)
    article_items = _recommended(evaluated_candidates, config, source_count=source_count)
    strong_recommendations = article_items
    # Read must consume the exact user-visible recommendation pool, not a second
    # recomputed copy. This keeps translated abstracts, human edits, and ranking
    # contract aligned for article.md, read_candidates.md, and Read API input.
    read_candidates = article_items
    triage_candidates = _triage_candidates(evaluated_candidates, config)
    critique_candidates = _critique_candidates(evaluated_candidates, config)
    for item in article_items:
        item["_user_visible_recommendation"] = True
    for item in read_candidates:
        item["_user_visible_recommendation"] = True
    def _build_find_artifacts(translation_status: str) -> dict:
        recommendation_target = _strong_recommendation_target_count(config, source_count)
        recommendation_shortfall = max(0, recommendation_target - len(strong_recommendations))
        recommendation_quality = _recommendation_quality_audit(strong_recommendations)
        artifacts = {
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "scoring_policy_version": SCORING_POLICY_VERSION,
            "selection": request.selection.model_dump(),
            "stage0_profile": stage0_result,
            "recommendation_quality": recommendation_quality,
            "recommendation_target_count": recommendation_target,
            "recommendation_actual_count": len(strong_recommendations),
            "strict_strong_anchor_count": len(strong_recommendations),
            "strong_recommendation_count": len(strong_recommendations),
            "recommendation_shortfall": recommendation_shortfall,
            "recommendation_policy": FIND_RECOMMENDATION_POLICY,
            "abstract_translation_status": translation_status,
            "raw_title_index": raw_title_index,
            "retrieval_candidates": title_candidates,
            "title_candidates": title_candidates,
            "evaluated_candidates": evaluated_candidates,
            "detail_fetched": list(venue_papers),
            "screened_ranking": _screened_ranking(evaluated_candidates, config),
            "strong_recommendations": strong_recommendations,
            "read_candidates": read_candidates,
            "triage_candidates": triage_candidates,
            "audit_candidates": triage_candidates,
            "critique_candidates": critique_candidates,
            "articles": article_items,
            "artifact_semantics": {
                "articles": "Backward-compatible alias for strong_recommendations and the source of article.md; contains the top final LLM title+abstract recommendations with real abstracts.",
                "strong_recommendations": "User-facing recommended papers: real-abstract rows ranked by final title+abstract LLM fit/recommendation score.",
                "screened_ranking": "Uncapped final-LLM recommendation ranking for inspection; article.md/read_candidates take the top configured rows from the same list.",
                "read_candidates": "Deep-reading input and backward-compatible alias for the user-facing recommended-paper pool. Recommendation count must equal read_candidates count.",
                "triage_candidates": "Machine inspection pool for near-threshold, failed, or contrast rows. It is not shown as recommended reading.",
                "audit_candidates": "Alias for triage_candidates retained for audit tooling; never the user-facing deep-reading count.",
                "critique_candidates": "Weak or boundary candidates retained for contrast, bad-case analysis, and search expansion; not recommended reading.",
            },
            "scoring_runtime": {
                "recommendation_quality": recommendation_quality,
                "find_final_scoring_temperature": FIND_FINAL_SCORING_TEMPERATURE,
                "ranking_score_policy": STABLE_RANKING_SCORE_POLICY,
                "source_context_bonus_policy": SOURCE_CONTEXT_BONUS_POLICY,
                "latest_released_venue": latest_released_venue,
                "strong_recommendation_source_count": source_count,
                "recommendation_target_count": recommendation_target,
                "recommendation_actual_count": len(strong_recommendations),
                "strict_strong_anchor_count": len(strong_recommendations),
                "recommendation_shortfall": recommendation_shortfall,
                "recommendation_policy": FIND_RECOMMENDATION_POLICY,
                "strong_recommendation_max_count": _strong_recommendation_output_count(config, source_count),
                "final_llm_scoring_limit": _final_llm_scoring_limit(config, len(evaluated_candidates)),
                "final_llm_scoring_skipped_count": sum(1 for item in evaluated_candidates if item.get("llm_final_scoring_skipped")),
                "llm": llm.summary(),
                "abstract_translation_status": translation_status,
                "llm_live_gate": llm_live,
                "llm_final_scoring_available": bool((not llm.enabled) or llm_live.get("ok")),
            },
            "huggingface": hf_items,
            "github": github_items,
            "source_status": source_status,
            "venue_health_report": venue_health_report,
            "category_scan_report": category_scan_report,
            "title_filter_report": title_filter_report,
            "arxiv_raw": arxiv_raw_items,
            "arxiv_prefiltered": arxiv_prefiltered_items,
            "arxiv_prefilter_report": arxiv_prefilter_report,
            "biorxiv_raw": biorxiv_raw_items,
            "biorxiv_prefiltered": biorxiv_prefiltered_items,
            "biorxiv_prefilter_report": biorxiv_prefilter_report,
            "nature_raw": nature_raw_items,
            "nature_prefiltered": nature_prefiltered_items,
            "science_raw": science_raw_items,
            "science_prefiltered": science_prefiltered_items,
        }
        artifacts["diagnostics"] = _run_diagnostics(artifacts)
        artifacts["recommendation_quality"] = recommendation_quality
        artifacts["diagnostics"]["recommendation_quality"] = recommendation_quality
        if llm.enabled and not llm_live.get("ok"):
            artifacts["status"] = "blocked"
            artifacts["blocked_reason"] = "LLM live gate failed; retrieval audit completed but final LLM scoring/recommendations are invalid."
            artifacts["diagnostics"].setdefault("warnings", []).append({
                "code": "llm_live_gate_failed",
                "severity": "error",
                "message": str(llm_live.get("error") or llm_live.get("reason") or "unknown"),
            })
        artifacts["survey_stats"] = artifacts["diagnostics"].get("survey_stats", {})
        return artifacts

    def _write_find_outputs(artifacts: dict) -> None:
        write_json(run_dir / "find_results.json", artifacts)
        write_json(run_dir / "venue_health_report.json", {"run_id": run_id, "results": venue_health_report})
        write_json(run_dir / "category_scan_report.json", {"run_id": run_id, "results": category_scan_report})
        write_json(run_dir / "title_filter_report.json", {"run_id": run_id, "results": title_filter_report})
        write_json(run_dir / "arxiv_raw.json", {"run_id": run_id, "results": arxiv_raw_items})
        write_json(run_dir / "arxiv_prefiltered.json", {"run_id": run_id, "results": arxiv_prefiltered_items, "report": arxiv_prefilter_report})
        write_json(run_dir / "biorxiv_raw.json", {"run_id": run_id, "results": biorxiv_raw_items})
        write_json(run_dir / "biorxiv_prefiltered.json", {"run_id": run_id, "results": biorxiv_prefiltered_items, "report": biorxiv_prefilter_report})
        write_json(run_dir / "nature_raw.json", {"run_id": run_id, "results": nature_raw_items})
        write_json(run_dir / "nature_prefiltered.json", {"run_id": run_id, "results": nature_prefiltered_items})
        write_json(run_dir / "science_raw.json", {"run_id": run_id, "results": science_raw_items})
        write_json(run_dir / "science_prefiltered.json", {"run_id": run_id, "results": science_prefiltered_items})

        article_md = paper_markdown(article_items, "Recommended Articles")
        screened_md = paper_markdown(artifacts.get("screened_ranking") or [], "Screened Strong Ranking")
        read_candidates_md = paper_markdown(read_candidates, "Read Candidates")
        triage_candidates_md = paper_markdown(triage_candidates, "Triage Candidates")
        audit_candidates_md = paper_markdown(triage_candidates, "Audit Candidates")
        critique_md = paper_markdown(critique_candidates, "Critique Candidates")
        biorxiv_md = paper_markdown(biorxiv_prefiltered_items, "bioRxiv Articles")
        nature_md = paper_markdown(nature_prefiltered_items, "Nature Portfolio Articles")
        science_md = paper_markdown(science_prefiltered_items, "Science Family Articles")
        hf_md = paper_markdown(hf_items, "HuggingFace Papers and Models")
        github_md = paper_markdown(github_items, "GitHub Trending Repositories")
        status_md = _status_markdown(source_status)
        write_text(run_dir / "article.md", article_md)
        write_text(run_dir / "screened_ranking.md", screened_md)
        write_text(run_dir / "read_candidates.md", read_candidates_md)
        write_text(run_dir / "triage_candidates.md", triage_candidates_md)
        write_text(run_dir / "audit_candidates.md", audit_candidates_md)
        write_text(run_dir / "critique_candidates.md", critique_md)
        write_text(run_dir / "biorxiv.md", biorxiv_md)
        write_text(run_dir / "nature.md", nature_md)
        write_text(run_dir / "science.md", science_md)
        write_text(run_dir / "hf.md", hf_md)
        write_text(run_dir / "github.md", github_md)
        write_text(run_dir / "source_status.md", status_md)
        sync_latest("auto_find", "article.md", run_dir / "article.md")
        sync_latest("auto_find", "screened_ranking.md", run_dir / "screened_ranking.md")
        sync_latest("auto_find", "read_candidates.md", run_dir / "read_candidates.md")
        sync_latest("auto_find", "triage_candidates.md", run_dir / "triage_candidates.md")
        sync_latest("auto_find", "audit_candidates.md", run_dir / "audit_candidates.md")
        sync_latest("auto_find", "critique_candidates.md", run_dir / "critique_candidates.md")
        sync_latest("auto_find", "biorxiv.md", run_dir / "biorxiv.md")
        sync_latest("auto_find", "nature.md", run_dir / "nature.md")
        sync_latest("auto_find", "science.md", run_dir / "science.md")
        sync_latest("auto_find", "hf.md", run_dir / "hf.md")
        sync_latest("auto_find", "github.md", run_dir / "github.md")
        sync_latest("auto_find", "source_status.md", run_dir / "source_status.md")
        update_manifest(run_dir, "find")

    # Persist real Find evidence and human-readable artifacts before Chinese UI
    # translation. The final packet below recomputes translation status from the
    # actual recommendation rows before it can be marked complete.
    preliminary_artifacts = _build_find_artifacts("pending")
    _write_find_outputs(preliminary_artifacts)
    _persist_find_progress("preliminary_artifacts_written", {"abstract_translation_status": "pending", "strong_recommendation_count": len(strong_recommendations), "strict_strong_anchor_count": len(strong_recommendations), "recommendation_target_count": _strong_recommendation_target_count(config, source_count), "recommendation_shortfall": max(0, _strong_recommendation_target_count(config, source_count) - len(strong_recommendations)), "recommendation_policy": FIND_RECOMMENDATION_POLICY})
    log("Find stage scored candidates; preliminary artifacts persisted before Chinese abstract translation")

    translation_status = "completed"
    try:
        translation_result = _attach_abstract_language_fields(
            article_items,
            llm,
            log,
            should_cancel,
            _progress,
        )
        if isinstance(translation_result, dict):
            translation_status = str(translation_result.get("status") or translation_status)
    except Exception as exc:
        translation_status = "pending"
        log(f"articles: abstract translation failed; final translation status will be recomputed from recommendation rows: {str(exc)[:240]}")

    translation_status = _recommendation_translation_status(article_items, translation_status)
    missing_translation_ids = _missing_chinese_abstract_ids(article_items)
    if missing_translation_ids:
        log(f"articles: final Find packet has {len(missing_translation_ids)} recommendation abstracts without Chinese translation; marking abstract_translation_status=partial")

    artifacts = _build_find_artifacts(translation_status)
    _write_find_outputs(artifacts)
    _persist_find_progress("complete", {"abstract_translation_status": translation_status, "strong_recommendation_count": len(strong_recommendations), "strict_strong_anchor_count": len(strong_recommendations), "recommendation_target_count": _strong_recommendation_target_count(config, source_count), "recommendation_shortfall": max(0, _strong_recommendation_target_count(config, source_count) - len(strong_recommendations)), "recommendation_policy": FIND_RECOMMENDATION_POLICY})
    progress("complete", 1, 1, "find complete")
    log("Find stage complete")
    return artifacts
