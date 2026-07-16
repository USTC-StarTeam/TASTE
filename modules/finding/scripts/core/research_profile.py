from __future__ import annotations


# ---- profile normalization ----

# ---- profile_normalize.py ----

import json
import re
from typing import Any

from finding_runtime import LLMClient
from finding_runtime import AppConfig


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


def _config_research_text(config: AppConfig) -> str:
    parts = [config.research_topic, config.research_interest, config.researcher_profile]
    return "\n".join(part for part in parts if part).strip()


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
    raw_text = _config_research_text(config)
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
    interest = _as_string(config.research_topic or config.research_interest)
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
- For every non-English application term and domain term, include at least one
  complete English safe expansion that can stand alone as a literature-search
  phrase. Preserve the full domain/object/task qualifiers from the explicit
  term, and use that complete explicit term as source_term rather than a
  context-free substring.
- Preserve conditional exclusions. For "avoid X unless Y", do not put X in hard_exclusions; put it in conditional_exclusions with the condition.
- Use soft_penalties for disliked topics that may still be useful if strongly relevant.
- Use preference_hints for ranking/filtering preferences such as practical systems, reproducible pipelines, or lightweight experiments.
- Do not put preferences in ambiguous_terms unless the system genuinely cannot interpret them for retrieval or ranking.
- Set needs_clarification=true only when missing or ambiguous information would block retrieval.
- If a field is missing, use an empty list or null.
- Output valid JSON only.

User input:

[TOPIC]
{config.research_topic}
[/TOPIC]

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
    if not (config.research_topic or config.research_interest or config.researcher_profile):
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
    raw_text = _config_research_text(config)
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
    summary = _as_string(explicit_profile.get("research_interest_summary"))
    if summary:
        parts.append("Core topic route: " + summary)
    background = _as_string(explicit_profile.get("researcher_background"))
    if background:
        parts.append("Researcher background: " + background)
    core_concepts = _as_string_list(explicit_signals.get("core_concepts"))
    if core_concepts:
        parts.append("Core concept terms: " + ", ".join(core_concepts))
    method_terms = _as_string_list(explicit_signals.get("method_terms"))
    if method_terms:
        parts.append("Retrieval method terms: " + ", ".join(method_terms))
    application_terms = _as_string_list(explicit_signals.get("application_terms"))
    if application_terms:
        parts.append("Retrieval application terms: " + ", ".join(application_terms))
    domain_terms = _as_string_list(explicit_signals.get("domain_terms"))
    if domain_terms:
        parts.append("Retrieval domain terms: " + ", ".join(domain_terms))
    expansions = [item["term"] for item in _normalize_expansions(safe_expansions.get("synonyms_or_abbreviations"))]
    if expansions:
        parts.append("Retrieval expansions: " + ", ".join(expansions))
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


# ---- search terms ----

"""Keyword-targeted search-term extraction for preprint/journal crawling.

This module turns a (possibly non-English) research topic, interest, and
researcher profile into a flat set of ENGLISH retrieval keywords used to build
targeted queries for arXiv / bioRxiv / OpenAlex / Crossref.

It is deliberately self-contained (no import of find_pipeline) so the four
source fetchers and the local ranker can share one canonical query model.

Output shape (``SearchTerms``-like dict)::

    {
      "search_keywords": [..], # equal-status English terms, ORed independently
      "arxiv_categories":[..], # explicit configured categories only
      "biorxiv_categories":[..], # explicit configured subjects only
      "source":          "llm" | "fallback",
    }
"""


import re
from typing import Any

from finding_runtime import LLMClient
from finding_runtime import AppConfig


SEARCH_TERMS_PROMPT_VERSION = "search_terms_flat_equal_v1"

