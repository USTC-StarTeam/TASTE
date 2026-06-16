from find_support import PROFILE_SCHEMA, normalize_profile_shape, normalize_user_profile, profile_retrieval_text
from auto_research.models import AppConfig


class ProfileLLM:
    enabled = True

    def json_or_none(self, _prompt):
        return {
            "explicit_profile": {
                "research_interest_summary": "LLM agents for research automation",
                "researcher_background": "works on RAG evaluation",
                "irrelevant_extra": "ignored",
            },
            "explicit_retrieval_signals": {
                "core_concepts": ["LLM agents", "research automation", "LLM agents"],
                "method_terms": ["RAG", "evaluation"],
                "application_terms": ["paper discovery"],
                "domain_terms": ["AI research"],
                "excluded_terms": ["medical diagnosis"],
            },
            "safe_expansions": {
                "synonyms_or_abbreviations": [
                    {"term": "large language model agents", "source_term": "LLM agents", "confidence": "high"},
                    {"term": "large language model agents", "source_term": "LLM agents"},
                    {"term": "", "source_term": "ignored"},
                ],
            },
            "filtering_hints": {
                "hard_exclusions": ["medical diagnosis"],
                "must_keep_if_present": ["benchmark"],
                "soft_exclusions": ["ignored"],
            },
            "uncertainty": {
                "ambiguous_terms": ["agents"],
                "needs_clarification": False,
                "clarifying_questions": ["ignored"],
            },
        }


class DisabledLLM:
    enabled = False


class EmptyProfileLLM:
    enabled = True

    def json_or_none(self, _prompt):
        return {}


def test_normalize_user_profile_uses_tight_schema_and_safe_expansions_only():
    cfg = AppConfig(research_interest="LLM agents for research automation", researcher_profile="works on RAG evaluation")

    profile, fallback_used, error = normalize_user_profile(cfg, ProfileLLM())

    assert fallback_used is False
    assert error == ""
    assert profile["explicit_profile"] == {
        "research_interest_summary": "LLM agents for research automation",
        "researcher_background": "works on RAG evaluation",
    }
    assert profile["explicit_retrieval_signals"]["core_concepts"] == ["LLM agents", "research automation"]
    expansions = profile["safe_expansions"]["synonyms_or_abbreviations"]
    assert {
        "term": "large language model agents",
        "source_term": "LLM agents",
        "expansion_type": "synonym",
        "reason": "Expansion provided for explicit source term: LLM agents.",
    } in expansions
    assert "soft_exclusions" not in profile["filtering_hints"]
    assert "clarifying_questions" not in profile["uncertainty"]


def test_profile_retrieval_text_combines_explicit_terms_and_safe_expansions():
    profile = normalize_profile_shape(ProfileLLM().json_or_none(""))

    text = profile_retrieval_text(profile)

    assert "LLM agents for research automation" in text
    assert "RAG" in text
    assert "large language model agents" in text
    assert "Excluded topics: medical diagnosis" in text


def test_normalize_user_profile_falls_back_without_llm():
    cfg = AppConfig(provider="mock", research_interest="LLM agents retrieval")

    profile, fallback_used, error = normalize_user_profile(cfg, DisabledLLM())

    assert fallback_used is True
    assert "LLM is not configured" in error
    assert profile["explicit_profile"]["research_interest_summary"] == "LLM agents retrieval"
    assert profile["explicit_retrieval_signals"]["core_concepts"] == ["LLM", "agents", "retrieval"]


def test_normalize_user_profile_falls_back_on_empty_llm_profile():
    cfg = AppConfig(research_interest="LLM agents retrieval")

    profile, fallback_used, error = normalize_user_profile(cfg, EmptyProfileLLM())

    assert fallback_used is True
    assert "empty profile" in error
    assert profile["explicit_profile"]["research_interest_summary"] == "LLM agents retrieval"


def test_conditional_exclusion_is_not_hard_exclusion():
    cfg = AppConfig(
        provider="mock",
        research_interest="AI agents for academic research automation; avoid robotics unless directly related to research automation",
    )

    profile, _fallback_used, _error = normalize_user_profile(cfg, DisabledLLM())

    assert "robotics" not in profile["filtering_hints"]["hard_exclusions"]
    assert any(
        "robotics" in item["terms"] and "not directly related to research automation" in item["condition"]
        for item in profile["filtering_hints"]["conditional_exclusions"]
    )


