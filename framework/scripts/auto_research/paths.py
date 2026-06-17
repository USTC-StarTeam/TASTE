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
PROJECT_ROOT = ROOT / "framework"
REFERENCE_ROOT = ROOT / "third_party" / "reference_TASTE_latest"
PACKAGE_RUNTIME_DIR = PROJECT_ROOT / "scripts" / "auto_research"
WORKFLOW_RUNTIME_DIR = Path(os.environ.get("WORKFLOW_RUNTIME_DIR") or ROOT / "runtime").expanduser()
LEGACY_WORKFLOW_RUNTIME_DIR = WORKFLOW_RUNTIME_DIR
DATA_DIR = ROOT / "modules" / "finding" / "data"
RUNS_DIR = WORKFLOW_RUNTIME_DIR / "runs"
STATE_DIR = WORKFLOW_RUNTIME_DIR / "state"
LOCAL_DATABASE_DIR = WORKFLOW_RUNTIME_DIR / "local_database"
CONFIG_PATH = WORKFLOW_RUNTIME_DIR / ".config.json"
LEGACY_RUNS_DIR = LEGACY_WORKFLOW_RUNTIME_DIR / "runs"
LEGACY_STATE_DIR = LEGACY_WORKFLOW_RUNTIME_DIR / "state"
LEGACY_LOCAL_DATABASE_DIR = LEGACY_WORKFLOW_RUNTIME_DIR / "local_database"
LEGACY_CONFIG_PATH = LEGACY_WORKFLOW_RUNTIME_DIR / ".config.json"
LEGACY_RUNS_DIRS = (LEGACY_RUNS_DIR,) if LEGACY_RUNS_DIR != RUNS_DIR else tuple()


def ensure_directories() -> None:
    for path in (
        RUNS_DIR,
        STATE_DIR,
        WORKFLOW_RUNTIME_DIR / "finding",
        WORKFLOW_RUNTIME_DIR / "reading",
        WORKFLOW_RUNTIME_DIR / "ideation",
        WORKFLOW_RUNTIME_DIR / "planning",
        LOCAL_DATABASE_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def stage_latest_path(stage: str, filename: str) -> Path:
    return WORKFLOW_RUNTIME_DIR / stage / filename
