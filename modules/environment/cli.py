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
    parser = argparse.ArgumentParser(description="Run the standalone environment module adapter.")
    parser.add_argument("--project", default="")
    parser.add_argument("--venue", default="")
    parser.add_argument("--real-bootstrap-env", action="store_true")
    parser.add_argument("--repo-search-rounds", default="3")
    parser.add_argument("--contract", action="store_true")
    args, rest = parser.parse_known_args()
    if args.contract:
        print(json.dumps(contract(), ensure_ascii=False, indent=2))
        return
    if not args.project:
        raise SystemExit("--project is required for the current Environment adapter")
    cmd = [sys.executable, str(ROOT / "modules" / "environment" / "scripts" / "run_environment_stage.py"), "--project", args.project, "--repo-search-rounds", str(args.repo_search_rounds)]
    if args.venue:
        cmd.extend(["--venue", args.venue])
    if args.real_bootstrap_env:
        cmd.append("--real-bootstrap-env")
    cmd.extend(rest)
    proc = subprocess.run(cmd, cwd=ROOT, text=True)
    raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()
