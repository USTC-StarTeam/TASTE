from __future__ import annotations

from pathlib import Path


READING_ROOT = Path(__file__).resolve().parents[2]
TASTE_ROOT = READING_ROOT.parents[1]
WORKSPACE_ROOT = READING_ROOT / "workspace"
RUNS_ROOT = WORKSPACE_ROOT / "runs"


def resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def ensure_inside_reading(path: Path, *, label: str = "path") -> Path:
    candidate = resolved(path)
    root = resolved(READING_ROOT)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} 必须位于 modules/reading 下：{candidate}") from exc
    return candidate


def ensure_workspace() -> Path:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    return WORKSPACE_ROOT


def run_dir(run_id: str) -> Path:
    path = ensure_inside_reading(RUNS_ROOT / run_id, label="运行目录")
    path.mkdir(parents=True, exist_ok=True)
    return path


def relative_to_reading(path: Path) -> str:
    candidate = ensure_inside_reading(path)
    return str(candidate.relative_to(resolved(READING_ROOT)))
