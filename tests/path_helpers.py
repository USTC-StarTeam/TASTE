from __future__ import annotations

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


def module_import_dirs() -> list[Path]:
    return [
        ROOT / "web" / "backend",
        *(ROOT / "modules" / name for name in STAGE_MODULES),
    ]


def framework_script_import_dirs() -> list[Path]:
    scripts = ROOT / "framework" / "scripts"
    return [scripts]


def ensure_script_paths() -> None:
    for path in reversed([ROOT, *framework_script_import_dirs(), *module_import_dirs()]):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
