from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from runtime.framework_io import read_json_default_strict as read_json
from runtime.framework_paths import RUNS_SEARCH_DIRS, ensure_directories


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


def redacted_config(data: dict[str, Any]) -> dict[str, Any]:
    secret_keys = {"api_key", "smtp_password", "password", "sender_password"}

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: ("********" if key in secret_keys and item else redact(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    return redact(dict(data))


def list_runs() -> list[dict[str, Any]]:
    ensure_directories()
    items: list[dict[str, Any]] = []
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
    shutil.rmtree(run_dir(run_id))
    return True
