from __future__ import annotations

import re
from typing import Any, Sequence

from artifact_io.workspace import compact_text, utc_now_iso


REQUIRED_IDEA_FIELDS = (
    "title",
    "new_method",
    "method_details",
    "initial_experiment",
    "bad_case_slice",
    "inspired_by",
)


def idea_output_schema(max_ideas: int) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "required": ["ideas"],
        "properties": {
            "ideas": {
                "type": "array",
                "minItems": 1,
                "maxItems": max(1, max_ideas),
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "required": list(REQUIRED_IDEA_FIELDS),
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "one_sentence": {"type": "string"},
                        "new_method": {"type": "string"},
                        "method_details": {"type": "string"},
                        "initial_experiment": {"type": "string"},
                        "bad_case_slice": {"type": "string"},
                        "why_novel": {"type": "string"},
                        "feasibility_notes": {"type": "string"},
                        "risks": {"type": "array", "items": {"type": "string"}},
                        "score": {"type": "number"},
                        "novelty": {"type": "string"},
                        "feasibility": {"type": "string"},
                        "evidence_strength": {"type": "string"},
                        "inspired_by": {"type": "array"},
                    },
                },
            },
            "generation_notes": {"type": "string"},
        },
    }


def _score_number(value: Any, default: float = 0.0) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return default


def _objective_scores(idea: dict[str, Any]) -> dict[str, float]:
    overall = _score_number(idea.get("score"), 7.0)
    return {
        "novelty": _score_number(idea.get("novelty_score"), min(10.0, overall + 0.1)),
        "evidence_alignment": _score_number(idea.get("evidence_alignment_score"), overall),
        "feasibility": _score_number(idea.get("feasibility_score"), max(0.0, overall - 0.2)),
        "experimentability": _score_number(idea.get("experimentability_score"), overall),
        "risk_control": _score_number(idea.get("risk_control_score"), max(0.0, overall - 0.4)),
        "overall": overall,
    }


