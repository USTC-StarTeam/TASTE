from __future__ import annotations

import json
import re
from typing import Any

from auto_research.llm import LLMClient
from auto_research.models import AppConfig


PROFILE_SCHEMA: dict[str, Any] = {
    "explicit_profile": {
        "research_interest_summary": "",
        "researcher_background": None,
    },
    "explicit_retrieval_signals": {
        "core_concepts": [],
        "method_terms": [],
        "application_terms": [],
        "domain_terms": [],
        "excluded_terms": [],
    },
    "safe_expansions": {
        "synonyms_or_abbreviations": [
            {
                "term": "",
                "source_term": "",
                "expansion_type": "synonym",
                "reason": "",
            }
        ],
    },
    "filtering_hints": {
        "hard_exclusions": [],
        "conditional_exclusions": [
            {
                "terms": [],
                "condition": "",
            }
        ],
        "soft_penalties": [],
        "must_keep_if_present": [],
        "preference_hints": [],
    },
    "uncertainty": {
        "ambiguous_terms": [],
        "needs_clarification": False,
    },
}


def _as_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_optional_string(value: Any) -> str | None:
    text = _as_string(value)
    return text or None


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _as_string(item)
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _normalize_expansions(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        term = _as_string(item.get("term"))
        source_term = _as_string(item.get("source_term"))
        if not term or not source_term:
            continue
        expansion_type = _as_string(item.get("expansion_type")) or "synonym"
        if expansion_type not in {"synonym", "abbreviation", "closely_related"}:
            expansion_type = "closely_related"
        reason = _as_string(item.get("reason")) or f"Expansion provided for explicit source term: {source_term}."
        key = (term.lower(), source_term.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "term": term,
            "source_term": source_term,
            "expansion_type": expansion_type,
            "reason": reason,
        })
    return result


