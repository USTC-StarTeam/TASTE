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
    parser = argparse.ArgumentParser(description="Run the standalone writing module adapter.")
    parser.add_argument("--project", default="")
    parser.add_argument("--venue", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--generate-paper-preview", action="store_true")
    parser.add_argument("--contract", action="store_true")
    args, rest = parser.parse_known_args()
    if args.contract:
        print(json.dumps(contract(), ensure_ascii=False, indent=2))
        return
    if not args.project:
        raise SystemExit("--project is required for the current Writing adapter")
    if not args.venue:
        raise SystemExit("--venue is required for the current Writing adapter")
    cmd = [sys.executable, str(ROOT / "modules" / "writing" / "scripts" / "run_paper_pipeline.py"), "--project", args.project, "--venue", args.venue]
    if args.title:
        cmd.extend(["--title", args.title])
    if args.generate_paper_preview:
        cmd.append("--generate-paper-preview")
    cmd.extend(rest)
    proc = subprocess.run(cmd, cwd=ROOT, text=True)
    raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()
