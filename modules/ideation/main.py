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

STAGE_NAME = 'ideation'
DISPLAY_NAME = 'Ideation'
RESPONSIBILITY = 'Turn reading/finding artifacts into editable research ideas without selecting an execution route.'
REQUIRED_EXTERNAL_INPUTS = ('llm_api_or_claude', 'reading_artifacts', 'research_profile')
ARTIFACTS_IN = ('find_results.json', 'read_results.json', 'read.md')
ARTIFACTS_OUT = ('ideas.json', 'idea.md', 'hypothesis_arena.md', 'idea candidate audits')
PRIVATE_BACKEND_ROOTS = ('modules/ideation/scripts/idea_pipeline.py', 'modules/ideation/scripts/assess_idea_candidates.py', 'modules/ideation/scripts/build_hypothesis_arena.py')


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


DIRECT_ACTIONS = {"", "idea", "ideation", "pipeline", "idea_pipeline"}
ACTION_ALIASES = {
    "assess": "assess_idea_candidates",
    "arena": "build_hypothesis_arena",
    "initialization": "prepare_initialization",
}


def _run_idea(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the Ideation module backend.")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--config-json", default="")
    parser.add_argument("--max-ideas", type=int, default=0)
    parser.add_argument("--parallel-workers", type=int, default=0)
    ns = parser.parse_args(list(args))
    if not ns.run_id:
        raise SystemExit("--run-id is required")
    _ensure_runtime_imports()
    from auto_research.models import AppConfig, IdeaRequest
    from idea_pipeline import run_idea

    config = AppConfig(**_load_json(ns.config_json, {}))
    result = run_idea(
        IdeaRequest(run_id=ns.run_id, max_ideas=ns.max_ideas or None, parallel_workers=ns.parallel_workers or None),
        config,
    )
    print(json.dumps({"stage": STAGE_NAME, "run_id": ns.run_id, "result": result}, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ideation module public backend entrypoint.", add_help=True)
    parser.add_argument("--action", default="idea", help="Backend action. Default: idea.")
    parser.add_argument("--contract", action="store_true")
    ns, rest = parser.parse_known_args(argv)
    if ns.contract:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    action = _normalize_action(ns.action)
    if action in DIRECT_ACTIONS:
        return _run_idea(rest)
    return _run_script(ACTION_ALIASES.get(action, action), rest)


if __name__ == "__main__":
    raise SystemExit(main())
