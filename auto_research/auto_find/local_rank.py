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

POSITIVE_PHRASES = [
    "research automation",
    "academic research automation",
    "literature review",
    "paper triage",
    "paper discovery",
    "hypothesis generation",
    "experiment planning",
    "research workflow",
    "information-seeking agent",
    "information seeking agent",
    "agent evaluation",
    "paper understanding",
    "retrieval augmented generation",
    "rag",
    "retrieval",
    "llm agent",
    "llm agents",
]

AVOID_PHRASES = [
    "image generation",
    "image segmentation",
    "video generation",
    "vision-language",
    "computer vision",
    "robotics",
    "robot",
    "theory",
    "optimization",
    "llm training",
    "pre-training",
    "pretraining",
    "translation",
    "education",
    "financial",
    "finance",
    "medical",
    "healthcare",
]

ANCHOR_PHRASES = [
    "research automation",
    "literature review",
    "paper triage",
    "paper discovery",
    "hypothesis generation",
    "experiment planning",
    "research workflow",
    "information-seeking",
    "agent evaluation",
    "paper understanding",
    "retrieval",
    "rag",
    "llm agent",
]


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


def _phrase_score(text: str) -> tuple[float, list[str], list[str]]:
    lowered = (text or "").lower()
    positives = [phrase for phrase in POSITIVE_PHRASES if phrase in lowered]
    avoids = [phrase for phrase in AVOID_PHRASES if phrase in lowered]
    anchors = [phrase for phrase in ANCHOR_PHRASES if phrase in lowered]
    boost = min(0.35, 0.055 * len(positives))
    penalty = 0.0
    if avoids:
        penalty = 0.08 * len(avoids)
        if not anchors:
            penalty += 0.18
    return boost - min(0.4, penalty), positives, avoids


def rank_papers_tfidf(
    papers: list[dict[str, Any]],
    query: str,
    *,
    per_category_limit: int = 100,
    global_limit: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not papers:
        return [], {"method": "tfidf", "input_count": 0, "selected_count": 0}

    query_terms = _tokens(query)
    if not query_terms:
        selected = [dict(paper, local_score=0.0, local_rank=index + 1, local_filter_reason="No query text; kept by source order.") for index, paper in enumerate(papers[:global_limit])]
        return selected, {"method": "tfidf", "input_count": len(papers), "selected_count": len(selected), "query_terms": []}

    paper_texts = [_paper_text(paper) for paper in papers]
    doc_terms = [_tokens(text) for text in paper_texts]
    doc_freq: Counter[str] = Counter()
    for terms in doc_terms:
        doc_freq.update(set(terms))

    total_docs = len(papers)
    query_counts = Counter(query_terms)
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
        adjustment, positive_matches, avoid_matches = _phrase_score(text)
        score = max(0.0, tfidf_score + adjustment)
        row = dict(paper)
        row["local_score"] = round(score, 6)
        row["local_tfidf_score"] = round(tfidf_score, 6)
        row["local_phrase_adjustment"] = round(adjustment, 6)
        row["local_positive_matches"] = positive_matches
        row["local_avoid_matches"] = avoid_matches
        row["local_filter_reason"] = "TF-IDF similarity plus phrase boosts/avoid-topic penalties using research interest/profile."
        ranked.append(row)

    ranked.sort(key=lambda item: float(item.get("local_score") or 0), reverse=True)
    by_category: dict[str, list[dict[str, Any]]] = {}
    for item in ranked:
        category = str(item.get("category") or item.get("metadata", {}).get("primary_category") or "unknown")
        bucket = by_category.setdefault(category, [])
        if len(bucket) < max(1, per_category_limit):
            bucket.append(item)

    balanced = [item for bucket in by_category.values() for item in bucket]
    balanced.sort(key=lambda item: float(item.get("local_score") or 0), reverse=True)
    selected = balanced[: max(1, global_limit)]
    for index, item in enumerate(selected, 1):
        item["local_rank"] = index

    return selected, {
        "method": "tfidf_phrase_adjusted",
        "input_count": len(papers),
        "selected_count": len(selected),
        "global_limit": global_limit,
        "per_category_limit": per_category_limit,
        "category_counts": {category: len(items) for category, items in by_category.items()},
        "query_terms": query_terms[:100],
        "positive_phrases": POSITIVE_PHRASES,
        "avoid_phrases": AVOID_PHRASES,
    }