class ConditionalLeakLLM:
    enabled = True

    def json_or_none(self, _prompt):
        return {
            "explicit_profile": {
                "research_interest_summary": "AI agents for academic research automation",
                "researcher_background": None,
            },
            "explicit_retrieval_signals": {
                "core_concepts": ["AI agents", "academic research automation"],
                "method_terms": ["LLM agents", "RAG"],
                "application_terms": ["paper discovery"],
                "domain_terms": ["academic research automation"],
                "excluded_terms": ["generic LLM training", "vision", "robotics", "theory papers"],
            },
            "safe_expansions": {"synonyms_or_abbreviations": []},
            "filtering_hints": {
                "hard_exclusions": ["generic LLM training", "vision", "robotics", "theory papers"],
                "conditional_exclusions": [
                    {
                        "terms": ["generic LLM training", "vision", "robotics", "theory papers"],
                        "condition": "unless they directly support academic research automation",
                    }
                ],
                "soft_penalties": [],
                "must_keep_if_present": [],
                "preference_hints": [],
            },
            "uncertainty": {"ambiguous_terms": [], "needs_clarification": False},
        }


def test_conditional_terms_do_not_leak_into_unconditional_retrieval_text():
    cfg = AppConfig(
        research_interest="AI agents for academic research automation",
        researcher_profile="Avoid generic LLM training, vision, robotics, or theory papers unless they directly support academic research automation.",
    )

    profile, fallback_used, error = normalize_user_profile(cfg, ConditionalLeakLLM())
    text = profile_retrieval_text(profile)

    assert fallback_used is False
    assert error == ""
    assert profile["explicit_retrieval_signals"]["excluded_terms"] == []
    assert profile["filtering_hints"]["hard_exclusions"] == []
    assert len(profile["filtering_hints"]["conditional_exclusions"]) == 1
    assert "Excluded topics:" not in text
    assert "Conditional exclusion:" in text
    assert "do not directly support academic research automation" in text


def test_preference_hints_are_not_ambiguity():
    cfg = AppConfig(
        provider="mock",
        research_interest="AI agents for academic research automation. I prefer practical systems, reproducible pipelines, lightweight experiments.",
    )

    profile, _fallback_used, _error = normalize_user_profile(cfg, DisabledLLM())

    assert "prefer practical systems" in profile["filtering_hints"]["preference_hints"]
    assert "prefer reproducible pipelines" in profile["filtering_hints"]["preference_hints"]
    assert "prefer lightweight experiments" in profile["filtering_hints"]["preference_hints"]
    assert not set(profile["filtering_hints"]["preference_hints"]) & set(profile["uncertainty"]["ambiguous_terms"])


def test_safe_expansions_are_traceable_for_research_automation_terms():
    cfg = AppConfig(
        provider="mock",
        research_interest="paper discovery, literature review automation, AI agents for academic research automation",
    )

    profile, _fallback_used, _error = normalize_user_profile(cfg, DisabledLLM())
    expansions = profile["safe_expansions"]["synonyms_or_abbreviations"]
    by_term = {item["term"]: item for item in expansions}

    for term in ["automated literature review", "paper recommendation", "research assistant agent", "AI scientist"]:
        assert term in by_term
        assert by_term[term]["source_term"]
        assert by_term[term]["expansion_type"] in {"synonym", "abbreviation", "closely_related"}
        assert by_term[term]["reason"]


def test_no_hallucinated_researcher_background_when_absent():
    cfg = AppConfig(provider="mock", research_interest="paper discovery with AI agents")

    profile, _fallback_used, _error = normalize_user_profile(cfg, DisabledLLM())

    assert profile["explicit_profile"]["researcher_background"] is None


def test_stage0_profile_schema_validity():
    cfg = AppConfig(provider="mock", research_interest="paper discovery with AI agents")

    profile, _fallback_used, _error = normalize_user_profile(cfg, DisabledLLM())

    assert profile.keys() == PROFILE_SCHEMA.keys()
    assert profile["explicit_profile"].keys() == PROFILE_SCHEMA["explicit_profile"].keys()
    assert profile["explicit_retrieval_signals"].keys() == PROFILE_SCHEMA["explicit_retrieval_signals"].keys()
    assert profile["safe_expansions"].keys() == PROFILE_SCHEMA["safe_expansions"].keys()
    assert profile["filtering_hints"].keys() == PROFILE_SCHEMA["filtering_hints"].keys()
    assert profile["uncertainty"].keys() == PROFILE_SCHEMA["uncertainty"].keys()
    assert isinstance(profile["filtering_hints"]["conditional_exclusions"], list)
    assert isinstance(profile["filtering_hints"]["soft_penalties"], list)
    assert isinstance(profile["filtering_hints"]["preference_hints"], list)
