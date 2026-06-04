from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

import requests

from auto_research.jobs import JobCancelled
from auto_research.llm import LLMClient
from auto_research.models import AppConfig, ReadRequest
from auto_research.storage import existing_stage_path, read_json, run_dir, stage_dir, sync_latest, update_manifest, write_json, write_text


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]

CONTENT_FIELDS = ["summary", "problem", "method", "experiments", "limitations", "relevance"]


def _raise_if_cancelled(should_cancel: CancelFn) -> None:
    if should_cancel():
        raise JobCancelled("Task cancelled by user.")


def _safe_id(value: Any, fallback: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or fallback)).strip("-")
    return safe or fallback


def _download_pdf(url: str, target: Path, retries: int = 2) -> tuple[bool, int, str]:
    if not url or not url.startswith("http"):
        return False, 0, "No HTTP PDF URL."
    last_error = ""
    for attempt in range(1, retries + 2):
        try:
            response = requests.get(url, timeout=45, headers={"User-Agent": "TASTE/0.1"})
            content_type = response.headers.get("content-type", "").lower()
            looks_like_pdf = "pdf" in content_type or url.lower().split("?", 1)[0].endswith(".pdf") or response.content.startswith(b"%PDF")
            if response.status_code == 200 and looks_like_pdf:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(response.content)
                return True, attempt, ""
            last_error = f"HTTP {response.status_code}, content-type={content_type or 'unknown'}"
        except Exception as exc:
            last_error = str(exc)
    return False, retries + 1, last_error


def _extract_pdf_text(path: Path) -> tuple[str, int, str]:
    try:
        import fitz
    except Exception as exc:
        return "", 0, f"PyMuPDF unavailable: {exc}"
    try:
        doc = fitz.open(path)
        chunks = [page.get_text("text") for page in doc]
        return "\n".join(chunks), len(doc), ""
    except Exception as exc:
        return "", 0, str(exc)


def _base_content(paper: dict, text: str) -> dict:
    abstract = str(paper.get("abstract", ""))
    basis = text.strip() or abstract
    return {
        "title": str(paper.get("title") or "Untitled"),
        "abstract": abstract,
        "venue": str(paper.get("venue") or paper.get("source") or ""),
        "year": str(paper.get("year") or ""),
        "summary": basis[:1200] if basis else "No readable full text was available; using title and abstract only.",
        "problem": "Inferred from title and abstract.",
        "method": "No agent structured reading was available; fallback mode kept content fields from available paper text.",
        "experiments": "Not extracted by fallback reader.",
        "limitations": "Fallback mode cannot reliably inspect all experimental details.",
        "relevance": str(paper.get("reason") or ""),
    }


def _base_metadata(paper: dict, index: int, pdf_path: Path) -> dict:
    return {
        "paper_id": str(paper.get("id") or ""),
        "url": str(paper.get("url") or ""),
        "pdf_url": str(paper.get("pdf_url") or ""),
        "source": str(paper.get("source") or ""),
        "paper_index": index,
        "pdf_path": str(pdf_path),
        "pdf_downloaded": False,
        "pdf_download_attempts": 0,
        "pdf_error": "",
        "pdf_pages": 0,
        "pdf_text_chars": 0,
        "source_mode": "abstract_only",
        "full_text_available": False,
        "worker_status": "fallback",
        "agent_status": "disabled",
        "agent_error": "",
        "agent_attempts": 0,
        "llm_provider": "",
        "llm_backend": "",
        "agent_session_id": "",
    }


def _read_worker_key(run_id: str, paper: dict, index: int) -> str:
    paper_id = _safe_id(paper.get("id"), f"paper-{index}")
    return f"run:{run_id}:worker:auto_read:{paper_id}"


def _agent_prompt(content: dict, source_text: str) -> str:
    return f"""
Read this paper for a researcher. Return one strict JSON object only.
Do not include paper_id, url, pdf_url, file paths, or other pipeline metadata.
Required JSON keys:
summary, problem, method, experiments, limitations, relevance.
Use Chinese.

Content fields already known:
Title: {content.get('title', '')}
Abstract: {content.get('abstract', '')}
Venue: {content.get('venue', '')}
Year: {content.get('year', '')}

Full paper text:
{source_text}
"""


def _repair_prompt(content: dict, source_text: str) -> str:
    return f"""
Your previous answer was not valid for the required schema.
Return strict JSON only, with exactly these keys:
summary, problem, method, experiments, limitations, relevance.
Use Chinese. Do not include metadata, markdown, or explanations.

Title: {content.get('title', '')}
Abstract: {content.get('abstract', '')}
Full paper text:
{source_text}
"""