_ARXIV_CATEGORY_CODES = {
    "astro-ph.CO", "astro-ph.EP", "astro-ph.GA", "astro-ph.HE", "astro-ph.IM", "astro-ph.SR",
    "cond-mat.dis-nn", "cond-mat.mes-hall", "cond-mat.mtrl-sci", "cond-mat.other",
    "cond-mat.quant-gas", "cond-mat.soft", "cond-mat.stat-mech", "cond-mat.str-el", "cond-mat.supr-con",
    "cs.AI", "cs.AR", "cs.CC", "cs.CE", "cs.CG", "cs.CL", "cs.CR", "cs.CV", "cs.CY",
    "cs.DB", "cs.DC", "cs.DL", "cs.DM", "cs.DS", "cs.ET", "cs.FL", "cs.GL", "cs.GR",
    "cs.GT", "cs.HC", "cs.IR", "cs.IT", "cs.LG", "cs.LO", "cs.MA", "cs.MM", "cs.MS",
    "cs.NA", "cs.NE", "cs.NI", "cs.OH", "cs.OS", "cs.PF", "cs.PL", "cs.RO", "cs.SC",
    "cs.SD", "cs.SE", "cs.SI", "cs.SY",
    "econ.EM", "econ.GN", "econ.TH",
    "eess.AS", "eess.IV", "eess.SP", "eess.SY",
    "gr-qc", "hep-ex", "hep-lat", "hep-ph", "hep-th", "math-ph", "nucl-ex", "nucl-th", "quant-ph",
    "math.AC", "math.AG", "math.AP", "math.AT", "math.CA", "math.CO", "math.CT", "math.CV",
    "math.DG", "math.DS", "math.FA", "math.GM", "math.GN", "math.GR", "math.GT", "math.HO",
    "math.IT", "math.KT", "math.LO", "math.MG", "math.MP", "math.NA", "math.NT", "math.OA",
    "math.OC", "math.PR", "math.QA", "math.RA", "math.RT", "math.SG", "math.SP", "math.ST",
    "nlin.AO", "nlin.CD", "nlin.CG", "nlin.PS", "nlin.SI",
    "physics.acc-ph", "physics.ao-ph", "physics.app-ph", "physics.atm-clus", "physics.atom-ph",
    "physics.bio-ph", "physics.chem-ph", "physics.class-ph", "physics.comp-ph", "physics.data-an", "physics.ed-ph",
    "physics.flu-dyn", "physics.gen-ph", "physics.geo-ph", "physics.hist-ph", "physics.ins-det",
    "physics.med-ph", "physics.optics", "physics.plasm-ph", "physics.pop-ph", "physics.soc-ph", "physics.space-ph",
    "q-bio.BM", "q-bio.CB", "q-bio.GN", "q-bio.MN", "q-bio.NC", "q-bio.OT", "q-bio.PE",
    "q-bio.QM", "q-bio.SC", "q-bio.TO",
    "q-fin.CP", "q-fin.EC", "q-fin.GN", "q-fin.MF", "q-fin.PM", "q-fin.PR", "q-fin.RM",
    "q-fin.ST", "q-fin.TR",
    "stat.AP", "stat.CO", "stat.ME", "stat.ML", "stat.OT", "stat.TH",
}

BIORXIV_SUBJECT_CATEGORIES = {
    "animal behavior and cognition", "biochemistry", "bioengineering", "bioinformatics", "biophysics",
    "cancer biology", "cell biology", "developmental biology", "ecology",
    "evolutionary biology", "genetics", "genomics", "immunology", "microbiology", "molecular biology",
    "neuroscience", "paleontology", "pathology", "pharmacology and toxicology", "physiology", "plant biology",
    "scientific communication and education", "synthetic biology", "systems biology", "zoology", "all",
}

_SEARCH_TRANSPORT_PREFIX_RE = re.compile(
    r"^(?:core topic routes?|core concept terms?|retrieval (?:method|application|domain) terms?|"
    r"retrieval expansions?|preference hints?|conditional exclusions?|researcher background|excluded topics?)\b",
    re.IGNORECASE,
)
_QUERY_SYNTAX_RE = re.compile(r"(?:\b(?:AND|OR|NOT)\b|\b(?:all|ti|abs|cat):|[\[\]{}])", re.IGNORECASE)

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "de", "for", "from", "in",
    "into", "is", "it", "of", "on", "or", "that", "the", "their", "to", "with",
    "using", "use", "based", "via", "study", "paper", "model", "models", "method",
    "methods", "approach", "architecture", "novel", "new", "toward", "towards",
}

def _han_ratio(text: str) -> float:
    chars = [ch for ch in str(text or "") if not ch.isspace()]
    if not chars:
        return 0.0
    han = sum(1 for ch in chars if "一" <= ch <= "鿿")
    return han / len(chars)


def _clean_phrase(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip().strip(".,;:!?\"'()[]{}")
    return text


def _dedupe_keep_order(items: list[str], *, lower_key: bool = True) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        text = _clean_phrase(raw)
        if not text:
            continue
        key = text.lower() if lower_key else text
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _is_english_phrase(text: str) -> bool:
    """A phrase usable in an English boolean query: ascii letters, no Han."""
    if not text or _han_ratio(text) > 0:
        return False
    return bool(re.search(r"[A-Za-z]", text))


def _is_search_phrase(text: str) -> bool:
    cleaned = _clean_phrase(text)
    if not _is_english_phrase(cleaned):
        return False
    if _SEARCH_TRANSPORT_PREFIX_RE.search(cleaned) or _QUERY_SYNTAX_RE.search(cleaned):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z0-9+./-]*", cleaned)
    return 1 <= len(words) <= 3


