from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

import requests

from auto_research.jobs import JobCancelled
from auto_research.llm import LLMClient
from auto_research.models import AppConfig, ReadRequest
from auto_research.storage import read_json, run_dir, sync_latest, update_manifest, write_json, write_text


FULL_TEXT_MIN_CHARS = 1200


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


def _raise_if_cancelled(should_cancel: CancelFn) -> None:
    if should_cancel():
        raise JobCancelled("Task cancelled by user.")


READ_USER_AGENT = "research-workflow/read-full-text"


def _download_pdf(url: str, target: Path) -> bool:
    if not url or not url.startswith("http"):
        return False
    try:
        response = requests.get(url, timeout=45, headers={"User-Agent": READ_USER_AGENT})
        content_type = response.headers.get("content-type", "").lower()
        if response.status_code != 200 or not ("pdf" in content_type or response.content.startswith(b"%PDF")):
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
        return True
    except Exception:
        return False


def _title_tokens(value: object) -> set[str]:
    stop = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "towards", "toward", "with"}
    return {token.lower() for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", str(value or "")) if len(token) >= 2 and token.lower() not in stop}


def _title_similarity(left: object, right: object) -> float:
    left_tokens = _title_tokens(left)
    right_tokens = _title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def _author_family_tokens(value: object) -> set[str]:
    if isinstance(value, list):
        names = [str(item or "") for item in value]
    else:
        names = re.split(r"[,;]", str(value or ""))
    tokens: set[str] = set()
    for name in names:
        parts = [part.lower() for part in re.findall(r"[A-Za-z][A-Za-z-]+", name)]
        if parts:
            tokens.add(parts[-1])
    return tokens


def _openreview_pdf_url(paper: dict) -> str:
    url = str(paper.get("url") or "")
    match = re.search(r"openreview\.net/forum\?id=([^&#]+)", url)
    return f"https://openreview.net/pdf?id={match.group(1)}" if match else ""


def _arxiv_title_query(title: str) -> str:
    terms = [term for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", title or "") if len(term) >= 3]
    return " AND ".join(f"ti:{term}" for term in terms[:10])


def _arxiv_title_queries(title: str) -> list[str]:
    cleaned = " ".join(str(title or "").split())
    terms_query = _arxiv_title_query(cleaned)
    queries: list[str] = []
    if terms_query:
        queries.append(terms_query)
    title_head = cleaned.split(":", 1)[0].strip()
    if title_head and len(title_head.split()) >= 3:
        queries.append(f'ti:"{title_head}"')
        queries.append(f'all:"{title_head}"')
    compact = re.sub(r"[^A-Za-z0-9 /-]+", " ", cleaned).strip()
    head_terms = " ".join(compact.split()[:6])
    if head_terms and len(head_terms.split()) >= 3:
        queries.append(f'ti:"{head_terms}"')
    out: list[str] = []
    for query in queries:
        if query and query not in out:
            out.append(query)
    return out


def _arxiv_pdf_candidates(paper: dict, max_results: int = 5) -> list[dict]:
    title = str(paper.get("title") or "").strip()
    queries = _arxiv_title_queries(title)
    if not queries:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    expected_authors = _author_family_tokens(paper.get("authors"))
    candidates: list[dict] = []
    seen_entries: set[str] = set()
    attempts: list[dict] = []
    for query in queries:
        url = "https://export.arxiv.org/api/query?search_query=" + quote_plus(query) + f"&start=0&max_results={max_results}"
        try:
            response = requests.get(url, timeout=45, headers={"User-Agent": READ_USER_AGENT})
            if response.status_code != 200:
                attempts.append({"kind": "arxiv_title_search", "url": url, "query": query, "status_code": response.status_code, "accepted": False})
                continue
            root = ET.fromstring(response.content)
        except Exception as exc:
            attempts.append({"kind": "arxiv_title_search", "url": url, "query": query, "status_code": 0, "accepted": False, "error": exc.__class__.__name__})
            continue
        entries = root.findall("a:entry", ns)
        if not entries:
            attempts.append({"kind": "arxiv_title_search", "url": url, "query": query, "status_code": 200, "accepted": False, "reason": "no_arxiv_candidates"})
        for entry in entries:
            candidate_title = " ".join((entry.findtext("a:title", default="", namespaces=ns) or "").split())
            entry_id = entry.findtext("a:id", default="", namespaces=ns) or ""
            if entry_id in seen_entries:
                continue
            seen_entries.add(entry_id)
            candidate_authors = [node.text or "" for node in entry.findall("a:author/a:name", ns)]
            pdf_url = ""
            for link in entry.findall("a:link", ns):
                if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                    pdf_url = link.attrib.get("href", "")
                    break
            if not pdf_url and "/abs/" in entry_id:
                pdf_url = entry_id.replace("/abs/", "/pdf/")
            similarity = _title_similarity(title, candidate_title)
            author_overlap = sorted(expected_authors & _author_family_tokens(candidate_authors))
            accepted = bool(pdf_url and (similarity >= 0.95 if not expected_authors else (similarity >= 0.82 and author_overlap) or (similarity >= 0.78 and len(author_overlap) >= 2)))
            candidates.append({
                "kind": "arxiv_title_search_candidate",
                "search_url": url,
                "query": query,
                "title": candidate_title,
                "entry_id": entry_id,
                "pdf_url": pdf_url,
                "similarity": round(similarity, 4),
                "author_overlap": author_overlap,
                "accepted": accepted,
            })
            if accepted:
                return candidates
    return candidates or attempts or [{"kind": "arxiv_title_search", "accepted": False, "reason": "no_arxiv_candidates"}]


def _pdf_candidates_for_reading(paper: dict) -> list[dict]:
    candidates: list[dict] = []
    seen: set[str] = set()

    def add(kind: str, url: object, **extra: object) -> None:
        pdf_url = str(url or "").strip()
        if not pdf_url or not pdf_url.startswith("http") or pdf_url in seen:
            return
        seen.add(pdf_url)
        candidates.append({"kind": kind, "pdf_url": pdf_url, "accepted": True, **extra})

    add("indexed_pdf", paper.get("pdf_url"))
    add("openreview_pdf_from_forum_url", _openreview_pdf_url(paper))
    doi_blob = " ".join(str(paper.get(key) or "") for key in ["url", "doi", "pdf_url"]).lower()
    if not candidates or "doi.org/10.1145" in doi_blob or "dl.acm.org" in doi_blob:
        for candidate in _arxiv_pdf_candidates(paper):
            if candidate.get("accepted"):
                add("arxiv_title_verified_pdf", candidate.get("pdf_url"), arxiv_match=candidate)
    return candidates


def _download_first_readable_pdf(paper: dict, pdf_dir: Path, log: LogFn) -> tuple[bool, Path, str, dict]:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(paper.get("id") or paper.get("paper_id") or "paper")).strip("_") or "paper"
    attempts: list[dict] = []
    for index, candidate in enumerate(_pdf_candidates_for_reading(paper), 1):
        pdf_url = str(candidate.get("pdf_url") or "")
        pdf_path = pdf_dir / f"{safe_id}_{index}.pdf"
        downloaded = _download_pdf(pdf_url, pdf_path)
        attempt = dict(candidate)
        attempt.update({"downloaded": downloaded, "pdf_path": str(pdf_path) if downloaded else ""})
        attempts.append(attempt)
        if downloaded:
            if candidate.get("kind") == "arxiv_title_verified_pdf":
                match = candidate.get("arxiv_match") if isinstance(candidate.get("arxiv_match"), dict) else {}
                log(f"Reading PDF acquired by arXiv title match: {paper.get('title', 'Untitled')} -> {match.get('entry_id') or pdf_url}")
            return True, pdf_path, pdf_url, {"attempts": attempts, "selected": attempt}
        time.sleep(0.2)
    return False, pdf_dir / f"{safe_id}.pdf", "", {"attempts": attempts, "selected": {}}


def _extract_pdf_text(path: Path, max_chars: int = 50000) -> str:
    try:
        import fitz
    except Exception:
        return ""
    try:
        doc = fitz.open(path)
        chunks = []
        for page in doc[: min(len(doc), 20)]:
            chunks.append(page.get_text("text"))
        return "\n".join(chunks)[:max_chars]
    except Exception:
        return ""


def _clean_text(text: str, max_chars: int = 900) -> str:
    value = re.sub(r"-\s*\n\s*", "", str(text or ""))
    value = re.sub(r"\s*[:：]\s*no\s*[.。]?\s*$", "", value, flags=re.I)
    value = re.sub(r"\s*[:：]\s*[.。]\s*$", "", value)
    value = re.sub(r"\s*\n\s*", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:max_chars].rstrip()


_READ_PUBLIC_FORBIDDEN_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"对\s*(?:TASTE\s*)?(?:系统)?实现的直接含义", re.I), ""),
    (re.compile(r"(?:TASTE\s*)?系统实现", re.I), ""),
    (re.compile(r"\bGuardrail\b", re.I), ""),
    (re.compile(r"\bproject_topic\b", re.I), "当前主题"),
    (re.compile(r"摘要级线索"), "摘要信息"),
    (re.compile(r"Strong/foundation\s+anchors?\s+may\s+guide\s+planning[^.。]*[.。]?", re.I), ""),
    (re.compile(r"\bpaper\s+claims?\b", re.I), "论文结论"),
    (re.compile(r"论文\s*claim", re.I), "论文结论"),
    (re.compile(r"\bclaim\s+promotion\b", re.I), ""),
    (re.compile(r"repo/data/env/experiment\s+gate", re.I), "实验验证"),
    (re.compile(r"只有\s*repo/data/env/experiment[^。]*。?", re.I), ""),
    (re.compile(r"该条目是当前用户可见推荐文章[^。]*。?"), ""),
    (re.compile(r"必须进入精读[^。]*。?"), ""),
]