def _apply_agent_content(content: dict, data: dict) -> None:
    for key in CONTENT_FIELDS:
        content[key] = str(data.get(key, content.get(key, "")))


def _run_agent_read(llm: LLMClient, content: dict, source_text: str) -> tuple[dict | None, str, str, int]:
    result = llm.json_or_error(_agent_prompt(content, source_text))
    if result.get("ok") and isinstance(result.get("data"), dict):
        return result["data"], "accepted", "", 1
    first_error = str(result.get("error") or "Agent returned non-object JSON")
    retry = llm.json_or_error(_repair_prompt(content, source_text))
    if retry.get("ok") and isinstance(retry.get("data"), dict):
        return retry["data"], "repaired", first_error, 2
    return None, "fallback", str(retry.get("error") or first_error), 2


def _content_for_agent(reading: dict) -> dict:
    content = reading.get("content", {})
    return content if isinstance(content, dict) else {}


def _render_single_reading(content: dict) -> str:
    lines = [
        f"# {content.get('title', 'Untitled')}",
        "",
        f"- **Venue**: {content.get('venue', '')}",
        f"- **Year**: {content.get('year', '')}",
        "",
        "## Abstract",
        content.get("abstract", ""),
        "",
        "## Summary",
        content.get("summary", ""),
        "",
        "## Problem",
        content.get("problem", ""),
        "",
        "## Method",
        content.get("method", ""),
        "",
        "## Experiments",
        content.get("experiments", ""),
        "",
        "## Limitations",
        content.get("limitations", ""),
        "",
        "## Relevance",
        content.get("relevance", ""),
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _fallback_cross_summary(readings: list[dict]) -> dict:
    titles = [str(_content_for_agent(item).get("title", "Untitled")) for item in readings]
    methods = [str(_content_for_agent(item).get("method", "")) for item in readings if _content_for_agent(item).get("method")]
    limitations = [str(_content_for_agent(item).get("limitations", "")) for item in readings if _content_for_agent(item).get("limitations")]
    return {
        "overview": f"Processed {len(readings)} paper(s): " + "; ".join(titles),
        "common_themes": "Fallback synthesis: inspect the individual paper blocks for recurring problems, methods, and evaluation settings.",
        "method_comparison": " | ".join(methods[:3]),
        "limitations_comparison": " | ".join(limitations[:3]),
        "next_stage_notes": "Use the structured content fields from each paper as evidence for idea generation.",
    }


def _cross_summary_prompt(readings: list[dict]) -> str:
    content_only = [_content_for_agent(item) for item in readings]
    return f"""
Synthesize these paper reading notes for the next idea-generation stage.
Use only the content fields below. Do not mention or infer pipeline metadata.
Return strict JSON with keys:
overview, common_themes, method_comparison, limitations_comparison, next_stage_notes.
Use Chinese.

Paper contents:
{json.dumps(content_only, ensure_ascii=False, indent=2)}
"""


def _run_cross_summary(run_id: str, readings: list[dict], config: AppConfig, log: LogFn) -> dict:
    summary = _fallback_cross_summary(readings)
    llm = LLMClient(config, "read", conversation_key=f"run:{run_id}:worker:auto_read:synthesis")
    if not llm.enabled or not readings:
        return summary
    result = llm.json_or_error(_cross_summary_prompt(readings))
    data = result.get("data")
    if result.get("ok") and isinstance(data, dict):
        log("Read synthesis: cross-paper summary accepted")
        return {key: str(data.get(key, summary.get(key, ""))) for key in summary}
    log(f"Read synthesis: fallback used: {str(result.get('error') or '')[:300]}")
    return summary


def _render_read_markdown(readings: list[dict], cross_summary: dict) -> str:
    lines = ["# Paper Readings", ""]
    for index, reading in enumerate(readings, 1):
        content = _content_for_agent(reading)
        lines.extend([
            f"## {index}. {content.get('title', 'Untitled')}",
            "",
            f"- **Venue**: {content.get('venue', '')}",
            f"- **Year**: {content.get('year', '')}",
            "",
            "### Abstract",
            content.get("abstract", ""),
            "",
            "### Summary",
            content.get("summary", ""),
            "",
            "### Problem",
            content.get("problem", ""),
            "",
            "### Method",
            content.get("method", ""),
            "",
            "### Experiments",
            content.get("experiments", ""),
            "",
            "### Limitations",
            content.get("limitations", ""),
            "",
            "### Relevance",
            content.get("relevance", ""),
            "",
        ])
    lines.extend([
        "## Cross-Paper Synthesis",
        "",
        "### Overview",
        cross_summary.get("overview", ""),
        "",
        "### Common Themes",
        cross_summary.get("common_themes", ""),
        "",
        "### Method Comparison",
        cross_summary.get("method_comparison", ""),
        "",
        "### Limitations Comparison",
        cross_summary.get("limitations_comparison", ""),
        "",
        "### Next Stage Notes",
        cross_summary.get("next_stage_notes", ""),
        "",
    ])
    return "\n".join(lines).rstrip() + "\n"


def run_read(request: ReadRequest, config: AppConfig, log: LogFn = print, should_cancel: CancelFn = lambda: False) -> dict:
    directory = run_dir(request.run_id)
    read_dir = stage_dir(directory, "read")
    find_results = read_json(existing_stage_path(directory, "find", "find_results.json"), {})
    papers = find_results.get("articles", [])
    if request.paper_ids:
        selected = [paper for paper in papers if paper.get("id") in request.paper_ids]
    else:
        selected = papers[: request.max_papers]
    selected = selected[: request.max_papers]

    readings: list[dict] = []
    pdf_dir = read_dir / "pdfs"
    log(f"Read selected {len(selected)} paper(s)")

    for index, paper in enumerate(selected, 1):
        _raise_if_cancelled(should_cancel)
        log(f"Read worker {index}/{len(selected)} assigned: {paper.get('title', 'Untitled')}")
        pdf_path = pdf_dir / f"{_safe_id(paper.get('id'), f'paper-{index}')}.pdf"
        downloaded, download_attempts, download_error = _download_pdf(paper.get("pdf_url", ""), pdf_path)
        text, pdf_pages, extract_error = _extract_pdf_text(pdf_path) if downloaded else ("", 0, "")
        content = _base_content(paper, text)
        metadata = _base_metadata(paper, index, pdf_path)
        metadata.update({
            "pdf_downloaded": downloaded,
            "pdf_download_attempts": download_attempts,
            "pdf_error": extract_error or download_error,
            "pdf_pages": pdf_pages,
            "pdf_text_chars": len(text),
            "source_mode": "full_pdf_text" if text else "abstract_only",
            "full_text_available": bool(text),
        })
        log(f"Read worker {index}: pdf_downloaded={downloaded}, attempts={download_attempts}, pdf_text_chars={len(text)}, pdf_pages={pdf_pages}")

        llm = LLMClient(config, "read", conversation_key=_read_worker_key(request.run_id, paper, index), persist_session=False)
        llm_summary = llm.summary()
        metadata.update({
            "llm_backend": str(llm_summary.get("backend", "")),
            "llm_provider": str(llm_summary.get("provider", "")),
            "agent_session_id": str(llm_summary.get("session_id", "")),
        })
        log(
            "Read worker "
            f"{index}: provider={llm_summary.get('provider', '')}, "
            f"backend={llm_summary.get('backend', '')}, "
            f"enabled={llm_summary.get('enabled')}, "
            f"session={llm_summary.get('session_id', '') or 'n/a'}"
        )
        source_text = text if text else content.get("abstract", "")
        if llm.enabled:
            log(f"Read worker {index}: source_mode={metadata['source_mode']}, source_chars={len(source_text)}")
            data, status, error, attempts = _run_agent_read(llm, content, source_text)
            metadata["agent_attempts"] = attempts
            metadata["agent_error"] = error
            metadata["agent_status"] = status
            metadata["worker_status"] = status
            if data:
                _apply_agent_content(content, data)
                log(f"Read worker {index}: structured reading {status}")
            else:
                log(f"Read worker {index}: structured reading failed; fallback used: {error[:300]}")
        else:
            log(f"Read worker {index}: LLM/agent disabled; fallback used")

        reading = {"content": content, "metadata": metadata}
        readings.append(reading)
        suffix = f"{index:03d}"
        write_json(read_dir / f"read_paper_{suffix}.json", reading)
        write_text(read_dir / f"read_paper_{suffix}.md", _render_single_reading(content))

    _raise_if_cancelled(should_cancel)
    cross_summary = _run_cross_summary(request.run_id, readings, config, log)
    write_json(read_dir / "read_results.json", {"run_id": request.run_id, "readings": readings, "cross_summary": cross_summary})
    write_text(read_dir / "read.md", _render_read_markdown(readings, cross_summary))
    sync_latest("auto_read", "read.md", read_dir / "read.md")
    update_manifest(directory, "read")
    log("Read stage complete")
    return {"run_id": request.run_id, "readings": readings, "cross_summary": cross_summary}