def _english_terms_from_text(text: str) -> list[str]:
    """Deterministic English keyword extraction (fallback path)."""
    phrases: list[str] = []
    # Keep multi-word English chunks split on punctuation.
    for chunk in re.split(r"[\n,;，；。.!?、/]+", str(text or "")):
        chunk = chunk.strip()
        if not chunk or _han_ratio(chunk) > 0.2:
            continue
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9+\-]{1,}", chunk)
                 if w.lower() not in _STOPWORDS]
        if 1 <= len(words) <= 6:
            phrase = " ".join(words)
            if len(phrase) >= 3:
                phrases.append(phrase)
    return [phrase for phrase in _dedupe_keep_order(phrases) if _is_search_phrase(phrase)]


def _search_terms_config_research_text(config: AppConfig) -> str:
    parts = [
        getattr(config, "research_topic", ""),
        getattr(config, "research_interest", ""),
        getattr(config, "researcher_profile", ""),
    ]
    return "\n".join(str(p) for p in parts if p).strip()


def _structured_profile_search_phrases(profile: dict[str, Any] | None) -> list[str]:
    payload = profile if isinstance(profile, dict) else {}
    signals = payload.get("explicit_retrieval_signals") if isinstance(payload.get("explicit_retrieval_signals"), dict) else {}
    safe = payload.get("safe_expansions") if isinstance(payload.get("safe_expansions"), dict) else {}
    phrases = [
        value
        for key in ("core_concepts", "method_terms", "application_terms", "domain_terms")
        for value in _as_string_list(signals.get(key))
        if _is_search_phrase(value)
    ]
    for item in _normalize_expansions(safe.get("synonyms_or_abbreviations")):
        term = _clean_phrase(item.get("term"))
        if _is_search_phrase(term):
            phrases.append(term)
    return _dedupe_keep_order(phrases)


def _validate_categories(config_categories: list[str]) -> list[str]:
    canonical_by_lower = {category.lower(): category for category in _ARXIV_CATEGORY_CODES}

    def normalize(value: Any) -> str:
        return canonical_by_lower.get(_clean_phrase(value).lower(), "")

    config_clean = [
        category
        for category in _dedupe_keep_order([normalize(c) for c in (config_categories or [])], lower_key=False)
        if category
    ]
    return config_clean


def _validate_biorxiv_categories(config_categories: list[str]) -> list[str]:
    def normalize(value: Any) -> str:
        return re.sub(r"[_-]+", " ", _clean_phrase(value).lower())

    configured = _dedupe_keep_order([normalize(value) for value in (config_categories or [])])
    configured = [category for category in configured if category in BIORXIV_SUBJECT_CATEGORIES]
    return configured


def build_search_terms_prompt(config: AppConfig) -> str:
    topic = str(getattr(config, "research_topic", "") or "")
    interest = str(getattr(config, "research_interest", "") or "")
    researcher_profile = str(getattr(config, "researcher_profile", "") or "")
    queries = [str(value).strip() for value in (getattr(config, "arxiv_queries", []) or []) if str(value).strip()]
    return f"""
Extract literature-search keywords for arXiv and bioRxiv directly from the
user's research topic, research interest, researcher profile, and any explicit
search terms. The corpora are English, so every keyword must be English;
translate non-English concepts. Output JSON only.

Return this exact shape:
{{
  "search_keywords": ["english keyword or short phrase", "..."]
}}

Rules:
- Include the central domain, object, task, method, and training concepts stated
  by the user. Every item must contain 1-3 words.
- You MUST include the basic, standalone concepts themselves as equal-status
  items, such as the domain/object and each core method or training concept.
  Do not return only combined or specialized phrases. For example, when the
  input mentions proteins, diffusion, and reinforcement learning, the output
  must include basic items such as "protein", "diffusion", and
  "reinforcement learning" in addition to any useful short phrases.
- Every item has exactly the same retrieval status and will be ORed independently.
  Do not rank, group, label, anchor, refine, combine with AND, or exclude items.
- Do not infer source categories and do not add negative keywords or any concept
  that is absent from the user's inputs.
- Prefer direct standard terms used in paper titles. Keep useful standard
  acronyms as searchable terms when the user used them.
- Return 4-12 unique keywords or short phrases, each containing 1-3 words.

[TOPIC]
{topic}
[/TOPIC]
[INTEREST]
{interest}
[/INTEREST]
[RESEARCHER_PROFILE]
{researcher_profile}
[/RESEARCHER_PROFILE]
[EXPLICIT_SEARCH_TERMS]
{json.dumps(queries, ensure_ascii=False)}
[/EXPLICIT_SEARCH_TERMS]

Return JSON only.
""".strip()


