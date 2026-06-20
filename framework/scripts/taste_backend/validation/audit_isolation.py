from __future__ import annotations

from pathlib import Path
from typing import Any

from taste_backend.common.io import ensure_within, write_json
from taste_backend.contracts.module_catalog import STAGE_ORDER, load_all_contracts
from taste_backend.runtime.context import FrameworkContext


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
