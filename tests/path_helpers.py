from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE_MODULES = (
    "finding",
    "reading",
    "ideation",
    "planning",
    "environment",
    "experimenting",
    "writing",
)


def script_dirs() -> list[Path]:
    return [ROOT / "framework" / "scripts", *(ROOT / "modules" / name / "scripts" for name in STAGE_MODULES)]


def ensure_script_paths() -> None:
    for path in reversed(script_dirs()):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)


def script_path(name: str) -> Path:
    ensure_script_paths()
    filename = name if name.endswith((".py", ".sh")) else f"{name}.py"
    for directory in script_dirs():
        candidate = directory / filename
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"TASTE script not found in migrated script dirs: {filename}")


def load_script(name: str):
    path = script_path(name)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module