def _normalize_search_terms(
    data: Any,
    config: AppConfig,
    *,
    source: str,
) -> dict[str, Any]:
    data = data if isinstance(data, dict) else {}
    keywords = _dedupe_keep_order([
        value for value in (data.get("search_keywords") or []) if isinstance(value, str)
    ])
    keywords = [term for term in keywords if _is_search_phrase(term)][:12]
    configured_keywords = _dedupe_keep_order([
        str(value) for value in (getattr(config, "arxiv_queries", []) or [])
    ])
    configured_keywords = [term for term in configured_keywords if _is_search_phrase(term)]
    keywords = _dedupe_keep_order([*keywords, *configured_keywords])

    configured_arxiv = _validate_categories(list(getattr(config, "arxiv_categories", []) or []))
    configured_biorxiv = _validate_biorxiv_categories(list(getattr(config, "biorxiv_categories", []) or []))

    return {
        "search_keywords": keywords,
        "arxiv_categories": configured_arxiv,
        "arxiv_category_source": "configured" if configured_arxiv else "none",
        "biorxiv_categories": configured_biorxiv,
        "biorxiv_category_source": "configured" if configured_biorxiv else "none",
        "source": source,
    }


def _fallback_search_terms(config: AppConfig, normalized_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministic English search terms when no LLM is available.

    Pulls from config.arxiv_queries (already English in normal use) and any
    English text in the research fields. Cannot translate Chinese, but keeps the
    pipeline working and on-topic when English signals exist.
    """
    candidates: list[str] = []
    for q in (getattr(config, "arxiv_queries", []) or []):
        candidates.extend(_english_terms_from_text(str(q)))
    candidates.extend(_english_terms_from_text(str(getattr(config, "research_topic", "") or "")))
    candidates.extend(_english_terms_from_text(str(getattr(config, "research_interest", "") or "")))
    candidates.extend(_english_terms_from_text(str(getattr(config, "researcher_profile", "") or "")))
    keywords = _dedupe_keep_order(candidates)
    profile_keywords = _structured_profile_search_phrases(normalized_profile)
    data = {
        "search_keywords": _dedupe_keep_order([*profile_keywords, *keywords])[:20],
    }
    source = "profile_fallback" if isinstance(normalized_profile, dict) else "fallback"
    return _normalize_search_terms(data, config, source=source)


def extract_search_terms(
    config: AppConfig,
    llm: LLMClient | None,
    *,
    normalized_profile: dict[str, Any] | None = None,
    log=None,
) -> dict[str, Any]:
    """Return freshly extracted English search terms for the current topic."""

    research_text = _search_terms_config_research_text(config)
    if not research_text:
        return _fallback_search_terms(config, normalized_profile)

    if llm is None or not getattr(llm, "enabled", False):
        terms = _fallback_search_terms(config, normalized_profile)
        if log:
            log("search-terms: no LLM; deterministic English fallback")
        return terms

    data = None
    llm_error = ""
    try:
        prompt = build_search_terms_prompt(config)
        if hasattr(llm, "json_or_error"):
            result = llm.json_or_error(prompt, temperature=0.1, max_tokens=1800)
            if isinstance(result, dict) and result.get("ok") and isinstance(result.get("data"), dict):
                data = result["data"]
            elif isinstance(result, dict):
                llm_error = str(result.get("error") or "")
        else:
            data = llm.json_or_none(prompt, temperature=0.1, max_tokens=1800)
    except Exception as exc:  # pragma: no cover - defensive
        llm_error = str(exc)
        if log:
            log(f"search-terms: LLM error ({exc}); using fallback")
    if not isinstance(data, dict):
        terms = _fallback_search_terms(config, normalized_profile)
        if log:
            detail = f" ({llm_error[:240]})" if llm_error else ""
            log(f"search-terms: LLM returned no JSON{detail}; deterministic English fallback")
        return terms

    raw_keywords_valid = any(
        _is_search_phrase(value)
        for value in (data.get("search_keywords") or [])
        if isinstance(value, str)
    )
    terms = _normalize_search_terms(data, config, source="llm")
    if not raw_keywords_valid or not terms.get("search_keywords"):
        terms = _fallback_search_terms(config, normalized_profile)
        if log:
            log("search-terms: LLM returned no valid flat keywords; used deterministic fallback")
    if log:
        log(
            "search-terms: extracted "
            f"{len(terms.get('search_keywords', []))} equal-status keywords, "
            f"arxiv_cats={terms.get('arxiv_categories')}, "
            f"biorxiv_cats={terms.get('biorxiv_categories')}"
        )
    return terms


# Backward-compatible dotted imports for callers that still use the old package layout.
def _register_compat_aliases(*aliases: str) -> None:
    import sys as _sys
    _module = _sys.modules.get(__name__)
    if _module is None:
        return
    globals().setdefault("__path__", [])
    for _alias in aliases:
        _sys.modules.setdefault(_alias, _module)

_register_compat_aliases('research_profile.normalize', 'research_profile.search_terms')
