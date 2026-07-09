#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

STAGES = ("finding", "reading", "ideation", "planning", "environment", "experimenting", "writing")
ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = Path(__file__).resolve().parent

if str(ROOT / "framework" / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "framework" / "scripts"))

from auto_research.reading_bridge import (  # noqa: E402
    extract_last_json_object,
    prepare_current_find_read_input,
    sync_current_find_read_outputs,
)


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    entries = [str(ROOT / "framework"), str(ROOT / "framework" / "scripts"), str(ROOT / "web" / "backend"), str(ROOT)]
    for stage in STAGES:
        entries.append(str(ROOT / "modules" / stage))
        entries.append(str(ROOT / "modules" / stage / "scripts"))
    existing = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    seen: set[str] = set()
    merged: list[str] = []
    for item in [*entries, *existing]:
        if item and item not in seen:
            seen.add(item)
            merged.append(item)
    env["WORKSPACE_ROOT"] = str(ROOT)
    env["PYTHONPATH"] = os.pathsep.join(merged)
    return env


def _ensure_framework_imports() -> None:
    for entry in reversed([str(ROOT), str(ROOT / "framework"), str(SCRIPTS), str(ROOT / "web" / "backend")]):
        while entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)


def _load_framework_script_module(relative_path: str, module_name: str):
    _ensure_framework_imports()
    module_path = (SCRIPTS / relative_path).resolve(strict=False)
    try:
        module_path.relative_to(SCRIPTS.resolve(strict=False))
    except ValueError as exc:
        raise ModuleNotFoundError(f"Framework script module path escapes scripts root: {relative_path}") from exc
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"Cannot load framework script module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def module_entry(stage: str) -> Path:
    stage = str(stage or "").strip().replace("-", "_")
    if stage not in STAGES:
        raise SystemExit(f"Unknown TASTE module: {stage}. Expected one of: {', '.join(STAGES)}")
    return ROOT / "modules" / stage / "main.py"


def command_for(stage: str, action: str = "", args: Sequence[str] = ()) -> list[str]:
    cmd = [sys.executable, str(module_entry(stage))]
    if action:
        cmd.extend(["--action", action])
    cmd.extend(args)
    return cmd


def _arg_value(args: Sequence[str], name: str) -> str:
    for index, value in enumerate(args):
        if value == name and index + 1 < len(args):
            return str(args[index + 1])
        prefix = name + "="
        if str(value).startswith(prefix):
            return str(value)[len(prefix):]
    return ""


def _has_arg(args: Sequence[str], name: str) -> bool:
    return any(value == name or str(value).startswith(name + "=") for value in args)


def _current_find_read_action(stage: str, action: str) -> bool:
    normalized = str(action or "").strip().replace("-", "_")
    return stage == "reading" and normalized in {"current_find_research_plan", "ensure_current_find_research_plan", "current_find_read"}


def _run_streaming(cmd: list[str], *, env: dict[str, str]) -> tuple[int, str]:
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        print(line, end="", flush=True)
    return int(proc.wait()), "".join(lines)


def _run_current_find_read_bridge(action: str, rest: Sequence[str]) -> int:
    args = list(rest)
    project = _arg_value(args, "--project")
    if not project:
        print("reading current_find_research_plan requires --project so framework can prepare the local Reading input.", file=sys.stderr)
        return 2
    if not _has_arg(args, "--input-json"):
        try:
            prepared = prepare_current_find_read_input(project, read_limit=int(_arg_value(args, "--read-limit") or 0))
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 2
        args.extend(["--input-json", str(prepared["input_json"]), "--find-run-id", str(prepared["run_id"])])
    cmd = [sys.executable, str(module_entry("reading")), "--action", action, *args]
    rc, stdout_text = _run_streaming(cmd, env=_python_env())
    result_payload = extract_last_json_object(stdout_text)
    try:
        sync_result = sync_current_find_read_outputs(project, result_payload=result_payload, stdout_text=stdout_text)
        print(json.dumps({"status": "framework_synced_reading_outputs", "reading_sync": sync_result}, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"framework failed to sync Reading outputs: {exc}", file=sys.stderr)
        return 1 if rc == 0 else rc
    return rc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Framework bridge for running exactly one TASTE backend module entrypoint.")
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("--action", default="")
    parser.add_argument("--contract", action="store_true")
    ns, rest = parser.parse_known_args(argv)
    cmd = [sys.executable, str(module_entry(ns.stage))]
    if ns.contract:
        cmd.append("--contract")
    elif ns.action:
        cmd.extend(["--action", ns.action])
    cmd.extend(rest)
    if not ns.contract and _current_find_read_action(ns.stage, ns.action):
        return _run_current_find_read_bridge(ns.action, rest)
    proc = subprocess.run(cmd, cwd=ROOT, env=_python_env(), text=True)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
