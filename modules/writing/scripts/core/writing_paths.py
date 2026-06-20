from __future__ import annotations

from pathlib import Path as _WritingDevPath
import sys as _writing_sys
_WRITING_SCRIPT_ROOT = next((p for p in _WritingDevPath(__file__).resolve().parents if p.name == "scripts"), _WritingDevPath(__file__).resolve().parent)
for _writing_path in [_WRITING_SCRIPT_ROOT, *[p for p in _WRITING_SCRIPT_ROOT.iterdir() if p.is_dir()]]:
    _writing_text = str(_writing_path)
    if _writing_text not in _writing_sys.path:
        _writing_sys.path.insert(0, _writing_text)


import os
import sys
from pathlib import Path

SCRIPT_CATEGORIES = (
    "core",
    "pipeline",
    "venue",
    "rendering",
    "audit",
    "repair",
    "review",
    "maintenance",
    "orchestra_tools",
)


def module_root(start: Path | str | None = None) -> Path:
    current = Path(start or __file__).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if candidate.name == "writing" and (candidate / "scripts").is_dir():
            return candidate
    for candidate in (current, *current.parents):
        if (candidate / "main.py").is_file() and (candidate / "scripts").is_dir():
            return candidate
    raise RuntimeError("无法定位 writing 模块根目录")


MODULE_ROOT = module_root(__file__)
REPO_ROOT = MODULE_ROOT.parents[1]
SCRIPTS_ROOT = MODULE_ROOT / "scripts"
FRAMEWORK_SCRIPTS = REPO_ROOT / "framework" / "scripts"
MODULE_NAME = MODULE_ROOT.name


def local_script_dirs() -> list[Path]:
    dirs = [SCRIPTS_ROOT]
    dirs.extend(SCRIPTS_ROOT / name for name in SCRIPT_CATEGORIES if (SCRIPTS_ROOT / name).is_dir())
    return dirs


def external_script_dirs() -> list[Path]:
    modules_root = REPO_ROOT / "modules"
    dirs = [FRAMEWORK_SCRIPTS]
    for stage in ("finding", "reading", "ideation", "planning", "environment", "experimenting"):
        path = modules_root / stage / "scripts"
        if path.is_dir():
            dirs.append(path)
    return dirs


def add_script_paths() -> None:
    ordered = [*local_script_dirs(), *external_script_dirs(), MODULE_ROOT, REPO_ROOT]
    for path in reversed(ordered):
        text = str(path)
        sys.path[:] = [item for item in sys.path if item != text]
        sys.path.insert(0, text)


def pythonpath(existing: str = "") -> str:
    values = [str(p) for p in [*local_script_dirs(), *external_script_dirs(), MODULE_ROOT, REPO_ROOT, REPO_ROOT / "framework", REPO_ROOT / "web" / "backend"]]
    if existing:
        values.extend(part for part in existing.split(os.pathsep) if part)
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return os.pathsep.join(out)


def script_path(name: str | os.PathLike[str]) -> Path:
    raw = os.fspath(name).strip()
    if not raw:
        raise ValueError("script name is required")
    candidate = Path(raw)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    for directory in local_script_dirs():
        direct = directory / raw
        if direct.exists():
            return direct
    for directory in external_script_dirs():
        direct = directory / raw
        if direct.exists():
            return direct
    raise FileNotFoundError(f"writing 找不到脚本: {raw}")


class LocalScriptResolver:
    def __truediv__(self, name: str | os.PathLike[str]) -> Path:
        return script_path(name)

    def glob(self, pattern: str):
        for directory in local_script_dirs():
            yield from directory.glob(pattern)

    def exists(self) -> bool:
        return SCRIPTS_ROOT.exists()

    def __fspath__(self) -> str:
        return str(SCRIPTS_ROOT)

    def __str__(self) -> str:
        return str(SCRIPTS_ROOT)


def local_script_resolver() -> LocalScriptResolver:
    return LocalScriptResolver()


def module_action_cmd(action: str, *extra: str) -> list[str]:
    return [sys.executable, str(MODULE_ROOT / "main.py"), "--action", action, *extra]


def external_stage_cmd(stage: str, action: str, *extra: str) -> list[str]:
    return [sys.executable, str(FRAMEWORK_SCRIPTS / "run_module.py"), stage, "--action", action, *extra]
