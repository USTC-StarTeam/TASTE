from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from scripts.common.io_utils import coerce_list, read_text_limited, utc_now, write_json
from scripts.common.shell import isolated_runtime_env, runtime_env

TEXT_KEYS = (
    "abstract", "summary", "paper_summary", "method", "methods", "methodology",
    "training", "train", "reproduction", "hyperparameters", "expected_results",
    "results", "evaluation", "paper_text", "paper_notes", "implementation_notes",
)
MAX_PAPER_SOURCE_BYTES = 50 * 1024 * 1024
LOCAL_PAPER_TEXT_SUFFIXES = {".txt", ".md", ".rst", ".tex", ".html", ".htm"}


def _compact_text(value: Any, limit: int = 12000) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, indent=2)
    else:
        text = str(value)
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    return text[:limit]


def _collect_nested_text(prefix: str, value: Any, out: list[dict[str, str]]) -> None:
    if value is None:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            child = f"{prefix}.{key_text}" if prefix else key_text
            if key_text in TEXT_KEYS or key_text in {"metrics", "target_metrics", "dataset", "datasets"}:
                text = _compact_text(item)
                if text:
                    out.append({"source": child, "text": text})
            if isinstance(item, (dict, list)):
                _collect_nested_text(child, item, out)
    elif isinstance(value, list):
        for index, item in enumerate(value[:20]):
            child = f"{prefix}[{index}]"
            if isinstance(item, (dict, list)):
                _collect_nested_text(child, item, out)
            else:
                text = _compact_text(item)
                if text and prefix.split(".")[-1] in TEXT_KEYS:
                    out.append({"source": child, "text": text})


