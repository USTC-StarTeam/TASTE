from __future__ import annotations

from typing import Iterable


_PLACEHOLDER_ABSTRACT_MARKERS = (
    "当前候选缺少真实摘要",
    "当前索引元数据缺少真实摘要",
    "lacks a real abstract",
    "No abstract available in metadata",
    "Abstract not available in the indexed venue metadata",
)

_INTERNAL_PUBLIC_TEXT_MARKERS = (
    "weak:",
    "passed:",
    "strong:",
    "topic_evidence",
    "matched_topic_route",
    "adaptive topic evidence",
    "adaptive_llm_topic_route",
    "missing adaptive topic evidence",
    "缺少当前主题",
    "高召回",
    "内部候选",
    "对 实现",
    "对AR实现",
    "Guardrail",
    "最终 LLM",
    "LLM 题名",
    "LLM 评分",
    "题名+摘要评分",
    "最终题名+摘要",
    "题名筛选线索",
    "最终相关性评分",
    "Find",
    "Top-N",
    "证据门控",
    "用户可见推荐",
    "推荐池",
    "检索候选",
    "Gate reason",
    "paper-conclusion",
    "claim",
    "foundation",
    "high-recall",
    "internal candidate",
    "implementation",
    "final title+abstract",
    "LLM score",
    "evidence gate",
    "user-visible",
    "recommendation pool",
    "retrieval candidate",
    "fallback-only",
)


def _contains_internal_public_text(value: object) -> bool:
    lowered = str(value or "").lower()
    return bool(lowered) and any(marker.lower() in lowered for marker in _INTERNAL_PUBLIC_TEXT_MARKERS)



def table(headers: list[str], rows: Iterable[list[object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        safe = [str(value).replace("\n", " ").replace("|", "\\|") for value in row]
        lines.append("| " + " | ".join(safe) + " |")
    return "\n".join(lines)


def _paper_text(paper: dict, keys: list[str]) -> str:
    for key in keys:
        value = str(paper.get(key) or "").strip()
        if not value:
            continue
        if any(marker.lower() in value.lower() for marker in _PLACEHOLDER_ABSTRACT_MARKERS):
            continue
        return value
    return ""


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in str(text or ""))


def _is_probably_english(text: str) -> bool:
    value = str(text or "")
    letters = sum(1 for char in value if ("a" <= char.lower() <= "z"))
    cjk = sum(1 for char in value if "\u4e00" <= char <= "\u9fff")
    return letters >= 20 and letters > cjk * 2


def _paper_zh_text(paper: dict, zh_keys: list[str], fallback_keys: list[str] | None = None) -> str:
    value = _paper_text(paper, zh_keys)
    if value:
        return value
    for key in fallback_keys or []:
        fallback = _paper_text(paper, [key])
        if fallback and _contains_cjk(fallback):
            return fallback
    return ""


def _paper_display_text(paper: dict, zh_keys: list[str], fallback_keys: list[str] | None = None, *, public_text: bool = False) -> tuple[str, str]:
    for key in zh_keys:
        zh_value = _paper_text(paper, [key])
        if zh_value and not (public_text and _contains_internal_public_text(zh_value)):
            return zh_value, "zh"
    for key in fallback_keys or []:
        fallback = _paper_text(paper, [key])
        if fallback and not (public_text and _contains_internal_public_text(fallback)):
            return fallback, "en" if _is_probably_english(fallback) else "fallback"
    return "", "missing"

def _paper_zh_hits(paper: dict) -> str:
    value = paper.get("hit_directions_zh")
    if isinstance(value, list):
        return "，".join(str(item) for item in value if str(item).strip())
    if value:
        return str(value)
    value = paper.get("hit_directions")
    if isinstance(value, list):
        return "，".join(str(item) for item in value if _contains_cjk(str(item)))
    if value and _contains_cjk(str(value)):
        return str(value)
    return ""


_TITLE_ZH = {
    "Recommended Articles": "推荐文章",
    "Screened Strong Ranking": "推荐文章排名",
    "Read Candidates": "精读候选",
    "Critique Candidates": "边界/审计候选",
    "bioRxiv Articles": "bioRxiv 文章",
    "Nature Portfolio Articles": "Nature Portfolio 文章",
    "Science Family Articles": "Science Family 文章",
    "HuggingFace Papers and Models": "HuggingFace 论文和模型",
    "GitHub Trending Repositories": "GitHub 趋势仓库",
}


def _markdown_title(title: str) -> str:
    return _TITLE_ZH.get(str(title or ""), str(title or "推荐文章"))


def _nonempty_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _optional_metadata_lines(paper: dict) -> list[str]:
    lines: list[str] = []
    track = str(paper.get("track") or "").strip()
    if track:
        lines.append(f"- **Track/类型**: {track}")
    labels = _nonempty_list(paper.get("quality_labels"))
    if labels:
        lines.append(f"- **质量标签**: {', '.join(labels)}")
    return lines

def _paper_brief_metadata_lines(paper: dict) -> list[str]:
    lines: list[str] = []
    source = str(paper.get("source") or "").strip()
    venue_year = " ".join(str(paper.get(key) or "").strip() for key in ("venue", "year")).strip()
    category = str(paper.get("category") or "").strip()
    hit_text = _paper_zh_hits(paper)
    if venue_year:
        lines.append(f"- **会议/年份**: {venue_year}")
    if source:
        lines.append(f"- **来源**: {source}")
    if category:
        lines.append(f"- **方法/主题类别**: {category}")
    if hit_text:
        lines.append(f"- **命中方向**: {hit_text}")
    lines.extend(_optional_metadata_lines(paper))
    return lines


def paper_markdown(papers: list[dict], title: str = "Recommended Articles") -> str:
    lines = [f"# {_markdown_title(title)}", "", f"- **条目数**: {len(papers)}", ""]
    if not papers:
        lines.append("未选择条目。")
        return "\n".join(lines) + "\n"

    for index, paper in enumerate(papers, 1):
        abstract, abstract_lang = _paper_display_text(paper, ["abstract_zh", "summary_zh", "tldr_zh"], ["abstract_en", "abstract", "summary", "tldr"])
        fit_explanation, fit_lang = _paper_display_text(paper, ["fit_explanation_zh", "match_explanation_zh"], ["fit_explanation", "match_explanation", "reason_en", "reason"], public_text=True)
        recommendation, reason_lang = _paper_display_text(paper, ["reason_zh", "recommendation_reason_zh"], ["reason", "recommendation_reason", "reason_en", "fit_explanation_en"], public_text=True)
        abstract_note: list[str] = []
        fit_note: list[str] = []
        reason_note: list[str] = []
        metadata_lines = _paper_brief_metadata_lines(paper)
        lines.extend([
            f"## {index}. {paper.get('title', 'Untitled')}",
            "",
            *metadata_lines,
            "",
            "### 摘要",
            "",
            abstract or "当前条目缺少可展示的真实摘要；需要通过详情抓取、URL/PDF 精读或摘要翻译修复后再作为推荐证据。Abstract not available in the indexed venue metadata.",
            *abstract_note,
            "",
            "### 匹配解释",
            "",
            fit_explanation or "匹配解释缺失；需要重新执行标题+摘要评分或理由补全。",
            *fit_note,
            "",
            "### 为什么推荐",
            "",
            recommendation or "推荐理由缺失；需要说明该条目为什么值得精读、可借鉴的具体方法、数据、协议或边界价值。",
            *reason_note,
            "",
            "",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"