def _normalize_conditional_exclusions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for item in value:
        if isinstance(item, str):
            terms = [item]
            condition = item
        elif isinstance(item, dict):
            terms = _as_string_list(item.get("terms"))
            condition = _as_string(item.get("condition"))
        else:
            continue
        if not terms or not condition:
            continue
        key = (tuple(term.lower() for term in terms), condition.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append({"terms": terms, "condition": condition})
    return result


def _canonical_condition(value: str) -> str:
    text = _as_string(value).lower()
    text = re.sub(r"^unless\s+(?:they\s+)?directly support\s+", "exclude only if they do not directly support ", text)
    text = re.sub(r"^unless\s+(?:it\s+)?directly supports\s+", "exclude only if they do not directly support ", text)
    text = re.sub(r"^unless\s+", "exclude only if the exception is not met: ", text)
    text = text.replace("do not directly supports", "do not directly support")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .")


def _dedupe_conditional_exclusions(value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for item in value:
        terms = _as_string_list(item.get("terms"))
        condition = _as_string(item.get("condition"))
        if not terms or not condition:
            continue
        canonical_terms = tuple(sorted(term.lower() for term in terms))
        canonical_condition = _canonical_condition(condition)
        key = (canonical_terms, canonical_condition)
        if key in seen:
            continue
        seen.add(key)
        if condition.lower().startswith("unless "):
            condition = canonical_condition
        result.append({"terms": terms, "condition": condition})
    return result


def normalize_profile_shape(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    explicit_profile = data.get("explicit_profile") if isinstance(data.get("explicit_profile"), dict) else {}
    explicit_signals = data.get("explicit_retrieval_signals") if isinstance(data.get("explicit_retrieval_signals"), dict) else {}
    safe_expansions = data.get("safe_expansions") if isinstance(data.get("safe_expansions"), dict) else {}
    filtering_hints = data.get("filtering_hints") if isinstance(data.get("filtering_hints"), dict) else {}
    uncertainty = data.get("uncertainty") if isinstance(data.get("uncertainty"), dict) else {}

    return {
        "explicit_profile": {
            "research_interest_summary": _as_string(explicit_profile.get("research_interest_summary")),
            "researcher_background": _as_optional_string(explicit_profile.get("researcher_background")),
        },
        "explicit_retrieval_signals": {
            "core_concepts": _as_string_list(explicit_signals.get("core_concepts")),
            "method_terms": _as_string_list(explicit_signals.get("method_terms")),
            "application_terms": _as_string_list(explicit_signals.get("application_terms")),
            "domain_terms": _as_string_list(explicit_signals.get("domain_terms")),
            "excluded_terms": _as_string_list(explicit_signals.get("excluded_terms")),
        },
        "safe_expansions": {
            "synonyms_or_abbreviations": _normalize_expansions(safe_expansions.get("synonyms_or_abbreviations")),
        },
        "filtering_hints": {
            "hard_exclusions": _as_string_list(filtering_hints.get("hard_exclusions")),
            "conditional_exclusions": _normalize_conditional_exclusions(filtering_hints.get("conditional_exclusions")),
            "soft_penalties": _as_string_list(filtering_hints.get("soft_penalties")),
            "must_keep_if_present": _as_string_list(filtering_hints.get("must_keep_if_present")),
            "preference_hints": _as_string_list(filtering_hints.get("preference_hints")),
        },
        "uncertainty": {
            "ambiguous_terms": _as_string_list(uncertainty.get("ambiguous_terms")),
            "needs_clarification": bool(uncertainty.get("needs_clarification")),
        },
    }


def _keyword_terms(text: str) -> list[str]:
    terms = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9][a-zA-Z0-9_.-]{2,}", text or "")
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        cleaned = term.strip(".,;:!?()[]{}\"'")
        if cleaned and cleaned.lower() not in seen:
            result.append(cleaned)
            seen.add(cleaned.lower())
    return result


def _split_terms(text: str) -> list[str]:
    cleaned = re.sub(r"\b(or|and)\b", ",", text, flags=re.IGNORECASE)
    return [
        term.strip(" .;:")
        for term in cleaned.split(",")
        if term.strip(" .;:")
    ]


def _append_unique(items: list[str], value: str) -> None:
    text = _as_string(value)
    if text and text not in items:
        items.append(text)


def _append_expansion(expansions: list[dict[str, str]], term: str, source_term: str, expansion_type: str, reason: str) -> None:
    if not term or not source_term:
        return
    key = (term.lower(), source_term.lower())
    if any((item["term"].lower(), item["source_term"].lower()) == key for item in expansions):
        return
    expansions.append({
        "term": term,
        "source_term": source_term,
        "expansion_type": expansion_type,
        "reason": reason,
    })


def _extract_conditional_exclusions(raw_text: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for match in re.finditer(r"\b(?:avoid|exclude|skip|filter out)\s+(.+?)\s+unless\s+(.+?)(?:[.;\n]|$)", raw_text, flags=re.IGNORECASE):
        terms = _split_terms(match.group(1))
        unless_clause = _as_string(match.group(2))
        lowered = unless_clause.lower()
        if lowered.startswith(("it directly supports ", "they directly support ", "directly supports ", "directly support ")):
            target = re.sub(r"^(it\s+|they\s+)?directly supports?\s+", "", unless_clause, flags=re.IGNORECASE)
            condition = f"exclude only if they do not directly support {target}"
        elif lowered.startswith(("it is directly related to ", "they are directly related to ", "directly related to ")):
            target = re.sub(r"^(it is\s+|they are\s+)?directly related to\s+", "", unless_clause, flags=re.IGNORECASE)
            condition = f"exclude only if they are not directly related to {target}"
        else:
            condition = f"exclude only if the exception is not met: {unless_clause}"
        if terms and condition:
            results.append({"terms": terms, "condition": condition})
    return results


def _extract_preference_hints(raw_text: str) -> list[str]:
    hints: list[str] = []
    for match in re.finditer(r"\bprefer\s+(.+?)(?:[.;\n]|$)", raw_text, flags=re.IGNORECASE):
        for term in _split_terms(match.group(1)):
            _append_unique(hints, f"prefer {term}")
    return hints


def _augment_safe_expansions(profile: dict[str, Any], raw_text: str) -> None:
    explicit_profile = profile["explicit_profile"]
    explicit_signals = profile["explicit_retrieval_signals"]
    source_text = " ".join([
        raw_text,
        explicit_profile.get("research_interest_summary") or "",
        " ".join(explicit_signals.get("core_concepts") or []),
        " ".join(explicit_signals.get("method_terms") or []),
        " ".join(explicit_signals.get("application_terms") or []),
        " ".join(explicit_signals.get("domain_terms") or []),
    ]).lower()
    expansions = profile["safe_expansions"]["synonyms_or_abbreviations"]
    rules = [
        (
            "paper discovery",
            "paper recommendation",
            "closely_related",
            "Common retrieval wording for systems that surface relevant papers.",
        ),
        (
            "literature review automation",
            "automated literature review",
            "synonym",
            "Equivalent wording often used in paper titles and abstracts.",
        ),
        (
            "academic research automation",
            "research assistant agent",
            "closely_related",
            "Common term for agent systems that assist research workflows.",
        ),
        (
            "academic research automation",
            "AI scientist",
            "closely_related",
            "Common term for systems that automate parts of scientific research.",
        ),
        (
            "retrieval augmented generation",
            "RAG",
            "abbreviation",
            "Standard abbreviation for retrieval augmented generation.",
        ),
        (
            "large language model",
            "LLM",
            "abbreviation",
            "Standard abbreviation for large language model.",
        ),
    ]
    for source_term, term, expansion_type, reason in rules:
        if source_term in source_text:
            _append_expansion(expansions, term, source_term, expansion_type, reason)


def _postprocess_profile(profile: dict[str, Any], config: AppConfig) -> dict[str, Any]:
    raw_text = "\n".join(part for part in [config.research_interest, config.researcher_profile] if part)
    filtering_hints = profile["filtering_hints"]
    conditional_exclusions = filtering_hints["conditional_exclusions"]

    for exclusion in _extract_conditional_exclusions(raw_text):
        if exclusion not in conditional_exclusions:
            conditional_exclusions.append(exclusion)
    filtering_hints["conditional_exclusions"] = _dedupe_conditional_exclusions(conditional_exclusions)

    conditional_terms = {term.lower() for item in filtering_hints["conditional_exclusions"] for term in item.get("terms", [])}
    filtering_hints["hard_exclusions"] = [
        term for term in filtering_hints["hard_exclusions"]
        if " unless " not in term.lower() and term.lower() not in conditional_terms
    ]
    profile["explicit_retrieval_signals"]["excluded_terms"] = [
        term for term in profile["explicit_retrieval_signals"]["excluded_terms"]
        if " unless " not in term.lower() and term.lower() not in conditional_terms
    ]

    for hint in _extract_preference_hints(raw_text):
        _append_unique(filtering_hints["preference_hints"], hint)

    preference_terms = {
        term.removeprefix("prefer ").lower()
        for term in filtering_hints["preference_hints"]
    }
    profile["uncertainty"]["ambiguous_terms"] = [
        term for term in profile["uncertainty"]["ambiguous_terms"]
        if term.lower() not in preference_terms
    ]

    if profile_retrieval_text(profile):
        profile["uncertainty"]["needs_clarification"] = False

    _augment_safe_expansions(profile, raw_text)
    return profile


def fallback_profile(config: AppConfig) -> dict[str, Any]:
    interest = _as_string(config.research_interest)
    background = _as_optional_string(config.researcher_profile)
    terms = _keyword_terms(f"{interest}\n{background or ''}")[:24]
    profile = normalize_profile_shape({
        "explicit_profile": {
            "research_interest_summary": interest,
            "researcher_background": background,
        },
        "explicit_retrieval_signals": {
            "core_concepts": terms,
            "method_terms": [],
            "application_terms": [],
            "domain_terms": [],
            "excluded_terms": [],
        },
        "safe_expansions": {"synonyms_or_abbreviations": []},
        "filtering_hints": {
            "hard_exclusions": [],
            "conditional_exclusions": [],
            "soft_penalties": [],
            "must_keep_if_present": [],
            "preference_hints": [],
        },
        "uncertainty": {
            "ambiguous_terms": [],
            "needs_clarification": not bool(interest or background),
        },
    })
    return _postprocess_profile(profile, config)


def build_stage0_prompt(config: AppConfig) -> str:
    schema = json.dumps(PROFILE_SCHEMA, ensure_ascii=False, indent=2)
    return f"""
Your task is to convert the user's free-text research interest and researcher profile into a structured JSON profile for downstream paper retrieval and filtering.

Important rules:
- Extract only research-relevant information.
- Do not invent research interests.
- Do not recommend papers.
- Do not choose conferences, tracks, fields, or arXiv categories.
- Do not invent ranking or filtering preferences.
- Do not polish or rewrite the user's intent beyond concise normalization.
- Preserve uncertainty explicitly.
- Separate explicit user statements from safe retrieval expansions.
- Safe expansions must be conservative: direct synonyms, standard abbreviations, or clearly adjacent retrieval terms only.
- Every safe expansion must include term, source_term, expansion_type, and reason. The source_term must come from explicit user input.
- Preserve conditional exclusions. For "avoid X unless Y", do not put X in hard_exclusions; put it in conditional_exclusions with the condition.
- Use soft_penalties for disliked topics that may still be useful if strongly relevant.
- Use preference_hints for ranking/filtering preferences such as practical systems, reproducible pipelines, or lightweight experiments.
- Do not put preferences in ambiguous_terms unless the system genuinely cannot interpret them for retrieval or ranking.
- Set needs_clarification=true only when missing or ambiguous information would block retrieval.
- If a field is missing, use an empty list or null.
- Output valid JSON only.

User input:

[INTEREST]
{config.research_interest}
[/INTEREST]

[RESEARCHER_PROFILE]
{config.researcher_profile}
[/RESEARCHER_PROFILE]

Return JSON with exactly this schema:
{schema}
""".strip()


def _profile_signal_count(profile: dict[str, Any]) -> int:
    signals = profile.get("explicit_retrieval_signals", {}) if isinstance(profile, dict) else {}
    expansions = profile.get("safe_expansions", {}) if isinstance(profile, dict) else {}
    count = 0
    for key in ["core_concepts", "method_terms", "application_terms", "domain_terms"]:
        count += len(_as_string_list(signals.get(key)))
    count += len(_normalize_expansions(expansions.get("synonyms_or_abbreviations")))
    return count


def normalize_user_profile(config: AppConfig, llm: LLMClient) -> tuple[dict[str, Any], bool, str]:
    if not (config.research_interest or config.researcher_profile):
        return fallback_profile(config), True, ""
    if not llm.enabled:
        return fallback_profile(config), True, "LLM is not configured; used deterministic fallback."
    data = llm.json_or_none(build_stage0_prompt(config))
    if data is None:
        return fallback_profile(config), True, "LLM did not return valid JSON; used deterministic fallback."
    profile = _postprocess_profile(normalize_profile_shape(data), config)
    retrieval_text = profile_retrieval_text(profile)
    if not retrieval_text:
        return fallback_profile(config), True, "LLM returned an empty profile; used deterministic fallback."
    raw_text = "\n".join(part for part in [config.research_interest, config.researcher_profile] if part)
    raw_terms = _keyword_terms(raw_text)
    if len(raw_terms) >= 8 and _profile_signal_count(profile) < 4:
        return fallback_profile(config), True, "LLM profile was too sparse for the provided research interest; used deterministic fallback."
    return profile, False, ""


def profile_retrieval_text(profile: dict[str, Any]) -> str:
    explicit_profile = profile.get("explicit_profile", {})
    explicit_signals = profile.get("explicit_retrieval_signals", {})
    safe_expansions = profile.get("safe_expansions", {})
    filtering_hints = profile.get("filtering_hints", {})
    parts: list[str] = []
    for value in [
        explicit_profile.get("research_interest_summary"),
        explicit_profile.get("researcher_background"),
    ]:
        text = _as_string(value)
        if text:
            parts.append(text)
    for key in ["core_concepts", "method_terms", "application_terms", "domain_terms"]:
        parts.extend(_as_string_list(explicit_signals.get(key)))
    parts.extend(item["term"] for item in _normalize_expansions(safe_expansions.get("synonyms_or_abbreviations")))
    hard_exclusions = _as_string_list(filtering_hints.get("hard_exclusions"))
    conditional_terms = {
        term.lower()
        for item in _normalize_conditional_exclusions(filtering_hints.get("conditional_exclusions"))
        for term in item.get("terms", [])
    }
    excluded_terms = [
        term for term in _as_string_list(explicit_signals.get("excluded_terms"))
        if term.lower() not in conditional_terms
    ]
    if hard_exclusions or excluded_terms:
        exclusions = list(dict.fromkeys([*excluded_terms, *hard_exclusions]))
        parts.append("Excluded topics: " + ", ".join(exclusions))
    for item in _normalize_conditional_exclusions(filtering_hints.get("conditional_exclusions")):
        parts.append(f"Conditional exclusion: reject {', '.join(item['terms'])} {item['condition']}.")
    soft_penalties = _as_string_list(filtering_hints.get("soft_penalties"))
    if soft_penalties:
        parts.append("Soft penalties: " + ", ".join(soft_penalties))
    preference_hints = _as_string_list(filtering_hints.get("preference_hints"))
    if preference_hints:
        parts.append("Preference hints: " + ", ".join(preference_hints))
    return "\n".join(dict.fromkeys(part for part in parts if part))
