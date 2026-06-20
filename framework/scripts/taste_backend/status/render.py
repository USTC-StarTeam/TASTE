from __future__ import annotations

from taste_backend.contracts.module_catalog import STAGE_ORDER, ModuleContract
from taste_backend.orchestration.state import WorkflowState, progress_rows


def render_markdown(state: WorkflowState, contracts: dict[str, ModuleContract], run_dir: str) -> str:
    rows = progress_rows(state, contracts)
    lines = [
        "# TASTE framework 工作台状态",
        "",
        f"- run_id：`{state.run_id}`",
        f"- 状态：`{state.status}`",
        f"- 模式：`{state.mode}`；决策策略：`{state.strategy}`",
        f"- 项目：`{state.project or '未指定'}`；目标会议/期刊：`{state.venue or '未指定'}`",
        f"- 研究目标：{state.research_goal or '未填写'}",
        f"- 运行目录：`{run_dir}`",
        f"- 更新时间：`{state.updated_at}`",
        "",
        "## 七阶段进度",
        "",
        "| 阶段 | 状态 | 默认动作 | 最近动作 | 最近返回码 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['display_name']} | `{row['status']}` | `{row['default_action']}` | `{row['last_action'] or '-'}` | `{row['last_return_code'] if row['last_return_code'] is not None else '-'}` |"
        )
    lines.extend(["", "## 下一步", ""])
    if state.next_action:
        lines.append(f"- 模块：`{state.next_action.get('stage', '')}`")
        lines.append(f"- 动作：`{state.next_action.get('action', '')}`")
        lines.append(f"- 理由：{state.next_action.get('reason', '')}")
    else:
        lines.append("- 暂无下一步。")
    if state.blockers:
        lines.extend(["", "## 阻塞", ""])
        for blocker in state.blockers[-8:]:
            lines.append(f"- `{blocker.get('stage', '')}`：{blocker.get('reason', blocker)}")
    if state.records:
        lines.extend(["", "## 最近执行记录", ""])
        for record in state.records[-10:]:
            lines.append(
                f"- `{record.kind}` / `{record.stage}` / `{record.action}`：status=`{record.status}`，rc=`{record.return_code}`，stdout=`{record.stdout_log}`，stderr=`{record.stderr_log}`"
            )
    lines.append("")
    return "\n".join(lines)
