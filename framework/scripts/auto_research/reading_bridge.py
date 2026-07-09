from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROJECTS_ROOT = ROOT / "projects"
DEFAULT_READING_ROOT = ROOT / "modules" / "reading"


CURRENT_FIND_SYNC_FILES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("planning", "finding", "read.md"), ("planning", "finding", "read.md")),
    (("planning", "finding", "read_results.json"), ("planning", "finding", "read_results.json")),
    (("planning", "finding", "full_text_reading", "full_text_packet.json"), ("planning", "finding", "full_text_reading", "full_text_packet.json")),
    (("state", "current_find_research_plan.json"), ("state", "current_find_research_plan.json")),
    (("state", "current_find_claude_reading_validation.json"), ("state", "current_find_claude_reading_validation.json")),
)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_slug(value: Any, fallback: str = "item", max_len: int = 120) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or fallback)).strip("_.-")
    return (text or fallback)[:max_len]


def _timestamp_run_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _create_reading_run_dir(reading_root: Path) -> Path:
    output_root = reading_root / ".runtime" / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    for _attempt in range(100):
        run_id = _timestamp_run_id()
        run_dir = output_root / run_id
        try:
            run_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            continue
        _write_json(run_dir / "run_manifest.json", {
            "run_id": run_id,
            "created_at": _now_iso(),
            "creator": "framework_reading_bridge",
            "runtime_policy": "Reading run directory is created once before invoking modules/reading/main.py; all Reading outputs must remain under this fixed directory.",
        })
        return run_dir
    raise RuntimeError("Failed to create a unique timestamped Reading run directory.")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    for key in ("doi", "arxiv_id", "biorxiv_doi", "paper_id", "id"):
        value = str(row.get(key) or metadata.get(key) or "").strip().lower()
        if value:
            return f"{key}:{value}"
    return "title:" + re.sub(r"\W+", " ", _title_of(row).lower()).strip()


