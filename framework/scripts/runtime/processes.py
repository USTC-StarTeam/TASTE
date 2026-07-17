from __future__ import annotations

from typing import Any


def command_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(part) for part in value)
    return str(value or "")
