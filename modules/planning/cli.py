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
FRAMEWORK_SCRIPTS = ROOT / "framework" / "scripts"
if str(FRAMEWORK_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(FRAMEWORK_SCRIPTS))
from taste_pythonpath import ensure_taste_pythonpath, script_resolver
ensure_taste_pythonpath(ROOT)
SCRIPTS = script_resolver(ROOT)

from auto_research.models import AppConfig, PlanRequest  # noqa: E402
from auto_research.auto_plan.pipeline import run_plan  # noqa: E402


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
    config = AppConfig(**_load_json(args.config_json, {}))
    result = run_plan(PlanRequest(run_id=args.run_id, idea_ids=args.idea_id, repair_rounds=args.repair_rounds), config)
    print(json.dumps({"stage": STAGE_NAME, "run_id": args.run_id, "result": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
