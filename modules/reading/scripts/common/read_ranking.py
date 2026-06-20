from __future__ import annotations

import datetime as dt
import re
from typing import Any


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _text(value: Any, limit: int = 1200) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip()


def _numeric(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).strip())
    except Exception:
        return default


def _clamp_score(value: Any) -> float:
    return max(0.0, min(10.0, _numeric(value, 0.0)))


def _norm_title(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _paper_id(row: dict[str, Any], index: int) -> str:
    for key in ["paper_id", "id", "entry_id", "doi", "url", "pdf_url"]:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    title = _norm_title(row.get("title"))
    return title or f"reading_{index}"


def _full_text_chars(row: dict[str, Any]) -> int:
    values = [
        row.get("full_text_chars"),
        row.get("source_text_chars"),
        row.get("pdf_text_chars"),
        row.get("text_chars"),
    ]
    evidence = row.get("source_evidence") if isinstance(row.get("source_evidence"), dict) else {}
    values.extend([evidence.get("full_text_chars"), evidence.get("source_text_chars"), evidence.get("pdf_text_chars")])
    return max(0, int(max((_numeric(value, 0.0) for value in values), default=0.0)))


def _list_texts(row: dict[str, Any], *keys: str, limit: int = 2) -> list[str]:
    out: list[str] = []
    for key in keys:
        for item in _as_list(row.get(key)):
            text = _text(item, 360)
            if text and text not in out:
                out.append(text)
            if len(out) >= limit:
                return out
    return out


def _field_len(row: dict[str, Any], *keys: str) -> int:
    return max((len(_text(row.get(key), 8000)) for key in keys), default=0)


def _coverage_ratio(row: dict[str, Any]) -> float:
    checks = [
        (_field_len(row, "abstract_zh", "summary"), 260),
        (_field_len(row, "motivation_zh", "problem"), 180),
        (_field_len(row, "method_details_zh", "method"), 650),
        (_field_len(row, "experiments_zh", "experiments"), 420),
        (_field_len(row, "limitations_zh", "limitations"), 220),
        (sum(len(item) for item in _list_texts(row, "method_advantages_zh", limit=3)), 110),
        (sum(len(item) for item in _list_texts(row, "method_disadvantages_zh", limit=3)), 110),
    ]
    ratios = [min(1.0, got / need) if need else 0.0 for got, need in checks]
    return sum(ratios) / max(1, len(ratios))


def _role_penalty(row: dict[str, Any]) -> float:
    role = " ".join(str(row.get(key) or "") for key in ["verdict", "support_role", "evidence_role", "evidence_tier"]).lower()
    if row.get("claim_ready_anchor") is False:
        return 1.0
    if any(marker in role for marker in ["boundary", "critique", "audit", "weak"]):
        return 0.8
    return 0.0


def _claude_read_score(row: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    for key in ["read_score", "reading_score", "post_read_score", "read_relevance_score"]:
        value = row.get(key)
        if value not in (None, ""):
            score = _clamp_score(value)
            audit = row.get("read_score_audit") if isinstance(row.get("read_score_audit"), dict) else {}
            breakdown = row.get("read_score_breakdown") if isinstance(row.get("read_score_breakdown"), dict) else {}
            return score, {
                "source": "main_claude_code_after_deep_read" if audit else "reading_field_score",
                "score_field": key,
                "audit": audit,
                "breakdown": breakdown,
            }
    return None, {}


def _heuristic_breakdown(row: dict[str, Any]) -> dict[str, float]:
    chars = _full_text_chars(row)
    text_status = str(row.get("full_text_status") or "").lower()
    evidence_strength = 2.0
    if row.get("full_text_available") is True and any(marker in text_status for marker in ["pdf_text_read", "html_text_read", "full_text_read"]):
        evidence_strength = 7.2
        if chars >= 12000:
            evidence_strength = 8.2
        if chars >= 35000:
            evidence_strength = 9.0
    elif chars >= 1200:
        evidence_strength = 5.5

    coverage = _coverage_ratio(row)
    deep_read_quality = 2.0 + 7.5 * coverage
    method_transferability = 4.0 + 2.5 * bool(_text(row.get("method_family_zh"), 120)) + 2.0 * min(1.0, _field_len(row, "method_details_zh", "method") / 900)
    if _list_texts(row, "method_advantages_zh", limit=2) and _list_texts(row, "method_disadvantages_zh", limit=2):
        method_transferability += 1.0
    method_transferability = min(10.0, method_transferability)

    find_score = _clamp_score(row.get("score") or row.get("fit_score") or row.get("recommendation_score") or 5.0)
    topic_relevance = max(3.0, min(10.0, find_score + (0.8 if row.get("claim_ready_anchor") else -0.4)))
    risk_control = 5.0 + 2.0 * bool(_field_len(row, "limitations_zh", "limitations") >= 180) + 1.4 * bool(_list_texts(row, "method_disadvantages_zh", limit=2))
    return {
        "topic_relevance": round(topic_relevance, 3),
        "evidence_strength": round(evidence_strength, 3),
        "deep_read_quality": round(min(10.0, deep_read_quality), 3),
        "method_transferability": round(method_transferability, 3),
        "risk_control": round(min(10.0, risk_control), 3),
    }


def _overall_from_breakdown(breakdown: dict[str, float], row: dict[str, Any]) -> float:
    weights = {
        "topic_relevance": 0.28,
        "evidence_strength": 0.22,
        "deep_read_quality": 0.22,
        "method_transferability": 0.18,
        "risk_control": 0.10,
    }
    score = sum(float(breakdown.get(key, 0.0)) * weight for key, weight in weights.items())
    score -= _role_penalty(row)
    return round(max(0.0, min(10.0, score)), 3)


def _score_reading(row: dict[str, Any]) -> tuple[float, dict[str, float], dict[str, Any]]:
    claude_score, claude_meta = _claude_read_score(row)
    if claude_score is not None:
        raw_breakdown = claude_meta.get("breakdown") if isinstance(claude_meta.get("breakdown"), dict) else {}
        breakdown = {str(key): _clamp_score(value) for key, value in raw_breakdown.items()}
        if not breakdown:
            breakdown = _heuristic_breakdown(row)
        return claude_score, breakdown, {**claude_meta, "fallback_breakdown_used": not bool(raw_breakdown)}
    breakdown = _heuristic_breakdown(row)
    return _overall_from_breakdown(breakdown, row), breakdown, {"source": "deterministic_from_reading_product", "reason": "未发现主控 Claude read_score 字段，使用精读产物的全文证据、字段完整度、方法可迁移性和风险控制自动重排。"}


def _reason_zh(row: dict[str, Any], score: float, breakdown: dict[str, float], meta: dict[str, Any]) -> str:
    family = _text(row.get("method_family_zh"), 80) or "机制类别未明"
    status = _text(row.get("full_text_status"), 80) or "正文状态未明"
    source = str(meta.get("source") or "").strip()
    if source == "main_claude_code_after_deep_read":
        prefix = "主控 Claude Code 已在逐篇精读后给出读后评分"
    else:
        prefix = "系统根据已生成的精读产物做保底重排"
    return f"{prefix}；该文机制为{family}，全文状态为{status}，综合分 {score:.2f}，其中主题相关 {breakdown.get('topic_relevance', 0):.1f}、证据强度 {breakdown.get('evidence_strength', 0):.1f}、精读完整度 {breakdown.get('deep_read_quality', 0):.1f}。"


def build_reading_ranking(readings: list[dict[str, Any]], run_id: str = "", *, source: str = "reading_post_read_rerank") -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    indexed: list[tuple[int, dict[str, Any]]] = [(idx, row) for idx, row in enumerate(readings, 1) if isinstance(row, dict)]
    for index, row in indexed:
        score, breakdown, meta = _score_reading(row)
        rows.append({
            "original_order": index,
            "paper_id": _paper_id(row, index),
            "title": _text(row.get("title"), 260),
            "read_score": score,
            "score_breakdown": breakdown,
            "score_source": meta.get("source"),
            "score_audit": meta,
            "verdict": row.get("verdict") or "",
            "support_role": row.get("support_role") or "",
            "full_text_status": row.get("full_text_status") or "",
            "full_text_chars": _full_text_chars(row),
            "method_family_zh": _text(row.get("method_family_zh"), 140),
            "advantages_zh": _list_texts(row, "method_advantages_zh", limit=3),
            "disadvantages_zh": _list_texts(row, "method_disadvantages_zh", "limitations_zh", "limitations", limit=3),
        })
    rows.sort(key=lambda item: (-float(item.get("read_score") or 0.0), int(item.get("original_order") or 0), _norm_title(item.get("title"))))
    by_original = {index: row for index, row in indexed}
    for rank, item in enumerate(rows, 1):
        item["rank"] = rank
        original = by_original.get(int(item.get("original_order") or 0), {})
        item["why_ranked_here_zh"] = _reason_zh(
            original,
            float(item.get("read_score") or 0.0),
            item.get("score_breakdown") if isinstance(item.get("score_breakdown"), dict) else {},
            item.get("score_audit") if isinstance(item.get("score_audit"), dict) else {},
        )
    top = rows[:3]
    top_titles = "、".join(_text(item.get("title"), 80) for item in top if item.get("title"))
    comparative_summary = "读后重排尚无可用论文。" if not rows else f"读后最终排序优先考虑全文证据、精读字段完整度、方法可迁移性、主题贴合度和风险边界。当前靠前的是：{top_titles}。后续 idea/plan 应优先吸收靠前论文的可迁移机制，同时保留低分或边界论文揭示的失败条件。"
    return {
        "run_id": run_id,
        "source": source,
        "generated_at": _now_iso(),
        "ranking_policy": "基于每篇 read 产物重评分排序；若主控 Claude Code 已写 read_score/read_score_audit，则优先采用，否则用全文证据、精读完整度、方法可迁移性、主题贴合和风险控制做确定性保底。",
        "criteria": {
            "topic_relevance": "论文与当前研究问题/Find 推荐意图的贴合度。",
            "evidence_strength": "PDF/HTML 全文可读性、正文长度和正文状态。",
            "deep_read_quality": "摘要、动机、方法、实验、局限、优缺点字段是否完整。",
            "method_transferability": "方法机制对后续 idea/plan 的可迁移程度。",
            "risk_control": "局限性和不足是否足够清楚，能否帮助控制后续实验风险。",
        },
        "reading_ranking_order": [item.get("paper_id") for item in rows],
        "ranked_readings": rows,
        "comparative_summary_zh": comparative_summary,
    }


def sort_readings_by_ranking(readings: list[dict[str, Any]], run_id: str = "", *, source: str = "reading_post_read_rerank") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ranking = build_reading_ranking(readings, run_id, source=source)
    sorted_rows: list[dict[str, Any]] = []
    for item in ranking.get("ranked_readings", []):
        if not isinstance(item, dict):
            continue
        original_order = int(item.get("original_order") or 0)
        if original_order <= 0 or original_order > len(readings):
            continue
        row = dict(readings[original_order - 1])
        row["read_original_order"] = original_order
        row["read_rank"] = int(item.get("rank") or len(sorted_rows) + 1)
        row["read_score"] = item.get("read_score")
        row["read_score_breakdown"] = item.get("score_breakdown")
        row["read_score_audit"] = item.get("score_audit")
        row["post_read_rerank_reason_zh"] = item.get("why_ranked_here_zh")
        sorted_rows.append(row)
    if len(sorted_rows) != len([row for row in readings if isinstance(row, dict)]):
        return [dict(row) for row in readings if isinstance(row, dict)], ranking
    return sorted_rows, ranking
