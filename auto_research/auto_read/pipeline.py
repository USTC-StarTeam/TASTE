from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import requests

from auto_research.jobs import JobCancelled
from auto_research.llm import LLMClient
from auto_research.models import AppConfig, ReadRequest
from auto_research.storage import read_json, run_dir, sync_latest, update_manifest, write_json, write_text


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


def _raise_if_cancelled(should_cancel: CancelFn) -> None:
    if should_cancel():
        raise JobCancelled("Task cancelled by user.")


def _download_pdf(url: str, target: Path) -> bool:
    if not url or not url.startswith("http"):
        return False
    try:
        response = requests.get(url, timeout=45, headers={"User-Agent": "TASTE/0.1"})
        if response.status_code != 200 or "pdf" not in response.headers.get("content-type", "").lower():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
        return True
    except Exception:
        return False


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


def _fallback_reading(paper: dict, text: str) -> dict:
    abstract = paper.get("abstract", "")
    basis = text[:1200] if text else abstract
    return {
        "paper_id": paper.get("id", ""),
        "title": paper.get("title", "Untitled"),
        "url": paper.get("url", ""),
        "pdf_url": paper.get("pdf_url", ""),
        "summary": basis[:900] if basis else "No readable full text was available; using metadata only.",
        "problem": "Inferred from title and abstract.",
        "method": "Configure an LLM API key to generate a full structured reading.",
        "experiments": "Not extracted by fallback reader.",
        "limitations": "Fallback mode cannot reliably inspect all experimental details.",
        "relevance": paper.get("reason", ""),
    }


def run_read(request: ReadRequest, config: AppConfig, log: LogFn = print, should_cancel: CancelFn = lambda: False) -> dict:
    directory = run_dir(request.run_id)
    find_results = read_json(directory / "find_results.json", {})
    papers = find_results.get("articles", [])
    if request.paper_ids:
        selected = [paper for paper in papers if paper.get("id") in request.paper_ids]
    else:
        selected = papers[: request.max_papers]
    selected = selected[: request.max_papers]

    llm = LLMClient(config, "read")
    readings: list[dict] = []
    pdf_dir = directory / "pdfs"

    for paper in selected:
        _raise_if_cancelled(should_cancel)
        log(f"Reading: {paper.get('title', 'Untitled')}")
        pdf_path = pdf_dir / f"{paper.get('id', 'paper')}.pdf"
        downloaded = _download_pdf(paper.get("pdf_url", ""), pdf_path)
        text = _extract_pdf_text(pdf_path) if downloaded else ""
        reading = _fallback_reading(paper, text)
        if llm.enabled:
            source_text = text[:30000] if text else paper.get("abstract", "")
            prompt = f"""
Read this paper for a researcher. Return strict JSON with keys:
summary, problem, method, experiments, limitations, relevance.
Use Chinese.

Title: {paper.get('title')}
Abstract: {paper.get('abstract')}
Text excerpt:
{source_text}
"""
            data = llm.json_or_none(prompt)
            if isinstance(data, dict):
                reading.update({key: str(data.get(key, reading.get(key, ""))) for key in ["summary", "problem", "method", "experiments", "limitations", "relevance"]})
        reading["full_text_available"] = bool(text)
        readings.append(reading)

    _raise_if_cancelled(should_cancel)
    lines = ["# Paper Readings", ""]
    for index, item in enumerate(readings, 1):
        lines.extend([
            f"## {index}. {item['title']}",
            "",
            f"- **Paper ID**: `{item['paper_id']}`",
            f"- **URL**: {item.get('url', '')}",
            f"- **Full text available**: {item.get('full_text_available')}",
            "",
            "### Summary",
            item.get("summary", ""),
            "",
            "### Problem",
            item.get("problem", ""),
            "",
            "### Method",
            item.get("method", ""),
            "",
            "### Experiments",
            item.get("experiments", ""),
            "",
            "### Limitations",
            item.get("limitations", ""),
            "",
            "### Relevance",
            item.get("relevance", ""),
            "",
        ])

    write_json(directory / "read_results.json", {"run_id": request.run_id, "readings": readings})
    write_text(directory / "read.md", "\n".join(lines).rstrip() + "\n")
    sync_latest("auto_read", "read.md", directory / "read.md")
    update_manifest(directory, "read")
    log("Read stage complete")
    return {"run_id": request.run_id, "readings": readings}
