from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IDEATION_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = IDEATION_ROOT / "scripts"
RUNS_ROOT = IDEATION_ROOT / "runs"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_run_id(value: str = "", prefix: str = "ideation") -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._-")
    if text:
        return text[:96]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}"


def ensure_inside_ideation(path: Path) -> Path:
    target = path.expanduser()
    probe = target if target.exists() else target.parent
    resolved_probe = probe.resolve()
    root = IDEATION_ROOT.resolve()
    try:
        resolved_probe.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"写入路径必须位于 modules/ideation 内：{target}") from exc
    return target


def ensure_parent(path: Path) -> None:
    ensure_inside_ideation(path)
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data: Any) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    ensure_parent(path)
    path.write_text(str(text), encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def make_run_dir(run_id: str = "", output_root: str | Path | None = None) -> Path:
    root = ensure_inside_ideation(Path(output_root).expanduser()) if output_root else RUNS_ROOT
    run_dir = ensure_inside_ideation(root / safe_run_id(run_id))
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def public_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(IDEATION_ROOT.resolve()))
    except ValueError:
        return str(path)


def compact_text(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float, bool)):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]