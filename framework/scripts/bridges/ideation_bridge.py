from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from policies.current_find_route import current_find_run_id as _project_current_find_run_id
from policies.current_find_route import payload_run_id as _payload_run_id
from project.project_paths import require_project_root as _safe_project_root
from runtime.framework_io import read_json_default_strict as _read_json
from runtime.framework_paths import FRAMEWORK_INPUTS_DIR, ROOT


def _write_json(path: Path, data: Any) -> None:
    _write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(text))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def extract_last_json_object(stdout_text: str) -> dict[str, Any]:
    text = str(stdout_text or "")
    decoder = json.JSONDecoder()
    for start in range(len(text) - 1, -1, -1):
        if text[start] != "{":
            continue
        try:
            data, end = decoder.raw_decode(text[start:])
        except Exception:
            continue
        if isinstance(data, dict) and not text[start + end :].strip():
            return data
    return {}


def _read_markdown(path: Path, limit: int = 120_000) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= limit else text[:limit] + "\n\n[Framework truncated this input for Ideation.]\n"


def _source_rows(find_results: dict[str, Any], read_results: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    row_indexes: dict[str, int] = {}

    def append(item: Any, source: str) -> None:
        if not isinstance(item, dict):
            return
        title = str(item.get("title") or item.get("paper_title") or item.get("name") or "").strip()[:500]
        if not title:
            return
        url = str(item.get("url") or item.get("pdf_url") or item.get("html_url") or "").strip()[:2_000]
        key = re.sub(r"\W+", " ", title.casefold()).strip()
        if not key:
            return
        summary = item.get("summary") or item.get("reason") or item.get("fit_explanation") or item.get("abstract") or ""
        hit_directions = item.get("hit_directions") if isinstance(item.get("hit_directions"), list) else []
        row = {
            "source": source,
            "title": title,
            "url": url,
            "summary": str(summary)[:4_000],
            "score": item.get("score", ""),
            "fit_score": item.get("fit_score", ""),
            "hit_directions": [str(value)[:300] for value in hit_directions[:20]],
        }
        existing_index = row_indexes.get(key)
        if existing_index is None:
            row_indexes[key] = len(rows)
            rows.append(row)
            return
        existing = rows[existing_index]
        if source == "read":
            rows[existing_index] = {field: value if value not in ("", [], None) else existing.get(field, value) for field, value in row.items()}
        else:
            for field, value in row.items():
                if existing.get(field) in ("", [], None) and value not in ("", [], None):
                    existing[field] = value

    for item in read_results.get("readings", []) if isinstance(read_results.get("readings"), list) else []:
        append(item, "read")
    for key in ("strong_recommendations", "recommendations", "read_candidates", "articles", "huggingface", "github"):
        for item in find_results.get(key, []) if isinstance(find_results.get(key), list) else []:
            append(item, key)
    return rows[:500]


def prepare_current_find_ideation_input(
    project: str,
    *,
    requested_run_id: str = "",
    projects_root: Path | None = None,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    """Validate current-Find inputs and create the normalized bundle consumed by Ideation."""
    projects_root = projects_root or ROOT / "projects"
    project_root = _safe_project_root(project, projects_root)
    taste_dir = project_root / "planning" / "finding"
    find_path = taste_dir / "find_results.json"
    read_path = taste_dir / "read_results.json"
    read_md_path = taste_dir / "read.md"
    validation_path = project_root / "state" / "current_find_claude_reading_validation.json"

    find_results = _read_json(find_path, {})
    read_results = _read_json(read_path, {})
    validation = _read_json(validation_path, {})
    if not isinstance(find_results, dict) or not find_results:
        raise FileNotFoundError(f"Current Find results are missing: {find_path}")
    run_id = _payload_run_id(find_results) or _project_current_find_run_id(project_root)
    requested = str(requested_run_id or "").strip()
    if not run_id:
        raise ValueError(f"Current Find results do not contain run_id: {find_path}")
    if requested and requested != run_id:
        raise ValueError(f"Requested Ideation run_id {requested} does not match current Find {run_id}.")
    if not isinstance(read_results, dict) or _payload_run_id(read_results) != run_id:
        raise ValueError("Current Find Read results are missing or stale; run Read before Ideas.")
    if read_results.get("public_final_artifact_present") is not True:
        raise ValueError("Current Find Read public artifact is incomplete; run or repair Read before Ideas.")
    if not isinstance(validation, dict) or _payload_run_id(validation) != run_id or validation.get("valid") is not True:
        raise ValueError("Current Find Read validation has not passed; run or repair Read before Ideas.")
    read_markdown = _read_markdown(read_md_path)
    if not read_markdown.lstrip().startswith("# 论文精读"):
        raise ValueError(f"Current Find read.md is missing or invalid: {read_md_path}")
    items = _source_rows(find_results, read_results)
    if not items:
        raise ValueError("Current Find/Read artifacts contain no usable Ideation evidence.")

    bundle = {
        "schema_version": "taste.ideation_input.v1",
        "run_id": run_id,
        "source_run_id": run_id,
        "project": project,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "read_markdown": read_markdown,
        "source_manifest": {
            "find_results": str(find_path),
            "read_results": str(read_path),
            "read_markdown": str(read_md_path),
            "reading_validation": str(validation_path),
        },
    }
    runtime_root = runtime_root or FRAMEWORK_INPUTS_DIR / "ideation"
    runtime_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=runtime_root,
        prefix="current_find_",
        suffix=".json",
        delete=False,
    ) as handle:
        json.dump(bundle, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        input_path = Path(handle.name)
    return {
        "status": "prepared_current_find_ideation_input",
        "project": project,
        "run_id": run_id,
        "input_json": str(input_path),
        "item_count": len(items),
    }


def remove_prepared_ideation_input(path: str | Path) -> None:
    candidate = Path(path).expanduser()
    expected_root = (FRAMEWORK_INPUTS_DIR / "ideation").resolve(strict=False)
    try:
        candidate.resolve(strict=False).relative_to(expected_root)
    except ValueError:
        return
    candidate.unlink(missing_ok=True)


def current_find_ideation_run_dir(
    project: str,
    *,
    requested_run_id: str = "",
    projects_root: Path | None = None,
    ideation_root: Path | None = None,
) -> Path:
    projects_root = projects_root or ROOT / "projects"
    ideation_root = ideation_root or ROOT / "modules" / "ideation"
    project_root = _safe_project_root(project, projects_root)
    payload = _read_json(project_root / "planning" / "finding" / "ideas.json", {})
    run_id = _payload_run_id(payload)
    requested = str(requested_run_id or "").strip()
    if not isinstance(payload, dict) or not run_id:
        raise FileNotFoundError("Current project has no generated Ideation artifact.")
    current_run_id = _project_current_find_run_id(project_root)
    if not current_run_id:
        raise ValueError("Project does not identify a current Find run.")
    if run_id != current_run_id:
        raise ValueError(f"Current Ideation artifact is stale: ideas run {run_id}, current Find {current_run_id}.")
    if requested and requested != run_id:
        raise ValueError(f"Requested Ideation edit run_id {requested} does not match current Find {run_id}.")
    timestamp_id = str(payload.get("ideation_run_id") or "").strip()
    if not re.fullmatch(r"\d{8}T\d{12}Z", timestamp_id):
        raise ValueError("Current ideas.json does not identify a valid timestamped Ideation run.")
    run_dir = (ideation_root / ".runtime" / "output" / timestamp_id).resolve(strict=False)
    output_root = (ideation_root / ".runtime" / "output").resolve(strict=False)
    try:
        run_dir.relative_to(output_root)
    except ValueError as exc:
        raise ValueError(f"Ideation run escapes module output root: {run_dir}") from exc
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Ideation run no longer exists: {run_dir}")
    return run_dir


def _idea_text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _inspired_rows(row: dict[str, Any]) -> list[Any]:
    value = row.get("inspired_by") or row.get("supporting_papers") or row.get("positive_anchor_papers") or []
    return value if isinstance(value, list) else []


def _idea_ready_for_state(row: dict[str, Any]) -> bool:
    method = _idea_text(row, "new_method", "method_details")
    experiment = _idea_text(row, "initial_experiment")
    return len(method) >= 40 and len(experiment) >= 40 and bool(_inspired_rows(row))


def _module_run_dir(result_payload: dict[str, Any], ideation_root: Path) -> Path:
    result = result_payload.get("result") if isinstance(result_payload.get("result"), dict) else result_payload
    result = result if isinstance(result, dict) else {}
    run_dir_text = str(result.get("run_dir") or result.get("output_dir") or "").strip()
    if not run_dir_text:
        run_id_text = str(result.get("ideation_run_id") or "").strip()
        if run_id_text:
            run_dir_text = str(ideation_root / ".runtime" / "output" / run_id_text)
    if not run_dir_text:
        raise ValueError("Ideation result did not include result.run_dir or result.ideation_run_id.")
    run_dir = Path(run_dir_text).expanduser().resolve()
    output_root = (ideation_root / ".runtime" / "output").resolve()
    try:
        run_dir.relative_to(output_root)
    except ValueError as exc:
        raise ValueError(f"Ideation run_dir is outside .runtime/output: {run_dir}") from exc
    if run_dir.name == "latest_run":
        raise ValueError("latest_run is a human review copy and cannot be synced as a program run.")
    if not re.fullmatch(r"\d{8}T\d{12}Z", run_dir.name):
        raise ValueError(f"Ideation result is not a timestamped module run: {run_dir.name}")
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Ideation run_dir does not exist: {run_dir}")
    return run_dir


def sync_current_find_ideation_outputs(
    project: str,
    *,
    result_payload: dict[str, Any],
    projects_root: Path | None = None,
    ideation_root: Path | None = None,
) -> dict[str, Any]:
    projects_root = projects_root or ROOT / "projects"
    ideation_root = ideation_root or ROOT / "modules" / "ideation"
    project_root = _safe_project_root(project, projects_root)
    taste_dir = project_root / "planning" / "finding"
    run_dir = _module_run_dir(result_payload, ideation_root)
    ideas_payload = _read_json(run_dir / "ideas.json", {})
    if not isinstance(ideas_payload, dict):
        raise ValueError(f"Ideation ideas.json is not an object: {run_dir / 'ideas.json'}")
    run_id = _payload_run_id(ideas_payload)
    if str(ideas_payload.get("ideation_run_id") or "").strip() != run_dir.name:
        raise ValueError("Ideation ideas.json timestamp does not match the explicit module run directory.")
    if ideas_payload.get("machine_projection_from") != "idea.md":
        raise ValueError("Ideation ideas.json is not declared as a projection of idea.md.")
    manifest = _read_json(run_dir / "manifest.json", {})
    if not isinstance(manifest, dict):
        raise ValueError(f"Ideation manifest.json is not an object: {run_dir / 'manifest.json'}")
    if str(manifest.get("run_id") or "").strip() != run_dir.name:
        raise ValueError("Ideation manifest run_id does not match the explicit module run directory.")
    if str(manifest.get("source_run_id") or "").strip() != run_id:
        raise ValueError("Ideation manifest source_run_id does not match ideas.json.")
    if manifest.get("public_final_artifact") != "idea.md":
        raise ValueError("Ideation manifest does not declare idea.md as the public final artifact.")
    current_run_id = _project_current_find_run_id(project_root)
    if not current_run_id:
        raise ValueError("Project does not identify a current Find run for Ideation sync.")
    if run_id != current_run_id:
        raise ValueError(f"Ideation run_id {run_id} does not match project current Find run {current_run_id}.")
    idea_md = (run_dir / "idea.md").read_text(encoding="utf-8") if (run_dir / "idea.md").exists() else ""
    if not idea_md:
        raise FileNotFoundError(f"Ideation public artifact missing: {run_dir / 'idea.md'}")

    project_run_dir = taste_dir / "ideation_runs" / run_dir.name
    project_run_dir.parent.mkdir(parents=True, exist_ok=True)
    project_run_tmp = project_run_dir.parent / f".{run_dir.name}.tmp_{os.getpid()}"
    if project_run_tmp.exists():
        shutil.rmtree(project_run_tmp)
    try:
        shutil.copytree(run_dir, project_run_tmp)
        if project_run_dir.exists():
            shutil.rmtree(project_run_dir)
        project_run_tmp.rename(project_run_dir)
    finally:
        if project_run_tmp.exists():
            shutil.rmtree(project_run_tmp, ignore_errors=True)

    _write_json(taste_dir / "ideas.json", ideas_payload)
    _write_text(taste_dir / "idea.md", idea_md)

    ideas = [row for row in ideas_payload.get("ideas", []) if isinstance(row, dict)]
    now = str(ideas_payload.get("human_supervision_updated_at") or datetime.now(timezone.utc).isoformat())
    state_path = project_root / "state" / "current_find_research_plan.json"
    state = _read_json(state_path, {})
    if isinstance(state, dict) and (_payload_run_id(state) in {"", run_id}):
        state["run_id"] = run_id
        state.pop("ideas", None)
        state["current_find_idea_count"] = len(ideas)
        state["idea_schema_ready"] = bool(ideas) and all(_idea_ready_for_state(row) for row in ideas)
        state["human_supervision_updated_at"] = now
        state["human_supervision_source"] = ideas_payload.get("human_supervision_source") or "web_ideas_three_column_editor"
        state["ideation_run_id"] = run_dir.name
        _write_json(state_path, state)

    return {
        "status": "ok",
        "project": project,
        "run_id": run_id,
        "ideation_run_id": run_dir.name,
        "module_run_dir": str(run_dir),
        "runtime_project_sync_dir": str(project_run_dir),
        "artifacts": {
            "ideas": str(taste_dir / "ideas.json"),
            "idea_md": str(taste_dir / "idea.md"),
        },
        "idea_count": len(ideas),
    }
