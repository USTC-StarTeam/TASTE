#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "framework" / "scripts"
for _entry in [SCRIPTS]:
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from bridges.project_bridge import build_command  # noqa: E402


STAGE_ACTIONS = ("find", "read", "idea", "plan", "environment", "experiment", "paper")


def _stage_payload(args: argparse.Namespace, action: str) -> dict[str, Any]:
    return {
        "action": action,
        "project": args.project,
        "venue": args.venue,
        "topic": args.topic,
        "title": args.title,
        "max_papers": args.max_papers,
        "max_ideas": args.max_ideas,
        "repair_rounds": args.repair_rounds,
        "iterations": args.experiment_iterations,
        "skip_fetch": args.skip_fetch,
        "auto_install_latex": args.auto_install_latex,
    }


def _run_stage(action: str, payload: dict[str, Any]) -> int:
    project, command = build_command(payload)
    print(json.dumps({
        "event": "full_cycle_stage_started",
        "action": action,
        "project": project,
        "command": command,
    }, ensure_ascii=False), flush=True)
    proc = subprocess.Popen(
        command,
        cwd=str(ROOT),
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
    code = int(proc.wait())
    print(json.dumps({
        "event": "full_cycle_stage_finished",
        "action": action,
        "project": project,
        "return_code": code,
    }, ensure_ascii=False), flush=True)
    if code == 0 and action == "plan":
        return _run_stage("current-find-selection", {**payload, "action": "current-find-selection"})
    return code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Invoke the same seven Framework stage actions as the Web buttons, once and in order."
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", required=True)
    parser.add_argument("--topic", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--max-papers", type=int, default=20)
    parser.add_argument("--max-ideas", type=int, default=6)
    parser.add_argument("--repair-rounds", type=int, default=3)
    parser.add_argument("--experiment-iterations", type=int, default=1)
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--auto-install-latex", action="store_true")
    args = parser.parse_args()

    for action in STAGE_ACTIONS:
        code = _run_stage(action, _stage_payload(args, action))
        if code != 0:
            print(json.dumps({
                "status": "stopped",
                "failed_action": action,
                "return_code": code,
                "completed_actions": list(STAGE_ACTIONS[: STAGE_ACTIONS.index(action)]),
            }, ensure_ascii=False, indent=2))
            return code
    print(json.dumps({
        "status": "completed",
        "completed_actions": list(STAGE_ACTIONS),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
