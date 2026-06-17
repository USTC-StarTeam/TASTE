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

STAGE_NAME = 'reading'
DISPLAY_NAME = 'Reading'
RESPONSIBILITY = 'Acquire verified paper-body text for the selected Find packet and synthesize reading notes. Same-run replacements for unavailable public full text happen here, never inside Finding.'
REQUIRED_EXTERNAL_INPUTS = ('llm_api_or_claude', 'finding_artifact_packet', 'artifact_root')
ARTIFACTS_IN = ('find_results.json', 'article.md', 'full_text_reading/manual_full_text_sources.json')
ARTIFACTS_OUT = ('read_results.json', 'read.md', 'full_text_reading/full_text_packet.json', 'current_find_full_text_evidence_repair.json')
LEGACY_ROOTS = ('modules/reading/scripts/read_pipeline.py', 'modules/reading/scripts/repair_current_find_full_text_evidence.py', 'modules/reading/scripts/ensure_current_find_research_plan.py')


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
        "legacy_roots": list(LEGACY_ROOTS),
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


DIRECT_ACTIONS = {"", "read", "pipeline", "read_pipeline"}
ACTION_ALIASES = {
    "repair_full_text": "repair_current_find_full_text_evidence",
    "current_find_research_plan": "ensure_current_find_research_plan",
    "ensure_current_find_research_plan": "ensure_current_find_research_plan",
    "import": "import_paper",
    "import_paper": "import_paper",
}


def _run_read(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the Reading module backend.")
    parser.add_argument("--run-id", default="", help="Finding run id to read.")
    parser.add_argument("--config-json", default="", help="AppConfig-compatible JSON.")
    parser.add_argument("--max-papers", type=int, default=5)
    parser.add_argument("--paper-id", action="append", default=[])
    parser.add_argument("--project", default="", help="Project id for current-Find repair compatibility.")
    parser.add_argument("--repair-full-text", action="store_true", help="Compatibility alias for --action repair_full_text.")
    ns, rest = parser.parse_known_args(list(args))
    if ns.repair_full_text:
        if not ns.project:
            raise SystemExit("--project is required with --repair-full-text")
        forwarded = ["--project", ns.project, "--force", *rest]
        return _run_script("repair_current_find_full_text_evidence", forwarded)
    if not ns.run_id:
        raise SystemExit("--run-id is required unless --action selects a project adapter")
    _ensure_runtime_imports()
    from auto_research.models import AppConfig, ReadRequest
    from read_pipeline import run_read

    config = AppConfig(**_load_json(ns.config_json, {}))
    result = run_read(ReadRequest(run_id=ns.run_id, paper_ids=ns.paper_id, max_papers=ns.max_papers), config)
    print(json.dumps({"stage": STAGE_NAME, "run_id": ns.run_id, "result": result}, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reading module public backend entrypoint.", add_help=True)
    parser.add_argument("--action", default="read", help="Backend action. Default: read.")
    parser.add_argument("--contract", action="store_true")
    ns, rest = parser.parse_known_args(argv)
    if ns.contract:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    action = _normalize_action(ns.action)
    if action in DIRECT_ACTIONS:
        return _run_read(rest)
    return _run_script(ACTION_ALIASES.get(action, action), rest)


if __name__ == "__main__":
    raise SystemExit(main())
