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
            "### 启发来源",
        ])
        for source in idea.get("inspired_by", []):
            if isinstance(source, dict):
                title = compact_text(source.get("title"), 500)
                url = compact_text(source.get("url"), 700)
                reason = compact_text(source.get("reason"), 700)
                source_name = compact_text(source.get("source"), 120)
                label = f"[{title}]({url})" if title and url else title
                details = " - ".join(part for part in [source_name, reason] if part)
                lines.append(f"- {label}{(' - ' + details) if details else ''}")
        risks = idea.get("risks") if isinstance(idea.get("risks"), list) else []
        lines.extend(["", "### 风险与停止标准"])
        if risks:
            for risk in risks:
                lines.append(f"- {compact_text(risk, 500)}")
        else:
            lines.append("若同协议 baseline/control/ablation 下没有稳定改进，则暂停或剪枝该想法。")
        if issues_by_id.get(idea.get("id")):
            lines.append("")
            lines.append("### 质量审计提示")
            for issue in issues_by_id[idea.get("id")]:
                lines.append(f"- {issue}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
