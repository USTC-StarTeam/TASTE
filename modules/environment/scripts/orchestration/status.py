#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.common.io_utils import newest_existing, read_json

MODULE_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description="查看 environment 最近一次自主部署状态。")
    parser.add_argument("--run-id", default="", help="指定 run_id；不指定则读取 latest_decision.json 或最新 runs/*。")
    args = parser.parse_args()
    if args.run_id:
        decision_path = MODULE_ROOT / "runs" / args.run_id / "environment_deployment_decision.json"
    else:
        latest = MODULE_ROOT / "latest_decision.json"
        decision_path = latest if latest.exists() else None
        if decision_path is None:
            decision_path = newest_existing(list((MODULE_ROOT / "runs").glob("*/environment_deployment_decision.json"))) if (MODULE_ROOT / "runs").exists() else None
    payload = read_json(decision_path, {}) if isinstance(decision_path, Path) else {}
    status = "ok" if payload else "missing"
    print(
        json.dumps(
            {
                "status": status,
                "decision_path": str(decision_path or ""),
                "decision": payload,
                "latest_decision": payload,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if payload else 2


if __name__ == "__main__":
    raise SystemExit(main())
