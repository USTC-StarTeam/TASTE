from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from runtime.framework_io import read_json as _read_json
from runtime.framework_io import utc_now as _now_iso
from runtime.framework_io import write_json_raw as _write_json

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROJECTS_ROOT = ROOT / "projects"
DEFAULT_READING_ROOT = ROOT / "modules" / "reading"
READING_POLICY_VERSION = "full_text_required_v5_detailed_deep_read"
READING_SOURCE = "framework_current_find_read_adapter"
DEFAULT_MAX_READ_PAPERS = 50


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _timestamp_run_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return default if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return default


def _payload_run_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("run_id", "source_run_id", "find_run_id", "current_find_run_id", "taste_run_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _title_of(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("title") or row.get("paper_title") or row.get("name") or "Untitled").strip()


def _identity_key(row: dict[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    for key in ("doi", "arxiv_id", "biorxiv_doi", "paper_id", "id", "url", "pdf_url"):
        value = str(row.get(key) or metadata.get(key) or "").strip().lower()
        if value:
            return f"{key}:{value}"
    return "title:" + re.sub(r"\W+", " ", _title_of(row).lower()).strip()


def _rows_for_keys(payload: dict[str, Any], keys: tuple[str, ...]) -> tuple[list[dict[str, Any]], str]:
    for key in keys:
        rows = payload.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            identity = _identity_key(row)
            if identity in seen:
                continue
            seen.add(identity)
            out.append(dict(row))
        if out:
            return out, key
    return [], ""


def _recommendation_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows, _source = _rows_for_keys(
        payload,
        ("strong_recommendations", "recommendations", "read_candidates", "articles", "input_articles", "papers"),
    )
    return rows


def _find_recommendation_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("strong_recommendations", "recommendations"):
        if key not in payload:
            continue
        rows, _source = _rows_for_keys(payload, (key,))
        return rows
    return []


def _find_recommendation_count(payload: dict[str, Any]) -> int:
    for key in ("strong_recommendations", "recommendations"):
        if key in payload:
            return len(_find_recommendation_rows(payload))
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    for value in (
        payload.get("recommendation_actual_count"),
        payload.get("strong_recommendation_count"),
        counts.get("strong_recommendations"),
        counts.get("recommended"),
        counts.get("recommendation_actual_count"),
    ):
        parsed = _as_int(value, -1)
        if parsed >= 0:
            return parsed
    return 0


def _fallback_rank_key(row: dict[str, Any]) -> tuple[float, float, float, str]:
    def number(*keys: str) -> float:
        for key in keys:
            try:
                value = row.get(key)
                if value not in (None, ""):
                    return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    explicit_rank = number("final_rank", "recommendation_rank", "rank")
    rank_order = -explicit_rank if explicit_rank > 0 else float("-inf")
    return (
        rank_order,
        number("fit_score", "recommendation_score", "score"),
        number("diversity_score", "quality_score"),
        _title_of(row).lower(),
    )


def _final_ranked_rows(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    rows, source = _rows_for_keys(payload, ("screened_ranking", "final_ranking", "ranked_papers"))
    if rows:
        return rows, source
    evaluated, source = _rows_for_keys(payload, ("evaluated_candidates",))
    if evaluated:
        return sorted(evaluated, key=_fallback_rank_key, reverse=True), source
    return _rows_for_keys(
        payload,
        ("strong_recommendations", "recommendations", "read_candidates", "articles", "input_articles", "papers"),
    )


def update_project_read_default_after_find(
    project: str,
    find_results: dict[str, Any],
    *,
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
) -> dict[str, Any]:
    """Refresh Framework's per-project Read default after a successful Find.

    A user-edited value remains authoritative.  Values created by the module
    default or an earlier Find are refreshed to twice the new recommendation
    count.
    """
    project_root = safe_project_root(project, projects_root=projects_root)
    config_path = project_root / "project.json"
    cfg = _read_json(config_path, {})
    if not isinstance(cfg, dict):
        raise ValueError(f"Invalid project config: {config_path}")
    recommendation_count = _find_recommendation_count(find_results if isinstance(find_results, dict) else {})
    reading_cfg = dict(cfg.get("reading") or {}) if isinstance(cfg.get("reading"), dict) else {}
    source = str(reading_cfg.get("max_read_papers_source") or "").strip()
    configured = _as_int(cfg.get("max_read_papers"), 0)
    automatic_sources = {"module_default", "framework_find_default"}
    user_override = bool(configured > 0 and source not in automatic_sources) or source == "user"
    if user_override:
        return {
            "status": "preserved_user_override",
            "project": project,
            "max_read_papers": configured,
            "framework_default_max_read_papers": recommendation_count * 2 if recommendation_count else DEFAULT_MAX_READ_PAPERS,
            "recommendation_count": recommendation_count,
        }

    max_read_papers = recommendation_count * 2 if recommendation_count else DEFAULT_MAX_READ_PAPERS
    cfg["max_read_papers"] = max_read_papers
    reading_cfg.update({
        "max_read_papers_source": "framework_find_default",
        "max_read_papers_find_run_id": _payload_run_id(find_results),
        "find_recommendation_count": recommendation_count,
        "max_read_papers_updated_at": _now_iso(),
    })
    cfg["reading"] = reading_cfg
    _write_json(config_path, cfg)
    return {
        "status": "updated_framework_find_default" if recommendation_count else "reset_module_default_no_find_recommendations",
        "project": project,
        "max_read_papers": max_read_papers,
        "recommendation_count": recommendation_count,
    }


def _presentation_type(article: dict[str, Any], metadata: dict[str, Any]) -> str:
    venue = str(article.get("venue") or metadata.get("venue") or article.get("conference") or metadata.get("conference") or "").strip()
    source = str(article.get("source") or metadata.get("source") or "").lower()
    quality = metadata.get("quality") if isinstance(metadata.get("quality"), dict) else {}
    if not venue or not (
        sum(char.isupper() for char in venue) >= 2
        or "conference" in venue.lower()
        or any(marker in source for marker in ["openreview", "conference", "proceedings"])
        or quality.get("quality_kind") == "conference"
    ):
        return ""
    values = [
        article.get("presentation_type"),
        article.get("presentation_label"),
        metadata.get("presentation_type"),
        metadata.get("presentation_label"),
        article.get("tier"),
        metadata.get("tier"),
    ]
    for value in values:
        text = str(value or "")
        for label in ("oral", "spotlight", "poster"):
            if re.search(rf"(?<![A-Za-z]){label}(?![A-Za-z])", text, re.I):
                return label
    return ""


def _article_for_reading(row: dict[str, Any], index: int) -> dict[str, Any]:
    article = dict(row)
    metadata = dict(article.get("metadata")) if isinstance(article.get("metadata"), dict) else {}
    paper_id = str(
        article.get("paper_id")
        or article.get("id")
        or metadata.get("paper_id")
        or metadata.get("id")
        or f"paper_{index:03d}"
    ).strip()
    article.update({
        "paper_id": paper_id,
        "id": str(article.get("id") or paper_id),
        "title": _title_of(article),
        "source": str(article.get("source") or metadata.get("source") or metadata.get("channel") or "input").strip(),
        "url": str(article.get("url") or article.get("abs_url") or article.get("html_url") or metadata.get("url") or metadata.get("abs_url") or "").strip(),
        "pdf_url": str(article.get("pdf_url") or metadata.get("pdf_url") or "").strip(),
        "abstract": str(article.get("abstract") or article.get("abstract_en") or article.get("summary") or metadata.get("abstract") or "").strip(),
        "metadata": metadata,
    })
    presentation_type = _presentation_type(article, metadata)
    if presentation_type:
        article["presentation_type"] = presentation_type
    else:
        article.pop("presentation_type", None)
    return article


def _create_reading_run_dir(reading_root: Path) -> Path:
    output_root = reading_root / ".runtime" / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    for _attempt in range(100):
        run_dir = output_root / _timestamp_run_id()
        try:
            run_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            continue
        _write_json(run_dir / "run_manifest.json", {
            "run_id": run_dir.name,
            "created_at": _now_iso(),
            "creator": "framework_reading_bridge",
            "runtime_policy": "Framework creates one fixed Reading run directory before invoking modules/reading/main.py.",
        })
        return run_dir
    raise RuntimeError("Failed to create a unique timestamped Reading run directory.")


def safe_project_root(project: str, *, projects_root: Path = DEFAULT_PROJECTS_ROOT) -> Path:
    value = str(project or "").strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise ValueError("Invalid project name. Use only letters, numbers, dash, underscore, and dot.")
    root = (projects_root / value).resolve(strict=False)
    try:
        root.relative_to(projects_root.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"Project path escapes projects root: {value}") from exc
    if not root.exists():
        raise FileNotFoundError(f"Project not found: {value}")
    return root


def prepare_current_find_read_input(
    project: str,
    *,
    read_limit: int = 0,
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    reading_root: Path = DEFAULT_READING_ROOT,
) -> dict[str, Any]:
    project_root = safe_project_root(project, projects_root=projects_root)
    find_path = project_root / "planning" / "finding" / "find_results.json"
    find_results = _read_json(find_path, {})
    if not isinstance(find_results, dict) or not find_results:
        raise FileNotFoundError(f"Missing current Find results: {find_path}")
    find_run_id = _payload_run_id(find_results)
    if not find_run_id:
        raise ValueError(f"Current Find results do not contain run_id: {find_path}")
    ranked_rows, ranking_source = _final_ranked_rows(find_results)
    if not ranked_rows:
        raise ValueError("Current Find results contain no ranked papers.")
    recommendation_count = _find_recommendation_count(find_results)
    project_cfg = _read_json(project_root / "project.json", {})
    configured_limit = _as_int((project_cfg if isinstance(project_cfg, dict) else {}).get("max_read_papers"), DEFAULT_MAX_READ_PAPERS)
    limit = max(1, _as_int(read_limit, 0) or configured_limit or DEFAULT_MAX_READ_PAPERS)
    selected = ranked_rows[:limit]
    run_dir = _create_reading_run_dir(reading_root)
    input_path = run_dir / "input" / "source_input.json"
    payload = {
        "run_id": find_run_id,
        "source_run_id": find_run_id,
        "source": "framework_current_find_final_ranking",
        "project": project,
        "generated_at": _now_iso(),
        "reading_run_id": run_dir.name,
        "research_topic": str((project_cfg if isinstance(project_cfg, dict) else {}).get("topic") or "").strip(),
        "research_interest": str((project_cfg if isinstance(project_cfg, dict) else {}).get("research_interest") or (project_cfg if isinstance(project_cfg, dict) else {}).get("user_prompt") or "").strip(),
        "researcher_profile": str((project_cfg if isinstance(project_cfg, dict) else {}).get("researcher_profile") or "").strip(),
        "ranking_source": ranking_source,
        "total_current_find_ranked_count": len(ranked_rows),
        "total_current_find_recommendation_count": recommendation_count,
        "max_read_papers": limit,
        "input_article_count": len(selected),
        "limited": bool(limit < len(ranked_rows)),
        "articles": [_article_for_reading(row, index) for index, row in enumerate(selected, 1)],
        "policy": "Framework takes the first max_read_papers rows from the current Find final ranking and converts them to generic Reading input. A title is sufficient; supplied URL, PDF, DOI, arXiv, OpenReview, venue, presentation type, author, and metadata fields are optional same-paper hints.",
    }
    _write_json(input_path, payload)
    return {
        "status": "prepared_current_find_read_input",
        "project": project,
        "run_id": find_run_id,
        "reading_run_id": run_dir.name,
        "reading_run_dir": str(run_dir),
        "input_json": str(input_path),
        "input_article_count": len(selected),
        "max_read_papers": limit,
        "ranked_count": len(ranked_rows),
        "ranking_source": ranking_source,
        "recommendation_count": recommendation_count,
        "limited": payload["limited"],
    }


def extract_last_json_object(text: str) -> dict[str, Any]:
    joined = str(text or "")
    for match in reversed(list(re.finditer(r"(?m)^\{", joined))):
        try:
            candidate = json.loads(joined[match.start():])
        except Exception:
            continue
        if isinstance(candidate, dict):
            return candidate
    return {}


def _expected_project_find_run_id(project_root: Path) -> str:
    for rel in (
        ("planning", "finding", "find_progress.json"),
        ("planning", "finding", "find_results.json"),
        ("state", "finding_frontend.json"),
        ("state", "current_find_recommendation_projection.json"),
    ):
        run_id = _payload_run_id(_read_json(project_root.joinpath(*rel), {}))
        if run_id:
            return run_id
    return ""


def reading_validation_is_ready(validation: Any, run_id: str) -> bool:
    if not isinstance(validation, dict):
        return False
    if str(validation.get("run_id") or "").strip() != str(run_id or "").strip():
        return False
    if validation.get("valid") is not True:
        return False
    if str(validation.get("policy_version") or "").strip() != READING_POLICY_VERSION:
        return False
    expected = _as_int(validation.get("expected_reading_count") or validation.get("selected_reading_count"))
    actual = _as_int(validation.get("actual_reading_count"))
    full_text = _as_int(validation.get("full_text_reading_count"))
    pending_full_text = _as_int(validation.get("pending_full_text_reading_count"))
    deep_read = _as_int(validation.get("deep_read_complete_count"))
    pending_deep = _as_int(validation.get("pending_deep_read_synthesis_count"))
    if not expected or actual != expected or full_text < expected or pending_full_text > 0:
        return False
    if deep_read != expected or pending_deep > 0:
        return False
    if validation.get("read_quality_complete") is not True or validation.get("scoring_complete") is not True:
        return False
    return not validation.get("blockers")


def current_find_read_gate_status(paths: Any) -> dict[str, Any]:
    project_root = Path(getattr(paths, "root", paths)).expanduser().resolve()
    run_id = _expected_project_find_run_id(project_root)
    validation_path = project_root / "state" / "current_find_claude_reading_validation.json"
    validation = _read_json(validation_path, {})
    ready = reading_validation_is_ready(validation, run_id)
    expected = _as_int(validation.get("expected_reading_count") or validation.get("selected_reading_count")) if isinstance(validation, dict) else 0
    blockers = [str(item) for item in validation.get("blockers", []) if str(item or "").strip()] if isinstance(validation, dict) and isinstance(validation.get("blockers"), list) else []
    if run_id and not ready and not blockers:
        blockers.append("Read validation for the current Find run has not completed full-text reading, deep reading, and final scoring.")
    return {
        "status": "pass" if ready else "blocked_current_find_reading" if run_id else "unknown",
        "blocking": bool(run_id and not ready),
        "run_id": run_id,
        "expected_reading_count": expected,
        "actual_reading_count": _as_int(validation.get("actual_reading_count")) if isinstance(validation, dict) else 0,
        "full_text_reading_count": _as_int(validation.get("full_text_reading_count")) if isinstance(validation, dict) else 0,
        "pending_full_text_reading_count": _as_int(validation.get("pending_full_text_reading_count")) if isinstance(validation, dict) else 0,
        "deep_read_complete_count": _as_int(validation.get("deep_read_complete_count")) if isinstance(validation, dict) else 0,
        "pending_deep_read_synthesis_count": _as_int(validation.get("pending_deep_read_synthesis_count")) if isinstance(validation, dict) else 0,
        "scoring_complete": bool(isinstance(validation, dict) and validation.get("scoring_complete") is True),
        "policy_version": READING_POLICY_VERSION,
        "blockers": blockers,
        "evidence": [str(validation_path)],
    }


def _resolve_reading_run_dir(
    *,
    result_payload: dict[str, Any] | None,
    stdout_text: str,
    reading_root: Path,
) -> Path:
    payload = result_payload if isinstance(result_payload, dict) else {}
    if not payload and stdout_text:
        payload = extract_last_json_object(stdout_text)
    nested = payload.get("wrapper_result") if isinstance(payload.get("wrapper_result"), dict) else {}
    raw = str(payload.get("run_dir") or payload.get("reading_run_dir") or nested.get("run_dir") or "").strip()
    run_id = str(payload.get("run_id") or payload.get("reading_run_id") or nested.get("run_id") or "").strip()
    if raw:
        run_dir = Path(raw).expanduser()
        if not run_dir.is_absolute():
            run_dir = reading_root / run_dir
    elif run_id:
        run_dir = reading_root / ".runtime" / "output" / run_id
    else:
        raise ValueError("Reading result does not identify its timestamped run directory; refusing to use latest_run.")
    run_dir = run_dir.resolve(strict=False)
    output_root = (reading_root / ".runtime" / "output").resolve(strict=False)
    try:
        run_dir.relative_to(output_root)
    except ValueError as exc:
        raise ValueError(f"Reading run source must stay under modules/reading/.runtime/output: {run_dir}") from exc
    if not re.fullmatch(r"\d{8}T\d{12}Z", run_dir.name):
        raise ValueError(f"Reading run directory name must be a UTC precise timestamp: {run_dir.name}")
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Reading run directory is missing: {run_dir}")
    return run_dir


def _run_input_payload(run_dir: Path) -> dict[str, Any]:
    for path in (run_dir / "input" / "source_input.json", run_dir / "input" / "input.json"):
        payload = _read_json(path, {})
        if isinstance(payload, dict) and payload:
            return payload
    raise FileNotFoundError(f"Reading run input is missing: {run_dir / 'input'}")


def _item_full_text_ready(item: dict[str, Any]) -> bool:
    validation = item.get("validation") if isinstance(item.get("validation"), dict) else {}
    if validation.get("full_text_ready") is True:
        return True
    packet = item.get("full_text_packet") if isinstance(item.get("full_text_packet"), dict) else {}
    return bool(packet.get("full_text_available")) and _as_int(packet.get("full_text_chars") or packet.get("text_chars")) >= 1200


def _item_deep_read_complete(item: dict[str, Any]) -> bool:
    validation = item.get("validation") if isinstance(item.get("validation"), dict) else {}
    return validation.get("deep_read_complete") is True


def _source_row_for_item(item: dict[str, Any], source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
    reading = item.get("reading") if isinstance(item.get("reading"), dict) else {}
    candidates = [row for row in (paper, reading) if isinstance(row, dict) and row]
    for candidate in candidates:
        identity = _identity_key(candidate)
        match = next((row for row in source_rows if _identity_key(row) == identity), None)
        if match is not None:
            return match
    candidate_titles = {
        re.sub(r"\W+", " ", _title_of(candidate).lower()).strip()
        for candidate in candidates
        if _title_of(candidate)
    }
    if candidate_titles:
        match = next(
            (
                row
                for row in source_rows
                if re.sub(r"\W+", " ", _title_of(row).lower()).strip() in candidate_titles
            ),
            None,
        )
        if match is not None:
            return match
    return {}


def _reading_score(item: dict[str, Any], reading: dict[str, Any], name: str) -> float | None:
    for container in (reading, item):
        value = container.get(name)
        try:
            if value not in (None, ""):
                return max(0.0, min(10.0, float(value)))
        except (TypeError, ValueError):
            continue
    return None


def _project_reading_row(
    item: dict[str, Any],
    source_row: dict[str, Any],
    *,
    find_run_id: str,
    reading_run_id: str,
    index: int,
) -> dict[str, Any]:
    paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
    packet = item.get("full_text_packet") if isinstance(item.get("full_text_packet"), dict) else {}
    reading = item.get("reading") if isinstance(item.get("reading"), dict) else {}
    artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
    full_text_ready = _item_full_text_ready(item)
    deep_read_complete = _item_deep_read_complete(item)
    paper_id = str(reading.get("paper_id") or paper.get("paper_id") or paper.get("id") or source_row.get("paper_id") or source_row.get("id") or f"paper_{index:03d}")
    match_score = _reading_score(item, reading, "match_score")
    transferability_score = _reading_score(item, reading, "transferability_score")
    average_score = _reading_score(item, reading, "average_score")
    if average_score is None and match_score is not None and transferability_score is not None:
        average_score = round((match_score + transferability_score) / 2.0, 4)
    final_read_rank = _as_int(reading.get("final_read_rank") or item.get("final_read_rank"), index) or index
    paper_index = _as_int(item.get("paper_index") or source_row.get("paper_index"), index) or index
    return {
        "id": paper_id,
        "paper_id": paper_id,
        "run_id": find_run_id,
        "source_run_id": find_run_id,
        "reading_run_id": reading_run_id,
        "input_index": index,
        "paper_index": paper_index,
        "title": str(reading.get("title") or paper.get("title") or _title_of(source_row)),
        "authors": paper.get("authors") or source_row.get("authors") or [],
        "source": paper.get("source") or source_row.get("source") or "",
        "venue": reading.get("venue") or paper.get("venue") or source_row.get("venue") or "",
        "year": reading.get("year") or paper.get("year") or source_row.get("year") or "",
        "url": reading.get("url") or paper.get("url") or source_row.get("url") or source_row.get("abs_url") or "",
        "pdf_url": reading.get("pdf_url") or packet.get("pdf_url") or paper.get("pdf_url") or source_row.get("pdf_url") or "",
        "doi": paper.get("doi") or source_row.get("doi") or source_row.get("published_doi") or "",
        "score": reading.get("score") or source_row.get("recommendation_score") or source_row.get("score") or source_row.get("fit_score"),
        "match_score": match_score,
        "transferability_score": transferability_score,
        "average_score": average_score,
        "final_read_rank": final_read_rank,
        "verdict": reading.get("verdict") or "core_reading",
        "support_role": reading.get("support_role") or "",
        "full_text_available": full_text_ready,
        "full_text_status": reading.get("full_text_status") or packet.get("full_text_status") or "",
        "pdf_text_read": bool(reading.get("pdf_text_read") or packet.get("full_text_status") == "pdf_text_read"),
        "pdf_text_chars": _as_int(reading.get("pdf_text_chars") or packet.get("full_text_chars") or packet.get("text_chars")),
        "deep_read_complete": deep_read_complete,
        "article_markdown_path": str(artifacts.get("article_markdown") or artifacts.get("read_md") or ""),
        "reading_content_source": "article_markdown" if deep_read_complete else "machine_status_only",
        "reading_status_note_zh": "已完成精读。" if deep_read_complete else "已取得正文，等待精读。" if full_text_ready else "尚未取得可验证正文。",
        "strict_input_contract": True,
        "replacement_policy": "forbidden",
    }


def _project_packet_entry(
    item: dict[str, Any],
    source_row: dict[str, Any],
    *,
    find_run_id: str,
    reading_run_id: str,
    index: int,
) -> dict[str, Any]:
    paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
    packet = dict(item.get("full_text_packet")) if isinstance(item.get("full_text_packet"), dict) else {}
    paper_index = _as_int(item.get("paper_index") or source_row.get("paper_index"), index) or index
    packet.update({
        "run_id": find_run_id,
        "source_run_id": find_run_id,
        "reading_run_id": reading_run_id,
        "input_index": index,
        "paper_index": paper_index,
        "paper_id": packet.get("paper_id") or paper.get("paper_id") or paper.get("id") or source_row.get("paper_id") or source_row.get("id") or f"paper_{index:03d}",
        "title": packet.get("title") or paper.get("title") or _title_of(source_row),
        "url": packet.get("url") or paper.get("url") or source_row.get("url") or source_row.get("abs_url") or "",
        "pdf_url": packet.get("pdf_url") or paper.get("pdf_url") or source_row.get("pdf_url") or "",
        "doi": packet.get("doi") or paper.get("doi") or source_row.get("doi") or source_row.get("published_doi") or "",
        "reading_packet_role": "ranked_find_input",
    })
    return packet


def _warning_detail(item: dict[str, Any], source_row: dict[str, Any], index: int) -> dict[str, Any] | None:
    if _item_full_text_ready(item) and _item_deep_read_complete(item):
        return None
    paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
    packet = item.get("full_text_packet") if isinstance(item.get("full_text_packet"), dict) else {}
    full_text_ready = _item_full_text_ready(item)
    error = item.get("read_error") if isinstance(item.get("read_error"), dict) else {}
    if not error and isinstance(packet.get("error"), dict):
        error = packet["error"]
    return {
        "index": int(item.get("paper_index") or index),
        "title": str(paper.get("title") or _title_of(source_row)),
        "phase": "reading_subagent" if full_text_ready else "full_text_acquisition",
        "status": str(item.get("status") or ""),
        "message": "已取得全文，等待精读" if full_text_ready else "未取得同篇全文证据",
        "full_text_ready": full_text_ready,
        "deep_read_complete": _item_deep_read_complete(item),
        "full_text_status": str(packet.get("full_text_status") or ""),
        "error_type": str(error.get("error_type") or error.get("error") or ""),
        "error_message": str(error.get("error_message") or error.get("message") or "")[:1000],
    }


def _build_validation(
    *,
    find_run_id: str,
    expected_count: int,
    find_recommendation_count: int,
    source_rows: list[dict[str, Any]],
    items: list[dict[str, Any]],
    readings: list[dict[str, Any]],
    claude_mode: str,
    limited: bool,
    public_final_artifact_present: bool,
    reading_scoring: dict[str, Any] | None = None,
    scoring_required: bool = False,
) -> dict[str, Any]:
    scoring = reading_scoring if isinstance(reading_scoring, dict) else {}
    scoring_status = str(scoring.get("status") or "not_recorded").strip()
    scoring_expected_count = _as_int(scoring.get("expected_article_count"), 0)
    scoring_scored_count = _as_int(scoring.get("scored_article_count"), 0)
    full_text_rows = [row for row in readings if row.get("full_text_available") is True]
    deep_rows = [row for row in readings if row.get("deep_read_complete") is True]
    scoring_complete = bool(
        (not scoring_required and not scoring)
        or (
            scoring_status == "complete"
            and scoring_expected_count == expected_count
            and scoring_expected_count == len(deep_rows)
            and scoring_scored_count == scoring_expected_count
        )
    )
    pending_full_text = max(0, expected_count - len(full_text_rows))
    pending_deep_titles = [str(row.get("title") or "Untitled") for row in readings if row.get("full_text_available") and not row.get("deep_read_complete")]
    details = [
        detail
        for index, item in enumerate(items, 1)
        for detail in [_warning_detail(item, source_rows[index - 1] if index <= len(source_rows) else {}, index)]
        if detail is not None
    ]
    if len(readings) < expected_count:
        details.append({
            "index": 0,
            "title": "未进入本次 Reading 的输入论文",
            "phase": "input_selection",
            "status": "warning_unprocessed_input_papers",
            "message": f"本次处理 {len(readings)}/{expected_count} 篇",
            "full_text_ready": False,
            "deep_read_complete": False,
            "full_text_status": "",
            "error_type": "",
            "error_message": "",
        })
    errors = [detail for detail in details if detail.get("error_type") or str(detail.get("status") or "").startswith("error_")]
    if not scoring_complete:
        details.append({
            "index": 0,
            "title": "final Reading scoring",
            "phase": "final_scoring",
            "status": scoring_status,
            "message": f"统一评分仅覆盖 {scoring_scored_count}/{scoring_expected_count} 篇已完成精读。",
            "expected_article_count": scoring_expected_count,
            "scored_article_count": scoring_scored_count,
        })
    read_quality_complete = bool(expected_count and len(readings) == expected_count and len(full_text_rows) == expected_count and len(deep_rows) == expected_count and scoring_complete)
    warnings: list[str] = []
    if len(readings) < expected_count:
        warnings.append(f"Read 本次处理了 {len(readings)}/{expected_count} 篇输入论文。")
    if pending_full_text:
        warnings.append(f"{pending_full_text} 篇输入论文仍缺少同篇全文证据。")
    if pending_deep_titles:
        warnings.append(f"{len(pending_deep_titles)} 篇已有全文但尚未完成精读。")
    if not scoring_complete:
        warnings.append(f"统一评分仅覆盖 {scoring_scored_count}/{scoring_expected_count} 篇，最终排名未宣称完整。")
    valid = bool(readings) and public_final_artifact_present
    status = "current_find_deep_read_complete" if read_quality_complete else "current_find_deep_read_complete_with_warnings"
    if not public_final_artifact_present:
        status = "blocked_read_md_aggregation_failed"
    return {
        "run_id": find_run_id,
        "source_run_id": find_run_id,
        "source": READING_SOURCE,
        "generated_at": _now_iso(),
        "valid": valid,
        "status": status,
        "policy_version": READING_POLICY_VERSION,
        "claude_mode": claude_mode,
        "expected_reading_count": expected_count,
        "selected_reading_count": expected_count,
        "expected_recommendation_count": find_recommendation_count,
        "recommended_reading_count": find_recommendation_count,
        "actual_reading_count": len(readings),
        "full_text_reading_count": len(full_text_rows),
        "full_text_evidence_count": len(full_text_rows),
        "deep_read_complete_count": len(deep_rows),
        "pending_full_text_reading_count": pending_full_text,
        "pending_without_evidence_count": pending_full_text,
        "pending_deep_read_synthesis_count": len(pending_deep_titles),
        "full_text_reading_titles": [str(row.get("title") or "Untitled") for row in full_text_rows[:20]],
        "pending_full_text_reading_titles": [str(row.get("title") or "Untitled") for row in readings if not row.get("full_text_available")][:20],
        "pending_deep_read_synthesis_titles": pending_deep_titles[:20],
        "read_quality_complete": read_quality_complete,
        "scoring_status": scoring_status,
        "scoring_required": scoring_required,
        "scoring_expected_count": scoring_expected_count,
        "scoring_scored_count": scoring_scored_count,
        "scoring_complete": scoring_complete,
        "public_final_artifact_present": public_final_artifact_present,
        "warning_count": len(details),
        "warnings": warnings,
        "warning_details": details[:100],
        "error_count": len(errors),
        "error_details": errors[:100],
        "blockers": [],
        "replacement_policy": "forbidden",
        "same_paper_full_text_fallback_policy": "pdf_html_xml_only",
    }


def _project_read_status(validation: dict[str, Any]) -> tuple[str, str, str]:
    if validation.get("valid") is not True:
        status = str(validation.get("status") or "blocked_current_find_read_incomplete")
        if validation.get("pending_full_text_reading_count"):
            return status, "full_text_evidence_missing", "acquire_current_find_full_text_evidence"
        if validation.get("pending_deep_read_synthesis_count"):
            return status, "claude_deep_read_required", "rerun_current_find_read"
        return status, "current_find_read_incomplete", "run_read_for_current_find"
    if validation.get("scoring_required") is True and validation.get("scoring_complete") is not True:
        return str(validation.get("status") or "current_find_deep_read_complete_with_warnings"), "reading_scoring_incomplete", "rerun_current_find_read"
    if validation.get("read_quality_complete") is not True:
        return str(validation.get("status") or "current_find_deep_read_complete_with_warnings"), "current_find_read_quality_incomplete", "rerun_current_find_read"
    return str(validation.get("status") or "current_find_deep_read_complete"), "idea_plan_artifacts_incomplete", "run_or_approve_current_find_idea_plan"


def sync_current_find_read_outputs(
    project: str,
    *,
    result_payload: dict[str, Any] | None = None,
    stdout_text: str = "",
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    reading_root: Path = DEFAULT_READING_ROOT,
) -> dict[str, Any]:
    project_root = safe_project_root(project, projects_root=projects_root)
    run_dir = _resolve_reading_run_dir(result_payload=result_payload, stdout_text=stdout_text, reading_root=reading_root)
    input_payload = _run_input_payload(run_dir)
    aggregate = _read_json(run_dir / "read_results.json", {})
    if not isinstance(aggregate, dict) or not aggregate:
        raise FileNotFoundError(f"Reading run did not produce read_results.json: {run_dir}")
    find_run_id = _payload_run_id(input_payload) or _expected_project_find_run_id(project_root)
    expected_run_id = _expected_project_find_run_id(project_root)
    source_project = str(input_payload.get("project") or "").strip()
    if source_project and source_project != project:
        raise ValueError(f"Reading input belongs to project {source_project}, not {project}.")
    if expected_run_id and find_run_id and find_run_id != expected_run_id:
        raise ValueError(f"Reading input run_id {find_run_id} does not match current Find {expected_run_id}.")
    if not find_run_id:
        raise ValueError("Cannot determine the current Find run id for Reading synchronization.")

    source_rows = _recommendation_rows(input_payload)
    items = [item for item in aggregate.get("items", []) if isinstance(item, dict)]
    item_source_rows = [_source_row_for_item(item, source_rows) for item in items]
    readings = [
        _project_reading_row(
            item,
            item_source_rows[index - 1] if index <= len(item_source_rows) else {},
            find_run_id=find_run_id,
            reading_run_id=run_dir.name,
            index=index,
        )
        for index, item in enumerate(items, 1)
    ]
    packet_entries = [
        _project_packet_entry(
            item,
            item_source_rows[index - 1] if index <= len(item_source_rows) else {},
            find_run_id=find_run_id,
            reading_run_id=run_dir.name,
            index=index,
        )
        for index, item in enumerate(items, 1)
    ]
    read_md_path = run_dir / "read.md"
    read_md_text = read_md_path.read_text(encoding="utf-8", errors="replace") if read_md_path.exists() else ""
    aggregation = aggregate.get("read_markdown_aggregation") if isinstance(aggregate.get("read_markdown_aggregation"), dict) else {}
    reading_scoring = aggregate.get("reading_scoring") if isinstance(aggregate.get("reading_scoring"), dict) else {}
    scoring_required = bool(
        input_payload.get("source") == "framework_current_find_final_ranking"
        or input_payload.get("ranking_source")
        or "max_read_papers" in input_payload
    )
    public_final = bool(aggregation.get("valid") is True and read_md_text.strip().startswith("# 论文精读"))
    # Completion is measured against the papers actually selected for this
    # Read run, not the full Find ranking or recommendation statistics.
    expected_count = max(len(source_rows), _as_int(input_payload.get("input_article_count"), len(source_rows)))
    find_recommendation_count = _as_int(input_payload.get("total_current_find_recommendation_count"), 0)
    find_ranked_count = _as_int(input_payload.get("total_current_find_ranked_count"), len(source_rows))
    validation = _build_validation(
        find_run_id=find_run_id,
        expected_count=expected_count,
        find_recommendation_count=find_recommendation_count,
        source_rows=item_source_rows,
        items=items,
        readings=readings,
        claude_mode=str(aggregation.get("mode") or "run"),
        limited=bool(input_payload.get("limited")),
        public_final_artifact_present=public_final,
        reading_scoring=reading_scoring,
        scoring_required=scoring_required,
    )
    status, failure_type, next_required_action = _project_read_status(validation)
    now = _now_iso()
    finding_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    project_read_md = finding_dir / "read.md"
    project_read_results = finding_dir / "read_results.json"
    project_full_text = finding_dir / "full_text_reading" / "full_text_packet.json"
    read_payload = {
        "run_id": find_run_id,
        "source_run_id": find_run_id,
        "source": READING_SOURCE,
        "status": status,
        "failure_type": failure_type,
        "next_required_action": next_required_action,
        "generated_at": now,
        "project": project,
        "reading_run_id": run_dir.name,
        "reading_run_dir": str(run_dir),
        "input_json": aggregate.get("input_json") or "",
        "recommendation_count": find_recommendation_count,
        "ranked_paper_count": find_ranked_count,
        "selected_reading_count": expected_count,
        "processed_reading_count": len(source_rows),
        "readings": readings,
        "reading_validation": validation,
        "warning_count": validation["warning_count"],
        "warnings": validation["warnings"],
        "warning_details": validation["warning_details"],
        "error_count": validation["error_count"],
        "error_details": validation["error_details"],
        "public_final_artifact": str(project_read_md),
        "public_final_artifact_present": public_final,
        "machine_support_artifacts": [str(project_read_results), str(project_full_text)],
        "strict_input_contract": True,
        "replacement_policy": "forbidden",
        "read_markdown_aggregation": aggregation,
        "reading_scoring": reading_scoring,
    }
    packet_payload = {
        "run_id": find_run_id,
        "source_run_id": find_run_id,
        "source": READING_SOURCE,
        "generated_at": now,
        "project": project,
        "reading_run_id": run_dir.name,
        "papers": packet_entries,
        "policy": "Framework projects the generic Reading full-text results for the current Find run; article replacement is forbidden.",
    }
    plan_payload = {
        "run_id": find_run_id,
        "source_run_id": find_run_id,
        "current_find_run_id": find_run_id,
        "source": READING_SOURCE,
        "status": status,
        "failure_type": failure_type,
        "generated_at": now,
        "project": project,
        "current_find_reading_count": len(readings),
        "current_find_idea_count": 0,
        "current_find_plan_count": 0,
        "recommended_count": find_recommendation_count,
        "expected_reading_count": expected_count,
        "selected_reading_count": expected_count,
        "full_text_reading_count": validation["full_text_reading_count"],
        "pending_full_text_reading_count": validation["pending_full_text_reading_count"],
        "pending_deep_read_synthesis_count": validation["pending_deep_read_synthesis_count"],
        "reading_validation": validation,
        "read_idea_plan_ready": False,
        "claude_current_find_ready": False,
        "next_required_action": next_required_action,
        "summary_zh": (
            f"当前 Find 已生成 {len(readings)}/{expected_count} 条 Reading 记录；"
            f"同篇全文 {validation['full_text_reading_count']} 篇，完成精读 {validation['deep_read_complete_count']} 篇；"
            f"警告 {validation['warning_count']} 项、错误 {validation['error_count']} 项。"
        ),
        "artifacts": {
            "read_md": str(project_read_md),
            "public_final_artifact": str(project_read_md),
            "read_results": str(project_read_results),
            "full_text_packet": str(project_full_text),
            "reading_run_dir": str(run_dir),
        },
    }

    _write_json(project_read_results, read_payload)
    _write_json(project_full_text, packet_payload)
    if public_final:
        _atomic_write_text(project_read_md, read_md_text)
    else:
        project_read_md.unlink(missing_ok=True)

    project_run_copy = (finding_dir / "reading_runs" / run_dir.name).resolve(strict=False)
    try:
        project_run_copy.relative_to(project_root.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"Reading run copy target escapes project root: {project_run_copy}") from exc
    if project_run_copy.exists() or project_run_copy.is_symlink():
        if project_run_copy.is_symlink() or project_run_copy.is_file():
            project_run_copy.unlink()
        else:
            shutil.rmtree(project_run_copy)
    project_run_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(run_dir, project_run_copy, ignore=shutil.ignore_patterns("__pycache__"))
    _write_json(state_dir / "current_find_claude_reading_validation.json", validation)
    _write_json(state_dir / "current_find_research_plan.json", plan_payload)
    return {
        "status": status,
        "project": project,
        "run_id": find_run_id,
        "reading_run_id": run_dir.name,
        "reading_run_dir": str(run_dir),
        "project_reading_run_dir": str(project_run_copy),
        "public_final_artifact_present": public_final,
        "valid": validation.get("valid") is True,
        "readings": len(readings),
        "full_text_reading_count": validation["full_text_reading_count"],
        "pending_full_text_reading_count": validation["pending_full_text_reading_count"],
        "pending_deep_read_synthesis_count": validation["pending_deep_read_synthesis_count"],
        "project_files": {
            "read_md": str(project_read_md),
            "read_results": str(project_read_results),
            "full_text_packet": str(project_full_text),
            "current_find_research_plan": str(state_dir / "current_find_research_plan.json"),
            "reading_validation": str(state_dir / "current_find_claude_reading_validation.json"),
        },
    }
