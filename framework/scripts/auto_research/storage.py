from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .paths import ROOT, RUNS_DIR, RUNS_SEARCH_DIRS, ensure_directories, stage_latest_path


def utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def create_run_dir(prefix: str = "run") -> tuple[str, Path]:
    ensure_directories()
    run_id = f"{prefix}_{utc_run_id()}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_id, run_dir


def run_dir(run_id: str) -> Path:
    for runs_root in RUNS_SEARCH_DIRS:
        path = runs_root / run_id
        if path.exists():
            return path
    raise FileNotFoundError(f"Run not found: {run_id}")


def write_json(path: Path, data: Any) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            tmp_name = handle.name
            handle.write(payload)
        os.replace(tmp_name, path)
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def redacted_config(data: dict[str, Any]) -> dict[str, Any]:
    secret_keys = {"api_key", "smtp_password", "password", "sender_password"}

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: ("********" if key in secret_keys and item else redact(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    return redact(dict(data))


def _payload_run_id(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    return str(data.get("run_id") or data.get("source_run_id") or data.get("find_run_id") or data.get("current_find_run_id") or "").strip()


def _json_payload_run_id(path: Path) -> str:
    if path.suffix.lower() != ".json" or not path.exists():
        return ""
    try:
        return _payload_run_id(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return ""


def _project_current_find_run_id(project_root: Path) -> str:
    for rel in [
        Path("planning/finding/find_progress.json"),
        Path("planning/finding/find_results.json"),
        Path("state/current_find_research_plan.json"),
        Path("planning/finding/ideas.json"),
        Path("planning/finding/plans.json"),
        Path("planning/finding/read_results.json"),
    ]:
        run_id = _json_payload_run_id(project_root / rel)
        if run_id:
            return run_id
    return ""


PROJECT_SYNC_MARKDOWN_WITH_RUN_CONTEXT = {
    "find.md",
    "source_status.md",
    "biorxiv.md",
    "nature.md",
    "science.md",
    "hf.md",
    "github.md",
    "read.md",
    "read_results.md",
    "idea.md",
    "plan.md",
    "plans.md",
}


def _source_run_id_for_project_sync(source_path: Path, filename: str) -> str:
    run_id = _json_payload_run_id(source_path)
    if run_id:
        return run_id
    if filename in PROJECT_SYNC_MARKDOWN_WITH_RUN_CONTEXT:
        parent = source_path.parent
        for sibling in ["find_results.json", "read_results.json", "ideas.json", "plans.json", "find_progress.json"]:
            run_id = _json_payload_run_id(parent / sibling)
            if run_id:
                return run_id
    return ""


def _project_sync_allowed(project_root: Path, source_path: Path, filename: str) -> bool:
    source_run_id = _source_run_id_for_project_sync(source_path, filename)
    current_run_id = _project_current_find_run_id(project_root)
    if not source_run_id:
        return False
    if current_run_id:
        return source_run_id == current_run_id

    # Empty project bootstrap: allow the first artifact to establish the
    # project-level current packet, but do not let an unrelated historical run
    # overwrite a packet that already contains another run id.
    taste_dir = project_root / "planning" / "finding"
    known_run_ids = {
        run_id
        for rel in ["find_results.json", "find_progress.json", "read_results.json", "ideas.json", "plans.json"]
        for run_id in [_json_payload_run_id(taste_dir / rel)]
        if run_id
    }
    return not known_run_ids or known_run_ids == {source_run_id}


def sync_latest(stage: str, filename: str, source_path: Path) -> None:
    target = stage_latest_path(stage, filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, target)

    # The project literature frontend syncs only current-Find artifacts. Only sync into
    # the active project when the artifact belongs to the same current Find run;
    # unit tests, temporary runs, and historical runs must not overwrite the
    # project-level current-Find packet just because PROJECT_ID is set.
    project = (
        os.environ.get("PROJECT_ID")
        or os.environ.get("PROJECT_ID")
        or os.environ.get("DEFAULT_PROJECT_ID")
        or ""
    ).strip()
    root = Path(os.environ.get("WORKSPACE_ROOT") or ROOT).expanduser()
    if project:
        project_root = root / "projects" / project
        if not _project_sync_allowed(project_root, source_path, filename):
            return
        project_target = project_root / "planning" / "finding" / filename
        try:
            project_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, project_target)
        except OSError:
            pass


def list_runs() -> list[dict]:
    ensure_directories()
    items = []
    seen: set[str] = set()
    for runs_root in RUNS_SEARCH_DIRS:
        if not runs_root.exists():
            continue
        for path in runs_root.iterdir():
            if not path.is_dir() or path.name in seen:
                continue
            seen.add(path.name)
            manifest = read_json(path / "manifest.json", {})
            items.append({
                "run_id": path.name,
                "created_at": manifest.get("created_at", ""),
                "stages": manifest.get("stages", []),
                "path": str(path),
            })
    return sorted(items, key=lambda item: item["run_id"], reverse=True)


def delete_run(run_id: str) -> bool:
    path = run_dir(run_id)
    shutil.rmtree(path)
    return True


def update_manifest(path: Path, stage: str) -> None:
    manifest_path = path / "manifest.json"
    manifest = read_json(manifest_path, {})
    manifest.setdefault("created_at", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    stages = manifest.setdefault("stages", [])
    if stage not in stages:
        stages.append(stage)
    write_json(manifest_path, manifest)
