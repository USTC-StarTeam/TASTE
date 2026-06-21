#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlencode

import requests

from project_paths import build_paths


FULL_TEXT_MIN_CHARS = 1200
PAPER_BODY_MIN_CHARS = 8000
USER_AGENT = "research-workflow/full-text-repair"
REQUEST_TIMEOUT_SEC = max(5, int(os.environ.get("FULL_TEXT_REQUEST_TIMEOUT_SEC", "18")))
FULL_TEXT_REPAIR_TIMEOUT_SEC = max(60, int(os.environ.get("FULL_TEXT_REPAIR_TIMEOUT_SEC", "360")))
ARXIV_SEARCH_QUERY_VERSION = "v4_latex_unicode_title_variants"
ARXIV_MAX_SEARCH_QUERIES = 4
ARXIV_SEARCH_COOLDOWN_SEC = 0.75
RETRYABLE_FETCH_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
try:
    FULL_TEXT_FETCH_ATTEMPTS = max(1, int(os.environ.get("FULL_TEXT_FETCH_ATTEMPTS", "4") or 4))
except Exception:
    FULL_TEXT_FETCH_ATTEMPTS = 4
try:
    FULL_TEXT_FETCH_RETRY_BASE_DELAY_SEC = max(0.0, float(os.environ.get("FULL_TEXT_FETCH_RETRY_BASE_DELAY_SEC", "2.0") or 2.0))
except Exception:
    FULL_TEXT_FETCH_RETRY_BASE_DELAY_SEC = 2.0
try:
    FULL_TEXT_FETCH_RETRY_MAX_DELAY_SEC = max(0.0, float(os.environ.get("FULL_TEXT_FETCH_RETRY_MAX_DELAY_SEC", "12.0") or 12.0))
except Exception:
    FULL_TEXT_FETCH_RETRY_MAX_DELAY_SEC = 12.0
def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _retry_after_delay(response: requests.Response | None, attempt_index: int) -> float:
    header = ""
    if response is not None:
        header = str(response.headers.get("retry-after") or "").strip()
    delay = 0.0
    if header:
        try:
            delay = max(0.0, float(header))
        except Exception:
            delay = 0.0
    if delay <= 0:
        delay = FULL_TEXT_FETCH_RETRY_BASE_DELAY_SEC * (2 ** max(0, attempt_index - 1))
    return min(FULL_TEXT_FETCH_RETRY_MAX_DELAY_SEC, delay)


def _request_get(
    url: str,
    *,
    timeout: int = REQUEST_TIMEOUT_SEC,
    params: dict[str, str] | None = None,
    allow_redirects: bool = True,
    headers: dict[str, str] | None = None,
) -> tuple[requests.Response | None, dict[str, Any]]:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    attempts = max(1, FULL_TEXT_FETCH_ATTEMPTS)
    retry_statuses: list[int] = []
    last_error = ""
    response: requests.Response | None = None
    for attempt_index in range(1, attempts + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=timeout,
                headers=request_headers,
                allow_redirects=allow_redirects,
            )
            if response.status_code not in RETRYABLE_FETCH_STATUS_CODES:
                return response, {"attempt_count": attempt_index, "retry_statuses": retry_statuses}
            retry_statuses.append(response.status_code)
            if attempt_index >= attempts:
                return response, {"attempt_count": attempt_index, "retry_statuses": retry_statuses}
            delay = _retry_after_delay(response, attempt_index)
        except Exception as exc:
            response = None
            last_error = exc.__class__.__name__
            if attempt_index >= attempts:
                return None, {"attempt_count": attempt_index, "retry_statuses": retry_statuses, "error": last_error}
            delay = _retry_after_delay(None, attempt_index)
        if delay > 0:
            time.sleep(delay)
    return response, {"attempt_count": attempts, "retry_statuses": retry_statuses, "error": last_error}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def norm_title(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


LATEX_SYMBOL_NAMES = {
    "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta", "theta", "vartheta",
    "iota", "kappa", "lambda", "mu", "nu", "xi", "pi", "rho", "sigma", "tau", "upsilon", "phi",
    "varphi", "chi", "psi", "omega",
}

UNICODE_TITLE_REPLACEMENTS = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon", "θ": "theta",
    "λ": "lambda", "μ": "mu", "π": "pi", "ρ": "rho", "σ": "sigma", "τ": "tau", "φ": "phi", "ω": "omega",
    "Α": "Alpha", "Β": "Beta", "Γ": "Gamma", "Δ": "Delta", "Θ": "Theta", "Λ": "Lambda", "Π": "Pi", "Σ": "Sigma", "Τ": "Tau", "Φ": "Phi", "Ω": "Omega",
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
    "–": "-", "—": "-", "−": "-",
}


def latex_plain_title(value: Any) -> str:
    text = str(value or "")
    for src, dst in UNICODE_TITLE_REPLACEMENTS.items():
        text = text.replace(src, dst)
    text = text.replace("\\\\", "\\")
    text = re.sub(r"\\(?:texttt|textbf|emph|mathrm|mathbf|mathsf|operatorname)\{([^{}]+)\}", r"\1", text)
    for name in sorted(LATEX_SYMBOL_NAMES, key=len, reverse=True):
        text = re.sub(rf"\\{name}\b", name, text)
    text = re.sub(r"\$+", " ", text)
    text = re.sub(r"\{([^{}]+)\}", r"\1", text)
    text = re.sub(r"([A-Za-z]+)\s*\^\s*([0-9]+)", r"\1\2", text)
    text = re.sub(r"([A-Za-z]+)\s*\^\s*([A-Za-z]+)", r"\1 \2", text)
    text = re.sub(r"[^A-Za-z0-9:;,.+\-/ ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_query_variants(value: Any) -> list[str]:
    original = " ".join(str(value or "").split())
    plain = latex_plain_title(original)
    variants = [plain, original]
    if plain:
        variants.append(re.sub(r"\b([A-Za-z]+)([0-9]+)\b", r"\1 \2", plain))
        variants.append(re.sub(r"\b([A-Za-z])\s+([0-9]+)\s*([A-Za-z]+)\b", r"\1\2\3", plain))
        variants.append(re.sub(r"\b(tau)\s+([0-9]+)\b", r"\1\2", plain, flags=re.I))
    out: list[str] = []
    for item in variants:
        item = re.sub(r"\s+", " ", str(item or "").strip())
        item = item.strip(" :;,.+-")
        if item and item not in out:
            out.append(item)
    return out


def safe_slug(value: Any, fallback: str = "paper") -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or fallback)).strip("_")
    return (text or fallback)[:80]


