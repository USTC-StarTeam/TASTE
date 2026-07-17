#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from contracts.module_catalog import STAGE_ORDER, load_all_contracts
from runtime.framework_io import ensure_within, write_json
from runtime.workflow_context import FrameworkContext


def audit(ctx: FrameworkContext) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    try:
        ensure_within(ctx.framework_root, ctx.state_root)
        ensure_within(ctx.framework_root, ctx.run_dir)
    except ValueError as exc:
        issues.append({"level": "error", "message": str(exc)})
    for stage in STAGE_ORDER:
        entry = ctx.workspace_root / "modules" / stage / "main.py"
        if not entry.exists():
            issues.append({"level": "error", "message": f"缺少模块公开入口：{entry}"})
    contracts = load_all_contracts(python=ctx.python, use_cli=True)
    for stage, contract in contracts.items():
        if contract.contract_error:
            issues.append({"level": "warning", "message": f"{stage} 契约读取降级：{contract.contract_error}"})
    dev_dirs = [str(path.relative_to(ctx.workspace_root)) for path in ctx.workspace_root.glob("**/*_dev") if path.is_dir()]
    dev_dirs.extend(str(path.relative_to(ctx.workspace_root)) for path in ctx.workspace_root.glob("**/framework_dev") if path.is_dir())
    dev_dirs = sorted(set(dev_dirs))
    if dev_dirs:
        issues.append({"level": "error", "message": "仍存在 dev 更新目录：" + ", ".join(dev_dirs[:20])})
    payload = {
        "status": "pass" if not any(item["level"] == "error" for item in issues) else "fail",
        "framework_root": str(ctx.framework_root),
        "state_root": str(ctx.state_root),
        "run_dir": str(ctx.run_dir),
        "module_count": len(STAGE_ORDER),
        "dev_dirs": dev_dirs,
        "issues": issues,
    }
    write_json(ctx.public_dir / "isolation_audit.json", payload)
    return payload


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
