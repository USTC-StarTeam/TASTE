from __future__ import annotations

from typing import Any, Sequence

from artifact_io.workspace import compact_text, utc_now_iso


def _score_text(idea: dict[str, Any]) -> str:
    try:
        return f"{float(idea.get('score', 0)):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "未评分"


def render_ideas_markdown(ideas: Sequence[dict[str, Any]], audit: dict[str, Any]) -> str:
    lines = ["# Ideation 生成的新论文想法", ""]
    lines.append(f"- 生成时间: {audit.get('generated_at', utc_now_iso())}")
    lines.append(f"- idea 数量: {len(ideas)}")
    lines.append(f"- 质量门通过: {audit.get('passed_count', 0)} / {len(ideas)}")
    lines.append("")
    issues_by_id = {row.get("id"): row.get("issues", []) for row in audit.get("items", []) if isinstance(row, dict)}
    for index, idea in enumerate(ideas, 1):
        lines.extend([
            f"## {index}. {compact_text(idea.get('title'), 200) or '未命名想法'}",
            "",
            f"- id: `{compact_text(idea.get('id'), 80)}`",
            f"- status: {compact_text(idea.get('status'), 80) or 'pending'}",
            f"- score: {_score_text(idea)}",
            f"- novelty: {compact_text(idea.get('novelty'), 80)}",
            f"- feasibility: {compact_text(idea.get('feasibility'), 80)}",
            f"- evidence_strength: {compact_text(idea.get('evidence_strength'), 80)}",
            "",
            "### 新方法", compact_text(idea.get("new_method"), 5000), "",
            "### 机制细节", compact_text(idea.get("method_details"), 5000), "",
            "### 初步实验", compact_text(idea.get("initial_experiment"), 5000), "",
            "### 坏例切片", compact_text(idea.get("bad_case_slice"), 2000), "",
            "### 启发来源",
        ])
        for source in idea.get("inspired_by", []):
            if isinstance(source, dict):
                parts = [source.get("title", ""), source.get("source", ""), source.get("reason", ""), source.get("url", "")]
                lines.append("- " + " | ".join(compact_text(part, 500) for part in parts if compact_text(part, 20)))
        if issues_by_id.get(idea.get("id")):
            lines.append("")
            lines.append("### 质量审计提示")
            for issue in issues_by_id[idea.get("id")]:
                lines.append(f"- {issue}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_hypothesis_arena(ideas: Sequence[dict[str, Any]], topic_text: str) -> dict[str, Any]:
    hypotheses = []
    for index, idea in enumerate(ideas, 1):
        hypotheses.append({
            "hypothesis_id": f"h{index:02d}_{idea.get('id', f'idea_{index}')}",
            "title": idea.get("title", ""),
            "method_hypothesis": idea.get("new_method", ""),
            "nearest_evidence": idea.get("inspired_by", []),
            "minimal_test": idea.get("initial_experiment", ""),
            "counterexample_slice": idea.get("bad_case_slice", ""),
            "kill_criteria": "若同协议 baseline/control/ablation 下没有稳定改进，或坏例切片退化且无法由机制解释，则暂停或剪枝该想法。",
            "priority": index,
        })
    return {
        "generated_at": utc_now_iso(),
        "topic_text": topic_text,
        "hypotheses": hypotheses,
    }
