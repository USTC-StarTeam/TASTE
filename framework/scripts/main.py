#!/usr/bin/env python3
"""The single public entry point for TASTE framework functions."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Sequence


SCRIPTS_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPTS_ROOT.parents[1]
STAGES = (
    "finding",
    "reading",
    "ideation",
    "planning",
    "environment",
    "experimenting",
    "writing",
)
COMMANDS = {
    "web": ("shell", SCRIPTS_ROOT / "launchers" / "start_web.sh"),
    "find": ("python", SCRIPTS_ROOT / "orchestration" / "run_frontend.py"),
    "module": ("python", SCRIPTS_ROOT / "orchestration" / "run_module.py"),
    "workflow": ("python", SCRIPTS_ROOT / "orchestration" / "run_taste_framework.py"),
}


def _import_dirs() -> list[Path]:
    roots = [
        SCRIPTS_ROOT,
        WORKSPACE_ROOT / "framework",
        WORKSPACE_ROOT / "web" / "backend",
        WORKSPACE_ROOT,
    ]
    for stage in STAGES:
        module_root = WORKSPACE_ROOT / "modules" / stage
        roots.append(module_root)
        scripts = module_root / "scripts"
        if scripts.is_dir():
            roots.append(scripts)
            roots.extend(path for path in sorted(scripts.rglob("*")) if path.is_dir() and path.name != "__pycache__")
    return roots


def _runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    merged: list[str] = []
    for value in [*(str(path) for path in _import_dirs()), *existing]:
        if value and value not in merged:
            merged.append(value)
    env["WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
    env["PYTHONPATH"] = os.pathsep.join(merged)
    return env


def resolve_command(argv: Sequence[str]) -> list[str]:
    args = list(argv)
    command_name = args.pop(0) if args else "web"
    if command_name == "start":
        command_name = "web"
    if command_name not in COMMANDS:
        raise ValueError(f"unknown command: {command_name}")
    kind, target = COMMANDS[command_name]
    if not target.is_file():
        raise FileNotFoundError(f"TASTE private entry not found: {target}")
    executable = "/usr/bin/env"
    prefix = [executable, "bash"] if kind == "shell" else [sys.executable]
    return [*prefix, str(target), *args]


def _print_help() -> None:
    print(
        "Usage: python framework/scripts/main.py [web|find|module|workflow] [args...]\n"
        "\n"
        "With no command, TASTE starts the Web service.\n"
        "  web                 Start the TASTE Web/API service (default).\n"
        "  find ...            Run the Framework Find route.\n"
        "  module <stage> ...  Run one Framework module route.\n"
        "  workflow ...        Run/status/audit the Framework workflow."
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    exec_fn: Callable[[str, Sequence[str], dict[str, str]], object] = os.execvpe,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"-h", "--help", "help"}:
        _print_help()
        return 0
    try:
        command = resolve_command(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"TASTE: {exc}", file=sys.stderr)
        _print_help()
        return 2
    exec_fn(command[0], command, _runtime_env())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
