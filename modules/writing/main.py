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

STAGE_NAME = 'writing'
DISPLAY_NAME = 'Writing'
RESPONSIBILITY = 'Resolve venue requirements, draft/revise/compile the manuscript, and audit citations/figures/submission readiness from experiment evidence.'
REQUIRED_EXTERNAL_INPUTS = ('venue', 'selected_plan_contract', 'experiment_evidence', 'paper_config')
ARTIFACTS_IN = ('experiment_registry.json', 'claim ledger', 'venue template/requirements')
ARTIFACTS_OUT = ('paper draft/revision', 'compiled PDF', 'paper_pipeline.json', 'submission_readiness.json')
PRIVATE_BACKEND_ROOTS = ('modules/writing', 'modules/writing/scripts/run_paper_pipeline.py', 'modules/writing/scripts/paper_common.py')


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
    "": "run_paper_pipeline",
    "run": "run_paper_pipeline",
    "paper": "run_paper_pipeline",
    "paper_pipeline": "run_paper_pipeline",
    "pipeline": "run_paper_pipeline",
    "preview": "build_conference_preview_paper",
    "repair_preview": "repair_paper_preview_loop",
    "audit_evidence": "audit_paper_evidence",
    "submission_readiness": "audit_submission_readiness",
    "respond_reviews": "respond_to_paper_reviews",
    "respond_to_reviews": "respond_to_paper_reviews",
    "re_review": "re_review_paper",
    "comparison": "write_comparison",
    "sync_stack": "sync_third_party_research_stack",
    "submission_readiness": "audit_submission_readiness",
    "repair_figures": "repair_paper_figures_loop",
    "repair_preview": "repair_paper_preview_loop",
    "audit_normality": "audit_paper_normality",
    "audit_figures": "audit_paper_figures",
}
REVIEW_TOOL_ACTIONS = {
    "respond_to_paper_reviews": "respond",
    "respond_reviews": "respond",
    "respond_to_reviews": "respond",
    "re_review_paper": "re_review",
    "re_review": "re_review",
    "write_comparison": "comparison",
    "comparison": "comparison",
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Writing module public backend entrypoint.", add_help=True)
    parser.add_argument("--action", default="run", help="Backend action. Default: run.")
    parser.add_argument("--contract", action="store_true")
    ns, rest = parser.parse_known_args(argv)
    if ns.contract:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    action = _normalize_action(ns.action)
    if action in REVIEW_TOOL_ACTIONS:
        return _run_script("review_response_tools", ["--tool-action", REVIEW_TOOL_ACTIONS[action], *rest])
    target = ACTION_ALIASES.get(action, action)
    if target in REVIEW_TOOL_ACTIONS:
        return _run_script("review_response_tools", ["--tool-action", REVIEW_TOOL_ACTIONS[target], *rest])
    return _run_script(target, rest)


if __name__ == "__main__":
    raise SystemExit(main())