_READ_PUBLIC_SECTION_RE = re.compile(
    r"(?:^|\n)\s*(?:#{1,6}\s*)?(?:对\s*(?:TASTE\s*)?(?:系统)?实现的直接含义|实验与证据限制|Guardrail|使用边界)\s*[:：]?\s*.*?(?=(?:\n\s*(?:#{1,6}\s*)?(?:原论文摘要|论文动机|详细方法|实验设置与结果|局限性|方法优缺点|方法机制|摘要|动机|方法|实验|局限)\b)|\Z)",
    re.I | re.S,
)


def _sanitize_read_public_text(text: object, max_chars: int = 4000) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = _READ_PUBLIC_SECTION_RE.sub("\n", value)
    for pattern, replacement in _READ_PUBLIC_FORBIDDEN_REPLACEMENTS:
        value = pattern.sub(replacement, value)
    value = re.sub(r"\s*[:：]\s*no\s*[.。]?\s*$", "", value, flags=re.I)
    value = re.sub(r"\s*[:：]\s*[.。]\s*$", "", value)
    value = re.sub(r"\s*\n\s*", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -\t")
    return value[:max_chars].rstrip()


def _sanitize_read_public_value(value: object) -> object:
    if isinstance(value, str):
        return _sanitize_read_public_text(value, 8000)
    if isinstance(value, list):
        cleaned: list[object] = []
        for item in value:
            next_item = _sanitize_read_public_value(item)
            if next_item not in ("", [], {}):
                cleaned.append(next_item)
        return cleaned
    if isinstance(value, dict):
        return {str(key): _sanitize_read_public_value(item) for key, item in value.items()}
    return value


def _ensure_cjk_sentence_end(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if not re.search(r"[\u4e00-\u9fff]", value):
        return value
    if value[-1] in "。！？.!?":
        return value
    if value[-1] in "）)】]`$" and len(value) > 1 and value[-2] in "。！？.!?":
        return value
    return value + "。"


def _ensure_public_sentence_value(value: object) -> object:
    if isinstance(value, str):
        return _ensure_cjk_sentence_end(value)
    if isinstance(value, list):
        return [_ensure_public_sentence_value(item) for item in value]
    return value


def _sanitize_reading_public_fields(reading: dict) -> dict:
    public_keys = {
        "summary", "abstract_zh", "abstract_original", "problem", "motivation_zh",
        "method", "method_details_zh", "method_family_zh", "experiments", "experiments_zh",
        "limitations", "limitations_zh", "method_advantages_zh", "method_disadvantages_zh",
        "relevance", "critique_reason", "reading_status_note_zh",
    }
    sentence_keys = {
        "summary", "abstract_zh", "problem", "motivation_zh", "method", "method_details_zh",
        "experiments", "experiments_zh", "limitations", "limitations_zh", "relevance",
        "critique_reason", "reading_status_note_zh",
    }
    for key in public_keys:
        if key in reading:
            reading[key] = _sanitize_read_public_value(reading[key])
            if key in sentence_keys or key in {"method_advantages_zh", "method_disadvantages_zh"}:
                reading[key] = _ensure_public_sentence_value(reading[key])
    return reading


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(text or "")))

