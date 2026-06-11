from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any


GENERIC_TERMS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it", "of", "on", "or", "that", "the", "their", "to", "with",
    "ai", "research", "paper", "papers", "tool", "tools", "system", "systems", "model", "models", "method", "methods", "benchmark", "benchmarks",
    "using", "use", "used", "based", "including", "include", "improve", "interested", "prefer", "practical", "generic", "directly",
}


def _tokens(text: str) -> list[str]:
    raw = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9][a-zA-Z0-9_.-]{1,}", (text or "").lower())
    return [term.strip(".,;:!?()[]{}\"'") for term in raw if term.strip(".,;:!?()[]{}\"'") not in GENERIC_TERMS]


def _paper_text(paper: dict[str, Any]) -> str:
    title = str(paper.get("title") or "")
    abstract = str(paper.get("abstract") or "")
    category = str(paper.get("category") or "")
    categories = paper.get("categories") or paper.get("metadata", {}).get("all_categories") or []
    if isinstance(categories, list):
        category_text = " ".join(str(item) for item in categories)
    else:
        category_text = str(categories or "")
    return " ".join([title, title, title, abstract, category, category_text])


def _profile_phrases(profile_text: str) -> list[str]:
    phrases: list[str] = []
    for part in re.split(r"[\n,;，；。.!?、/]+", profile_text or ""):
        text = " ".join(str(part).lower().split())
        if len(text) >= 4:
            phrases.append(text)
    terms = [term for term in _tokens(profile_text) if re.fullmatch(r"[a-zA-Z0-9_.-]+", term)]
    for size in range(2, min(5, len(terms)) + 1):
        for index in range(0, len(terms) - size + 1):
            phrases.append(" ".join(terms[index:index + size]))
    return list(dict.fromkeys(phrase for phrase in phrases if len(phrase) >= 4))


def rank_papers_tfidf(
    papers: list[dict[str, Any]],
    query: str,
    *,
    per_category_limit: int = 100,
    global_limit: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not papers:
        return [], {"method": "adaptive_profile_similarity", "input_count": 0, "selected_count": 0}

    global_cap = len(papers) if int(global_limit or 0) <= 0 else max(1, min(len(papers), int(global_limit)))
    per_category_cap = len(papers) if int(per_category_limit or 0) <= 0 else max(1, int(per_category_limit))

    profile_signals = _tokens(query)
    profile_phrases = _profile_phrases(query)
    if not profile_signals:
        selected = [dict(paper, local_score=0.0, local_rank=index + 1, local_filter_reason="No research profile text; kept by source order.") for index, paper in enumerate(papers[:global_cap])]
        return selected, {"method": "adaptive_profile_similarity", "input_count": len(papers), "selected_count": len(selected), "global_limit": global_limit, "effective_global_limit": global_cap, "adaptive_profile_signal_count": 0, "adaptive_profile_phrase_count": 0, "profile_signal_source": "current research_interest/profile"}

    paper_texts = [_paper_text(paper) for paper in papers]
    doc_terms = [_tokens(text) for text in paper_texts]
    doc_freq: Counter[str] = Counter()
    for terms in doc_terms:
        doc_freq.update(set(terms))

    total_docs = len(papers)
    query_counts = Counter(profile_signals)
    query_weights = {
        term: (1.0 + math.log(count)) * (math.log((total_docs + 1) / (doc_freq.get(term, 0) + 1)) + 1.0)
        for term, count in query_counts.items()
    }
    query_norm = math.sqrt(sum(weight * weight for weight in query_weights.values())) or 1.0

    ranked: list[dict[str, Any]] = []
    for paper, text, terms in zip(papers, paper_texts, doc_terms, strict=False):
        counts = Counter(terms)
        dot = 0.0
        doc_norm_sq = 0.0
        for term, count in counts.items():
            idf = math.log((total_docs + 1) / (doc_freq.get(term, 0) + 1)) + 1.0
            weight = (1.0 + math.log(count)) * idf
            doc_norm_sq += weight * weight
            dot += weight * query_weights.get(term, 0.0)
        tfidf_score = dot / ((math.sqrt(doc_norm_sq) or 1.0) * query_norm)
        lowered = (text or "").lower()
        phrase_matches = [phrase for phrase in profile_phrases if phrase in lowered]
        adjustment = min(0.35, 0.06 * len(phrase_matches))
        score = max(0.0, tfidf_score + adjustment)
        row = dict(paper)
        row["local_score"] = round(score, 6)
        row["local_tfidf_score"] = round(tfidf_score, 6)
        row["local_phrase_adjustment"] = round(adjustment, 6)
        row["local_profile_phrase_match_count"] = len(phrase_matches)
        row["local_filter_reason"] = "Adaptive recall similarity from the current research interest/profile; used only for candidate retrieval, not as strong evidence."
        ranked.append(row)

    ranked.sort(key=lambda item: float(item.get("local_score") or 0), reverse=True)
    by_category: dict[str, list[dict[str, Any]]] = {}
    for item in ranked:
        category = str(item.get("category") or item.get("metadata", {}).get("primary_category") or "unknown")
        bucket = by_category.setdefault(category, [])
        if len(bucket) < per_category_cap:
            bucket.append(item)

    balanced = [item for bucket in by_category.values() for item in bucket]
    balanced.sort(key=lambda item: float(item.get("local_score") or 0), reverse=True)
    selected = balanced[:global_cap]

    # Some venue adapters, such as proceedings-style ICML/DBLP sources, do not
    # expose meaningful fine-grained categories. In that case a small
    # per-category cap can silently dominate the global recall target and keep
    # only the first ~200 papers even when the Find page asks for a much larger
    # detail-scoring pool. Preserve the balancing behavior, then fill the
    # remaining global budget from the source-ranked list.
    if len(selected) < min(global_cap, len(ranked)):
        seen = {str(item.get("id") or item.get("url") or item.get("title") or "") for item in selected}
        for item in ranked:
            key = str(item.get("id") or item.get("url") or item.get("title") or "")
            if key in seen:
                continue
            selected.append(item)
            seen.add(key)
            if len(selected) >= global_cap:
                break

    for index, item in enumerate(selected, 1):
        item["local_rank"] = index

    return selected, {
        "method": "adaptive_profile_similarity",
        "input_count": len(papers),
        "selected_count": len(selected),
        "global_limit": global_limit,
        "effective_global_limit": global_cap,
        "per_category_limit": per_category_limit,
        "effective_per_category_limit": per_category_cap,
        "balanced_selected_count": len(balanced),
        "category_counts": {category: len(items) for category, items in by_category.items()},
        "adaptive_profile_signal_count": len(profile_signals),
        "adaptive_profile_phrase_count": len(profile_phrases),
        "profile_signal_source": "current research_interest/profile",
    }
