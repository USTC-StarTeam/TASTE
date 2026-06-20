#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from taste_backend.runtime.context import FrameworkContext
from taste_backend.validation.audit_isolation import audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审计 framework 后端是否保持隔离和七模块公开入口可用。")
    parser.add_argument("--run-id", default="isolation_audit")
    parser.add_argument("--state-root", default="")
    parser.add_argument("--python", default="")
    args = parser.parse_args(argv)
    ctx = FrameworkContext.create(run_id=args.run_id, state_root=Path(args.state_root) if args.state_root else None, python=args.python, mode="dry-run")
    payload = audit(ctx)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