def _strip_html(text: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_http_url(url: str) -> bool:
    return urlparse(str(url or "").strip()).scheme.lower() in {"http", "https"}


def _looks_like_pdf_path(value: str) -> bool:
    lowered = str(value or "").lower().split("?", 1)[0]
    return lowered.endswith(".pdf") or "/pdf/" in lowered


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except Exception:
        return 0


def _local_paper_rejection(path: Path) -> dict[str, Any]:
    size = _file_size(path)
    if size > MAX_PAPER_SOURCE_BYTES:
        return {"status": "rejected_file_too_large", "path": str(path), "size_bytes": size, "max_source_bytes": MAX_PAPER_SOURCE_BYTES}
    suffix = path.suffix.lower()
    if not _looks_like_pdf_path(str(path)) and suffix not in LOCAL_PAPER_TEXT_SUFFIXES:
        return {
            "status": "rejected_unsupported_file_type",
            "path": str(path),
            "suffix": suffix or "missing",
            "allowed_text_suffixes": sorted(LOCAL_PAPER_TEXT_SUFFIXES),
            "pdf_allowed": True,
        }
    return {}


def _extract_pdf_text(pdf_path: Path, text_path: Path, timeout_sec: int = 60, env: dict[str, str] | None = None) -> dict[str, Any]:
    runtime = env or runtime_env()
    pdftotext = shutil.which("pdftotext", path=runtime.get("PATH", ""))
    if not pdftotext:
        return {"text_status": "skipped_pdftotext_missing", "text_path": str(text_path), "text_excerpt": ""}
    text_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run([pdftotext, str(pdf_path), str(text_path)], cwd=str(text_path.parent), text=True, capture_output=True, timeout=timeout_sec, env=runtime)
    except Exception as exc:
        return {"text_status": "error", "text_path": str(text_path), "text_excerpt": "", "pdftotext_error": f"{type(exc).__name__}: {exc}"}
    return {
        "text_status": "passed" if proc.returncode == 0 else "failed",
        "pdftotext_return_code": proc.returncode,
        "text_path": str(text_path),
        "text_excerpt": read_text_limited(text_path, 30000) if proc.returncode == 0 else "",
        "pdftotext_stderr_tail": (proc.stderr or "")[-1200:],
    }


def _safe_fetch_url(url: str, target_dir: Path, timeout_sec: int = 60, env: dict[str, str] | None = None) -> dict[str, Any]:
    target_dir.mkdir(parents=True, exist_ok=True)
    if not _is_http_url(url):
        return {"status": "rejected_url_scheme", "reason": "论文 URL 只允许 http/https", "url": url}
    runtime = env or runtime_env()
    curl = shutil.which("curl", path=runtime.get("PATH", ""))
    if not curl:
        return {"status": "skipped", "reason": "curl 不可用", "url": url}
    is_pdf = _looks_like_pdf_path(url)
    suffix = ".pdf" if is_pdf else ".html"
    raw_path = target_dir / ("paper_source" + suffix)
    cmd = [curl, "-L", "--max-time", str(timeout_sec), "--max-filesize", str(MAX_PAPER_SOURCE_BYTES), "--fail", "--silent", "--show-error", url, "-o", str(raw_path)]
    try:
        proc = subprocess.run(cmd, cwd=str(target_dir), text=True, capture_output=True, timeout=timeout_sec + 20, env=runtime)
    except Exception as exc:
        return {"status": "error", "url": url, "error": f"{type(exc).__name__}: {exc}"}
    receipt = {"status": "passed" if proc.returncode == 0 else "failed", "return_code": proc.returncode, "url": url, "path": str(raw_path), "stderr_tail": (proc.stderr or "")[-1200:], "max_source_bytes": MAX_PAPER_SOURCE_BYTES}
    if proc.returncode != 0:
        return receipt
    size = _file_size(raw_path)
    receipt["size_bytes"] = size
    if size > MAX_PAPER_SOURCE_BYTES:
        try:
            raw_path.unlink(missing_ok=True)
        except Exception:
            pass
        receipt.update({"status": "rejected_file_too_large", "text_excerpt": ""})
        return receipt
    if is_pdf:
        receipt.update(_extract_pdf_text(raw_path, target_dir / "paper_source.txt", timeout_sec=timeout_sec, env=runtime))
        return receipt
    raw_text = read_text_limited(raw_path, 30000)
    receipt["text_excerpt"] = _strip_html(raw_text)[:30000]
    return receipt


def _local_paper_path(raw_plan: dict[str, Any]) -> str:
    for key in ["paper_path", "pdf_path", "local_paper", "local_pdf"]:
        value = str(raw_plan.get(key) or "").strip()
        if value:
            return value
    paper = raw_plan.get("paper") if isinstance(raw_plan.get("paper"), dict) else {}
    for key in ["path", "pdf_path", "local_path"]:
        value = str(paper.get(key) or "").strip()
        if value:
            return value
    return ""


def collect_paper_evidence(normalized_plan: dict[str, Any], run_dir: Path, allow_network: bool = False, timeout_sec: int = 60) -> dict[str, Any]:
    raw = normalized_plan.get("raw") if isinstance(normalized_plan.get("raw"), dict) else {}
    evidence_env = isolated_runtime_env(run_dir, isolate_home=True)
    text_blocks: list[dict[str, str]] = []
    _collect_nested_text("plan", raw, text_blocks)
    for key in ["target_metrics", "dataset", "training"]:
        value = normalized_plan.get(key)
        text = _compact_text(value)
        if text and text not in [row.get("text") for row in text_blocks]:
            text_blocks.append({"source": f"normalized.{key}", "text": text})

    local_path = _local_paper_path(raw)
    local_evidence: dict[str, Any] = {}
    if local_path:
        path = Path(local_path).expanduser()
        if path.exists() and path.is_file():
            local_evidence = _local_paper_rejection(path)
            if not local_evidence:
                local_evidence = {"status": "passed", "path": str(path), "size_bytes": _file_size(path), "max_source_bytes": MAX_PAPER_SOURCE_BYTES}
                if _looks_like_pdf_path(str(path)):
                    local_evidence.update(_extract_pdf_text(path, run_dir / "paper" / "local_paper.txt", timeout_sec=timeout_sec, env=evidence_env))
                    local_evidence["source_type"] = "pdf"
                else:
                    local_evidence["source_type"] = "text"
                    local_evidence["text_excerpt"] = read_text_limited(path, 30000)
                if local_evidence.get("text_excerpt"):
                    text_blocks.append({"source": f"local_file:{path}", "text": str(local_evidence["text_excerpt"])})
        else:
            local_evidence = {"status": "missing", "path": str(path)}

    url = str(normalized_plan.get("paper_url") or "").strip()
    url_evidence: dict[str, Any] = {"url": url, "status": "not_requested" if url else "missing"}
    if url and allow_network:
        url_evidence = _safe_fetch_url(url, run_dir / "paper", timeout_sec=timeout_sec, env=evidence_env)
        excerpt = str(url_evidence.get("text_excerpt") or "").strip()
        if excerpt:
            text_blocks.append({"source": f"url:{url}", "text": excerpt})

    claims: list[str] = []
    for row in text_blocks:
        text = row.get("text", "")
        for line in text.splitlines():
            clean = line.strip(" -\t")
            lowered = clean.lower()
            if not clean or len(clean) < 12:
                continue
            if any(token in lowered for token in ["accuracy", "auc", "f1", "loss", "epoch", "batch", "learning rate", "lr", "dataset", "训练", "指标", "数据集", "复现"]):
                if clean not in claims:
                    claims.append(clean[:500])
            if len(claims) >= 80:
                break
        if len(claims) >= 80:
            break

    has_context = bool(text_blocks or normalized_plan.get("target_metrics") or normalized_plan.get("training") or normalized_plan.get("dataset"))
    payload = {
        "schema_version": "environment.paper_evidence.v1",
        "created_at": utc_now(),
        "has_paper_context": has_context,
        "paper_url": url,
        "local_paper": local_evidence,
        "url_fetch": url_evidence,
        "text_blocks": text_blocks[:40],
        "paper_claims_or_training_signals": claims,
        "target_metrics": normalized_plan.get("target_metrics") or [],
        "dataset": normalized_plan.get("dataset") or [],
        "training": normalized_plan.get("training") or {},
        "limits": {"max_source_bytes": MAX_PAPER_SOURCE_BYTES, "local_text_suffixes": sorted(LOCAL_PAPER_TEXT_SUFFIXES), "local_pdf_allowed": True, "fetch_env_isolated": True},
        "note": "该证据包只收集 plan/受限本地文本或 PDF 文件/可选 http/https URL 文本，最终是否满足论文复现仍由 Claude Code 和后端命令证据共同裁决。",
    }
    write_json(run_dir / "paper_evidence.json", payload)
    return payload
