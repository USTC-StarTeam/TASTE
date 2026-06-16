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


def _ensure_runtime_imports() -> None:
    framework_scripts = ROOT / "framework" / "scripts"
    if str(framework_scripts) not in sys.path:
        sys.path.insert(0, str(framework_scripts))
    from taste_pythonpath import ensure_taste_pythonpath

    ensure_taste_pythonpath(ROOT)


def _load_json(path: str, default):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else default


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the standalone Reading module backend.")
    parser.add_argument("--run-id", default="", help="Finding run id to read.")
    parser.add_argument("--config-json", default="", help="AppConfig-compatible JSON.")
    parser.add_argument("--max-papers", type=int, default=5)
    parser.add_argument("--paper-id", action="append", default=[])
    parser.add_argument("--project", default="", help="Project id for the Read-stage full-text repair adapter.")
    parser.add_argument("--repair-full-text", action="store_true", help="Repair the Read-stage full_text_packet from current Find without rewriting Find artifacts.")
    parser.add_argument("--contract", action="store_true")
    args = parser.parse_args()
    if args.contract:
        print(json.dumps(contract(), ensure_ascii=False, indent=2))
        return
    if args.repair_full_text:
        if not args.project:
            raise SystemExit("--project is required with --repair-full-text")
        cmd = [sys.executable, str(ROOT / "modules" / "reading" / "scripts" / "repair_current_find_full_text_evidence.py"), "--project", args.project, "--force"]
        proc = subprocess.run(cmd, cwd=ROOT, text=True)
        raise SystemExit(proc.returncode)
    if not args.run_id:
        raise SystemExit("--run-id is required unless --repair-full-text is used")
    _ensure_runtime_imports()
    from auto_research.models import AppConfig, ReadRequest
    from read_pipeline import run_read

    config = AppConfig(**_load_json(args.config_json, {}))
    result = run_read(ReadRequest(run_id=args.run_id, paper_ids=args.paper_id, max_papers=args.max_papers), config)
    print(json.dumps({"stage": STAGE_NAME, "run_id": args.run_id, "result": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
