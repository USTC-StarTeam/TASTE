from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(text), encoding="utf-8")


def safe_slug(value: Any, fallback: str = "paper", max_len: int = 90) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or fallback)).strip("_.-")
    return (text or fallback)[:max_len]


def coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return [part.strip() for part in re.split(r"[,;]", str(value or "")) if part.strip()]


def first_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", raw, re.I | re.S):
        block = match.group(1).strip()
        try:
            payload = json.loads(block)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}