def identity_values(row: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ["id", "paper_id", "entry_id", "url", "abs_url", "pdf_url", "doi"]:
        value = str(row.get(key) or "").strip().lower()
        if value:
            values.add(f"{key}:{value}")
    raw_title = row.get("title") or row.get("paper_title")
    title = norm_title(raw_title)
    if title:
        values.add(f"title:{title}")
    plain_title = norm_title(latex_plain_title(raw_title))
    if plain_title and plain_title != title:
        values.add(f"title:{plain_title}")
    return values


def dedupe_rows(rows: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        keys = identity_values(row)
        key = sorted(keys)[0] if keys else norm_title(row.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def current_recommendations(find_results: dict[str, Any]) -> list[dict[str, Any]]:
    return dedupe_rows(as_list(find_results.get("strong_recommendations")) + as_list(find_results.get("articles")))


READ_REPLACEMENT_CANDIDATE_POOLS = (
    "screened_ranking",
    "read_candidates",
    "evaluated_candidates",
    "critique_candidates",
    "title_candidates",
)
NON_REPLACEMENT_EVIDENCE_TIERS = {
    "weak_or_boundary",
    "retrieval_only",
    "nethreshold_for_reading",
    "critique_or_boundary_case",
    "audit_or_search_expansion_only",
}
NON_REPLACEMENT_ROLES = {"weak_or_boundary", "negative", "critique_only"}


def numeric_score(row: dict[str, Any]) -> float:
    for key in ["recommendation_score", "score", "fit_score", "llm_fit_score"]:
        try:
            return float(row.get(key))
        except (TypeError, ValueError):
            continue
    return 0.0


def read_stage_replacement_candidate_ok(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict) or not str(row.get("title") or row.get("paper_title") or "").strip():
        return False
    if row.get("weak_candidate_for_critique") or row.get("not_positive_support") or row.get("foundation_demoted_from_strong"):
        return False
    if row.get("topic_evidence_supported") is False:
        return False
    tier = str(row.get("evidence_tier") or "").strip().lower()
    role = str(row.get("evidence_role") or "").strip().lower()
    if tier in NON_REPLACEMENT_EVIDENCE_TIERS or role in NON_REPLACEMENT_ROLES:
        return False
    if not str(row.get("abstract") or row.get("abstract_en") or row.get("summary") or "").strip():
        return False
    return True


def read_stage_replacement_candidates(find_results: dict[str, Any], exclude_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    excluded: set[str] = set()
    for row in exclude_rows:
        if isinstance(row, dict):
            excluded.update(identity_values(row))
    out: list[dict[str, Any]] = []
    seen: set[str] = set(excluded)
    for pool in READ_REPLACEMENT_CANDIDATE_POOLS:
        for index, raw in enumerate(as_list((find_results if isinstance(find_results, dict) else {}).get(pool)), 1):
            if not isinstance(raw, dict) or not read_stage_replacement_candidate_ok(raw):
                continue
            keys = identity_values(raw)
            if not keys or keys & seen:
                continue
            seen.update(keys)
            row = dict(raw)
            row["read_replacement_source_pool"] = pool
            row["read_replacement_source_rank"] = index
            row.setdefault("taste_pool", pool)
            row.setdefault("taste_pool_rank", index)
            out.append(row)
    return out


def update_packet_entry_with_evidence(entry: dict[str, Any], paper: dict[str, Any], title: str, evidence: dict[str, Any], *, replacement_meta: dict[str, Any] | None = None) -> None:
    text_chars = int(evidence.get("text_chars") or 0)
    entry.update({
        "title": paper.get("title") or paper.get("paper_title") or title,
        "paper_id": paper.get("paper_id") or paper.get("id") or entry.get("paper_id") or safe_slug(title),
        "url": paper.get("url") or paper.get("abs_url") or entry.get("url") or "",
        "pdf_url": evidence.get("pdf_url") or paper.get("pdf_url") or entry.get("pdf_url") or "",
        "text_path": evidence.get("text_path") or entry.get("text_path") or "",
        "pdf_path": evidence.get("pdf_path") or entry.get("pdf_path") or "",
        "html_url": evidence.get("html_url") or entry.get("html_url") or "",
        "text_chars": text_chars,
        "pdf_text_chars": text_chars,
        "full_text_chars": text_chars,
        "page_count": evidence.get("page_count") or 0,
        "pdf_status": evidence.get("full_text_status") or evidence.get("kind") or "full_text_read",
        "full_text_status": evidence.get("full_text_status") or "full_text_read",
        "repair_status": "full_text_evidence_acquired",
        "acquired_at": now_iso(),
        "acquisition_source": evidence.get("source") or "repair_current_find_full_text_evidence.py",
    })
    for key in [
        "abstract", "abstract_en", "abstract_zh", "summary", "summary_zh", "authors", "venue", "year",
        "score", "fit_score", "recommendation_score", "topic_evidence_supported", "evidence_tier", "evidence_role",
        "hit_directions", "hit_directions_zh", "fit_explanation", "fit_explanation_zh", "reason", "reason_zh",
    ]:
        if paper.get(key) not in (None, "", []):
            entry[key] = paper.get(key)
    if replacement_meta:
        entry.update(replacement_meta)


def pending_titles(validation: dict[str, Any]) -> list[str]:
    titles = [str(item).strip() for item in as_list(validation.get("pending_without_evidence_titles")) if str(item or "").strip()]
    if titles:
        return titles
    return [str(item).strip() for item in as_list(validation.get("pending_full_text_reading_titles")) if str(item or "").strip()]


def packet_entry_has_text(entry: dict[str, Any]) -> bool:
    if not isinstance(entry, dict):
        return False
    chars = int(entry.get("text_chars") or entry.get("pdf_text_chars") or entry.get("full_text_chars") or 0)
    return chars >= FULL_TEXT_MIN_CHARS and bool(str(entry.get("text_path") or "").strip())


def packet_entry_for_paper(packet: dict[str, Any], paper: dict[str, Any]) -> dict[str, Any]:
    index = packet_index(packet if isinstance(packet, dict) else {})
    for key in identity_values(paper):
        if key in index:
            return index[key]
    return {}


def packet_missing_titles(find_results: dict[str, Any], packet: dict[str, Any], run_id: str) -> list[str]:
    recommendations = current_recommendations(find_results if isinstance(find_results, dict) else {})
    if not recommendations:
        return []
    packet_run_id = str((packet if isinstance(packet, dict) else {}).get("run_id") or (packet if isinstance(packet, dict) else {}).get("current_find_run_id") or "").strip()
    if packet_run_id and run_id and packet_run_id != run_id:
        return [str(row.get("title") or row.get("paper_title") or "Untitled").strip() for row in recommendations]
    missing: list[str] = []
    for row in recommendations:
        entry = packet_entry_for_paper(packet if isinstance(packet, dict) else {}, row)
        if not packet_entry_has_text(entry):
            missing.append(str(row.get("title") or row.get("paper_title") or "Untitled").strip())
    return missing


def find_row_for_title(rows: list[dict[str, Any]], title: str) -> dict[str, Any]:
    wanted = norm_title(title)
    for row in rows:
        if norm_title(row.get("title") or row.get("paper_title")) == wanted:
            return row
    return {}


def full_text_reading_dir(paths: Any) -> Path:
    custom = getattr(paths, "full_text_reading_dir", None) or getattr(paths, "full_text_base", None)
    if custom:
        return Path(custom)
    return paths.planning / "finding" / "full_text_reading"


def full_text_packet_path(paths: Any) -> Path:
    return full_text_reading_dir(paths) / "full_text_packet.json"


def packet_index(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in as_list(packet.get("papers")):
        if not isinstance(row, dict):
            continue
        for key in identity_values(row):
            index[key] = row
    return index


def save_repair_progress(
    packet_path: Path,
    receipt_path: Path,
    packet: dict[str, Any],
    *,
    project: str,
    run_id: str,
    pending: list[str],
    acquired: list[dict[str, Any]],
    unavailable: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    current_title: str = "",
    status: str = "full_text_evidence_repair_running",
    validation_generated_at: str = "",
) -> dict[str, Any]:
    packet["run_id"] = run_id
    packet.setdefault("source", "repair_current_find_full_text_evidence.py")
    packet["updated_at"] = now_iso()
    packet["repair_source"] = "repair_current_find_full_text_evidence.py"
    save_json(packet_path, packet)
    processed_titles = [str(row.get("title") or "").strip() for row in acquired + unavailable if str(row.get("title") or "").strip()]
    receipt = {
        "project": project,
        "run_id": run_id,
        "status": status,
        "generated_at": now_iso(),
        "validation_generated_at": validation_generated_at,
        "pending_titles": pending,
        "processed_count": len(processed_titles),
        "remaining_count": max(0, len(pending) - len(processed_titles)),
        "current_title": current_title,
        "acquired_count": len(acquired),
        "unavailable_count": len(unavailable),
        "acquired": acquired,
        "unavailable": unavailable,
        "attempts_tail": attempts[-10:],
        "files": {"full_text_packet": str(packet_path), "receipt": str(receipt_path)},
        "policy": "Full-text repair writes same-run packet progress after each paper so web state never shows a stale packet while evidence acquisition is active.",
    }
    save_json(receipt_path, receipt)
    return receipt


def ensure_packet_entry(packet: dict[str, Any], paper: dict[str, Any], rank: int) -> dict[str, Any]:
    papers = packet.setdefault("papers", [])
    if not isinstance(papers, list):
        papers = []
        packet["papers"] = papers
    index = packet_index(packet)
    for key in identity_values(paper):
        if key in index:
            return index[key]
    entry = {
        "title": paper.get("title") or paper.get("paper_title") or "Untitled",
        "paper_id": paper.get("paper_id") or paper.get("id") or f"paper_{rank}",
        "url": paper.get("url") or paper.get("abs_url") or "",
        "pdf_url": paper.get("pdf_url") or "",
        "text_chars": 0,
        "page_count": 0,
        "text_path": "",
        "pdf_status": "missing_pdf_url" if not paper.get("pdf_url") else "pending_download",
    }
    papers.append(entry)
    return entry


def extract_pdf_text(pdf_path: Path, max_chars: int = 140000) -> tuple[str, int]:
    try:
        import fitz  # type: ignore
    except Exception:
        return "", 0
    try:
        doc = fitz.open(pdf_path)
        chunks: list[str] = []
        total = 0
        for page in doc:
            text = page.get_text("text")
            chunks.append(text)
            total += len(text)
            if total >= max_chars:
                break
        return "\n".join(chunks)[:max_chars], len(doc)
    except Exception:
        return "", 0


def fetch_url(url: str, timeout: int = REQUEST_TIMEOUT_SEC) -> tuple[int, str, bytes, str]:
    if not url.startswith("http"):
        return 0, "", b"", "missing_url"
    response, meta = _request_get(url, timeout=timeout, allow_redirects=True)
    if response is None:
        return 0, "", b"", str(meta.get("error") or "request_failed")
    return response.status_code, response.headers.get("content-type", ""), response.content, response.url


def html_to_text(content: bytes) -> str:
    text = content.decode("utf-8", errors="ignore")
    text = re.sub(r"(?is)<script.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def links_from_paper(row: dict[str, Any]) -> list[str]:
    blob = "\n".join(str(row.get(key) or "") for key in ["url", "pdf_url", "abstract", "summary", "reason", "reason_zh", "fit_explanation", "fit_explanation_zh"])
    links: list[str] = []
    for match in re.finditer(r"https?://[^\s\]\)\}\>,;]+", blob):
        link = match.group(0).rstrip(".")
        if link not in links:
            links.append(link)
    return links


def github_raw_readme_candidates(url: str) -> list[str]:
    match = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)", url)
    if not match:
        return []
    owner, repo = match.group(1), match.group(2).removesuffix(".git")
    return [
        f"https://raw.githubusercontent.com/{owner}/{repo}/main/README.md",
        f"https://raw.githubusercontent.com/{owner}/{repo}/master/README.md",
        f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md",
    ]


def title_tokens(value: Any) -> set[str]:
    stopwords = {"the", "and", "for", "with", "from", "into", "that", "this", "using", "based", "large", "language", "models", "model", "texttt", "mathrm", "mathbf"}
    normalized = norm_title(latex_plain_title(value))
    tokens = {token for token in re.findall(r"[a-z0-9]{2,}", normalized) if token not in stopwords}
    expanded: set[str] = set(tokens)
    for token in tokens:
        match = re.match(r"([a-z]+)([0-9]+)$", token)
        if match:
            expanded.update({match.group(1), match.group(2)})
        match = re.match(r"([a-z])([0-9]+)([a-z]+)$", token)
        if match:
            expanded.update({match.group(1), match.group(2), match.group(3), match.group(1) + match.group(2)})
    return expanded


def title_similarity(a: Any, b: Any) -> float:
    left = title_tokens(a)
    right = title_tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def doi_from_text(value: Any) -> str:
    match = re.search(r"10\.\d{4,9}/[^\s\"<>]+", str(value or ""))
    return match.group(0).rstrip(".,);]") if match else ""


def doi_from_paper(row: dict[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    for value in [row.get("doi"), metadata.get("doi"), metadata.get("doi_url"), metadata.get("publisher_url"), row.get("url"), row.get("pdf_url")]:
        doi = doi_from_text(value)
        if doi:
            return doi
    return ""


def acm_ids_from_doi(doi: str) -> tuple[str, str]:
    match = re.match(r"10\.1145/(\d+)(?:\.(\d+))?", doi.strip(), flags=re.I)
    if not match:
        return "", ""
    proceedings_id = match.group(1) or ""
    article_id = match.group(2) or proceedings_id
    return proceedings_id, article_id


def acm_full_text_candidates(doi: str) -> list[dict[str, str]]:
    proceedings_id, article_id = acm_ids_from_doi(doi)
    if not article_id:
        return []
    return [
        {"kind": "acm_abs_html", "url": f"https://dl.acm.org/doi/abs/{doi}"},
        {"kind": "acm_full_html", "url": f"https://dl.acm.org/doi/fullHtml/{doi}"},
        {"kind": "acm_pdf", "url": f"https://dl.acm.org/doi/pdf/{doi}"},
        {"kind": "acm_epdf", "url": f"https://dl.acm.org/doi/epdf/{doi}"},
        {"kind": "acm_legacy_pdf", "url": f"https://dl.acm.org/ft_gateway.cfm?id={article_id}&type=pdf"},
    ]


def publisher_full_text_candidates(row: dict[str, Any]) -> list[dict[str, str]]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    doi = doi_from_paper(row)
    candidates: list[dict[str, str]] = []
    if doi:
        candidates.append({"kind": "publisher_doi", "url": f"https://doi.org/{doi}", "doi": doi})
        if doi.lower().startswith("10.1145/"):
            candidates.extend({**item, "doi": doi} for item in acm_full_text_candidates(doi))
    for key, kind in [
        ("acm_abs_url", "acm_abs_html"),
        ("acm_full_html_url", "acm_full_html"),
        ("acm_pdf_url", "acm_pdf"),
        ("acm_epdf_url", "acm_epdf"),
        ("acm_legacy_pdf_url", "acm_legacy_pdf"),
        ("doi_url", "publisher_doi"),
        ("publisher_url", "publisher_page"),
    ]:
        value = str(metadata.get(key) or "").strip()
        if value.startswith("http"):
            candidates.append({"kind": kind, "url": value, "doi": doi})
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for candidate in candidates:
        url = candidate.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(candidate)
    return unique


def fetch_block_reason(status: int, content_type: str, content: bytes, final_url: str) -> str:
    lowered = content[:4000].lower()
    if status == 0:
        return final_url or "request_failed"
    if status == 403 and (b"cloudflare" in lowered or b"just a moment" in lowered):
        return "cloudflare_challenge"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status >= 400:
        return f"http_{status}"
    if "pdf" not in content_type.lower() and not content.startswith(b"%PDF"):
        return "not_pdf_response"
    return "pdf_text_contract_not_satisfied"


def author_family_tokens(value: Any) -> set[str]:
    if isinstance(value, str):
        raw_names = re.split(r",|;| and ", value)
    elif isinstance(value, list):
        raw_names = [str(item) for item in value]
    else:
        raw_names = []
    tokens: set[str] = set()
    for name in raw_names:
        parts = re.findall(r"[A-Za-z][A-Za-z'-]+", name.lower())
        if parts:
            tokens.add(parts[-1])
    return tokens


def openalex_title(item: dict[str, Any]) -> str:
    return str(item.get("display_name") or item.get("title") or "")


def openalex_author_family_tokens(item: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for authorship in as_list(item.get("authorships")):
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") if isinstance(authorship.get("author"), dict) else {}
        tokens.update(author_family_tokens(str(author.get("display_name") or "")))
    return tokens


def openalex_pdf_url(item: dict[str, Any]) -> str:
    candidates: list[str] = []
    primary = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
    candidates.append(str(primary.get("pdf_url") or ""))
    open_access = item.get("open_access") if isinstance(item.get("open_access"), dict) else {}
    candidates.append(str(open_access.get("oa_url") or ""))
    for loc in as_list(item.get("locations")):
        if isinstance(loc, dict):
            candidates.append(str(loc.get("pdf_url") or ""))
    for url in candidates:
        url = url.strip()
        if url and (".pdf" in url.lower() or "/pdf/" in url.lower()):
            return url
    return ""


def openalex_landing_url(item: dict[str, Any]) -> str:
    primary = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
    return str(primary.get("landing_page_url") or item.get("doi") or item.get("id") or "")


def title_author_match_details(row: dict[str, Any], candidate_title: Any, candidate_authors: Any) -> dict[str, Any]:
    similarity = title_similarity(row.get("title") or row.get("paper_title"), candidate_title)
    expected = author_family_tokens(row.get("authors"))
    candidate = author_family_tokens(candidate_authors)
    overlap = sorted(expected & candidate)
    if expected:
        required_overlap = 1 if len(expected) <= 2 else 3
        strong_author_match = len(overlap) >= required_overlap
        accepted = bool(overlap) and (similarity >= 0.82 or (similarity >= 0.75 and strong_author_match))
    else:
        accepted = similarity >= 0.95
    return {"title_similarity": similarity, "author_overlap": overlap, "accepted": accepted}


_OPENREVIEW_SCAN_CACHE: dict[str, list[dict[str, Any]]] = {}


def _content_value(content: dict[str, Any], key: str) -> Any:
    value = content.get(key)
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _openreview_note_title(note: dict[str, Any]) -> str:
    content = note.get("content") if isinstance(note.get("content"), dict) else {}
    return str(_content_value(content, "title") or "").strip()


def _openreview_note_authors(note: dict[str, Any]) -> list[str]:
    content = note.get("content") if isinstance(note.get("content"), dict) else {}
    authors = _content_value(content, "authors")
    if isinstance(authors, list):
        return [str(author) for author in authors if str(author or "").strip()]
    if isinstance(authors, str):
        return [part.strip() for part in re.split(r",|;| and ", authors) if part.strip()]
    return []


def _openreview_pdf_url_from_note(note: dict[str, Any]) -> str:
    note_id = str(note.get("id") or note.get("forum") or "").strip()
    return f"https://openreview.net/pdf?id={note_id}" if note_id else ""


def _openreview_scan_specs(row: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    years: list[int] = []
    try:
        year = int(row.get("year") or 0)
        if year > 2000:
            years.append(year)
    except Exception:
        pass
    current_year = datetime.now(UTC).year
    for year in [current_year, current_year - 1, current_year + 1]:
        if year > 2000 and year not in years:
            years.append(year)
    specs: list[tuple[str, dict[str, str]]] = []
    for year in years[:4]:
        specs.extend([
            (f"openreview_iclr_{year}_submissions", {"invitations": f"ICLR.cc/{year}/Conference/-/Submission"}),
            (f"openreview_iclr_{year}_accepted", {"content.venueid": f"ICLR.cc/{year}/Conference"}),
            (f"openreview_neurips_{year}_accepted", {"content.venueid": f"NeurIPS.cc/{year}/Conference"}),
        ])
    return specs


def _fetch_openreview_scan(spec_name: str, params: dict[str, str]) -> list[dict[str, Any]]:
    cache_key = spec_name + ":" + json.dumps(params, sort_keys=True)
    if cache_key in _OPENREVIEW_SCAN_CACHE:
        return _OPENREVIEW_SCAN_CACHE[cache_key]
    try:
        page_size = max(100, min(1000, int(os.environ.get("OPENREVIEW_REPAIR_PAGE_SIZE", "1000") or 1000)))
    except Exception:
        page_size = 1000
    try:
        max_notes = max(page_size, int(os.environ.get("OPENREVIEW_REPAIR_SCAN_MAX", "20000") or 20000))
    except Exception:
        max_notes = 20000
    notes: list[dict[str, Any]] = []
    for offset in range(0, max_notes, page_size):
        query = {**params, "limit": str(page_size), "offset": str(offset)}
        response, _meta = _request_get("https://api2.openreview.net/notes", params=query, timeout=REQUEST_TIMEOUT_SEC)
        if response is None or response.status_code != 200:
            break
        payload = response.json()
        page_notes = payload.get("notes") if isinstance(payload, dict) and isinstance(payload.get("notes"), list) else []
        notes.extend(note for note in page_notes if isinstance(note, dict))
        if len(page_notes) < page_size:
            break
    _OPENREVIEW_SCAN_CACHE[cache_key] = notes
    return notes


def openreview_title_candidates(row: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    title = str(row.get("title") or row.get("paper_title") or "").strip()
    attempts: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    if not title:
        return candidates, attempts
    for spec_name, params in _openreview_scan_specs(row):
        notes = _fetch_openreview_scan(spec_name, params)
        attempts.append({"kind": "openreview_title_scan", "scan": spec_name, "params": params, "status_code": 200 if notes else 0, "note_count": len(notes), "accepted": False})
        for note in notes:
            note_title = _openreview_note_title(note)
            if not note_title:
                continue
            match = title_author_match_details(row, note_title, _openreview_note_authors(note))
            pdf_url = _openreview_pdf_url_from_note(note)
            if not match["accepted"] or not pdf_url:
                continue
            candidate = {
                "kind": "openreview_title_candidate",
                "scan": spec_name,
                "note_id": note.get("id") or "",
                "forum": note.get("forum") or note.get("id") or "",
                "pdf_url": pdf_url,
                "url": f"https://openreview.net/forum?id={note.get('forum') or note.get('id')}",
                "matched_title": note_title,
                "title_similarity": match["title_similarity"],
                "author_overlap": match["author_overlap"],
                "accepted": True,
            }
            attempts.append(candidate)
            candidates.append(candidate)
        if candidates:
            break
    return candidates, attempts


def manual_full_text_candidates(paths: Any, row: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_path = full_text_reading_dir(paths) / "manual_full_text_sources.json"
    payload = load_json(source_path, [])
    records = payload.get("sources") if isinstance(payload, dict) else payload
    attempts: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for record in as_list(records):
        if not isinstance(record, dict):
            continue
        url = str(record.get("pdf_url") or record.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        candidate_title = record.get("title") or record.get("paper_title") or ""
        candidate_authors = record.get("authors") or []
        match = title_author_match_details(row, candidate_title, candidate_authors)
        attempt = {
            "kind": "manual_full_text_source_candidate",
            "url": url,
            "matched_title": candidate_title,
            "title_similarity": match["title_similarity"],
            "author_overlap": match["author_overlap"],
            "accepted": bool(match["accepted"]),
            "source_file": str(source_path),
        }
        attempts.append(attempt)
        if match["accepted"]:
            candidates.append({**attempt, "pdf_url": url, "source_note": record.get("note") or record.get("source_note") or ""})
    return candidates, attempts


def openalex_match_details(row: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return title_author_match_details(row, openalex_title(item), sorted(openalex_author_family_tokens(item)))


def openalex_repository_candidates(row: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    title = str(row.get("title") or row.get("paper_title") or "").strip()
    doi = doi_from_paper(row)
    attempts: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    def consume_payload(payload: Any, source_kind: str, source_url: str) -> None:
        raw_items = payload.get("results") if isinstance(payload, dict) and isinstance(payload.get("results"), list) else [payload]
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            match = openalex_match_details(row, item)
            pdf_url = openalex_pdf_url(item)
            attempt = {
                "kind": f"{source_kind}_candidate",
                "url": source_url,
                "openalex_id": item.get("id") or "",
                "openalex_doi": item.get("doi") or "",
                "matched_title": openalex_title(item),
                "landing_url": openalex_landing_url(item),
                "pdf_url": pdf_url,
                "title_similarity": match["title_similarity"],
                "author_overlap": match["author_overlap"],
                "accepted": bool(match["accepted"] and pdf_url),
            }
            attempts.append(attempt)
            if attempt["accepted"]:
                candidates.append({**attempt, "item": item})

    if doi:
        url = f"https://api.openalex.org/works/doi:{doi}"
        response, meta = _request_get(url, timeout=REQUEST_TIMEOUT_SEC)
        attempts.append({"kind": "openalex_doi_lookup", "url": url, "status_code": response.status_code if response is not None else 0, "accepted": False, "error": meta.get("error", "")})
        if response is not None and response.status_code == 200:
            try:
                consume_payload(response.json(), "openalex_doi_lookup", url)
            except Exception as exc:
                attempts.append({"kind": "openalex_doi_lookup_parse", "url": url, "status_code": response.status_code, "accepted": False, "error": exc.__class__.__name__})
    if title and not candidates:
        for query_title in title_query_variants(title):
            url = f"https://api.openalex.org/works?search={quote_plus(query_title)}&per-page=5"
            response, meta = _request_get(url, timeout=REQUEST_TIMEOUT_SEC)
            attempts.append({"kind": "openalex_title_search", "url": url, "query_title": query_title, "status_code": response.status_code if response is not None else 0, "accepted": False, "error": meta.get("error", "")})
            if response is not None and response.status_code == 200:
                try:
                    consume_payload(response.json(), "openalex_title_search", url)
                except Exception as exc:
                    attempts.append({"kind": "openalex_title_search_parse", "url": url, "query_title": query_title, "status_code": response.status_code, "accepted": False, "error": exc.__class__.__name__})
            if candidates:
                break
    return candidates, attempts


def download_pdf_text(paths: Any, rank: int, paper: dict[str, Any], pdf_url: str, *, kind: str, suffix: str = "") -> tuple[dict[str, Any] | None, dict[str, Any]]:
    pdf_dir = full_text_reading_dir(paths) / "pdfs"
    status, content_type, content, final_url = fetch_url(pdf_url)
    attempt = {"kind": kind, "url": pdf_url, "status_code": status, "content_type": content_type, "final_url": final_url}
    if status == 200 and ("pdf" in content_type.lower() or content.startswith(b"%PDF")):
        pdf_dir.mkdir(parents=True, exist_ok=True)
        stem = safe_slug(paper.get("id") or paper.get("paper_id") or kind)
        suffix_text = f"_{safe_slug(suffix)}" if suffix else ""
        pdf_path = pdf_dir / f"{rank:02d}_{stem}{suffix_text}.pdf"
        pdf_path.write_bytes(content)
        text, pages = extract_pdf_text(pdf_path)
        attempt.update({"pdf_path": str(pdf_path), "text_chars": len(text), "page_count": pages})
        if text_looks_like_paper(text, str(paper.get("title") or paper.get("paper_title") or "")):
            text_path = write_text_evidence(paths, rank, paper, text)
            return {
                "source": "repair_current_find_full_text_evidence.py",
                "kind": kind,
                "pdf_url": pdf_url,
                "pdf_path": str(pdf_path),
                "text_path": text_path,
                "text_chars": len(text),
                "page_count": pages,
                "full_text_status": kind,
            }, {**attempt, "accepted": True}
    return None, {**attempt, "accepted": False, "reason": fetch_block_reason(status, content_type, content, final_url)}


def arxiv_search_queries(title: str) -> list[str]:
    queries: list[str] = []
    for variant in title_query_variants(title):
        clean_title = " ".join(re.findall(r"[A-Za-z0-9]+", variant or ""))
        terms = [
            term
            for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", variant or "")
            if len(term) >= 3 and term.lower() not in {"and", "for", "the", "with", "via", "texttt"}
        ]
        lowered_terms = {term.lower().replace("-", "") for term in terms}
        token_set = title_tokens(variant)
        if "tau2" in token_set or ("tau" in token_set and "bench" in token_set):
            queries.extend(['all:"tau2 Bench"', 'all:"tau 2 Bench"', 'all:"Dual Control Environment"'])
        if "tau" in token_set and "knowledge" in token_set:
            queries.extend(['all:"tau Knowledge"', 'all:"Unstructured Knowledge"'])
        if "r3" in token_set or "dao" in token_set or "r3dao" in lowered_terms:
            queries.extend(['all:"R3DAO"', 'all:"R3 DAO"', 'all:"Reactive Recovery Reconstruction"'])
        if clean_title:
            queries.append(f'ti:"{clean_title}"')
            queries.append(f'all:"{clean_title}"')
        if terms:
            head_terms = [re.sub(r"[^A-Za-z0-9]+", " ", term).strip() for term in terms[:8]]
            head = " ".join(term for term in head_terms if term)
            if len(head.split()) >= 3:
                queries.append(f'ti:"{head}"')
                queries.append(f'all:"{head}"')
            queries.append(" AND ".join(f"all:{term}" for term in terms[:8]))
    out: list[str] = []
    for query in queries:
        if query and query not in out:
            out.append(query)
    return out[:ARXIV_MAX_SEARCH_QUERIES]


def parse_arxiv_search_feed(row: dict[str, Any], content: bytes, url: str, status: int, content_type: str, final_url: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(content)
    except Exception as exc:
        return [{"kind": "arxiv_search", "url": url, "status_code": status, "content_type": content_type, "final_url": final_url, "accepted": False, "error": exc.__class__.__name__}]
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    candidates: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns):
        candidate_title = re.sub(r"\s+", " ", entry.findtext("atom:title", default="", namespaces=ns)).strip()
        candidate_authors = [node.text or "" for node in entry.findall("atom:author/atom:name", ns)]
        entry_id = entry.findtext("atom:id", default="", namespaces=ns).strip()
        pdf_url = ""
        for link in entry.findall("atom:link", ns):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href", "")
                break
        if not pdf_url and entry_id.startswith("http"):
            pdf_url = entry_id.replace("/abs/", "/pdf/")
        match = title_author_match_details(row, candidate_title, candidate_authors)
        candidates.append({
            "kind": "arxiv_search_candidate",
            "search_url": url,
            "query_version": ARXIV_SEARCH_QUERY_VERSION,
            "title": candidate_title,
            "authors": candidate_authors,
            "entry_id": entry_id,
            "pdf_url": pdf_url,
            "similarity": match["title_similarity"],
            "author_overlap": match["author_overlap"],
            "accepted": bool(match["accepted"] and pdf_url),
        })
    return candidates


def arxiv_search_candidates(paper: dict[str, Any] | str, max_results: int = 5) -> list[dict[str, Any]]:
    row = paper if isinstance(paper, dict) else {"title": str(paper or "")}
    title = str(row.get("title") or row.get("paper_title") or "")
    queries = arxiv_search_queries(title)
    if not queries:
        return []
    all_candidates: list[dict[str, Any]] = []
    seen_entries: set[str] = set()
    for index, query in enumerate(queries):
        if index:
            time.sleep(ARXIV_SEARCH_COOLDOWN_SEC)
        url = "https://export.arxiv.org/api/query?" + urlencode({"search_query": query, "start": 0, "max_results": max_results})
        status, content_type, content, final_url = fetch_url(url, timeout=REQUEST_TIMEOUT_SEC)
        if status != 200:
            all_candidates.append({"kind": "arxiv_search", "url": url, "query": query, "status_code": status, "content_type": content_type, "final_url": final_url, "accepted": False})
            if status == 429:
                break
            continue
        candidates = parse_arxiv_search_feed(row, content, url, status, content_type, final_url)
        for candidate in candidates:
            key = str(candidate.get("entry_id") or candidate.get("title") or candidate.get("search_url") or "")
            if key and key in seen_entries:
                continue
            if key:
                seen_entries.add(key)
            candidate["query"] = query
            all_candidates.append(candidate)
        if any(candidate.get("accepted") for candidate in candidates):
            break
    return all_candidates or [{"kind": "arxiv_search", "query_version": ARXIV_SEARCH_QUERY_VERSION, "accepted": False, "reason": "no_arxiv_candidates"}]


def text_looks_like_paper(text: str, title: str) -> bool:
    lowered = text.lower()
    title_tokens = [token for token in re.findall(r"[a-zA-Z]{4,}", title.lower())[:8]]
    title_hits = sum(1 for token in title_tokens if token in lowered)
    major_section_hits = sum(1 for marker in ["introduction", "method", "methodology", "experiment", "experiments", "evaluation", "results", "conclusion", "references"] if marker in lowered)
    abstract_hit = "abstract" in lowered
    return len(text) >= PAPER_BODY_MIN_CHARS and title_hits >= max(1, min(3, len(title_tokens))) and abstract_hit and major_section_hits >= 3


def write_text_evidence(paths: Any, rank: int, paper: dict[str, Any], text: str) -> str:
    paper_id = paper.get("paper_id") or paper.get("id") or f"paper_{rank}"
    text_path = full_text_reading_dir(paths) / "texts" / f"{rank:02d}_{safe_slug(paper_id)}.txt"
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(text, encoding="utf-8")
    try:
        return str(text_path.relative_to(paths.root))
    except Exception:
        return str(text_path)


def try_acquire_for_paper(paths: Any, paper: dict[str, Any], rank: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    title = str(paper.get("title") or paper.get("paper_title") or "")
    tried_urls: set[str] = set()

    for candidate in publisher_full_text_candidates(paper):
        url = candidate.get("url", "")
        if not url or url in tried_urls:
            continue
        tried_urls.add(url)
        kind = candidate.get("kind") or "publisher_page"
        if kind.endswith("pdf") or kind in {"acm_pdf", "acm_epdf", "acm_legacy_pdf"}:
            evidence, attempt = download_pdf_text(paths, rank, paper, url, kind="publisher_pdf_text_read", suffix=kind)
            attempt.update({"publisher_channel": kind, "doi": candidate.get("doi") or doi_from_paper(paper)})
            attempts.append(attempt)
            if evidence:
                evidence.update({"source_channel": kind, "publisher_doi": candidate.get("doi") or doi_from_paper(paper), "source_policy": "publisher PDF/HTML full text can be treated as the official paper text source."})
                return evidence, attempts
            continue
        status, content_type, content, final_url = fetch_url(url)
        text = html_to_text(content) if status == 200 else ""
        attempt = {"kind": kind, "url": url, "status_code": status, "content_type": content_type, "final_url": final_url, "text_chars": len(text), "doi": candidate.get("doi") or doi_from_paper(paper)}
        if text_looks_like_paper(text, title):
            text_path = write_text_evidence(paths, rank, paper, text)
            attempts.append({**attempt, "accepted": True})
            return {
                "source": "repair_current_find_full_text_evidence.py",
                "kind": "publisher_html_text_read",
                "html_url": url,
                "text_path": text_path,
                "text_chars": len(text),
                "page_count": 0,
                "full_text_status": "publisher_html_text_read",
                "source_channel": kind,
                "publisher_doi": candidate.get("doi") or doi_from_paper(paper),
                "source_policy": "publisher PDF/HTML full text can be treated as the official paper text source.",
            }, attempts
        attempts.append({**attempt, "accepted": False, "reason": fetch_block_reason(status, content_type, content, final_url) if status != 200 else "html_text_contract_not_satisfied"})

    pdf_urls: list[tuple[str, str]] = []
    if str(paper.get("pdf_url") or "").startswith("http"):
        pdf_urls.append((str(paper.get("pdf_url")), "indexed_pdf_text_read"))
    url = str(paper.get("url") or "")
    openreview = re.search(r"openreview\.net/forum\?id=([^&#]+)", url)
    if openreview:
        pdf_urls.append((f"https://openreview.net/pdf?id={openreview.group(1)}", "openreview_pdf_text_read"))

    for pdf_url, kind in pdf_urls:
        if pdf_url in tried_urls:
            continue
        tried_urls.add(pdf_url)
        evidence, attempt = download_pdf_text(paths, rank, paper, pdf_url, kind=kind, suffix=kind)
        attempts.append(attempt)
        if evidence:
            evidence.update({"source_channel": kind, "source_policy": "Indexed PDF full text can clear reading evidence only after paper-body validation."})
            return evidence, attempts

    manual_candidates, manual_attempts = manual_full_text_candidates(paths, paper)
    attempts.extend(manual_attempts)
    for candidate in manual_candidates:
        pdf_url = str(candidate.get("pdf_url") or "")
        if not pdf_url or pdf_url in tried_urls:
            continue
        tried_urls.add(pdf_url)
        evidence, attempt = download_pdf_text(paths, rank, paper, pdf_url, kind="manual_verified_pdf_text_read", suffix="manual_verified")
        attempt.update({"matched_title": candidate.get("matched_title"), "title_similarity": candidate.get("title_similarity"), "author_overlap": candidate.get("author_overlap"), "source_note": candidate.get("source_note")})
        attempts.append(attempt)
        if evidence:
            evidence.update({
                "source_channel": "manual_title_author_verified_pdf",
                "source_policy": "A project-local manually supplied full-text URL can clear deep-reading evidence only after title/author matching and paper-body validation.",
                "matched_title": candidate.get("matched_title"),
                "title_similarity": candidate.get("title_similarity"),
                "author_overlap": candidate.get("author_overlap"),
                "source_note": candidate.get("source_note"),
            })
            return evidence, attempts

    openreview_candidates, openreview_attempts = openreview_title_candidates(paper)
    attempts.extend(openreview_attempts)
    for candidate in openreview_candidates:
        pdf_url = str(candidate.get("pdf_url") or "")
        if not pdf_url or pdf_url in tried_urls:
            continue
        tried_urls.add(pdf_url)
        evidence, attempt = download_pdf_text(paths, rank, paper, pdf_url, kind="openreview_title_verified_pdf_text_read", suffix="openreview_title_verified")
        attempt.update({"matched_title": candidate.get("matched_title"), "title_similarity": candidate.get("title_similarity"), "author_overlap": candidate.get("author_overlap"), "openreview_forum": candidate.get("forum"), "openreview_url": candidate.get("url")})
        attempts.append(attempt)
        if evidence:
            evidence.update({
                "source_channel": "openreview_title_author_verified_pdf",
                "source_policy": "OpenReview PDF can clear deep-reading evidence after title/author matching and paper-body validation.",
                "matched_title": candidate.get("matched_title"),
                "title_similarity": candidate.get("title_similarity"),
                "author_overlap": candidate.get("author_overlap"),
                "openreview_forum": candidate.get("forum"),
                "openreview_url": candidate.get("url"),
            })
            return evidence, attempts

    openalex_candidates, openalex_attempts = openalex_repository_candidates(paper)
    attempts.extend(openalex_attempts)
    for candidate in openalex_candidates:
        pdf_url = str(candidate.get("pdf_url") or "")
        if not pdf_url or pdf_url in tried_urls:
            continue
        tried_urls.add(pdf_url)
        evidence, attempt = download_pdf_text(paths, rank, paper, pdf_url, kind="openalex_repository_pdf_text_read", suffix="openalex_repository")
        attempt.update({
            "matched_title": candidate.get("matched_title"),
            "title_similarity": candidate.get("title_similarity"),
            "author_overlap": candidate.get("author_overlap"),
            "openalex_id": candidate.get("openalex_id"),
            "openalex_doi": candidate.get("openalex_doi"),
            "landing_url": candidate.get("landing_url"),
        })
        attempts.append(attempt)
        if evidence:
            evidence.update({
                "repository_source": "openalex",
                "source_channel": "openalex_title_or_doi_verified_repository_pdf",
                "source_policy": "Repository full text can clear deep-reading evidence after title/author and paper-body validation, but it is not promoted as the official ACM published PDF.",
                "matched_title": candidate.get("matched_title"),
                "title_similarity": candidate.get("title_similarity"),
                "author_overlap": candidate.get("author_overlap"),
                "openalex_id": candidate.get("openalex_id"),
                "openalex_doi": candidate.get("openalex_doi"),
                "openalex_landing_url": candidate.get("landing_url"),
                "publisher_doi": doi_from_paper(paper),
            })
            return evidence, attempts

    for link in links_from_paper(paper):
        for raw_url in github_raw_readme_candidates(link):
            if raw_url in tried_urls:
                continue
            tried_urls.add(raw_url)
            status, content_type, content, final_url = fetch_url(raw_url)
            text = content.decode("utf-8", errors="ignore") if status == 200 else ""
            attempts.append({
                "kind": "github_readme_supplement",
                "url": raw_url,
                "status_code": status,
                "content_type": content_type,
                "final_url": final_url,
                "text_chars": len(text),
                "accepted": False,
                "reason": "repository README is supplemental project evidence, not a full paper unless it satisfies the paper-text section contract",
            })
            if text_looks_like_paper(text, title):
                text_path = write_text_evidence(paths, rank, paper, text)
                return {"source": "repair_current_find_full_text_evidence.py", "kind": "repository_html_text_read", "html_url": raw_url, "text_path": text_path, "text_chars": len(text), "page_count": 0, "full_text_status": "repository_html_text_read", "source_policy": "Repository HTML can clear reading evidence only after paper-body validation."}, attempts

    for page_url in [url] + [link for link in links_from_paper(paper) if link != url]:
        if not page_url.startswith("http") or "github.com" in page_url.lower() or page_url in tried_urls:
            continue
        tried_urls.add(page_url)
        status, content_type, content, final_url = fetch_url(page_url)
        text = html_to_text(content) if status == 200 else ""
        attempt = {"kind": "html_page", "url": page_url, "status_code": status, "content_type": content_type, "final_url": final_url, "text_chars": len(text)}
        if text_looks_like_paper(text, title):
            text_path = write_text_evidence(paths, rank, paper, text)
            attempts.append({**attempt, "accepted": True})
            return {"source": "repair_current_find_full_text_evidence.py", "kind": "html_text_read", "html_url": page_url, "text_path": text_path, "text_chars": len(text), "page_count": 0, "full_text_status": "html_text_read", "source_policy": "HTML page can clear reading evidence only after paper-body validation."}, attempts
        attempts.append({**attempt, "accepted": False, "reason": fetch_block_reason(status, content_type, content, final_url) if status != 200 else "html_text_contract_not_satisfied"})

    for candidate in arxiv_search_candidates(paper):
        attempts.append(candidate)
        if candidate.get("kind") != "arxiv_search_candidate" or not candidate.get("accepted"):
            continue
        pdf_url = str(candidate.get("pdf_url") or "")
        if not pdf_url or pdf_url in tried_urls:
            continue
        tried_urls.add(pdf_url)
        evidence, attempt = download_pdf_text(paths, rank, paper, pdf_url, kind="arxiv_pdf_text_read", suffix="arxiv")
        attempt.update({"matched_title": candidate.get("title"), "similarity": candidate.get("similarity"), "author_overlap": candidate.get("author_overlap"), "arxiv_entry_id": candidate.get("entry_id")})
        attempts.append(attempt)
        if evidence:
            evidence.update({
                "source_channel": "arxiv_api_title_verified_pdf",
                "source_policy": "arXiv full text can clear deep-reading evidence after title and paper-body validation, but it is not promoted as the official publisher PDF.",
                "matched_title": candidate.get("title"),
                "arxiv_entry_id": candidate.get("entry_id"),
                "title_similarity": candidate.get("similarity"),
                "author_overlap": candidate.get("author_overlap"),
            })
            return evidence, attempts
    return None, attempts



def record_unavailable_full_text_evidence_blocker(paths: Any, find_results: dict[str, Any], packet: dict[str, Any], unavailable: list[dict[str, Any]]) -> dict[str, Any]:
    """Record a Read-stage full-text blocker without rewriting Find outputs."""
    run_id = str(find_results.get("run_id") or packet.get("run_id") or "").strip()
    if not run_id or not unavailable:
        return {"status": "skipped", "reason": "missing_run_or_unavailable"}
    recommendations = current_recommendations(find_results)
    unavailable_titles = [str(row.get("title") or "").strip() for row in unavailable if str(row.get("title") or "").strip()]
    receipt = {
        "project": getattr(paths, "name", ""),
        "run_id": run_id,
        "status": "read_stage_full_text_unavailable_recorded",
        "source": "read_stage_full_text_evidence_blocker",
        "generated_at": now_iso(),
        "recommended_count": len(recommendations),
        "unavailable_titles": unavailable_titles,
        "unavailable_count": len(unavailable_titles),
        "policy": "For an already-adopted current Find, Read/full-text acquisition must not rewrite articles, strong_recommendations, or read_candidates. If an original recommendation has no title/author-verified public full text, Read may use a same-run ranked replacement in full_text_packet only.",
        "next_required_action": "acquire_title_author_verified_full_text_for_read_stage",
    }
    try:
        paths.state.mkdir(parents=True, exist_ok=True)
        save_json(paths.state / "current_find_full_text_unavailable_read_stage_blocker.json", receipt)
    except Exception:
        pass
    return receipt

def repair_current_find_full_text_evidence(project: str, *, force: bool = False) -> tuple[int, dict[str, Any]]:
    paths = build_paths(project)
    taste_dir = paths.planning / "finding"
    find_results = load_json(taste_dir / "find_results.json", {})
    validation = load_json(paths.state / "current_find_claude_reading_validation.json", {})
    run_id = str((find_results if isinstance(find_results, dict) else {}).get("run_id") or "")
    packet_path = full_text_packet_path(paths)
    packet = load_json(packet_path, {})
    if not isinstance(packet, dict):
        packet = {}
    validation_run_id = str((validation if isinstance(validation, dict) else {}).get("run_id") or "").strip()
    pending = pending_titles(validation if isinstance(validation, dict) and (not validation_run_id or validation_run_id == run_id) else {})
    if not pending:
        pending = packet_missing_titles(find_results if isinstance(find_results, dict) else {}, packet, run_id)
    receipt_path = paths.state / "current_find_full_text_evidence_repair.json"
    validation_generated_at = str((validation if isinstance(validation, dict) else {}).get("generated_at") or "")
    last = load_json(receipt_path, {})
    if not force and isinstance(last, dict) and last.get("run_id") == run_id and last.get("validation_generated_at") == validation_generated_at and last.get("pending_titles") == pending and last.get("status") in {"blocked_full_text_evidence_unavailable", "already_attempted_full_text_evidence_repair"}:
        receipt = {**last, "status": "already_attempted_full_text_evidence_repair", "checked_at": now_iso()}
        save_json(receipt_path, receipt)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 2, receipt
    if not run_id or not pending:
        receipt = {"project": project, "run_id": run_id, "status": "no_pending_full_text_evidence_gap", "generated_at": now_iso(), "pending_titles": pending}
        save_json(receipt_path, receipt)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0, receipt

    recommendations = current_recommendations(find_results if isinstance(find_results, dict) else {})
    packet_run_id = str(packet.get("run_id") or packet.get("current_find_run_id") or "").strip()
    if packet_run_id and run_id and packet_run_id != run_id:
        packet = {
            "run_id": run_id,
            "source": "repair_current_find_full_text_evidence.py",
            "papers": [],
            "previous_packet_run_id": packet_run_id,
            "previous_packet_replaced_at": now_iso(),
        }
    else:
        packet["run_id"] = run_id
        packet.setdefault("source", "repair_current_find_full_text_evidence.py")
        packet.setdefault("papers", [])

    acquired: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    all_attempts: list[dict[str, Any]] = []
    overall_deadline = time.monotonic() + FULL_TEXT_REPAIR_TIMEOUT_SEC
    original_deadline = time.monotonic() + max(60, int(FULL_TEXT_REPAIR_TIMEOUT_SEC * 0.65))
    save_repair_progress(packet_path, receipt_path, packet, project=project, run_id=run_id, pending=pending, acquired=acquired, unavailable=unavailable, attempts=all_attempts, status="full_text_evidence_repair_running", validation_generated_at=validation_generated_at)
    for title in pending:
        if time.monotonic() >= original_deadline:
            unavailable.append({"title": title, "reason": "original_recommendation_repair_budget_exhausted_before_paper", "attempt_count": 0})
            save_repair_progress(packet_path, receipt_path, packet, project=project, run_id=run_id, pending=pending, acquired=acquired, unavailable=unavailable, attempts=all_attempts, current_title=title, status="original_full_text_repair_budget_exhausted_checking_replacements", validation_generated_at=validation_generated_at)
            continue
        paper = find_row_for_title(recommendations, title)
        if not paper:
            paper = {"title": title, "id": safe_slug(title)}
        try:
            rank = recommendations.index(paper) + 1
        except ValueError:
            rank = len(acquired) + len(unavailable) + 1
        entry = ensure_packet_entry(packet, paper, rank)
        evidence, attempts = try_acquire_for_paper(paths, paper, rank)
        all_attempts.append({"title": title, "attempts": attempts})
        if evidence:
            update_packet_entry_with_evidence(entry, paper, title, evidence)
            acquired.append({"title": title, "evidence": evidence})
        else:
            entry.update({
                "title": paper.get("title") or paper.get("paper_title") or title,
                "paper_id": paper.get("paper_id") or paper.get("id") or entry.get("paper_id") or safe_slug(title),
                "url": paper.get("url") or paper.get("abs_url") or entry.get("url") or "",
                "pdf_url": paper.get("pdf_url") or entry.get("pdf_url") or "",
                "text_chars": 0,
                "pdf_text_chars": 0,
                "full_text_chars": 0,
                "pdf_status": "full_text_evidence_unavailable_after_repair_attempt",
                "full_text_status": "full_text_evidence_unavailable_after_repair_attempt",
                "repair_status": "blocked_full_text_evidence_unavailable",
                "checked_at": now_iso(),
            })
            unavailable.append({"title": title, "reason": "no accepted PDF/HTML paper text source", "attempt_count": len(attempts)})
        save_repair_progress(packet_path, receipt_path, packet, project=project, run_id=run_id, pending=pending, acquired=acquired, unavailable=unavailable, attempts=all_attempts, current_title=title, status="full_text_evidence_repair_running", validation_generated_at=validation_generated_at)

    original_unavailable = list(unavailable)
    replacement_acquired: list[dict[str, Any]] = []
    replacement_attempted: list[dict[str, Any]] = []
    remaining_unavailable: list[dict[str, Any]] = []
    if original_unavailable:
        replacement_rows = read_stage_replacement_candidates(find_results if isinstance(find_results, dict) else {}, recommendations)
        replacement_used: set[str] = set()
        for missing_item in original_unavailable:
            missing_title = str(missing_item.get("title") or "").strip()
            matched_replacement = False
            for candidate in replacement_rows:
                candidate_keys = identity_values(candidate)
                if not candidate_keys or candidate_keys & replacement_used:
                    continue
                if norm_title(candidate.get("title") or candidate.get("paper_title")) == norm_title(missing_title):
                    continue
                if time.monotonic() >= overall_deadline:
                    replacement_attempted.append({"replacement_for": missing_title, "reason": "full_text_repair_timeout_before_replacement", "accepted": False})
                    break
                replacement_rank = int(candidate.get("read_replacement_source_rank") or 0) or max(1, len(as_list(packet.get("papers"))) + 1)
                replacement_title = str(candidate.get("title") or candidate.get("paper_title") or "Untitled").strip()
                evidence, attempts = try_acquire_for_paper(paths, candidate, replacement_rank)
                replacement_attempted.append({
                    "replacement_for": missing_title,
                    "candidate_title": replacement_title,
                    "source_pool": candidate.get("read_replacement_source_pool"),
                    "source_rank": candidate.get("read_replacement_source_rank"),
                    "attempts": attempts,
                    "accepted": bool(evidence),
                })
                if not evidence:
                    continue
                entry = ensure_packet_entry(packet, candidate, replacement_rank)
                update_packet_entry_with_evidence(
                    entry,
                    candidate,
                    replacement_title,
                    evidence,
                    replacement_meta={
                        "read_replacement": True,
                        "reading_packet_role": "read_stage_replacement",
                        "replacement_for_unavailable_recommendation": missing_title,
                        "replacement_source_pool": candidate.get("read_replacement_source_pool"),
                        "replacement_source_rank": candidate.get("read_replacement_source_rank"),
                        "replacement_policy": "Read-stage full-text selection can use the next same-run ranked candidate when an original Find recommendation has no title/author-verified public full text. Find recommendation artifacts are not rewritten.",
                    },
                )
                replacement_used.update(candidate_keys)
                replacement_acquired.append({"replacement_for": missing_title, "title": replacement_title, "evidence": evidence})
                matched_replacement = True
                break
            if not matched_replacement:
                remaining_unavailable.append(missing_item)
        save_repair_progress(packet_path, receipt_path, packet, project=project, run_id=run_id, pending=pending, acquired=acquired + replacement_acquired, unavailable=remaining_unavailable, attempts=all_attempts + replacement_attempted, status="full_text_evidence_repair_replacements_checked", validation_generated_at=validation_generated_at)

    read_stage_blocker_receipt: dict[str, Any] = {}
    if remaining_unavailable:
        read_stage_blocker_receipt = record_unavailable_full_text_evidence_blocker(paths, find_results if isinstance(find_results, dict) else {}, packet, remaining_unavailable)
        pending_after_repair = [row["title"] for row in remaining_unavailable if row.get("title")]
        status = "partial_full_text_evidence_repair" if (acquired or replacement_acquired) else "blocked_full_text_evidence_unavailable"
    else:
        pending_after_repair = []
        if replacement_acquired:
            status = "repaired_full_text_evidence_with_read_stage_replacements"
        else:
            status = "repaired_full_text_evidence" if acquired else "no_pending_full_text_evidence_gap"
    receipt = {
        "project": project,
        "run_id": run_id,
        "status": status,
        "generated_at": now_iso(),
        "validation_generated_at": validation_generated_at,
        "pending_titles": pending,
        "pending_after_repair": pending_after_repair,
        "acquired_count": len(acquired) + len(replacement_acquired),
        "original_acquired_count": len(acquired),
        "replacement_acquired_count": len(replacement_acquired),
        "unavailable_count": len(remaining_unavailable),
        "original_unavailable_count": len(original_unavailable),
        "acquired": acquired,
        "replacement_acquired": replacement_acquired,
        "replacement_attempted": replacement_attempted,
        "unavailable": remaining_unavailable,
        "unavailable_original_recommendations": original_unavailable,
        "replaced_unavailable_recommendation_titles": [row.get("replacement_for") for row in replacement_acquired if row.get("replacement_for")],
        "read_stage_blocker": read_stage_blocker_receipt,
        "attempts": all_attempts,
        "files": {"full_text_packet": str(packet_path), "receipt": str(receipt_path)},
        "next_required_action": "rerun_current_find_claude_takeover_deep_read_synthesis" if not pending_after_repair else "acquire_title_author_verified_full_text_for_read_stage",
        "policy": "Only accepted PDF/HTML paper-body text can clear Read-stage full-text evidence. Find recommendation artifacts stay immutable; when an original recommendation has no verified public full text, Read may fill its reading packet from the next same-run ranked candidate with verified paper-body text.",
    }
    save_json(receipt_path, receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return (0 if not pending_after_repair else 2), receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Acquire missing full-text evidence for current user-visible Find recommendations without changing scientific content.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    rc, _receipt = repair_current_find_full_text_evidence(args.project, force=args.force)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
