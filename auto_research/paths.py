from __future__ import annotations

from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
REFERENCE_ROOT = PROJECT_ROOT / "reference_repo"
AUTO_RESEARCH_DIR = PROJECT_ROOT / "auto_research"
DATA_DIR = AUTO_RESEARCH_DIR / "data"
RUNS_DIR = AUTO_RESEARCH_DIR / "runs"
STATE_DIR = AUTO_RESEARCH_DIR / "state"
CONFIG_PATH = AUTO_RESEARCH_DIR / ".config.json"


def ensure_directories() -> None:
    for path in (
        RUNS_DIR,
        STATE_DIR,
        AUTO_RESEARCH_DIR / "auto_find",
        AUTO_RESEARCH_DIR / "auto_read",
        AUTO_RESEARCH_DIR / "auto_idea",
        AUTO_RESEARCH_DIR / "auto_plan",
    ):
        path.mkdir(parents=True, exist_ok=True)


def stage_latest_path(stage: str, filename: str) -> Path:
    return AUTO_RESEARCH_DIR / stage / filename
