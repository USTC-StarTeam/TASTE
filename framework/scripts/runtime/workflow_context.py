from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from contracts.module_catalog import runtime_env as catalog_runtime_env
from contracts.module_catalog import workspace_root as detect_workspace_root
from runtime.framework_io import ensure_within, slugify, utc_now


def _default_run_id(research_goal: str = "") -> str:
    stamp = utc_now().replace(":", "").replace("+", "Z").replace(".", "_")
    prefix = slugify(research_goal, default="framework_run", limit=36)
    return f"{prefix}_{stamp}"


@dataclass(slots=True)
class FrameworkContext:
    workspace_root: Path
    framework_root: Path
    state_root: Path
    run_id: str
    python: str = field(default_factory=lambda: sys.executable)
    mode: str = "dry-run"

    def __post_init__(self) -> None:
        self.workspace_root = Path(self.workspace_root).expanduser().resolve()
        self.framework_root = Path(self.framework_root).expanduser().resolve()
        self.state_root = Path(self.state_root).expanduser().resolve()
        self.python = str(self.python or sys.executable)
        self.run_id = str(self.run_id or _default_run_id()).strip()
        self.mode = str(self.mode or "dry-run")
        ensure_within(self.framework_root, self.state_root)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.public_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def create(
        cls,
        *,
        run_id: str = "",
        state_root: Path | None = None,
        python: str = "",
        mode: str = "dry-run",
        research_goal: str = "",
    ) -> "FrameworkContext":
        workspace = detect_workspace_root()
        framework = workspace / "framework"
        selected_state_root = state_root or framework / "workspace"
        return cls(
            workspace_root=workspace,
            framework_root=framework,
            state_root=selected_state_root,
            run_id=run_id or _default_run_id(research_goal),
            python=python or sys.executable,
            mode=mode,
        )

    @property
    def run_dir(self) -> Path:
        return self.state_root / "runs" / self.run_id

    @property
    def state_dir(self) -> Path:
        return self.run_dir / "state"

    @property
    def public_dir(self) -> Path:
        return self.run_dir / "public"

    def env(self) -> dict[str, str]:
        env = catalog_runtime_env(self.workspace_root)
        env["TASTE_FRAMEWORK_RUN_ID"] = self.run_id
        env["TASTE_FRAMEWORK_MODE"] = self.mode
        env["TASTE_FRAMEWORK_STATE_ROOT"] = str(self.state_root)
        env["TASTE_FRAMEWORK_RUN_DIR"] = str(self.run_dir)
        path_entries = [
            str(self.framework_root / "scripts"),
            str(self.framework_root),
            str(self.workspace_root),
        ]
        existing_path = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
        seen: set[str] = set()
        merged: list[str] = []
        for item in [*path_entries, *existing_path]:
            if item and item not in seen:
                seen.add(item)
                merged.append(item)
        env["PYTHONPATH"] = os.pathsep.join(merged)
        return env