def _dedupe_source_rows(rows: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for row in rows:
        key = (row.get("title", "") + "|" + row.get("url", "")).lower()
        if row.get("title") and key not in seen:
            seen.add(key)
            out.append(row)
    return out


def normalize_inspired_by(value: Any, evidence_titles: Sequence[str]) -> list[dict[str, str]]:
    source = value if isinstance(value, list) else ([value] if value else [])
    rows: list[dict[str, str]] = []
    for item in source:
        if isinstance(item, dict):
            title = compact_text(item.get("title") or item.get("paper_title") or item.get("name"), 300)
            rows.append({
                "title": title,
                "source": compact_text(item.get("source"), 120),
                "url": compact_text(item.get("url"), 600),
                "reason": compact_text(item.get("reason") or item.get("why") or item.get("mechanism"), 600),
            })
        else:
            rows.append({"title": compact_text(item, 300), "source": "", "url": "", "reason": ""})
    rows = [row for row in rows if row.get("title")]
    if not rows and evidence_titles:
        rows = [{
            "title": evidence_titles[0],
            "source": "reading",
            "url": "",
            "reason": "Claude 输出缺少 inspired_by，暂用最高相关精读证据锚点。",
        }]
    return _dedupe_source_rows(rows)[:8]


def normalize_idea(row: dict[str, Any], index: int, evidence_titles: Sequence[str]) -> dict[str, Any]:
    idea = dict(row) if isinstance(row, dict) else {}
    idea["id"] = compact_text(idea.get("id"), 60) or f"idea-{index:03d}"
    idea["title"] = compact_text(idea.get("title"), 180) or f"候选想法 {index}"
    idea["status"] = compact_text(idea.get("status"), 40) or "pending"
    method = compact_text(idea.get("new_method") or idea.get("hypothesis"), 2400)
    details = compact_text(idea.get("method_details") or idea.get("mechanism"), 2600)
    experiment = compact_text(idea.get("initial_experiment") or idea.get("min_experiment") or idea.get("experiment_design"), 2600)
    idea["new_method"] = method
    idea["method_details"] = details
    idea["hypothesis"] = method
    idea["mechanism"] = details
    idea["initial_experiment"] = experiment
    idea["min_experiment"] = experiment
    idea["bad_case_slice"] = compact_text(idea.get("bad_case_slice") or idea.get("counterexample_slice"), 1200)
    idea["why_novel"] = compact_text(idea.get("why_novel") or idea.get("novelty_reason"), 1200)
    idea["feasibility_notes"] = compact_text(idea.get("feasibility_notes") or idea.get("feasibility_reason"), 1200)
    idea["inspired_by"] = normalize_inspired_by(idea.get("inspired_by"), evidence_titles)
    source_lines: list[str] = []
    for item in idea["inspired_by"]:
        source_lines.append(" | ".join(part for part in [item.get("title", ""), item.get("source", ""), item.get("reason", ""), item.get("url", "")] if part))
    idea["inspired_by_text"] = "\n".join(source_lines)
    idea["score"] = _score_number(idea.get("score") or idea.get("idea_score"), default=7.0)
    idea["objective_scores"] = _objective_scores(idea)
    risks = idea.get("risks") if isinstance(idea.get("risks"), list) else []
    idea["risks"] = [compact_text(item, 300) for item in risks if compact_text(item, 30)][:8]
    for key in ("novelty", "feasibility", "evidence_strength"):
        idea[key] = compact_text(idea.get(key), 80) or "MEDIUM"
    return idea


def normalize_ideas(payload: dict[str, Any], evidence_titles: Sequence[str], max_ideas: int) -> list[dict[str, Any]]:
    source = payload.get("ideas") if isinstance(payload, dict) else []
    if not isinstance(source, list):
        source = []
    ideas = [normalize_idea(row, index, evidence_titles) for index, row in enumerate(source[:max_ideas], 1)]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for idea in ideas:
        key = re.sub(r"\s+", " ", idea.get("title", "").lower()).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(idea)
    return out


def _topic_terms(topic_text: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{3,}|[\u4e00-\u9fff]{2,}", topic_text.lower()):
        if token in {"research", "paper", "model", "method", "dataset", "experiment", "baseline"}:
            continue
        if token not in terms:
            terms.append(token)
    return terms[:30]


def _contains_experiment_protocol(text: str) -> bool:
    markers = ("baseline", "control", "ablation", "metric", "指标", "对照", "消融", "坏例", "切片")
    return sum(1 for marker in markers if marker.lower() in text.lower()) >= 3


def idea_quality_issues(idea: dict[str, Any], evidence_titles: Sequence[str], topic_text: str) -> list[str]:
    issues: list[str] = []
    for key, min_len in {"title": 8, "new_method": 80, "method_details": 80, "initial_experiment": 100, "bad_case_slice": 30}.items():
        if len(compact_text(idea.get(key), 5000)) < min_len:
            issues.append(f"字段过短:{key}")
    experiment = compact_text(idea.get("initial_experiment"), 4000)
    if not _contains_experiment_protocol(experiment):
        issues.append("初步实验缺少 baseline/control/ablation/指标/坏例切片等可执行协议")
    if not idea.get("inspired_by"):
        issues.append("缺少启发来源")
    known_titles = {title.lower() for title in evidence_titles if title}
    has_known_source = False
    for source in idea.get("inspired_by", []):
        title = compact_text(source.get("title") if isinstance(source, dict) else source, 300).lower()
        if known_titles and title:
            matched_source = title in known_titles or any(title in known or known in title for known in known_titles)
            has_known_source = has_known_source or matched_source
            if not matched_source:
                issues.append(f"启发来源不在输入精读证据中:{title[:80]}")
    terms = _topic_terms(topic_text)
    haystack = " ".join(compact_text(idea.get(key), 2000).lower() for key in ("title", "new_method", "method_details", "initial_experiment"))
    if terms and not has_known_source and not any(term in haystack for term in terms):
        issues.append("缺少研究主题/兴趣关键词贴合")
    return issues


def build_quality_audit(ideas: Sequence[dict[str, Any]], evidence_titles: Sequence[str], topic_text: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for idea in ideas:
        issues = idea_quality_issues(idea, evidence_titles, topic_text)
        rows.append({
            "id": idea.get("id", ""),
            "title": idea.get("title", ""),
            "score": idea.get("score", 0),
            "passed": not issues,
            "issues": issues,
        })
    return {
        "generated_at": utc_now_iso(),
        "idea_count": len(ideas),
        "passed_count": sum(1 for row in rows if row.get("passed")),
        "has_blocking_issue": any(row.get("issues") for row in rows),
        "items": rows,
    }
