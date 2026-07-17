from __future__ import annotations

import os
from pathlib import Path


def _repo_root(start: Path) -> Path:
    env_root = os.environ.get("WORKSPACE_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    current = start.expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "modules").is_dir() and (candidate / "framework").is_dir() and (candidate / "web").is_dir():
            return candidate
    return Path(__file__).resolve().parents[2]


PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = _repo_root(PACKAGE_DIR)
FRAMEWORK_ROOT = ROOT / "framework"
FRAMEWORK_RUNTIME_DIR = Path(os.environ.get("FRAMEWORK_RUNTIME_DIR") or FRAMEWORK_ROOT / ".runtime").expanduser()
WEB_RUNTIME_DIR = Path(os.environ.get("WEB_RUNTIME_DIR") or ROOT / "web" / ".runtime").expanduser()
WORKFLOW_RUNTIME_DIR = Path(os.environ.get("WORKFLOW_RUNTIME_DIR") or FRAMEWORK_RUNTIME_DIR).expanduser()
FRAMEWORK_INPUTS_DIR = FRAMEWORK_RUNTIME_DIR / "inputs"
FRAMEWORK_LOCKS_DIR = FRAMEWORK_RUNTIME_DIR / "locks"
RUNS_DIR = WORKFLOW_RUNTIME_DIR / "runs"
STATE_DIR = WEB_RUNTIME_DIR / "state"
LOCAL_DATABASE_DIR = WORKFLOW_RUNTIME_DIR / "local_database"
CONFIG_PATH = FRAMEWORK_RUNTIME_DIR / ".config.json"
FINDING_RUNTIME_DIR = Path(os.environ.get("FINDING_RUNTIME_DIR") or ROOT / "modules" / "finding" / ".runtime").expanduser()
FINDING_RUNS_DIR = FINDING_RUNTIME_DIR / "runs"
RUNS_SEARCH_DIRS = tuple(dict.fromkeys([RUNS_DIR, FINDING_RUNS_DIR]))


def ensure_directories() -> None:
    for path in (
        RUNS_DIR,
        STATE_DIR,
        WORKFLOW_RUNTIME_DIR / "finding",
        WORKFLOW_RUNTIME_DIR / "reading",
        WORKFLOW_RUNTIME_DIR / "ideation",
        WORKFLOW_RUNTIME_DIR / "planning",
        LOCAL_DATABASE_DIR,
        FRAMEWORK_INPUTS_DIR,
        FRAMEWORK_LOCKS_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