def _deep_read_text_ok(value: object, min_chars: int = 80) -> bool:
    text = _sanitize_read_public_text(value, 12000)
    return bool(_contains_cjk(text) and len(text) >= min_chars)


def _deep_read_list_ok(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    return all(_deep_read_text_ok(item, 24) for item in value[:2])


def _deep_read_contract_gaps(reading: dict) -> list[str]:
    gaps: list[str] = []
    checks = {
        "abstract_zh": 120,
        "motivation_zh": 100,
        "method_details_zh": 180,
        "experiments_zh": 140,
        "limitations_zh": 80,
    }
    for key, min_chars in checks.items():
        if not _deep_read_text_ok(reading.get(key) or reading.get(key.replace("_zh", "")), min_chars):
            gaps.append(key)
    if not _deep_read_text_ok(reading.get("method_family_zh"), 8):
        gaps.append("method_family_zh")
    if not _deep_read_list_ok(reading.get("method_advantages_zh")):
        gaps.append("method_advantages_zh")
    if not _deep_read_list_ok(reading.get("method_disadvantages_zh")):
        gaps.append("method_disadvantages_zh")
    return gaps


def _mark_deep_read_contract(reading: dict) -> dict:
    gaps = _deep_read_contract_gaps(reading)
    reading["deep_read_contract_gaps"] = gaps
    reading["deep_read_contract_ready"] = not gaps and bool(reading.get("full_text_available"))
    if gaps:
        if str(reading.get("full_text_status") or "") == "pdf_text_read":
            reading["full_text_status"] = "deep_read_contract_incomplete"
        reading["pdf_text_read"] = False
        reading["full_text_available"] = False
        reading["reading_status_note_zh"] = "已取得正文文本，但精读字段未满足完整合同；需要重新由项目代理精读。"
    return reading


def _zh_or_pending(text: str, fallback: str, max_chars: int = 900) -> str:
    value = _clean_text(text, max_chars)
    return value if _contains_cjk(value) else fallback


def _fallback_method_family() -> str:
    return "机制类别缺失"


def _full_text_chars(text: str) -> int:
    return len(_clean_text(text, 100000))


def _fallback_reading(paper: dict, text: str, *, downloaded: bool = False) -> dict:
    abstract = paper.get("abstract_zh", "") or paper.get("abstract", "") or paper.get("abstract_en", "")
    title = _clean_text(paper.get("title", "Untitled"), 220)
    venue = _clean_text(paper.get("venue", ""), 80)
    year = _clean_text(paper.get("year", ""), 20)
    relevance = _clean_text(paper.get("reason_zh", "") or paper.get("reason", "") or paper.get("fit_explanation_zh", "") or paper.get("fit_explanation", "") or paper.get("recommendation_note_zh", "") or paper.get("recommendation_note", ""), 900)
    weak = bool(paper.get("weak_candidate_for_critique") or paper.get("not_positive_support") or str(paper.get("evidence_tier") or "").lower() in {"nethreshold_for_reading", "critique_or_boundary_case", "retrieval_only", "weak_or_boundary"})
    full_text = " ".join(str(part or "") for part in [title, abstract, relevance, text[:5000]])
    family = _fallback_method_family()
    text_chars = _full_text_chars(text)
    evidence_available = text_chars >= FULL_TEXT_MIN_CHARS
    full_text_available = False
    full_text_status = "full_text_packet_ready_pending_deep_read_synthesis" if evidence_available else ("pdf_text_too_short" if downloaded or text_chars else "pending_full_text_reading")
    abstract_zh = _zh_or_pending(abstract, "", 1600)
    motivation = relevance if _contains_cjk(relevance) else ""
    method_details = ""
    experiments = ""
    limitations = ""
    advantages = []
    disadvantages = []
    resolved_pdf_url = paper.get("resolved_pdf_url", "") or paper.get("pdf_url", "")
    source_evidence = {
        "pdf_url": resolved_pdf_url,
        "pdf_downloaded": bool(downloaded),
        "pdf_text_chars": text_chars,
        "full_text_available": evidence_available,
        "full_text_status": full_text_status,
    }
    return {
        "paper_id": paper.get("id", "") or paper.get("paper_id", ""),
        "title": paper.get("title", "Untitled"),
        "url": paper.get("url", ""),
        "pdf_url": paper.get("resolved_pdf_url", "") or paper.get("pdf_url", ""),
        "venue": venue,
        "year": year,
        "score": paper.get("recommendation_score") or paper.get("score") or paper.get("fit_score"),
        "summary": abstract_zh,
        "abstract_zh": abstract_zh,
        "abstract_original": paper.get("abstract", "") or paper.get("summary", ""),
        "problem": motivation,
        "motivation_zh": motivation,
        "method": method_details,
        "method_details_zh": method_details,
        "method_family_zh": family,
        "experiments": experiments,
        "experiments_zh": experiments,
        "limitations": limitations,
        "limitations_zh": limitations,
        "method_advantages_zh": advantages,
        "method_disadvantages_zh": disadvantages,
        "relevance": relevance,
        "verdict": "contrast_or_boundary_reading" if weak else "core_reading",
        "support_role": "contrast_or_boundary_reference" if weak else "core_method_reference",
        "critique_reason": relevance if weak else "",
        "claim_ready_anchor": not weak,
        "recommended_for_deep_reading": True,
        "full_text_available": full_text_available,
        "full_text_status": full_text_status,
        "pdf_text_read": full_text_status == "pdf_text_read",
        "pdf_text_chars": text_chars,
        "source_evidence": source_evidence,
        "reading_status_note_zh": "已抽取正文证据，等待受控精读综合。" if evidence_available else "正文证据尚未取得；当前不生成精读正文。",
    }


def _paper_key(paper: dict) -> str:
    for key in ["id", "paper_id", "url", "pdf_url"]:
        value = str(paper.get(key) or "").strip().lower()
        if value:
            return value
    return " ".join(str(paper.get("title") or "").lower().split())


def _dedupe_papers(papers: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        key = _paper_key(paper)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(paper)
    return out


def _recommended_reading_pool(find_results: dict) -> list[dict]:
    rows = _dedupe_papers(list(find_results.get("strong_recommendations") or []) + list(find_results.get("articles") or []))
    if rows:
        for idx, row in enumerate(rows, 1):
            row.setdefault("recommended_for_deep_reading", True)
            row.setdefault("taste_pool", "strong_recommendations")
            row.setdefault("taste_pool_rank", idx)
        return rows
    return _dedupe_papers(list(find_results.get("read_candidates") or []))


def run_read(request: ReadRequest, config: AppConfig, log: LogFn = print, should_cancel: CancelFn = lambda: False) -> dict:
    directory = run_dir(request.run_id)
    find_results = read_json(directory / "find_results.json", {})
    papers = _recommended_reading_pool(find_results if isinstance(find_results, dict) else {})
    if not papers and isinstance(find_results, dict):
        fallback_pool = find_results.get("screened_ranking", []) or find_results.get("title_candidates", []) or find_results.get("evaluated_candidates", [])
        papers = list(fallback_pool)
        for paper in papers:
            paper.setdefault("reason_source", "weak-candidate critique mode")
            paper.setdefault("fit_explanation", "No recommended articles survived the evidence gate; read this weak candidate only to critique gaps and guide better discovery.")
            paper["weak_candidate_for_critique"] = True
    if request.paper_ids:
        wanted = {str(pid) for pid in request.paper_ids}
        selected = [paper for paper in papers if str(paper.get("id") or paper.get("paper_id") or "") in wanted]
        max_papers = len(selected) or len(request.paper_ids)
    else:
        max_papers = request.max_papers if request.max_papers and request.max_papers > 0 else len(papers)
        selected = papers[: max_papers]
    selected = selected[: max_papers]

    llm = LLMClient(config, "read")
    readings: list[dict] = []
    pdf_dir = directory / "pdfs"

    for paper in selected:
        _raise_if_cancelled(should_cancel)
        log(f"Reading: {paper.get('title', 'Untitled')}")
        pdf_path = pdf_dir / f"{paper.get('id', 'paper')}.pdf"
        skip_pdf = os.environ.get("SKIP_PDF", "0").lower() in {"1", "true", "yes"}
        use_read_llm = os.environ.get("READ_USE_LLM", "1").lower() not in {"0", "false", "no"}
        if skip_pdf:
            downloaded = False
            text = ""
            resolved_pdf_url = ""
            pdf_acquisition = {"attempts": [], "selected": {}, "skipped": "SKIP_PDF"}
        else:
            downloaded, pdf_path, resolved_pdf_url, pdf_acquisition = _download_first_readable_pdf(paper, pdf_dir, log)
            text = _extract_pdf_text(pdf_path) if downloaded else ""
            if resolved_pdf_url:
                paper["resolved_pdf_url"] = resolved_pdf_url
        reading = _fallback_reading(paper, text, downloaded=downloaded)
        evidence = reading.get("source_evidence") if isinstance(reading.get("source_evidence"), dict) else {}
        evidence["pdf_acquisition"] = pdf_acquisition
        if resolved_pdf_url:
            evidence["selected_pdf_url"] = resolved_pdf_url
            reading["pdf_url"] = resolved_pdf_url
        has_readable_evidence = bool(_full_text_chars(text) >= FULL_TEXT_MIN_CHARS or int(evidence.get("pdf_text_chars") or 0) >= FULL_TEXT_MIN_CHARS)
        source_text = text[:30000] if has_readable_evidence else ""
        if llm.enabled and use_read_llm and has_readable_evidence:
            prompt = f"""
请以论文精读者身份阅读这篇论文。只返回严格 JSON，字段必须包括：
summary, abstract_zh, motivation_zh, method_family_zh, method_details_zh, experiments_zh, limitations_zh, method_advantages_zh, method_disadvantages_zh, relevance。
全部中文表达；不要写自动科研系统名、流程护栏词、结论门控词、项目实现含义或审计流程。精读内容只讨论论文摘要、动机、方法、实验、局限和方法优缺点。
所有公式、LaTeX、代码式标识符、模型名、数据集名、指标名、阿拉伯数字、百分比、p 值、K 值、top-K、学习率、温度、显存/参数规模和实验结果必须保留原始符号写法；禁止把 0.0264 写成“零点零二六四”、把 50.86% 写成“百分之五十点八六”、把 1e-5 写成“一乘以十的负五次方”，也不要把普通短语包成公式。
abstract_zh 必须忠实翻译原论文摘要，不得复用推荐理由；motivation_zh 至少两句，说明论文要解决的具体科学/技术问题；method_details_zh 至少分解 3 个机制步骤，写清输入、模型/目标函数、训练或推理流程；experiments_zh 必须包含数据集、基线、指标和主要结果；limitations_zh 必须列出正文或可由实验设计推出的限制；method_advantages_zh 与 method_disadvantages_zh 必须是非空中文数组，每项为完整句子；method_family_zh 必须概括方法类别。
如果正文证据不足以完成任一字段，不要用摘要或推荐理由凑数，应在相应字段中明确指出缺失证据。

Title: {paper.get('title')}
Abstract: {paper.get('abstract')}
Text excerpt:
{source_text}
"""
            data = llm.json_or_none(prompt)
            if isinstance(data, dict):
                for key in ["summary", "abstract_zh", "motivation_zh", "method_family_zh", "method_details_zh", "experiments_zh", "limitations_zh", "relevance"]:
                    if data.get(key):
                        reading[key] = str(data.get(key))
                if data.get("method_advantages_zh"):
                    reading["method_advantages_zh"] = data.get("method_advantages_zh") if isinstance(data.get("method_advantages_zh"), list) else [str(data.get("method_advantages_zh"))]
                if data.get("method_disadvantages_zh"):
                    reading["method_disadvantages_zh"] = data.get("method_disadvantages_zh") if isinstance(data.get("method_disadvantages_zh"), list) else [str(data.get("method_disadvantages_zh"))]
                reading["problem"] = reading.get("motivation_zh", reading.get("problem", ""))
                reading["method"] = reading.get("method_details_zh", reading.get("method", ""))
                reading["experiments"] = reading.get("experiments_zh", reading.get("experiments", ""))
                reading["limitations"] = reading.get("limitations_zh", reading.get("limitations", ""))
                required_text = [str(reading.get(key) or "").strip() for key in ["abstract_zh", "motivation_zh", "method_details_zh", "experiments_zh", "limitations_zh"]]
                required_lists = [reading.get("method_advantages_zh"), reading.get("method_disadvantages_zh")]
                if all(required_text) and all(isinstance(value, list) and value for value in required_lists):
                    reading["full_text_available"] = True
                    reading["full_text_status"] = "pdf_text_read"
                    reading["pdf_text_read"] = True
                    reading["reading_status_note_zh"] = "全文证据已读取并完成中文精读。"
        reading["weak_candidate_for_critique"] = bool(paper.get("weak_candidate_for_critique"))
        if reading["weak_candidate_for_critique"]:
            note = "该论文更适合作为边界或对照参考；不能直接作为正向方法结论使用。"
            reading["limitations"] = (reading.get("limitations", "") + "\n" + note).strip()
            reading["limitations_zh"] = reading["limitations"]
        _sanitize_reading_public_fields(reading)
        _mark_deep_read_contract(reading)
        readings.append(reading)

    _raise_if_cancelled(should_cancel)
    lines = ["# 当前推荐论文精读", "", f"- run_id: `{request.run_id}`", f"- readings: {len(readings)}", ""]
    for index, item in enumerate(readings, 1):
        lines.extend([
            f"## {index}. {item['title']}",
            "",
            f"- venue/year: {item.get('venue', '')} {item.get('year', '')}".rstrip(),
            f"- score: {item.get('score') if item.get('score') not in (None, '') else '未评分'}",
            f"- URL: {item.get('url') or '未回填'}",
            f"- PDF: {item.get('pdf_url') or '未回填'}",
            "",
            "### 原论文摘要（中文）",
            item.get("abstract_zh") or item.get("summary") or "（该字段未提供合格精读内容。）",
            "",
            "### 论文动机",
            item.get("motivation_zh") or item.get("problem") or "（该字段未提供合格精读内容。）",
            "",
            "### 详细方法",
            item.get("method_details_zh") or item.get("method") or "（该字段未提供合格精读内容。）",
            "",
            "### 实验设置与结果",
            item.get("experiments_zh") or item.get("experiments") or "（该字段未提供合格精读内容。）",
            "",
            "### 局限性",
            item.get("limitations_zh") or item.get("limitations") or "（该字段未提供合格精读内容。）",
            "",
            "### 方法优缺点",
            "优点：",
        ])
        for value in item.get("method_advantages_zh") or []:
            lines.append(f"- {value}")
        lines.append("不足：")
        for value in item.get("method_disadvantages_zh") or []:
            lines.append(f"- {value}")
        lines.append("")
    lines.extend(["## 方法差异、优缺点总览", "", "| # | 论文 | 机制类别 | 主要优点 | 主要局限 |", "|---|---|---|---|---|"])
    for index, item in enumerate(readings, 1):
        adv = "；".join((item.get("method_advantages_zh") or [])[:2])
        dis = "；".join((item.get("method_disadvantages_zh") or [])[:2])
        title = str(item.get("title") or "").replace("|", " ")
        family = str(item.get("method_family_zh") or "").replace("|", " ")
        lines.append(f"| {index} | {title} | {family} | {adv.replace('|', ' ')} | {dis.replace('|', ' ')} |")

    incomplete_readings = [item for item in readings if item.get("deep_read_contract_gaps")]
    read_payload = {
        "run_id": request.run_id,
        "source": "reading_recommended_articles",
        "status": "blocked_incomplete_deep_read" if incomplete_readings else "complete",
        "recommendation_count": len(papers),
        "readings": readings,
        "deep_read_contract": {
            "ready": not incomplete_readings,
            "incomplete_count": len(incomplete_readings),
            "policy": "Every recommended paper must have Chinese abstract, motivation, detailed method, experiments, limitations, method family, and advantages/disadvantages derived from full text.",
        },
        "policy": "Read covers every user-visible recommended article by default; max_papers<=0 means all recommendations.",
    }
    write_json(directory / "read_results.json", read_payload)
    write_text(directory / "read.md", "\n".join(lines).rstrip() + "\n")
    sync_latest("reading", "read_results.json", directory / "read_results.json")
    sync_latest("reading", "read.md", directory / "read.md")
    update_manifest(directory, "read")
    log("Read stage complete")
    return {"run_id": request.run_id, "recommendation_count": len(papers), "readings": readings}
