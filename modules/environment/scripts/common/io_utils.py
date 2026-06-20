from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def json_safe(value: Any, _seen: set[int] | None = None) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if _seen is None:
        _seen = set()
    if isinstance(value, dict):
        value_id = id(value)
        if value_id in _seen:
            return "<circular-reference>"
        _seen.add(value_id)
        try:
            return {str(key): json_safe(item, _seen) for key, item in value.items()}
        finally:
            _seen.remove(value_id)
    if isinstance(value, (list, tuple, set)):
        value_id = id(value)
        if value_id in _seen:
            return ["<circular-reference>"]
        _seen.add(value_id)
        try:
            return [json_safe(item, _seen) for item in value]
        finally:
            _seen.remove(value_id)
    return str(value)


def read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_text_limited(path: Path, limit: int = 20000) -> str:
    try:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except Exception:
        return ""
    return ""


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def slugify(value: str, default: str = "item") -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip()).strip("._-")
    return text[:96] or default


def short_hash(value: str, length: int = 10) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:length]


def coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def ensure_within(child: Path, parent: Path) -> Path:
    resolved_child = child.expanduser().resolve()
    resolved_parent = parent.expanduser().resolve()
    if resolved_child == resolved_parent or resolved_parent in resolved_child.parents:
        return resolved_child
    raise ValueError(f"路径越界：{resolved_child} 不在 {resolved_parent} 内")


def newest_existing(paths: list[Path]) -> Path | None:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)
