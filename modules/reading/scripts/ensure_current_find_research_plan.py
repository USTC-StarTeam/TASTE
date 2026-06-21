from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_reading_script_path() -> None:
    root = Path(__file__).resolve().parents[3]
    scripts = Path(__file__).resolve().parent
    entries: list[Path] = [
        root / "modules" / "reading",
        scripts,
        *(path for path in sorted(scripts.iterdir()) if path.is_dir() and path.name != "__pycache__"),
        root / "framework",
        root / "framework" / "scripts",
        root / "web" / "backend",
        root,
    ]
    for stage_dir in sorted((root / "modules").iterdir()):
        if stage_dir.is_dir():
            entries.extend([stage_dir, stage_dir / "scripts"])
    for entry in [str(path) for path in reversed(entries) if path.exists()]:
        while entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)


_bootstrap_reading_script_path()
from orchestration.ensure_current_find_research_plan import *  # noqa: F401,F403


if __name__ == "__main__":
    raise SystemExit(main())
