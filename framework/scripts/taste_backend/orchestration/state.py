from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from taste_backend.common.io import json_safe, utc_now, write_json
from taste_backend.contracts.module_catalog import STAGE_ORDER, ModuleContract


@dataclass(slots=True)
class StageRecord:
    stage: str
    action: str
    status: str
    return_code: int
    started_at: str
    finished_at: str
    command: list[str]
    kind: str = "module"
    stdout_log: str = ""
    stderr_log: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return json_safe(self)


@dataclass(slots=True)
class WorkflowState:
    run_id: str
    research_goal: str
    project: str = ""
    venue: str = ""
    mode: str = "dry-run"
    strategy: str = "deterministic"
    status: str = "created"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    current_stage: str = ""
    completed_stages: list[str] = field(default_factory=list)
    records: list[StageRecord] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    next_action: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    module_args: dict[str, list[str]] = field(default_factory=dict)
    stage_scope: list[str] = field(default_factory=list)

    def mark_updated(self) -> None:
        self.updated_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return json_safe(self)

    def completed_set(self) -> set[str]:
        return set(self.completed_stages)


def record_from_result(result, *, message: str = "") -> StageRecord:
    return StageRecord(
        stage=result.stage,
        action=result.action,
        status=result.status,
        return_code=result.return_code,
        started_at=result.started_at,
        finished_at=result.finished_at,
        command=result.command,
        kind=result.kind,
        stdout_log=result.stdout_log,
        stderr_log=result.stderr_log,
        message=message,
    )


def progress_rows(state: WorkflowState, contracts: dict[str, ModuleContract]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    completed = state.completed_set()
    scope = tuple(stage for stage in (state.stage_scope or list(STAGE_ORDER)) if stage in contracts)
    for stage in scope:
        contract = contracts[stage]
        latest = next((record for record in reversed(state.records) if record.stage == stage and record.kind == "module"), None)
        if stage in completed:
            status = "completed"
        elif state.current_stage == stage:
            status = state.status
        elif latest is not None:
            status = latest.status
        else:
            status = "pending"
        rows.append({
            "stage": stage,
            "display_name": contract.display_name,
            "status": status,
            "default_action": contract.default_action,
            "last_return_code": latest.return_code if latest else None,
            "last_action": latest.action if latest else "",
            "last_log": latest.stdout_log if latest else "",
        })
    return rows


def frontend_status_payload(state: WorkflowState, contracts: dict[str, ModuleContract], run_dir: str) -> dict[str, Any]:
    rows = progress_rows(state, contracts)
    done = sum(1 for row in rows if row["status"] == "completed")
    latest = state.records[-1] if state.records else None
    return {
        "run_id": state.run_id,
        "project": state.project,
        "venue": state.venue,
        "mode": state.mode,
        "strategy": state.strategy,
        "status": state.status,
        "progress": {
            "completed": done,
            "total": len(rows),
            "percent": round(done * 100 / max(1, len(rows)), 1),
        },
        "stage_scope": list(state.stage_scope or list(STAGE_ORDER)),
        "modules": rows,
        "current_stage": state.current_stage,
        "next_action": state.next_action,
        "latest_message": latest.message if latest else "尚未执行模块。",
        "latest_record": latest.to_dict() if latest else {},
        "blockers": state.blockers,
        "run_dir": run_dir,
        "updated_at": state.updated_at,
    }


def save_state(ctx, state: WorkflowState, contracts: dict[str, ModuleContract], render_markdown) -> None:
    state.mark_updated()
    write_json(ctx.state_dir / "workflow_state.json", state.to_dict())
    write_json(ctx.public_dir / "frontend_status.json", frontend_status_payload(state, contracts, str(ctx.run_dir)))
    write_json(ctx.public_dir / "module_contracts.json", {stage: contracts[stage].to_dict() for stage in STAGE_ORDER})
    (ctx.public_dir / "workflow_status.md").write_text(render_markdown(state, contracts, str(ctx.run_dir)), encoding="utf-8")
