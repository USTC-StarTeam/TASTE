from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

try:
    from .contracts import STAGE_NAME, contract
except ImportError:
    from contracts import STAGE_NAME, contract

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
    payload["compat_cli"] = f"modules/{STAGE_NAME}/cli.py"
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
