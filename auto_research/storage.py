from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .paths import RUNS_DIR, ensure_directories, stage_latest_path


def utc_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")


def create_run_dir(prefix: str = "run") -> tuple[str, Path]:
    ensure_directories()
    run_id = f"{prefix}_{utc_run_id()}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_id, run_dir


def run_dir(run_id: str) -> Path:
    path = RUNS_DIR / run_id
    if not path.exists():
        raise FileNotFoundError(f"Run not found: {run_id}")
    return path


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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


def sync_latest(stage: str, filename: str, source_path: Path) -> None:
    target = stage_latest_path(stage, filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, target)


def list_runs() -> list[dict]:
    ensure_directories()
    items = []
    for path in RUNS_DIR.iterdir():
        if not path.is_dir():
            continue
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
    manifest.setdefault("created_at", datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    stages = manifest.setdefault("stages", [])
    if stage not in stages:
        stages.append(stage)
    write_json(manifest_path, manifest)
