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

CONTENT_FIELDS = ["summary", "motivation", "method_summary", "experiments", "limitations", "relevance"]
REQUIRED_AGENT_FIELDS = ["motivation", "method_summary", "limitations"]


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


def _get_cached_or_download_pdf(url: str, target: Path) -> tuple[bool, bool, int, str]:
    if target.exists():
        try:
            with target.open("rb") as handle:
                header = handle.read(4)
            if header == b"%PDF":
                return True, True, 0, ""
        except OSError:
            pass
    downloaded, attempts, error = _download_pdf(url, target)
    return downloaded, False, attempts, error


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
        "motivation": "Inferred from title and abstract.",
        "method_summary": "No agent structured reading was available; fallback mode kept content fields from available paper text.",
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
        "pdf_cache_hit": False,
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
summary, motivation, method_summary, experiments, limitations, relevance.
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
summary, motivation, method_summary, experiments, limitations, relevance.
Use Chinese. Do not include metadata, markdown, or explanations.

Title: {content.get('title', '')}
Abstract: {content.get('abstract', '')}
Full paper text:
{source_text}
"""


def _apply_agent_content(content: dict, data: dict) -> None:
    for key in CONTENT_FIELDS:
        content[key] = str(data.get(key, content.get(key, "")))


def _valid_agent_data(data: Any) -> bool:
    return isinstance(data, dict) and all(str(data.get(key, "")).strip() for key in REQUIRED_AGENT_FIELDS)


def _run_agent_read(llm: LLMClient, content: dict, source_text: str) -> tuple[dict | None, str, str, int]:
    result = llm.json_or_error(_agent_prompt(content, source_text))
    if result.get("ok") and _valid_agent_data(result.get("data")):
        return result["data"], "accepted", "", 1
    first_error = str(result.get("error") or "Agent response omitted mandatory reading fields")
    retry = llm.json_or_error(_repair_prompt(content, source_text))
    if retry.get("ok") and _valid_agent_data(retry.get("data")):
        return retry["data"], "repaired", first_error, 2
    return None, "fallback", str(retry.get("error") or first_error), 2


def _content_for_agent(reading: dict) -> dict:
    content = reading.get("content", {})
    return content if isinstance(content, dict) else {}


def _venue_with_year(content: dict) -> str:
    venue = str(content.get("venue", ""))
    year = str(content.get("year", ""))
    return f"{venue} ({year})" if venue and year else venue or year


def _render_single_reading(reading: dict) -> str:
    content = _content_for_agent(reading)
    metadata = reading.get("metadata", {}) if isinstance(reading.get("metadata"), dict) else {}
    lines = [
        f"# {content.get('title', 'Untitled')}",
        "",
        f"- **Venue**: {_venue_with_year(content)}",
        f"- **URL**: {metadata.get('url', '')}",
        f"- **PDF URL**: {metadata.get('pdf_url', '')}",
        "",
        "## Abstract",
        content.get("abstract", ""),
        "",
        "## Summary",
        content.get("summary", ""),
        "",
        "## Motivation",
        content.get("motivation", ""),
        "",
        "## Method Summary",
        content.get("method_summary", ""),
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
    methods = [str(_content_for_agent(item).get("method_summary", "")) for item in readings if _content_for_agent(item).get("method_summary")]
    limitations = [str(_content_for_agent(item).get("limitations", "")) for item in readings if _content_for_agent(item).get("limitations")]
    return {
        "overview": f"Processed {len(readings)} paper(s): " + "; ".join(titles),
        "common_themes": "Fallback synthesis: inspect the individual paper blocks for recurring problems, methods, and evaluation settings.",
        "method_comparison": " | ".join(methods[:3]),
        "limitations_comparison": " | ".join(limitations[:3]),
        "next_stage_notes": "Use the structured content fields from each paper as evidence for idea generation.",
    }


def _fallback_method_analysis(readings: list[dict]) -> dict:
    content_items = [_content_for_agent(item) for item in readings]
    return {
        "summary": "Fallback method analysis based on individual worker readings.",
        "method_differences": " | ".join(str(item.get("method_summary", "")) for item in content_items if item.get("method_summary")),
        "pros_cons": [
            {
                "title": str(item.get("title", "Untitled")),
                "pros": str(item.get("relevance", "")),
                "cons": str(item.get("limitations", "")),
            }
            for item in content_items
        ],
    }


def _main_agent_prompt(readings: list[dict], is_rerun: bool = False) -> str:
    content_only = [_content_for_agent(item) for item in readings]
    rerun_instruction = (
        "This is an auto_read rerun. Replace your previous read-stage understanding with these latest worker results; treat them as authoritative."
        if is_rerun
        else "This is the first auto_read result for this research run."
    )
    return f"""
