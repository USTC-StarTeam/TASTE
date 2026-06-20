from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

STAGE_NAME = "experimenting"
DISPLAY_NAME = "Experimenting"
RESPONSIBILITY = "Backend-only autonomous experiment execution: lock a configured Conda/NVM runtime, let Claude Code iterate from an experiment plan on an existing repo, and maintain auditable experiment records."
REQUIRED_EXTERNAL_INPUTS = ("experiment_plan", "conda_env", "repo_path")
ARTIFACTS_IN = ("experiment_plan.json/yaml/txt", "existing code repository", "configured conda environment")
ARTIFACTS_OUT = ("environment_lock.json", "runs/<run_id>/iteration_*/logs", "experiment_registry.json", "experiment_records.csv", "实验记录.md")
PRIVATE_BACKEND_ROOTS = (
    "modules/experimenting/scripts/orchestration/run_autonomous_experiment.py",
    "modules/experimenting/scripts/common/runtime_environment.py",
    "modules/experimenting/scripts/common/experiment_records.py",
    "modules/experimenting/scripts/agent/run_coding_agent.py",
    "modules/experimenting/scripts/execution/launch_experiment_run.py",
)


@dataclass(slots=True)
class ArtifactRef:
    name: str
    path: str = ""
    kind: str = "json"
    role: str = "input"
    required: bool = False


@dataclass(slots=True)
class StageInvocation:
    research_topic: str = ""
    research_interest: str = ""
    researcher_profile: str = ""
    artifact_root: str = ""
    llm: dict[str, Any] = field(default_factory=dict)
    inputs: list[ArtifactRef] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)

    def root_path(self) -> Path:
        return Path(self.artifact_root).expanduser() if self.artifact_root else Path.cwd()


@dataclass(slots=True)
class StageResult:
    status: str
    artifacts: list[ArtifactRef] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    message: str = ""


def contract() -> dict[str, Any]:
    return {
        "stage": STAGE_NAME,
        "display_name": DISPLAY_NAME,
        "responsibility": RESPONSIBILITY,
        "backend_only": True,
        "frontend_dependency": False,
        "required_external_inputs": list(REQUIRED_EXTERNAL_INPUTS),
        "artifacts_in": list(ARTIFACTS_IN),
        "artifacts_out": list(ARTIFACTS_OUT),
        "private_backend_roots": list(PRIVATE_BACKEND_ROOTS),
    }


ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = Path(__file__).resolve().parent
SCRIPTS = MODULE_DIR / "scripts"


def _script_dirs() -> list[Path]:
    dirs = [SCRIPTS]
    dirs.extend(path for path in sorted(SCRIPTS.iterdir()) if path.is_dir())
    return dirs


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    entries: list[str] = [
        str(MODULE_DIR),
        str(SCRIPTS),
        *[str(path) for path in _script_dirs()],
        str(ROOT / "framework"),
        str(ROOT / "framework" / "scripts"),
        str(ROOT),
    ]
    existing = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    seen: set[str] = set()
    merged: list[str] = []
    for item in [*entries, *existing]:
        if item and item not in seen:
            seen.add(item)
            merged.append(item)
    env["PYTHONPATH"] = os.pathsep.join(merged)
    env["WORKSPACE_ROOT"] = str(ROOT)
    return env


def _contract_payload() -> dict[str, Any]:
    payload = contract()
    payload["entrypoint"] = f"modules/{STAGE_NAME}/main.py"
    payload["scripts_are_private_backend"] = True
    payload["standalone_actions"] = ["autonomous_experiment", "runtime_env"]
    return payload


def _normalize_action(action: str) -> str:
    return str(action or "").strip().replace("-", "_")


ACTION_SCRIPTS = {
    "": "orchestration/run_autonomous_experiment.py",
    "run": "orchestration/run_autonomous_experiment.py",
    "auto": "orchestration/run_autonomous_experiment.py",
    "standalone": "orchestration/run_autonomous_experiment.py",
    "autonomous_experiment": "orchestration/run_autonomous_experiment.py",
    "runtime_env": "common/runtime_environment.py",
    "setup_runtime": "common/runtime_environment.py",
    "loop": "orchestration/run_loop.py",
    "framework_loop": "orchestration/run_loop.py",
    "coding_agent": "agent/run_coding_agent.py",
    "agent": "agent/run_coding_agent.py",
    "launch": "execution/launch_experiment_run.py",
    "launch_run": "execution/launch_experiment_run.py",
    "active_repo_smoke": "execution/run_active_repo_smoke.py",
    "real_repo_smoke": "execution/run_real_repo_smoke.py",
    "watchdog": "execution/experiment_run_watchdog.py",
    "contracts": "common/experiment_contracts.py",
    "analyze_failures": "analysis/analyze_experiment_failures.py",
    "reference_reproduction": "audits/audit_reference_reproduction.py",
    "reference_audit": "audits/audit_reference_reproduction.py",
    "audit_iteration": "audits/audit_experiment_iteration.py",
    "runtime_integrity": "audits/audit_experiment_runtime_integrity.py",
    "import_artifacts": "records/import_experiment_artifacts.py",
}


def _resolve_script(action: str) -> Path:
    key = _normalize_action(action)
    mapped = ACTION_SCRIPTS.get(key, f"{key}.py")
    candidate = SCRIPTS / mapped
    if candidate.exists():
        return candidate
    for directory in _script_dirs():
        fallback = directory / f"{key}.py"
        if fallback.exists():
            return fallback
    raise SystemExit(f"未知 {STAGE_NAME} 后端 action: {action}")


def _run_script(action: str, args: Sequence[str]) -> int:
    script = _resolve_script(action)
    proc = subprocess.run([sys.executable, str(script), *args], cwd=ROOT, env=_python_env(), text=True)
    return int(proc.returncode)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Experimenting module public backend entrypoint.", add_help=True)
    parser.add_argument("--action", default="run", help="Backend action. Default: autonomous experiment runner.")
    parser.add_argument("--contract", action="store_true")
    ns, rest = parser.parse_known_args(argv)
    if ns.contract:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    action = _normalize_action(ns.action)
    if action in {"log", "record_table"}:
        return _run_script("records/experiment_record_tools", ["--tool-action", action, *rest])
    return _run_script(action, rest)


if __name__ == "__main__":
    raise SystemExit(main())