def _recommendation_rows(find_results: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("strong_recommendations", "recommendations", "read_candidates", "articles", "papers"):
        rows = find_results.get(key)
        if isinstance(rows, list) and rows:
            out: list[dict[str, Any]] = []
            seen: set[str] = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                ident = _identity_key(row)
                if ident in seen:
                    continue
                seen.add(ident)
                out.append(row)
            if out:
                return out
    return []


def _article_for_reading(row: dict[str, Any], index: int) -> dict[str, Any]:
    article = dict(row)
    metadata = dict(article.get("metadata")) if isinstance(article.get("metadata"), dict) else {}
    paper_id = str(article.get("paper_id") or article.get("id") or metadata.get("paper_id") or metadata.get("id") or f"current_find_{index:03d}").strip()
    article["paper_id"] = paper_id
    article["id"] = str(article.get("id") or paper_id)
    article["title"] = _title_of(article)
    article["source"] = str(article.get("source") or metadata.get("source") or metadata.get("channel") or "current_find").strip()
    article["url"] = str(article.get("url") or article.get("abs_url") or article.get("html_url") or metadata.get("url") or metadata.get("abs_url") or "").strip()
    article["pdf_url"] = str(article.get("pdf_url") or metadata.get("pdf_url") or "").strip()
    article["abstract"] = str(article.get("abstract") or article.get("abstract_en") or article.get("summary") or metadata.get("abstract") or "").strip()
    article["metadata"] = metadata
    return article


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
    limit = max(0, int(read_limit or 0))
    find_path = project_root / "planning" / "finding" / "find_results.json"
    find_results = _read_json(find_path, {})
    if not isinstance(find_results, dict) or not find_results:
        raise FileNotFoundError(f"Missing current Find results: {find_path}")
    find_run_id = _payload_run_id(find_results)
    if not find_run_id:
        raise ValueError(f"Current Find results do not contain run_id: {find_path}")
    recommendations = _recommendation_rows(find_results)
    if not recommendations:
        raise ValueError("Current Find results contain no strong_recommendations/read_candidates/articles.")
    selected = recommendations[:limit] if limit else recommendations
    run_dir = _create_reading_run_dir(reading_root)
    run_id = run_dir.name
    input_path = run_dir / "input" / "source_input.json"
    articles = [_article_for_reading(row, index) for index, row in enumerate(selected, 1)]
    payload = {
        "run_id": find_run_id,
        "source_run_id": find_run_id,
        "source": "framework_current_find_project_recommendations",
        "project": project,
        "generated_at": _now_iso(),
        "project_find_results": str(find_path),
        "reading_run_id": run_id,
        "reading_run_dir": str(run_dir),
        "total_current_find_recommendation_count": len(recommendations),
        "input_article_count": len(articles),
        "limited": bool(limit and limit < len(recommendations)),
        "articles": articles,
        "policy": "Framework/Web supplies fixed current-Find papers to Reading. Reading works only inside modules/reading and may repair same-paper PDF/HTML/XML routes, but must not replace articles.",
    }
    _write_json(input_path, payload)
    return {
        "status": "prepared_current_find_read_input",
        "project": project,
        "run_id": find_run_id,
        "reading_run_id": run_id,
        "reading_run_dir": str(run_dir),
        "input_json": str(input_path),
        "input_article_count": len(articles),
        "recommendation_count": len(recommendations),
        "limited": payload["limited"],
    }


def extract_last_json_object(text: str) -> dict[str, Any]:
    joined = str(text or "")
    for start in [idx for idx, char in enumerate(joined) if char == "{"][-12:]:
        try:
            candidate = json.loads(joined[start:])
        except Exception:
            continue
        if isinstance(candidate, dict):
            return candidate
    return {}


def _resolve_project_sync_dir(
    *,
    result_payload: dict[str, Any] | None = None,
    stdout_text: str = "",
    reading_root: Path = DEFAULT_READING_ROOT,
) -> Path:
    payload = result_payload if isinstance(result_payload, dict) else {}
    if not payload and stdout_text:
        payload = extract_last_json_object(stdout_text)
    raw = str(payload.get("runtime_project_sync_dir") or "").strip()
    if not raw and isinstance(payload.get("wrapper_result"), dict):
        raw = str(payload["wrapper_result"].get("runtime_project_sync_dir") or "").strip()
    if not raw:
        raise ValueError("Reading wrapper result does not contain runtime_project_sync_dir; refusing to sync from latest_run.")
    sync_dir = Path(raw).expanduser()
    if not sync_dir.is_absolute():
        sync_dir = reading_root / sync_dir
    return sync_dir.resolve(strict=False)


def _resolve_reading_run_dir(
    *,
    result_payload: dict[str, Any] | None = None,
    sync_dir: Path,
    reading_root: Path = DEFAULT_READING_ROOT,
) -> Path:
    payload = result_payload if isinstance(result_payload, dict) else {}
    raw = str(payload.get("reading_run_dir") or payload.get("run_dir") or "").strip()
    if raw:
        run_dir = Path(raw).expanduser()
        if not run_dir.is_absolute():
            run_dir = reading_root / run_dir
        return run_dir.resolve(strict=False)
    return sync_dir.parent.resolve(strict=False)


def _expected_project_find_run_id(project_root: Path) -> str:
    for rel in [
        ("planning", "finding", "find_progress.json"),
        ("state", "finding_frontend.json"),
        ("state", "current_find_recommendation_projection.json"),
    ]:
        payload = _read_json(project_root.joinpath(*rel), {})
        run_id = _payload_run_id(payload)
        if run_id:
            return run_id
    return ""


def sync_current_find_read_outputs(
    project: str,
    *,
    result_payload: dict[str, Any] | None = None,
    stdout_text: str = "",
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    reading_root: Path = DEFAULT_READING_ROOT,
) -> dict[str, Any]:
    project_root = safe_project_root(project, projects_root=projects_root)
    reading_runtime = (reading_root / ".runtime").resolve(strict=False)
    sync_dir = _resolve_project_sync_dir(result_payload=result_payload, stdout_text=stdout_text, reading_root=reading_root)
    reading_run_dir = _resolve_reading_run_dir(result_payload=result_payload, sync_dir=sync_dir, reading_root=reading_root)
    try:
        sync_dir.relative_to(reading_runtime)
    except ValueError as exc:
        raise ValueError(f"Reading project_sync source must stay under modules/reading/.runtime: {sync_dir}") from exc
    output_root = (reading_root / ".runtime" / "output").resolve(strict=False)
    try:
        reading_run_dir.relative_to(output_root)
    except ValueError as exc:
        raise ValueError(f"Reading run source must stay under modules/reading/.runtime/output: {reading_run_dir}") from exc
    if not re.fullmatch(r"\d{8}T\d{6}\d{6}Z", reading_run_dir.name):
        raise ValueError(f"Reading run directory name must be a UTC precise timestamp: {reading_run_dir.name}")
    if not sync_dir.is_dir():
        raise FileNotFoundError(f"Reading project_sync directory is missing: {sync_dir}")
    if not reading_run_dir.is_dir():
        raise FileNotFoundError(f"Reading run directory is missing: {reading_run_dir}")
    source_payload = _read_json(sync_dir / "state" / "current_find_research_plan.json", {})
    if not isinstance(source_payload, dict) or not source_payload:
        source_payload = _read_json(sync_dir / "planning" / "finding" / "read_results.json", {})
    source_project = str((source_payload if isinstance(source_payload, dict) else {}).get("project") or "").strip()
    source_run_id = _payload_run_id(source_payload)
    expected_run_id = _expected_project_find_run_id(project_root)
    if source_project and source_project != project:
        raise ValueError(f"Reading project_sync belongs to project {source_project}, not {project}.")
    if expected_run_id and source_run_id and source_run_id != expected_run_id:
        raise ValueError(f"Reading project_sync run_id {source_run_id} does not match current Find {expected_run_id}.")

    copied: list[dict[str, str]] = []
    missing: list[str] = []
    for source_parts, target_parts in CURRENT_FIND_SYNC_FILES:
        src = sync_dir.joinpath(*source_parts).resolve(strict=False)
        try:
            src.relative_to(sync_dir)
        except ValueError as exc:
            raise ValueError(f"Reading project_sync source escapes sync dir: {src}") from exc
        dst = project_root.joinpath(*target_parts).resolve(strict=False)
        try:
            dst.relative_to(project_root.resolve(strict=False))
        except ValueError as exc:
            raise ValueError(f"Reading project target escapes project root: {dst}") from exc
        if not src.exists():
            missing.append("/".join(source_parts))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        copied.append({"source": str(src), "target": str(dst)})
    project_run_copy_root = project_root / "planning" / "finding" / "reading_runs"
    project_run_copy = (project_run_copy_root / reading_run_dir.name).resolve(strict=False)
    try:
        project_run_copy.relative_to(project_root.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"Reading run copy target escapes project root: {project_run_copy}") from exc
    if project_run_copy.exists():
        if project_run_copy.is_symlink() or project_run_copy.is_file():
            project_run_copy.unlink()
        else:
            shutil.rmtree(project_run_copy)
    project_run_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(reading_run_dir, project_run_copy, ignore=shutil.ignore_patterns("__pycache__"))
    return {
        "status": "synced" if not missing else "partial",
        "project": project,
        "reading_run_id": reading_run_dir.name,
        "reading_run_dir": str(reading_run_dir),
        "project_reading_run_dir": str(project_run_copy),
        "runtime_project_sync_dir": str(sync_dir),
        "project_root": str(project_root),
        "copied": copied,
        "missing": missing,
    }