You are the persistent main research agent. Synthesize these worker reading notes and retain their important context for later stages.
{rerun_instruction}
Use only the content fields below. Do not mention or infer pipeline metadata.
Return strict JSON with this shape:
{{
  "cross_summary": {{
    "overview": "",
    "common_themes": "",
    "method_comparison": "",
    "limitations_comparison": "",
    "next_stage_notes": ""
  }},
  "method_analysis": {{
    "summary": "",
    "method_differences": "",
    "pros_cons": [
      {{"title": "", "pros": "", "cons": ""}}
    ]
  }}
}}
Compare how the methods differ and clearly explain the pros and cons of each method.
Use Chinese.

Paper contents:
{json.dumps(content_only, ensure_ascii=False, indent=2)}
"""


def _run_main_agent(run_id: str, readings: list[dict], config: AppConfig, log: LogFn, resume_session: bool = False) -> tuple[dict, dict, dict]:
    cross_summary = _fallback_cross_summary(readings)
    method_analysis = _fallback_method_analysis(readings)
    conversation_key = f"run:{run_id}:main"
    llm = LLMClient(config, "read", conversation_key=conversation_key, persist_session=True, resume_session=resume_session)
    llm_summary = llm.summary()
    main_agent = {
        **llm_summary,
        "conversation_key": conversation_key,
        "persist_session": True,
        "resume_session": resume_session,
        "invocation": "resumed" if resume_session else "created",
        "status": "disabled",
        "error": "",
    }
    log(
        "Read main agent: "
        f"provider={llm_summary.get('provider', '')}, "
        f"backend={llm_summary.get('backend', '')}, "
        f"enabled={llm_summary.get('enabled')}, "
        f"invocation={main_agent['invocation']}, "
        f"session={llm_summary.get('session_id', '') or 'n/a'}"
    )
    if not llm.enabled or not readings:
        return cross_summary, method_analysis, main_agent
    result = llm.json_or_error(_main_agent_prompt(readings, is_rerun=resume_session))
    data = result.get("data")
    received_cross_summary = data.get("cross_summary") if isinstance(data, dict) else None
    received_method_analysis = data.get("method_analysis") if isinstance(data, dict) else None
    if result.get("ok") and isinstance(received_cross_summary, dict) and isinstance(received_method_analysis, dict):
        cross_summary = {key: str(received_cross_summary.get(key, cross_summary.get(key, ""))) for key in cross_summary}
        pros_cons = received_method_analysis.get("pros_cons")
        method_analysis = {
            "summary": str(received_method_analysis.get("summary", method_analysis["summary"])),
            "method_differences": str(received_method_analysis.get("method_differences", method_analysis["method_differences"])),
            "pros_cons": pros_cons if isinstance(pros_cons, list) else method_analysis["pros_cons"],
        }
        main_agent["status"] = "accepted"
        log("Read main agent: synthesis and method analysis accepted")
        return cross_summary, method_analysis, main_agent
    main_agent["status"] = "fallback"
    main_agent["error"] = str(result.get("error") or "Main agent returned invalid synthesis output")
    log(f"Read main agent: fallback used: {main_agent['error'][:300]}")
    return cross_summary, method_analysis, main_agent


def _render_read_markdown(readings: list[dict], cross_summary: dict, method_analysis: dict) -> str:
    lines = ["# Paper Readings", ""]
    for index, reading in enumerate(readings, 1):
        content = _content_for_agent(reading)
        metadata = reading.get("metadata", {}) if isinstance(reading.get("metadata"), dict) else {}
        lines.extend([
            f"## {index}. {content.get('title', 'Untitled')}",
            "",
            f"- **Venue**: {_venue_with_year(content)}",
            f"- **URL**: {metadata.get('url', '')}",
            f"- **PDF URL**: {metadata.get('pdf_url', '')}",
            "",
            "### Abstract",
            content.get("abstract", ""),
            "",
            "### Summary",
            content.get("summary", ""),
            "",
            "### Motivation",
            content.get("motivation", ""),
            "",
            "### Method Summary",
            content.get("method_summary", ""),
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
        "## Main Agent Synthesis",
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
        "## Method Cross-Comparison",
        "",
        "### Summary",
        method_analysis.get("summary", ""),
        "",
        "### Method Differences",
        method_analysis.get("method_differences", ""),
        "",
        "### Pros and Cons",
    ])
    for item in method_analysis.get("pros_cons", []):
        if isinstance(item, dict):
            lines.extend([
                f"#### {item.get('title', 'Untitled')}",
                f"- **Pros**: {item.get('pros', '')}",
                f"- **Cons**: {item.get('cons', '')}",
                "",
            ])
    return "\n".join(lines).rstrip() + "\n"


def run_read(request: ReadRequest, config: AppConfig, log: LogFn = print, should_cancel: CancelFn = lambda: False) -> dict:
    directory = run_dir(request.run_id)
    read_dir = stage_dir(directory, "read")
    previous_read_results = read_json(existing_stage_path(directory, "read", "read_results.json"), {})
    previous_main_agent = previous_read_results.get("main_agent", {}) if isinstance(previous_read_results, dict) else {}
    resume_main_agent = bool(isinstance(previous_main_agent, dict) and previous_main_agent.get("session_id"))
    find_results = read_json(existing_stage_path(directory, "find", "find_results.json"), {})
    papers = find_results.get("articles", [])
    if request.paper_ids:
        selected = [paper for paper in papers if paper.get("id") in request.paper_ids]
    else:
        selected = papers[: request.max_papers]
    selected = selected[: request.max_papers]

    readings: list[dict] = []
    pdf_dir = directory / "pdf"
    log(f"Read selected {len(selected)} paper(s)")

    for index, paper in enumerate(selected, 1):
        _raise_if_cancelled(should_cancel)
        log(f"Read worker {index}/{len(selected)} assigned: {paper.get('title', 'Untitled')}")
        pdf_path = pdf_dir / f"{_safe_id(paper.get('id'), f'paper-{index}')}.pdf"
        downloaded, cache_hit, download_attempts, download_error = _get_cached_or_download_pdf(paper.get("pdf_url", ""), pdf_path)
        text, pdf_pages, extract_error = _extract_pdf_text(pdf_path) if downloaded else ("", 0, "")
        content = _base_content(paper, text)
        metadata = _base_metadata(paper, index, pdf_path)
        metadata.update({
            "pdf_downloaded": downloaded,
            "pdf_cache_hit": cache_hit,
            "pdf_download_attempts": download_attempts,
            "pdf_error": extract_error or download_error,
            "pdf_pages": pdf_pages,
            "pdf_text_chars": len(text),
            "source_mode": "full_pdf_text" if text else "abstract_only",
            "full_text_available": bool(text),
        })
        log(f"Read worker {index}: pdf_downloaded={downloaded}, cache_hit={cache_hit}, attempts={download_attempts}, pdf_text_chars={len(text)}, pdf_pages={pdf_pages}")

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
        write_text(read_dir / f"read_paper_{suffix}.md", _render_single_reading(reading))

    _raise_if_cancelled(should_cancel)
    cross_summary, method_analysis, main_agent = _run_main_agent(request.run_id, readings, config, log, resume_session=resume_main_agent)
    result = {
        "run_id": request.run_id,
        "readings": readings,
        "main_agent_summary": cross_summary,
        "cross_summary": cross_summary,
        "method_analysis": method_analysis,
        "main_agent": main_agent,
    }
    write_json(read_dir / "read_results.json", result)
    write_text(read_dir / "read.md", _render_read_markdown(readings, cross_summary, method_analysis))
    sync_latest("auto_read", "read.md", read_dir / "read.md")
    update_manifest(directory, "read")
    log("Read stage complete")
    return result
