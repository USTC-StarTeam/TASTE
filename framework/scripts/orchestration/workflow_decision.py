from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

from contracts.module_catalog import STAGE_ORDER, ModuleContract
from runtime.framework_io import compact_text, json_safe, write_text
from runtime.workflow_context import FrameworkContext
from runtime.workflow_state import WorkflowState


@dataclass(slots=True)
class Decision:
    stage: str = ""
    action: str = ""
    args: list[str] = field(default_factory=list)
    reason: str = ""
    stop: bool = False
    stop_status: str = ""
    source: str = "deterministic"

    def to_dict(self) -> dict[str, Any]:
        return json_safe(self)


def deterministic_decision(state: WorkflowState, contracts: dict[str, ModuleContract]) -> Decision:
    completed = state.completed_set()
    scope = tuple(stage for stage in (state.stage_scope or list(STAGE_ORDER)) if stage in contracts)
    for stage in scope:
        if stage not in completed:
            contract = contracts[stage]
            return Decision(
                stage=stage,
                action=contract.default_action,
                args=list(state.module_args.get(stage, [])),
                reason=f"按标准科研流程推进到 {contract.display_name}。",
                source="deterministic",
            )
    stop_status = "paper_pipeline_finished" if len(scope) == len(STAGE_ORDER) else "stage_scope_finished"
    reason = "七个模块均已完成；等待论文质量门控或人工验收。" if len(scope) == len(STAGE_ORDER) else "本次限定阶段已完成；完整科研流程仍由 framework 七阶段状态和项目门控决定。"
    return Decision(stop=True, stop_status=stop_status, reason=reason, source="deterministic")


def _state_for_prompt(state: WorkflowState) -> dict[str, Any]:
    payload = state.to_dict()
    payload["records"] = payload.get("records", [])[-12:]
    return payload


def claude_prompt(state: WorkflowState, contracts: dict[str, ModuleContract]) -> str:
    contract_payload = {key: contracts[key].to_dict() for key in STAGE_ORDER}
    return f"""
你是 TASTE 框架层的科研流程总控，只能通过七个模块公开入口解决问题，不能替代任何模块内部实现，也不能直接依赖 Web 前端。

目标：根据当前科研状态，自主决定下一步应该调用哪个模块、哪个 action，以及需要传给模块公开 CLI 的参数。你的职责是把真实科研流程推进到高质量论文：发现、精读、想法、计划、环境、实验、论文和证据门控。若证据不足，应回到合适模块修复，而不是强行写论文。

可用模块契约：
```json
{json.dumps(contract_payload, ensure_ascii=False, indent=2)}
```

当前框架状态：
```json
{json.dumps(_state_for_prompt(state), ensure_ascii=False, indent=2)}
```

请只返回一个 JSON object，禁止输出 Markdown。格式如下：
{{
  "decision": "run" 或 "stop",
  "stage": "finding|reading|ideation|planning|environment|experimenting|writing",
  "action": "模块公开 action，留空表示默认 action",
  "args": ["只包含公开 CLI 参数，不要包含 shell 管道"],
  "reason": "中文说明为什么现在调用这个模块",
  "stop_status": "decision=stop 时填写"
}}
""".strip()


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    candidates = [text]
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def ask_claude_decision(ctx: FrameworkContext, state: WorkflowState, contracts: dict[str, ModuleContract], *, timeout_sec: int = 120) -> Decision | None:
    claude = shutil.which("claude", path=ctx.env().get("PATH"))
    if not claude:
        return None
    prompt = claude_prompt(state, contracts)
    prompt_path = ctx.state_dir / "last_claude_decision_prompt.md"
    write_text(prompt_path, prompt + "\n")
    try:
        proc = subprocess.run(
            [claude, "-p", "--tools", "", prompt],
            cwd=ctx.run_dir,
            env=ctx.env(),
            text=True,
            capture_output=True,
            timeout=timeout_sec,
        )
    except Exception as exc:
        state.notes.append(f"Claude 决策不可用：{exc}")
        return None
    write_text(ctx.state_dir / "last_claude_decision_stdout.txt", proc.stdout or "")
    write_text(ctx.state_dir / "last_claude_decision_stderr.txt", proc.stderr or "")
    if proc.returncode != 0:
        state.notes.append(f"Claude 决策返回码 {proc.returncode}：{compact_text(proc.stderr or proc.stdout)}")
        return None
    payload = _extract_json_object(proc.stdout or "")
    if not payload:
        state.notes.append("Claude 决策输出不是 JSON object。")
        return None
    if str(payload.get("decision") or "run").strip().lower() == "stop":
        return Decision(stop=True, stop_status=str(payload.get("stop_status") or "claude_stopped"), reason=str(payload.get("reason") or "Claude 要求停止。"), source="claude")
    stage = str(payload.get("stage") or "").strip().replace("-", "_")
    if stage not in STAGE_ORDER:
        state.notes.append(f"Claude 选择了未知模块：{stage}")
        return None
    args = payload.get("args") if isinstance(payload.get("args"), list) else []
    return Decision(
        stage=stage,
        action=str(payload.get("action") or contracts[stage].default_action),
        args=[str(item) for item in args],
        reason=str(payload.get("reason") or "Claude Code 根据当前科研状态选择下一步。"),
        source="claude",
    )


def choose_decision(ctx: FrameworkContext, state: WorkflowState, contracts: dict[str, ModuleContract]) -> Decision:
    if state.strategy in {"claude", "hybrid"}:
        decision = ask_claude_decision(ctx, state, contracts)
        if decision is not None:
            return decision
        if state.strategy == "claude":
            return Decision(stop=True, stop_status="blocked_claude_decision_unavailable", reason="策略要求 Claude Code 自主决策，但当前无法得到有效 Claude JSON 决策。", source="claude")
    return deterministic_decision(state, contracts)
