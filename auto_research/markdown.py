from __future__ import annotations

from typing import Iterable


def table(headers: list[str], rows: Iterable[list[object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        safe = [str(value).replace("\n", " ").replace("|", "\\|") for value in row]
        lines.append("| " + " | ".join(safe) + " |")
    return "\n".join(lines)


def paper_markdown(papers: list[dict], title: str = "Recommended Articles") -> str:
    lines = [f"# {title}", ""]
    if not papers:
        lines.append("No items were selected.")
        return "\n".join(lines) + "\n"

    for index, paper in enumerate(papers, 1):
        lines.extend([
            f"## {index}. {paper.get('title', 'Untitled')}",
            "",
            f"- **ID**: `{paper.get('id', '')}`",
            f"- **Source**: {paper.get('source', '')}",
            f"- **Venue/Year**: {paper.get('venue', '')} {paper.get('year', '')}",
            f"- **Category**: {paper.get('category', '')} (`{paper.get('classification_source', '')}`)",
            f"- **Fit Score**: {paper.get('fit_score', paper.get('score', ''))}",
            f"- **Diversity Score**: {paper.get('diversity_score', '')}",
            f"- **Final Score**: {paper.get('score', '')}",
            f"- **Hit Directions**: {', '.join(paper.get('hit_directions', [])) if isinstance(paper.get('hit_directions', []), list) else paper.get('hit_directions', '')}",
            f"- **URL**: {paper.get('url', '')}",
            f"- **PDF**: {paper.get('pdf_url', '')}",
            "",
            "### Abstract",
            "",
            paper.get("abstract", "") or "No abstract available.",
            "",
            "### Fit Explanation",
            "",
            paper.get("fit_explanation", "") or "No fit explanation available.",
            "",
            "### Recommendation",
            "",
            paper.get("reason", "") or "Selected by the local ranking fallback.",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"
