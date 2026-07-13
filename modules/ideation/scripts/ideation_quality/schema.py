from __future__ import annotations

import re
from typing import Any, Sequence

from artifact_io.workspace import compact_text, utc_now_iso


def _score_number(value: Any, default: float = 0.0) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return default


def _section_text(value: Any, limit: int) -> str:
    if isinstance(value, str):
        return value.strip()[:limit]
    return compact_text(value, limit)


def _dedupe_source_rows(rows: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for row in rows:
        key = (row.get("title", "") + "|" + row.get("url", "")).lower()
        if row.get("title") and key not in seen:
            seen.add(key)
            out.append(row)
    return out


def normalize_inspired_by(value: Any) -> list[dict[str, str]]:
    source = value if isinstance(value, list) else ([value] if value else [])
    rows: list[dict[str, str]] = []
    for item in source:
        if isinstance(item, dict):
            title = compact_text(item.get("title") or item.get("paper_title") or item.get("name"), 500)
            rows.append({
                "title": title,
                "source": compact_text(item.get("source"), 120),
                "url": compact_text(item.get("url"), 2000),
                "reason": compact_text(item.get("reason") or item.get("why") or item.get("mechanism"), 600),
            })
        else:
            rows.append({"title": compact_text(item, 300), "source": "", "url": "", "reason": ""})
    rows = [row for row in rows if row.get("title")]
    return _dedupe_source_rows(rows)[:8]


def normalize_idea(row: dict[str, Any], index: int) -> dict[str, Any]:
    idea = dict(row) if isinstance(row, dict) else {}
    idea["id"] = compact_text(idea.get("id"), 80) or f"idea-{index:03d}"
    idea["title"] = compact_text(idea.get("title"), 300) or f"候选想法 {index}"
    idea["status"] = compact_text(idea.get("status"), 40) or "pending"
    method = _section_text(idea.get("new_method") or idea.get("hypothesis"), 12000)
    details = _section_text(idea.get("method_details") or idea.get("mechanism"), 12000)
    experiment = _section_text(idea.get("initial_experiment") or idea.get("min_experiment") or idea.get("experiment_design"), 12000)
    idea["new_method"] = method
    idea["method_details"] = details
    idea["initial_experiment"] = experiment
    idea["inspired_by"] = normalize_inspired_by(idea.get("inspired_by"))
    idea["score"] = _score_number(idea.get("score") or idea.get("idea_score"), default=7.0)
    risks = idea.get("risks") if isinstance(idea.get("risks"), list) else []
    idea["risks"] = [compact_text(item, 300) for item in risks if compact_text(item, 30)][:8]
    for key in ("novelty", "feasibility", "evidence_strength"):
        idea[key] = compact_text(idea.get(key), 80) or "MEDIUM"
    return idea


def _strip_markdown_link(text: str) -> tuple[str, str]:
    match = re.search(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return compact_text(text, 700).strip("- "), ""


def _parse_source_line(line: str) -> dict[str, str] | None:
    text = line.strip().lstrip("-").strip()
    if not text:
        return None
    title, url = _strip_markdown_link(text)
    if url:
        reason = compact_text(text.replace(f"[{title}]({url})", ""), 600).strip(" -:|")
        return {"title": title, "source": "reading", "url": url, "reason": reason}
    parts = [part.strip() for part in text.split("|")]
    title = parts[0] if parts else text
    if not title:
        return None
    found_url = next((part for part in parts[1:] if part.startswith(("http://", "https://"))), "")
    reason = " / ".join(part for part in parts[1:] if part and part != found_url)
    return {"title": compact_text(title, 500), "source": "reading", "url": compact_text(found_url, 2000), "reason": compact_text(reason, 600)}


def _parse_meta_line(line: str) -> tuple[str, str] | None:
    text = line.strip().lstrip("-").strip()
    if ":" not in text:
        return None
    key, value = text.split(":", 1)
    key = key.strip().lower().replace("`", "")
    value = value.strip().strip("`")
    return key, value


def _section_map(block: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in block.splitlines():
        match = re.match(r"^###\s+(.+?)\s*$", line)
        if match:
            current = match.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections.setdefault(current, []).append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def ideas_from_markdown(markdown: str, evidence_titles: Sequence[str], max_ideas: int) -> list[dict[str, Any]]:
    text = str(markdown or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    matches = list(re.finditer(r"^##\s+(?:\d+\.\s*)?(.+?)\s*$", text, flags=re.MULTILINE))
    rows: list[dict[str, Any]] = []
    for index, match in enumerate(matches[:max_ideas], 1):
        start = match.end()
        end = matches[index].start() if index < len(matches) else len(text)
        block = text[start:end].strip()
        meta: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith("### "):
                break
            parsed = _parse_meta_line(line)
            if parsed:
                meta[parsed[0]] = parsed[1]
        sections = _section_map(block)
        inspired_rows = [
            row
            for line in sections.get("启发来源", "").splitlines()
            if (row := _parse_source_line(line)) is not None
        ]
        raw = {
            "id": meta.get("id") or f"idea-{index:03d}",
            "title": match.group(1).strip(),
            "status": meta.get("status") or "pending",
            "score": meta.get("score"),
            "novelty": meta.get("novelty"),
            "feasibility": meta.get("feasibility"),
            "evidence_strength": meta.get("evidence_strength"),
            "new_method": sections.get("新方法", ""),
            "method_details": sections.get("机制细节", ""),
            "initial_experiment": sections.get("初步实验", ""),
            "risks": [line.strip().lstrip("-").strip() for line in sections.get("风险与停止标准", "").splitlines() if line.strip()],
            "inspired_by": inspired_rows,
        }
        rows.append(normalize_idea(raw, index))
    return rows


def markdown_contract_issues(
    markdown: str,
    ideas: Sequence[dict[str, Any]],
    evidence_items: Sequence[dict[str, Any]] = (),
    max_ideas: int = 0,
) -> list[str]:
    text = str(markdown or "")
    issues: list[str] = []
    if not text.lstrip().startswith("# Ideation 生成的新论文想法"):
        issues.append("idea.md 必须以 '# Ideation 生成的新论文想法' 开头")
    forbidden = ("待项目代理", "待补齐", "TODO", "TBD", "需要说明基于哪项工作或基底")
    for token in forbidden:
        if token.lower() in text.lower():
            issues.append(f"idea.md 包含未完成占位:{token}")
    if re.search(r"<(?!https?://)[A-Za-z\u4e00-\u9fff][^>\n]{0,79}>", text):
        issues.append("idea.md 包含未替换的尖括号占位")
    if text.count("```") % 2:
        issues.append("Markdown fenced code block 未闭合")
    if text.count("$$") % 2:
        issues.append("Markdown 块级数学公式 $$ 未成对")
    inline_dollars = len(re.findall(r"(?<!\$)\$(?!\$)", text))
    if inline_dollars % 2:
        issues.append("Markdown 行内数学公式 $ 未成对")
    naked_urls = [
        item
        for item in re.findall(r"(?<!\]\()https?://[^\s)]+", text)
        if not item.endswith(">")
    ]
    if naked_urls:
        issues.append("网页引用存在非 Markdown 链接格式")
    if re.search(r"^###\s+自检\s*$", text, flags=re.MULTILINE):
        issues.append("idea.md 不得包含面向用户的 '### 自检' 栏目")
    if re.search(r"^###\s+(?:坏例切片|重点验证场景)\s*$", text, flags=re.MULTILINE):
        issues.append("idea.md 不得包含无直接输入证据的坏例或验证场景栏目")
    expected_headings = ("新方法", "机制细节", "初步实验", "启发来源", "风险与停止标准")
    idea_matches = list(re.finditer(r"^##\s+(?:\d+\.\s*)?(.+?)\s*$", text, flags=re.MULTILINE))
    if max_ideas > 0 and len(idea_matches) > max_ideas:
        issues.append(f"idea 数量超过本次上限：max={max_ideas}, actual={len(idea_matches)}")
    declared_count = re.search(r"^-\s*idea\s+数量\s*:\s*(\d+)\s*$", text, flags=re.MULTILINE | re.IGNORECASE)
    if not declared_count or int(declared_count.group(1)) != len(idea_matches):
        issues.append(f"idea 数量声明必须等于实际章节数：actual={len(idea_matches)}")
    idea_ids: list[str] = []
    for index, match in enumerate(idea_matches, 1):
        if len(match.group(1).strip()) > 300:
            issues.append(f"idea {index} 标题不能超过 300 个字符")
        end = idea_matches[index].start() if index < len(idea_matches) else len(text)
        block = text[match.end():end]
        headings = tuple(re.findall(r"^###\s+(.+?)\s*$", block, flags=re.MULTILINE))
        if headings != expected_headings:
            issues.append(f"idea {index} 栏目必须严格按固定顺序出现且无额外栏目")
        meta: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith("### "):
                break
            parsed = _parse_meta_line(line)
            if parsed:
                meta[parsed[0]] = parsed[1]
        idea_id = meta.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", idea_id):
            issues.append(f"idea {index} 缺少合法显式 id")
        else:
            idea_ids.append(idea_id)
        if meta.get("status", "").lower() not in {"pending", "approved", "deleted"}:
            issues.append(f"idea {index} status 必须是 pending/approved/deleted")
        try:
            score = float(meta.get("score", ""))
        except (TypeError, ValueError):
            issues.append(f"idea {index} score 必须是 0-10 数字")
        else:
            if not 0 <= score <= 10:
                issues.append(f"idea {index} score 必须位于 0-10")
        for key in ("novelty", "feasibility", "evidence_strength"):
            if meta.get(key, "").upper() not in {"HIGH", "MEDIUM", "LOW"}:
                issues.append(f"idea {index} {key} 必须是 HIGH/MEDIUM/LOW")
    if len(idea_ids) != len(set(idea_ids)):
        issues.append("idea id 必须唯一")
    for heading in expected_headings:
        count = len(re.findall(rf"^###\s+{re.escape(heading)}\s*$", text, flags=re.MULTILINE))
        if count != len(ideas):
            issues.append(f"每个 idea 必须各有一个 '### {heading}' 栏目：expected={len(ideas)}, actual={count}")
    allowed_sources: dict[str, set[str]] = {}
    for item in evidence_items:
        if not isinstance(item, dict):
            continue
        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip().casefold()
        if not title:
            continue
        url = str(item.get("url") or "").strip().rstrip("/")
        allowed_sources.setdefault(title, set())
        if url:
            allowed_sources[title].add(url)
    if allowed_sources:
        for label, raw_url in re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", text):
            title = re.sub(r"\s+", " ", label).strip().casefold()
            url = raw_url.strip().rstrip("/")
            if title not in allowed_sources or url not in allowed_sources[title]:
                issues.append(f"Markdown 链接不对应输入证据的精确标题和 URL:{label}")
        for idea in ideas:
            for source in idea.get("inspired_by", []):
                if not isinstance(source, dict):
                    continue
                title = re.sub(r"\s+", " ", str(source.get("title") or "")).strip().casefold()
                url = str(source.get("url") or "").strip().rstrip("/")
                if title not in allowed_sources:
                    issues.append(f"{idea.get('id')} 启发来源标题不在输入证据中:{source.get('title', '')}")
                    continue
                expected_urls = allowed_sources[title]
                if expected_urls and url not in expected_urls:
                    issues.append(f"{idea.get('id')} 启发来源 URL 与输入证据不一致:{source.get('title', '')}")
                if not expected_urls and url:
                    issues.append(f"{idea.get('id')} 为无输入 URL 的证据编造了链接:{source.get('title', '')}")
    required = ("new_method", "method_details", "initial_experiment", "inspired_by")
    for idea in ideas:
        for key in required:
            value = idea.get(key)
            if key == "inspired_by":
                if not value:
                    issues.append(f"{idea.get('id')} 缺少启发来源")
            elif len(compact_text(value, 5000)) < 20:
                issues.append(f"{idea.get('id')} 缺少或过短:{key}")
    return list(dict.fromkeys(issues))


def _topic_terms(topic_text: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{3,}|[\u4e00-\u9fff]{2,}", topic_text.lower()):
        if token in {"research", "paper", "model", "method", "dataset", "experiment", "baseline"}:
            continue
        if token not in terms:
            terms.append(token)
    return terms[:30]


def _contains_experiment_protocol(text: str) -> bool:
    markers = ("baseline", "control", "ablation", "metric", "指标", "对照", "消融")
    return sum(1 for marker in markers if marker.lower() in text.lower()) >= 3


def idea_quality_issues(idea: dict[str, Any], evidence_titles: Sequence[str], topic_text: str) -> list[str]:
    issues: list[str] = []
    for key, min_len in {"title": 8, "new_method": 80, "method_details": 80, "initial_experiment": 100}.items():
        if len(compact_text(idea.get(key), 5000)) < min_len:
            issues.append(f"字段过短:{key}")
    experiment = compact_text(idea.get("initial_experiment"), 4000)
    if not _contains_experiment_protocol(experiment):
        issues.append("初步实验缺少 baseline/control/ablation/指标等可执行协议")
    if not idea.get("inspired_by"):
        issues.append("缺少启发来源")
    known_titles = {re.sub(r"\s+", " ", title).strip().casefold() for title in evidence_titles if title}
    has_known_source = False
    for source in idea.get("inspired_by", []):
        title = re.sub(r"\s+", " ", compact_text(source.get("title") if isinstance(source, dict) else source, 300)).strip().casefold()
        if known_titles and title:
            matched_source = title in known_titles
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
