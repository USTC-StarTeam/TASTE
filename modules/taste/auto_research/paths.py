from __future__ import annotations

import os
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
REFERENCE_ROOT = PROJECT_ROOT / "reference_repo"
PACKAGE_RUNTIME_DIR = PROJECT_ROOT / "auto_research"
ROOT = PACKAGE_DIR.parents[2]
WORKFLOW_RUNTIME_DIR = Path(os.environ.get("WORKFLOW_RUNTIME_DIR") or ROOT / "runtime" / "auto_research").expanduser()
LEGACY_WORKFLOW_RUNTIME_DIR = PACKAGE_RUNTIME_DIR
DATA_DIR = PACKAGE_RUNTIME_DIR / "data"
RUNS_DIR = WORKFLOW_RUNTIME_DIR / "runs"
STATE_DIR = WORKFLOW_RUNTIME_DIR / "state"
LOCAL_DATABASE_DIR = WORKFLOW_RUNTIME_DIR / "local_database"
CONFIG_PATH = WORKFLOW_RUNTIME_DIR / ".config.json"
LEGACY_RUNS_DIR = LEGACY_WORKFLOW_RUNTIME_DIR / "runs"
LEGACY_STATE_DIR = LEGACY_WORKFLOW_RUNTIME_DIR / "state"
LEGACY_LOCAL_DATABASE_DIR = LEGACY_WORKFLOW_RUNTIME_DIR / "local_database"
LEGACY_CONFIG_PATH = LEGACY_WORKFLOW_RUNTIME_DIR / ".config.json"


def ensure_directories() -> None:
    for path in (
        RUNS_DIR,
        STATE_DIR,
        WORKFLOW_RUNTIME_DIR / "auto_find",
        WORKFLOW_RUNTIME_DIR / "auto_read",
        WORKFLOW_RUNTIME_DIR / "auto_idea",
        WORKFLOW_RUNTIME_DIR / "auto_plan",
        LOCAL_DATABASE_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def stage_latest_path(stage: str, filename: str) -> Path:
    return WORKFLOW_RUNTIME_DIR / stage / filename
