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

STAGE_NAME = 'environment'
DISPLAY_NAME = 'Environment'
RESPONSIBILITY = 'Select audited code/data bases, probe loaders, and lock the experiment runtime. It does not run novel experiments or write paper claims.'
REQUIRED_EXTERNAL_INPUTS = ('selected_plan_contract', 'candidate_repo_data_artifacts', 'runtime_config')
ARTIFACTS_IN = ('plans.json', 'literature_tool_packet.json', 'repo/data candidates')
ARTIFACTS_OUT = ('evidence_ready_repo_selection.json', 'repo_env_bootstrap.json', 'dataset registry', 'reference/data gates')
PRIVATE_BACKEND_ROOTS = ('modules/environment/scripts/run_environment_stage.py', 'modules/environment/scripts/select_evidence_ready_repo.py', 'modules/environment/scripts/bootstrap_repo_env.py', 'modules/environment/scripts/environment_data_tools.py')


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


ENVIRONMENT_DATA_TOOL_ACTIONS = {
    "assess_repo": "assess_repo",
    "assess_repo_candidates": "assess_repo",
    "select_repo_candidate": "select_repo_candidate",
    "register_repo_candidate": "register_repo_candidate",
    "register_dataset": "register_dataset",
    "reconcile_candidates": "reconcile_candidates",
    "reconcile_active_and_pool_candidates": "reconcile_candidates",
    "plan_data": "plan_data",
    "plan_data_acquisition": "plan_data",
    "attempt_data": "attempt_data",
    "attempt_data_acquisition": "attempt_data",
    "data_policy": "data_policy",
    "data_unavailability_policy": "data_policy",
}

ACTION_ALIASES = {
    "": "run_environment_stage",
    "run": "run_environment_stage",
    "run_stage": "run_environment_stage",
    "stage": "run_environment_stage",
    "environment": "run_environment_stage",
    "select_repo": "select_evidence_ready_repo",
    "select_evidence_ready": "select_evidence_ready_repo",
    "probe_selected_base": "probe_selected_base_reference",
    "probe_selected_base_reference": "probe_selected_base_reference",
    "bootstrap": "bootstrap_repo_env",
    "bootstrap_repo": "bootstrap_repo_env",
    "data_requirements": "build_repo_data_requirements",
    "probe_repo": "probe_repo_dataset",
    "restart_discovery": "restart_after_data_blocker",
    "candidate_pool": "audit_repo_candidate_pool",
    "fresh_base_data_probe": "probe_fresh_base_data_acquisition",
    "probe_candidate_base_reference": "probe_candidate_base_reference",
    "base_switch_gate": "audit_deterministic_base_switch_gate",
    "execute_base_switch": "execute_authorized_base_switch",
    "repo_data_requirements": "build_repo_data_requirements",
    "obsolete_cleanup": "audit_obsolete_baseline_cleanup",
    "fresh_base_plan": "build_fresh_base_implementation_plan",
    "selected_base_viability": "audit_selected_base_viability",
    "safe_unblock": "run_safe_unblock",
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Environment module public backend entrypoint.", add_help=True)
    parser.add_argument("--action", default="run_stage", help="Backend action. Default: run_stage.")
    parser.add_argument("--contract", action="store_true")
    ns, rest = parser.parse_known_args(argv)
    if ns.contract:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    action = _normalize_action(ns.action)
    if action in ENVIRONMENT_DATA_TOOL_ACTIONS:
        return _run_script("environment_data_tools", ["--tool-action", ENVIRONMENT_DATA_TOOL_ACTIONS[action], *rest])
    return _run_script(ACTION_ALIASES.get(action, action), rest)


if __name__ == "__main__":
    raise SystemExit(main())
