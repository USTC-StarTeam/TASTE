from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

STAGE_NAME = 'planning'
DISPLAY_NAME = 'Planning'
RESPONSIBILITY = 'Select and repair executable research plans from approved ideas; downstream modules consume only explicit selected plan contracts.'
REQUIRED_EXTERNAL_INPUTS = ('llm_api_or_claude', 'idea_artifacts', 'project_constraints')
ARTIFACTS_IN = ('ideas.json', 'idea.md', 'user selection/approval')
ARTIFACTS_OUT = ('plans.json', 'plan.md', 'experiment_plan.json', 'taste_plan_bridge.json', 'blocker action plans')
PRIVATE_BACKEND_ROOTS = (
    'modules/planning/scripts/core/plan_pipeline.py',
    'modules/planning/scripts/tools/planning_tools.py',
    'modules/planning/scripts/blockers/build_blocker_action_plan.py',
    'modules/planning/scripts/actions/propose_next_actions.py',
)
COMPATIBILITY_SCRIPT_ROOTS = (
    'modules/planning/scripts/plan_pipeline.py',
    'modules/planning/scripts/planning_tools.py',
    'modules/planning/scripts/build_blocker_action_plan.py',
    'modules/planning/scripts/propose_next_actions.py',
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
PLANNING_ROOT = Path(__file__).resolve().parent
SCRIPTS = PLANNING_ROOT / "scripts"


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    entries: list[str] = [
        str(ROOT / "framework"),
        str(ROOT / "framework" / "scripts"),
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
    payload["compatibility_script_roots"] = list(COMPATIBILITY_SCRIPT_ROOTS)
    return payload


def _normalize_action(action: str) -> str:
    return str(action or "").strip().replace("-", "_")


def _run_script(script_stem: str, args: Sequence[str]) -> int:
    script = SCRIPTS / f"{_normalize_action(script_stem)}.py"
    if not script.exists():
        raise SystemExit(f"Unknown {STAGE_NAME} module action: {script_stem}")
    proc = subprocess.run([sys.executable, str(script), *args], cwd=ROOT, env=_python_env(), text=True)
    return int(proc.returncode)


DIRECT_ACTIONS = {"", "plan", "planning", "pipeline", "plan_pipeline"}
PLANNING_TOOL_ACTIONS = {
    "experiments": "experiments",
    "workflow": "workflow",
    "blocker_resolution": "blocker_resolution",
    "review_board": "review_board",
    "method_frontier": "method_frontier",
    "reflect": "reflect",
}
ACTION_ALIASES = {
    "blocker_action": "build_blocker_action_plan",
    "build_blocker_action_plan": "build_blocker_action_plan",
    "next_actions": "propose_next_actions",
    "propose_next_actions": "propose_next_actions",
}


def _safe_slug(value: str, default: str = "idea") -> str:
    text = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(value or "").strip()).strip("-").lower()
    return (text or default)[:80]


def _standalone_run_id(seed: str = "") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = _safe_slug(seed, "idea")
    return f"planning-{stamp}-{suffix}"[:120]


def _read_idea_text(path: str = "", text: str = "") -> str:
    if path:
        return Path(path).expanduser().read_text(encoding="utf-8")
    return str(text or "")


def _idea_from_markdown(markdown: str, run_id: str) -> dict[str, Any]:
    lines = [line.strip() for line in str(markdown or "").splitlines() if line.strip()]
    title = ""
    for line in lines:
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            break
    if not title and lines:
        title = lines[0][:120]
    return {
        "id": _safe_slug(title or run_id),
        "title": title or run_id,
        "new_method": markdown.strip(),
        "method_details": markdown.strip(),
        "initial_experiment": "Planning standalone input did not provide a separate initial_experiment; Claude/LLM must derive the minimum executable experiment from the idea text.",
        "approved_for_planning": True,
        "source": "planning_standalone_markdown",
    }


def _standalone_ideas_payload(ns: argparse.Namespace, run_id: str) -> dict[str, Any]:
    if ns.idea_json:
        idea_path = Path(ns.idea_json).expanduser()
        try:
            raw = json.loads(idea_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"failed to load --idea-json {idea_path}: {exc}") from exc
        if isinstance(raw, dict) and isinstance(raw.get("ideas"), list):
            ideas = [dict(item) for item in raw["ideas"] if isinstance(item, dict)]
        elif isinstance(raw, dict):
            ideas = [dict(raw)]
        elif isinstance(raw, list):
            ideas = [dict(item) for item in raw if isinstance(item, dict)]
        else:
            raise SystemExit("--idea-json must contain an object, an ideas object, or a list of objects")
    else:
        idea_text = _read_idea_text(ns.idea_md, ns.idea_text)
        if not idea_text.strip():
            raise SystemExit("standalone planning requires --idea-json, --idea-md, or --idea-text")
        ideas = [_idea_from_markdown(idea_text, run_id)]
    for index, idea in enumerate(ideas, 1):
        idea.setdefault("id", _safe_slug(idea.get("idea_id") or idea.get("title") or f"idea-{index}"))
        idea.setdefault("title", idea.get("id") or f"idea-{index}")
        idea.setdefault("approved_for_planning", True)
        idea.setdefault("status", "approved")
    return {"run_id": run_id, "source": "planning_standalone", "ideas": ideas}


def _standalone_output_dir(value: str, run_id: str) -> Path:
    root = PLANNING_ROOT.resolve()
    path = Path(value).expanduser().resolve() if value else (root / "runs" / run_id).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SystemExit(f"standalone --output-dir must stay under {root}") from exc
    return path


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run_plan(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the Planning module backend.")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--config-json", default="")
    parser.add_argument("--idea-id", action="append", default=[])
    parser.add_argument("--repair-rounds", type=int, default=3)
    parser.add_argument("--idea-json", default="", help="Standalone mode: JSON file containing one idea, a list of ideas, or {'ideas': [...]}.")
    parser.add_argument("--idea-md", default="", help="Standalone mode: Markdown file describing one idea.")
    parser.add_argument("--idea-text", default="", help="Standalone mode: literal idea text.")
    parser.add_argument("--output-dir", default="", help="Standalone output directory. Defaults to modules/planning/runs/<run-id>.")
    parser.add_argument("--backend", choices=["", "llm", "claude_code", "off"], default="", help="Optional backend hint; off sets PLAN_USE_LLM=0.")
    ns = parser.parse_args(list(args))
    standalone = bool(ns.idea_json or ns.idea_md or ns.idea_text)
    if not ns.run_id:
        if not standalone:
            raise SystemExit("--run-id is required unless --idea-json/--idea-md/--idea-text is provided")
        ns.run_id = _standalone_run_id(ns.idea_json or ns.idea_md or ns.idea_text[:80])
    if ns.backend == "off":
        os.environ["PLAN_USE_LLM"] = "0"
    elif ns.backend:
        os.environ["PLAN_BACKEND"] = ns.backend
    _ensure_runtime_imports()
    from auto_research.models import AppConfig, PlanRequest
    from plan_pipeline import run_plan, run_plan_at_directory

    config = AppConfig(**_load_json(ns.config_json, {}))
    request = PlanRequest(run_id=ns.run_id, idea_ids=ns.idea_id, repair_rounds=ns.repair_rounds)
    if standalone:
        output_dir = _standalone_output_dir(ns.output_dir, ns.run_id)
        payload = _standalone_ideas_payload(ns, ns.run_id)
        _write_json(output_dir / "ideas.json", payload)
        result = run_plan_at_directory(output_dir, request, config, sync_latest_outputs=False, sync_project=False)
        print(json.dumps({"stage": STAGE_NAME, "run_id": ns.run_id, "output_dir": str(output_dir), "result": result}, ensure_ascii=False, indent=2))
        return 0
    result = run_plan(request, config)
    print(json.dumps({"stage": STAGE_NAME, "run_id": ns.run_id, "result": result}, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Planning module public backend entrypoint.", add_help=True)
    parser.add_argument("--action", default="plan", help="Backend action. Default: plan.")
    parser.add_argument("--contract", action="store_true")
    ns, rest = parser.parse_known_args(argv)
    if ns.contract:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    action = _normalize_action(ns.action)
    if action in DIRECT_ACTIONS:
        return _run_plan(rest)
    if action in PLANNING_TOOL_ACTIONS:
        return _run_script("planning_tools", ["--tool-action", PLANNING_TOOL_ACTIONS[action], *rest])
    return _run_script(ACTION_ALIASES.get(action, action), rest)


if __name__ == "__main__":
    raise SystemExit(main())
