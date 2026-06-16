#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

STAGES = ("finding", "reading", "ideation", "planning", "environment", "experimenting", "writing")
ROOT = Path(__file__).resolve().parents[2]


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
    proc = subprocess.run(cmd, cwd=ROOT, env=_python_env(), text=True)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
