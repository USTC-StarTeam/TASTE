from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

STAGE_NAME = 'experimenting'
DISPLAY_NAME = 'Experimenting'
RESPONSIBILITY = 'Modify or execute selected project code, run auditable experiments, parse metrics/logs, and produce evidence gates for claims.'
REQUIRED_EXTERNAL_INPUTS = ('selected_plan_contract', 'locked_environment', 'repo_path', 'experiment_python')
ARTIFACTS_IN = ('evidence_ready_repo_selection.json', 'repo_env_bootstrap.json', 'experiment_plan.json')
ARTIFACTS_OUT = ('experiment_registry.json', 'experiment artifacts/logs', 'runtime integrity audit', 'reference/scientific progress gates')
PRIVATE_BACKEND_ROOTS = (
    'modules/experimenting/scripts/run_coding_agent.py',
    'modules/experimenting/scripts/launch_experiment_run.py',
    'modules/experimenting/scripts/experiment_contracts.py',
    'modules/experimenting/scripts/experiment_record_tools.py',
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
        "required_external_inputs": list(REQUIRED_EXTERNAL_INPUTS),
        "artifacts_in": list(ARTIFACTS_IN),
        "artifacts_out": list(ARTIFACTS_OUT),
        "private_backend_roots": list(PRIVATE_BACKEND_ROOTS),
    }


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = Path(__file__).resolve().parent / "scripts"


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    entries: list[str] = [
        str(ROOT / "framework"),
        str(ROOT / "framework" / "scripts"),
        str(ROOT / "web" / "backend"),
        str(ROOT),
    ]
    modules_root = ROOT / "modules"
    for stage_dir in sorted(path for path in modules_root.iterdir() if path.is_dir()):
        entries.append(str(stage_dir))
        scripts = stage_dir / "scripts"
        if scripts.is_dir():
            entries.append(str(scripts))
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


def _ensure_runtime_imports() -> None:
    for entry in reversed(_python_env()["PYTHONPATH"].split(os.pathsep)):
        if entry and entry not in sys.path:
            sys.path.insert(0, entry)


def _load_json(path: str, default):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else default


def _contract_payload() -> dict:
    payload = contract()
    payload["entrypoint"] = f"modules/{STAGE_NAME}/main.py"
    payload["scripts_are_private_backend"] = True
    return payload


def _normalize_action(action: str) -> str:
    return str(action or "").strip().replace("-", "_")


def _run_script(script_stem: str, args: Sequence[str]) -> int:
    script = SCRIPTS / f"{_normalize_action(script_stem)}.py"
    if not script.exists():
        raise SystemExit(f"Unknown {STAGE_NAME} module action: {script_stem}")
    proc = subprocess.run([sys.executable, str(script), *args], cwd=ROOT, env=_python_env(), text=True)
    return int(proc.returncode)


ACTION_ALIASES = {
    "": "run_loop",
    "run": "run_loop",
    "loop": "run_loop",
    "experiment": "run_loop",
    "coding_agent": "run_coding_agent",
    "agent": "run_coding_agent",
    "launch": "launch_experiment_run",
    "launch_run": "launch_experiment_run",
    "active_repo_smoke": "run_active_repo_smoke",
    "real_repo_smoke": "run_real_repo_smoke",
    "watchdog": "experiment_run_watchdog",
    "contracts": "experiment_contracts",
    "analyze_failures": "analyze_experiment_failures",
    "reference_reproduction": "audit_reference_reproduction",
    "audit_iteration": "audit_experiment_iteration",
    "runtime_integrity": "audit_experiment_runtime_integrity",
    "import_artifacts": "import_experiment_artifacts",
    "reference_audit": "audit_reference_reproduction",
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Experimenting module public backend entrypoint.", add_help=True)
    parser.add_argument("--action", default="run", help="Backend action. Default: run.")
    parser.add_argument("--contract", action="store_true")
    ns, rest = parser.parse_known_args(argv)
    if ns.contract:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    action = _normalize_action(ns.action)
    if action in {"log", "record_table"}:
        return _run_script("experiment_record_tools", ["--tool-action", action, *rest])
    return _run_script(ACTION_ALIASES.get(action, action), rest)


if __name__ == "__main__":
    raise SystemExit(main())
