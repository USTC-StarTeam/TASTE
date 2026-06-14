from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

try:
    from .contracts import STAGE_NAME, contract
except ImportError:
    from contracts import STAGE_NAME, contract

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the standalone experimenting module adapter.")
    parser.add_argument("--project", default="")
    parser.add_argument("--iterations", default="1")
    parser.add_argument("--execute-plan", action="store_true")
    parser.add_argument("--prepare-env", action="store_true")
    parser.add_argument("--venue", default="")
    parser.add_argument("--contract", action="store_true")
    args, rest = parser.parse_known_args()
    if args.contract:
        print(json.dumps(contract(), ensure_ascii=False, indent=2))
        return
    if not args.project:
        raise SystemExit("--project is required for the current Experimenting adapter")
    cmd = [sys.executable, str(ROOT / "framework" / "scripts" / "run_autonomous_research.py"), "--project", args.project, "--iterations", str(args.iterations), "--skip-paper"]
    if args.execute_plan:
        cmd.append("--execute-plan")
    if args.prepare_env:
        cmd.append("--prepare-env")
    if args.venue:
        cmd.extend(["--venue", args.venue])
    cmd.extend(rest)
    proc = subprocess.run(cmd, cwd=ROOT, text=True)
    raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()
