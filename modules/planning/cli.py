from __future__ import annotations

import argparse
import json
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
    parser = argparse.ArgumentParser(description="Run the standalone Planning module backend.")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--config-json", default="")
    parser.add_argument("--idea-id", action="append", default=[])
    parser.add_argument("--repair-rounds", type=int, default=3)
    parser.add_argument("--contract", action="store_true")
    args = parser.parse_args()
    if args.contract:
        print(json.dumps(contract(), ensure_ascii=False, indent=2))
        return
    if not args.run_id:
        raise SystemExit("--run-id is required")
    _ensure_runtime_imports()
    from auto_research.models import AppConfig, PlanRequest
    from plan_pipeline import run_plan

    config = AppConfig(**_load_json(args.config_json, {}))
    result = run_plan(PlanRequest(run_id=args.run_id, idea_ids=args.idea_id, repair_rounds=args.repair_rounds), config)
    print(json.dumps({"stage": STAGE_NAME, "run_id": args.run_id, "result": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
