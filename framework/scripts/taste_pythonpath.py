from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

STAGE_MODULE_DIRS = (
    "finding",
    "reading",
    "ideation",
    "planning",
    "environment",
    "experimenting",
    "writing",
)


def resolve_repo_root(start: Path | str | None = None) -> Path:
    if start is not None:
        current = Path(start).expanduser().resolve()
        if current.is_file():
            current = current.parent
        for candidate in (current, *current.parents):
            if (candidate / "framework").is_dir() and (candidate / "modules").is_dir() and (candidate / "web").is_dir():
                return candidate
        return current
    env_root = os.environ.get("WORKSPACE_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    current = Path(__file__).expanduser().resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / "framework").is_dir() and (candidate / "modules").is_dir() and (candidate / "web").is_dir():
            return candidate
    return Path(__file__).resolve().parents[2]


def taste_script_dirs(root: Path | str | None = None) -> list[Path]:
    repo = resolve_repo_root(root)
    return [repo / "framework" / "scripts", *(repo / "modules" / name / "scripts" for name in STAGE_MODULE_DIRS)]


def resolve_script_path(name: str, root: Path | str | None = None) -> Path:
    script_name = str(name or "").strip()
    if not script_name:
        raise ValueError("script name is required")
    for directory in taste_script_dirs(root):
        candidate = directory / script_name
        if candidate.exists():
            return candidate
    repo = resolve_repo_root(root)
    raise FileNotFoundError(f"TASTE script not found after module migration: {script_name} under {repo}")


class ScriptPathResolver:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = resolve_repo_root(root)

    def __truediv__(self, name: str | os.PathLike[str]) -> Path:
        return resolve_script_path(os.fspath(name), self.root)

    def glob(self, pattern: str):
        for directory in taste_script_dirs(self.root):
            yield from directory.glob(pattern)

    def exists(self) -> bool:
        return all(directory.exists() for directory in taste_script_dirs(self.root))

    def __fspath__(self) -> str:
        return str(self.root / "framework" / "scripts")

    def __str__(self) -> str:
        return self.__fspath__()


def script_resolver(root: Path | str | None = None) -> ScriptPathResolver:
    return ScriptPathResolver(root)


def taste_pythonpath_entries(root: Path | str | None = None) -> list[Path]:
    repo = resolve_repo_root(root)
    entries: list[Path] = [
        repo / "framework",
        repo / "web" / "backend",
    ]
    entries.extend(repo / "modules" / name for name in STAGE_MODULE_DIRS)
    entries.append(repo)
    entries.extend(taste_script_dirs(repo))
    return entries


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def taste_pythonpath_string(root: Path | str | None = None, existing: str = "") -> str:
    values = [str(path) for path in taste_pythonpath_entries(root)]
    if existing:
        values.extend(part for part in existing.split(os.pathsep) if part)
    return os.pathsep.join(_dedupe_strings(values))


def ensure_taste_pythonpath(root: Path | str | None = None) -> list[str]:
    entries = [str(path) for path in taste_pythonpath_entries(root)]
    for entry in reversed(entries):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return entries
