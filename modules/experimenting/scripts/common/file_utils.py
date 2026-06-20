#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def slugify(value: Any, fallback: str = "experiment", limit: int = 96) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._-")
    return (text[:limit].strip("._-") or fallback)


def compact_text(value: Any, limit: int = 500) -> str:
    if isinstance(value, (list, tuple)):
        text = "; ".join(str(item) for item in value if str(item).strip())
    elif isinstance(value, dict):
        text = "; ".join(f"{key}={val}" for key, val in value.items() if not isinstance(val, (dict, list)))
    else:
        text = str(value or "")
    text = " ".join(text.replace("\n", " ").split())
    return text[:limit] + ("..." if len(text) > limit else "")
