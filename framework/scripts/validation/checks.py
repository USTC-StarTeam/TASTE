from __future__ import annotations

from typing import Any


def check_result(
    name: str,
    ok: bool,
    *,
    severity: str = "block",
    evidence: Any = None,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "id": name,
        "name": name,
        "status": "pass" if ok else severity,
        "severity": "pass" if ok else severity,
        "detail": detail,
        "evidence": evidence or [],
    }
