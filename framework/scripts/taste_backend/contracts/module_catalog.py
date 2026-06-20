from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from taste_backend.common.io import compact_text, json_safe

STAGE_ORDER: tuple[str, ...] = (
    "finding",
    "reading",
    "ideation",
    "planning",
    "environment",
    "experimenting",
    "writing",
)

DISPLAY_NAMES: dict[str, str] = {
    "finding": "Finding / 发现",
    "reading": "Reading / 精读",
    "ideation": "Ideation / 想法",
    "planning": "Planning / 计划",
    "environment": "Environment / 环境",
    "experimenting": "Experimenting / 实验",
    "writing": "Writing / 论文",
}

DEFAULT_ACTIONS: dict[str, str] = {
    "finding": "find",
    "reading": "read",
    "ideation": "run",
    "planning": "plan",
    "environment": "deploy_from_plan",
    "experimenting": "run",
    "writing": "run",
}

QUALITY_GATE_ACTIONS: dict[str, tuple[str, ...]] = {
    "planning": ("blocker_action",),
    "experimenting": ("reference_reproduction", "audit_iteration", "runtime_integrity"),
    "writing": ("audit_evidence", "submission_readiness"),
}

RESPONSIBILITIES: dict[str, str] = {
    "finding": "只负责候选论文、工具和来源的召回、过滤、评分、排序；不做全文精读。",
    "reading": "只负责当前 Find 结果的全文证据获取、精读摘要和阅读边界审计。",
    "ideation": "把 Find/Read 证据转成可编辑研究想法；不选择执行路线。",
    "planning": "从已通过想法中生成、评估、修复可执行计划，并形成唯一执行合同。",
    "environment": "选择和审计代码/数据基底，探测 loader，锁定实验运行环境；不做新方法实验。",
    "experimenting": "在锁定环境和选定仓库内执行真实实验、解析指标、产出 claim 证据门控。",
    "writing": "基于实验和引用证据生成、修订、编译论文，并审计投稿准备度。",
}


@dataclass(frozen=True)
class ModuleContract:
    key: str
    display_name: str
    entrypoint: str
    default_action: str
    responsibility: str
    required_external_inputs: tuple[str, ...] = ()
    artifacts_in: tuple[str, ...] = ()
    artifacts_out: tuple[str, ...] = ()
    gate_actions: tuple[str, ...] = ()
    source: str = "static"
    contract_error: str = ""

    def command(self, workspace_root: Path, python: str, action: str | None = None, args: Iterable[str] = ()) -> list[str]:
        selected_action = action if action is not None else self.default_action
        cmd = [python, str(workspace_root / self.entrypoint)]
        if selected_action:
            cmd.extend(["--action", selected_action])
        cmd.extend(str(item) for item in args)
        return cmd

    def to_dict(self) -> dict[str, Any]:
        return json_safe(self)


def framework_root() -> Path:
    return Path(__file__).resolve().parents[3]


def workspace_root() -> Path:
    return framework_root().parent


def _fallback_contract(stage: str, *, error: str = "") -> ModuleContract:
    return ModuleContract(
        key=stage,
        display_name=DISPLAY_NAMES.get(stage, stage),
        entrypoint=f"modules/{stage}/main.py",
        default_action=DEFAULT_ACTIONS.get(stage, ""),
        responsibility=RESPONSIBILITIES.get(stage, "七阶段模块之一，具体实现由模块公开入口负责。"),
        gate_actions=QUALITY_GATE_ACTIONS.get(stage, ()),
        source="static_fallback" if error else "static",
        contract_error=compact_text(error, 500),
    )


def _tuple_text(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    if value:
        return (str(value),)
    return ()


def _contract_from_payload(stage: str, payload: dict[str, Any]) -> ModuleContract:
    contract = payload.get("contract") if isinstance(payload.get("contract"), dict) else payload
    return ModuleContract(
        key=stage,
        display_name=str(contract.get("display_name") or DISPLAY_NAMES.get(stage, stage)),
        entrypoint=str(payload.get("entrypoint") or contract.get("entrypoint") or f"modules/{stage}/main.py"),
        default_action=DEFAULT_ACTIONS.get(stage, ""),
        responsibility=str(contract.get("responsibility") or RESPONSIBILITIES.get(stage, "")),
        required_external_inputs=_tuple_text(contract.get("required_external_inputs") or contract.get("external_inputs")),
        artifacts_in=_tuple_text(contract.get("artifacts_in")),
        artifacts_out=_tuple_text(contract.get("artifacts_out")),
        gate_actions=QUALITY_GATE_ACTIONS.get(stage, ()),
        source="module_cli_contract",
    )


def runtime_env(root: Path | None = None) -> dict[str, str]:
    root = root or workspace_root()
    env = os.environ.copy()
    framework = root / "framework"
    entries = [
        str(framework / "scripts"),
        str(framework),
        str(root / "web" / "backend"),
        str(root),
    ]
    modules_root = root / "modules"
    if modules_root.is_dir():
        for stage_dir in sorted(path for path in modules_root.iterdir() if path.is_dir()):
            entries.append(str(stage_dir))
            scripts = stage_dir / "scripts"
            if scripts.is_dir():
                entries.append(str(scripts))
    existing = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    seen: set[str] = set()
    merged: list[str] = []
    for item in [*entries, *existing]:
        if item and item not in seen:
            seen.add(item)
            merged.append(item)
    env["PYTHONPATH"] = os.pathsep.join(merged)
    env["WORKSPACE_ROOT"] = str(root)
    env["TASTE_FRAMEWORK_ROOT"] = str(framework)
    return env


def load_contract_from_module(stage: str, *, python: str | None = None, timeout_sec: int = 20) -> ModuleContract:
    if stage not in STAGE_ORDER:
        raise ValueError(f"未知模块：{stage}")
    root = workspace_root()
    entry = root / "modules" / stage / "main.py"
    if not entry.exists():
        return _fallback_contract(stage, error=f"模块入口不存在：{entry}")
    cmd = [python or sys.executable, str(entry), "--contract"]
    try:
        proc = subprocess.run(cmd, cwd=root, env=runtime_env(root), text=True, capture_output=True, timeout=timeout_sec)
    except Exception as exc:
        return _fallback_contract(stage, error=f"读取公开契约失败：{exc}")
    if proc.returncode != 0:
        return _fallback_contract(stage, error=proc.stderr or proc.stdout or f"返回码 {proc.returncode}")
    try:
        payload = json.loads(proc.stdout)
    except Exception as exc:
        return _fallback_contract(stage, error=f"契约 JSON 解析失败：{exc}; 输出：{proc.stdout[:300]}")
    if not isinstance(payload, dict):
        return _fallback_contract(stage, error="契约输出不是 JSON object")
    return _contract_from_payload(stage, payload)


def load_all_contracts(*, python: str | None = None, use_cli: bool = True) -> dict[str, ModuleContract]:
    contracts: dict[str, ModuleContract] = {}
    for stage in STAGE_ORDER:
        contracts[stage] = load_contract_from_module(stage, python=python) if use_cli else _fallback_contract(stage)
    return contracts


def contracts_payload(contracts: dict[str, ModuleContract]) -> dict[str, Any]:
    return {
        "stage_order": list(STAGE_ORDER),
        "module_count": len(contracts),
        "modules": [contracts[stage].to_dict() for stage in STAGE_ORDER if stage in contracts],
    }
