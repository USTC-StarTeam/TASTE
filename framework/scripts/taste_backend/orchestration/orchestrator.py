from __future__ import annotations

import argparse
import importlib.util
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from taste_backend.common.io import append_text, read_json, utc_now, write_json
from taste_backend.contracts.module_catalog import STAGE_ORDER, contracts_payload, load_all_contracts
from taste_backend.orchestration.decision import Decision, choose_decision
from taste_backend.orchestration.state import WorkflowState, record_from_result, save_state
from taste_backend.runtime.context import FrameworkContext
from taste_backend.runtime.executor import run_module
from taste_backend.status.render import render_markdown


def parse_module_args(items: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--module-arg 格式错误：{item}；应为 stage=--flag value")
        stage, raw = item.split("=", 1)
        stage = stage.strip().replace("-", "_")
        if stage not in STAGE_ORDER:
            raise SystemExit(f"--module-arg 使用未知模块：{stage}")
        result.setdefault(stage, []).extend(shlex.split(raw))
    return result


def load_plan_json(path: str) -> dict[str, Any]:
    return read_json(Path(path), {}) if path else {}


def parse_stage_scope(values: list[str] | None) -> list[str]:
    scope: list[str] = []
    for raw in values or []:
        for item in str(raw or "").replace(";", ",").split(","):
            stage = item.strip().replace("-", "_")
            if not stage:
                continue
            if stage not in STAGE_ORDER:
                raise SystemExit(f"未知限定模块：{stage}")
            if stage not in scope:
                scope.append(stage)
    return scope


def merge_plan_args(base: dict[str, list[str]], plan: dict[str, Any]) -> dict[str, list[str]]:
    merged = {key: list(value) for key, value in base.items()}
    raw = plan.get("module_args") if isinstance(plan.get("module_args"), dict) else {}
    for stage, value in raw.items():
        normalized = str(stage).strip().replace("-", "_")
        if normalized not in STAGE_ORDER:
            continue
        if isinstance(value, list):
            merged.setdefault(normalized, []).extend(str(item) for item in value)
        elif isinstance(value, str):
            merged.setdefault(normalized, []).extend(shlex.split(value))
    return merged


def _compact_public_reason(value: Any, limit: int = 360) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    if not text:
        return ""
    return text[:limit] + ("..." if len(text) > limit else "")


def _flag_value(command: list[str], flag: str) -> str:
    for index, item in enumerate(command):
        if item == flag and index + 1 < len(command):
            return str(command[index + 1]).strip()
        prefix = f"{flag}="
        if str(item).startswith(prefix):
            return str(item)[len(prefix):].strip()
    return ""


def _module_root_from_command(result) -> Path | None:
    marker = f"modules/{result.stage}/main.py"
    for item in result.command:
        text = str(item)
        if text.endswith(marker):
            return Path(text).expanduser().resolve().parent
    return None


def _module_run_dir(result) -> Path | None:
    run_id = _flag_value(list(result.command), "--run-id")
    module_root = _module_root_from_command(result)
    if not run_id or module_root is None:
        return None
    return module_root / "runs" / run_id


def _text_items(value: Any, limit: int = 4) -> list[str]:
    if isinstance(value, list):
        return [_compact_public_reason(item, 160) for item in value if str(item or "").strip()][:limit]
    if str(value or "").strip():
        return [_compact_public_reason(value, 160)]
    return []


def _environment_blocker_reason(result) -> str:
    run_dir = _module_run_dir(result)
    if run_dir is None:
        return ""
    decision_payload = read_json(run_dir / "environment_deployment_decision.json", {})
    if not isinstance(decision_payload, dict):
        return ""
    verdict = decision_payload.get("verdict") if isinstance(decision_payload.get("verdict"), dict) else {}
    parts: list[str] = []
    reject_reason = verdict.get("reject_reason") or verdict.get("reason") or decision_payload.get("reject_reason") or decision_payload.get("reason")
    if reject_reason:
        parts.append(_compact_public_reason(reject_reason, 180))
    taxonomy = verdict.get("failure_taxonomy") if isinstance(verdict.get("failure_taxonomy"), list) else []
    for row in taxonomy[:2]:
        if not isinstance(row, dict):
            continue
        category = str(row.get("category") or "").strip()
        evidence = "；".join(_text_items(row.get("evidence"), 2))
        if category and evidence:
            parts.append(_compact_public_reason(f"{category}: {evidence}", 220))
        elif evidence:
            parts.append(_compact_public_reason(evidence, 220))
    if parts:
        return "；".join(part for part in parts if part)
    decision_name = str(decision_payload.get("decision") or "").strip()
    return f"environment 阶段返回 {decision_name}，需继续处理模块产物中的证据门控。" if decision_name else ""


def _public_blocker_reason(state: WorkflowState, decision: Decision, result) -> str:
    if result.stage == "environment":
        reason = _environment_blocker_reason(result)
        if reason:
            return reason
    return _compact_public_reason(decision.reason or result.stderr_tail or result.stdout_tail or "模块停在证据门控。")


def _record_blocker(state: WorkflowState, decision: Decision, result) -> None:
    state.blockers.append({
        "stage": result.stage,
        "action": result.action,
        "return_code": result.return_code,
        "reason": _public_blocker_reason(state, decision, result),
        "stdout_log": result.stdout_log,
        "stderr_log": result.stderr_log,
        "status": result.status,
    })


def _current_find_run_id(project_root: Path) -> str:
    for rel in [
        "planning/finding/find_progress.json",
        "state/current_find_research_plan.json",
        "state/current_find_recommendation_projection.json",
        "planning/finding/find_results.json",
    ]:
        payload = read_json(project_root / rel, {})
        if isinstance(payload, dict):
            value = str(payload.get("run_id") or payload.get("find_run_id") or payload.get("source_run_id") or payload.get("current_find_run_id") or "").strip()
            if value:
                return value
    return ""


def _current_selected_execution_ids(project_root: Path) -> tuple[str, str]:
    for rel in ["state/current_find_research_plan.json", "planning/finding/plans.json"]:
        payload = read_json(project_root / rel, {})
        if isinstance(payload, dict):
            plan_id = str(payload.get("selected_plan_id") or "").strip()
            idea_id = str(payload.get("selected_idea_id") or "").strip()
            if plan_id or idea_id:
                return plan_id, idea_id
    return "", ""


def _environment_autonomous_deploy_module(ctx: FrameworkContext):
    module_root = ctx.workspace_root / "modules" / "environment"
    script = module_root / "scripts" / "orchestration" / "autonomous_deploy.py"
    if not script.exists():
        raise FileNotFoundError(f"缺少 environment handoff 刷新脚本：{script}")
    for entry in [str(module_root), str(module_root / "scripts")]:
        if entry not in sys.path:
            sys.path.insert(0, entry)
    spec = importlib.util.spec_from_file_location("taste_environment_autonomous_deploy_sync", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 environment handoff 刷新脚本：{script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _refresh_environment_handoff_for_run(ctx: FrameworkContext, run_dir: Path, decision: dict[str, Any]) -> dict[str, Any]:
    normalized = read_json(run_dir / "input_plan.normalized.json", {})
    repo_info = read_json(run_dir / "repo_info.json", {})
    if not isinstance(decision, dict) or not isinstance(normalized, dict) or not isinstance(repo_info, dict):
        return decision if isinstance(decision, dict) else {}
    if not normalized or not repo_info:
        return decision
    module = _environment_autonomous_deploy_module(ctx)
    refreshed = module.attach_environment_handoff(dict(decision), run_dir, normalized, repo_info)
    write_json(run_dir / "environment_deployment_decision.json", refreshed)
    latest_path = ctx.workspace_root / "modules" / "environment" / "latest_decision.json"
    latest = read_json(latest_path, {})
    if not isinstance(latest, dict) or not latest or str(latest.get("run_id") or "") == str(refreshed.get("run_id") or ""):
        write_json(latest_path, refreshed)
    return refreshed


def _environment_run_dir_from_arg(ctx: FrameworkContext, value: str) -> Path:
    raw = Path(str(value or "")).expanduser()
    run_dir = raw if raw.is_absolute() else ctx.workspace_root / raw
    run_dir = run_dir.resolve()
    runs_root = (ctx.workspace_root / "modules" / "environment" / "runs").resolve()
    try:
        run_dir.relative_to(runs_root)
    except ValueError as exc:
        raise SystemExit(f"environment run_dir 必须位于 {runs_root} 内：{run_dir}") from exc
    return run_dir


def _sync_environment_decision_to_project(ctx: FrameworkContext, project: str, run_dir: Path, decision: dict[str, Any]) -> str:
    if not project or not isinstance(decision, dict):
        return ""
    handoff = decision.get("environment_handoff") if isinstance(decision.get("environment_handoff"), dict) else {}
    if handoff.get("ready_for_experimenting") is not True:
        return ""
    repo = handoff.get("repo") if isinstance(handoff.get("repo"), dict) else {}
    conda = handoff.get("conda") if isinstance(handoff.get("conda"), dict) else {}
    paper = handoff.get("paper") if isinstance(handoff.get("paper"), dict) else {}
    repo_path = str(repo.get("repo_path") or "").strip()
    if not repo_path or not Path(repo_path).exists():
        return ""
    project_root = ctx.workspace_root / "projects" / project
    project_state = project_root / "state"
    project_state.mkdir(parents=True, exist_ok=True)
    current_run = _current_find_run_id(project_root)
    current_plan_id, current_idea_id = _current_selected_execution_ids(project_root)
    selected_plan_id = str(paper.get("selected_plan_id") or current_plan_id).strip()
    selected_idea_id = str(paper.get("selected_idea_id") or current_idea_id).strip()
    selected = {
        "name": str(repo.get("repo_url") or repo_path),
        "repo": str(repo.get("repo_url") or ""),
        "repo_url": str(repo.get("repo_url") or ""),
        "repo_path": repo_path,
        "local_path": repo_path,
        "head_commit": str(repo.get("head_commit") or ""),
        "fresh_find_run_id": current_run,
        "current_find_run_id": current_run,
        "selected_plan_id": selected_plan_id,
        "selected_idea_id": selected_idea_id,
        "selection_stage": "environment_claude_code",
        "selected_by_stage": "environment_claude_code",
        "selection_gate": "environment_handoff_ready_for_experimenting",
        "environment_run_id": str(handoff.get("run_id") or decision.get("run_id") or run_dir.name),
    }
    conda_prefix = str(conda.get("prefix") or "")
    experiment_python = str(conda.get("python") or (str(Path(conda_prefix) / "bin" / "python") if conda_prefix else ""))
    env_record = {
        "schema_version": "project.environment_handoff_projection.v1",
        "updated_at": utc_now(),
        "status": "ready_for_experimenting",
        "valid": True,
        "source": str(run_dir / "environment_deployment_decision.json"),
        "environment_handoff": handoff,
        "repo_path": repo_path,
        "local_path": repo_path,
        "repo_url": str(repo.get("repo_url") or ""),
        "conda_env": conda_prefix or str(conda.get("env_name") or ""),
        "conda_env_prefix": conda_prefix,
        "experiment_python": experiment_python,
        "pending_downstream_metrics": handoff.get("pending_downstream_metrics") if isinstance(handoff.get("pending_downstream_metrics"), list) else [],
        "selected": selected,
    }
    selection_record = {
        "schema_version": "project.evidence_ready_repo_selection.v2",
        "status": "ready_for_experimenting",
        "decision": "environment_handoff_ready_for_experimenting",
        "valid": True,
        "accepted_by_claude": True,
        "selection_stage": "environment_claude_code",
        "selected_by_stage": "environment_claude_code",
        "selection_gate": "environment_handoff_ready_for_experimenting",
        "fresh_find_run_id": current_run,
        "current_find_run_id": current_run,
        "selected_plan_id": selected_plan_id,
        "selected_idea_id": selected_idea_id,
        "selected": selected,
        "environment_handoff_path": str(project_state / "environment_handoff.json"),
    }
    active_repo = {
        "name": selected["name"],
        "repo": selected["repo"],
        "repo_url": selected["repo_url"],
        "repo_path": repo_path,
        "local_path": repo_path,
        "head_commit": selected["head_commit"],
        "conda_env": env_record["conda_env"],
        "conda_env_prefix": env_record["conda_env_prefix"],
        "experiment_python": env_record["experiment_python"],
        "environment_run_id": selected["environment_run_id"],
        "role": "main_fresh_base",
        "selection_stage": "environment_claude_code",
        "selection_gate": "environment_handoff_ready_for_experimenting",
        "selected_plan_id": selected_plan_id,
        "selected_idea_id": selected_idea_id,
    }
    write_json(project_state / "environment_handoff.json", env_record)
    write_json(project_state / "evidence_ready_repo_selection.json", selection_record)
    write_json(project_state / "active_repo.json", active_repo)
    return repo_path


def _sync_environment_handoff_to_project(ctx: FrameworkContext, state: WorkflowState, result) -> None:
    if result.stage != "environment" or not state.project:
        return
    run_dir = _module_run_dir(result)
    if run_dir is None:
        return
    decision = read_json(run_dir / "environment_deployment_decision.json", {})
    if not isinstance(decision, dict):
        return
    repo_path = _sync_environment_decision_to_project(ctx, state.project, run_dir, decision)
    if repo_path:
        state.notes.append(f"environment handoff 已同步到项目 {state.project}: {repo_path}")


def sync_environment_handoff(args: argparse.Namespace) -> int:
    ctx = FrameworkContext.create(
        run_id=args.run_id or "sync_environment_handoff",
        state_root=Path(args.state_root) if args.state_root else None,
        python=args.python,
        mode="dry-run",
    )
    run_dir = _environment_run_dir_from_arg(ctx, args.environment_run_dir)
    decision = read_json(run_dir / "environment_deployment_decision.json", {})
    if not isinstance(decision, dict) or not decision:
        print(json.dumps({"status": "missing_decision", "run_dir": str(run_dir)}, ensure_ascii=False, indent=2))
        return 2
    if not args.no_refresh:
        decision = _refresh_environment_handoff_for_run(ctx, run_dir, decision)
    repo_path = _sync_environment_decision_to_project(ctx, str(args.project or "").strip(), run_dir, decision)
    payload = {
        "status": "synced" if repo_path else "not_ready_for_experimenting",
        "project": str(args.project or ""),
        "run_dir": str(run_dir),
        "repo_path": repo_path,
        "ready_for_experimenting": bool((decision.get("environment_handoff") if isinstance(decision.get("environment_handoff"), dict) else {}).get("ready_for_experimenting")),
        "decision": decision.get("decision"),
        "exit_code": decision.get("exit_code"),
        "environment_handoff_path": str(ctx.workspace_root / "projects" / str(args.project or "") / "state" / "environment_handoff.json") if repo_path else "",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if repo_path else 2

def run_workflow(args: argparse.Namespace) -> int:
    plan = load_plan_json(args.plan_json)
    research_goal = args.research_goal or str(plan.get("research_goal") or plan.get("goal") or "")
    ctx = FrameworkContext.create(
        run_id=args.run_id,
        state_root=Path(args.state_root) if args.state_root else None,
        python=args.python,
        mode=args.mode,
        research_goal=research_goal,
    )
    contracts = load_all_contracts(python=ctx.python, use_cli=not args.no_contract_probe)
    write_json(ctx.public_dir / "module_contracts_payload.json", contracts_payload(contracts))

    stage_scope = parse_stage_scope(args.only_stage or [])
    state = WorkflowState(
        run_id=ctx.run_id,
        research_goal=research_goal,
        project=args.project or str(plan.get("project") or ""),
        venue=args.venue or str(plan.get("venue") or ""),
        mode=args.mode,
        strategy=args.strategy,
        status="running",
        module_args=merge_plan_args(parse_module_args(args.module_arg or []), plan),
        stage_scope=stage_scope,
    )
    append_text(ctx.run_dir / "运行说明.txt", "本目录由 framework 后端编排器生成，只记录框架层状态、命令回执和前端可读状态；模块科学产物仍由各模块公开入口写入各自目录，并由 framework 同步给项目和 web。\n")
    save_state(ctx, state, contracts, render_markdown)

    command_index = 1
    for _step in range(max(1, args.max_steps)):
        decision = choose_decision(ctx, state, contracts)
        state.next_action = decision.to_dict()
        if decision.stop:
            state.status = decision.stop_status or "stopped"
            state.current_stage = ""
            state.next_action = decision.to_dict()
            save_state(ctx, state, contracts, render_markdown)
            return 0 if state.status in {"paper_pipeline_finished", "stage_scope_finished", "completed"} else 2
        contract = contracts[decision.stage]
        state.current_stage = decision.stage
        state.status = "running"
        save_state(ctx, state, contracts, render_markdown)

        result = run_module(
            ctx,
            contract=contract,
            action=decision.action or contract.default_action,
            args=decision.args or list(state.module_args.get(decision.stage, [])),
            index=command_index,
            kind="module",
            timeout_sec=args.timeout_sec,
        )
        command_index += 1
        state.records.append(record_from_result(result, message=decision.reason))
        _sync_environment_handoff_to_project(ctx, state, result)
        if result.return_code == 0:
            if decision.stage not in state.completed_stages:
                state.completed_stages.append(decision.stage)
            if args.run_gates:
                for gate_action in contract.gate_actions:
                    gate_result = run_module(
                        ctx,
                        contract=contract,
                        action=gate_action,
                        args=list(state.module_args.get(decision.stage, [])),
                        index=command_index,
                        kind="gate",
                        timeout_sec=args.timeout_sec,
                    )
                    command_index += 1
                    state.records.append(record_from_result(gate_result, message=f"{contract.display_name} 质量门控：{gate_action}"))
                    if gate_result.return_code != 0:
                        _record_blocker(state, decision, gate_result)
                        if not args.repair_loop:
                            state.status = "blocked"
                            save_state(ctx, state, contracts, render_markdown)
                            return gate_result.return_code or 2
            state.status = "running"
        else:
            _record_blocker(state, decision, result)
            if args.repair_loop and args.strategy in {"claude", "hybrid"}:
                state.status = "needs_claude_repair_decision"
            else:
                state.status = result.status
                save_state(ctx, state, contracts, render_markdown)
                return result.return_code or 2
        save_state(ctx, state, contracts, render_markdown)

    scope = state.stage_scope or list(STAGE_ORDER)
    if all(stage in state.completed_stages for stage in scope):
        full_scope = len(scope) == len(STAGE_ORDER) and all(stage in scope for stage in STAGE_ORDER)
        state.status = "paper_pipeline_finished" if full_scope else "stage_scope_finished"
        state.current_stage = ""
        state.next_action = {
            "stop": True,
            "stop_status": state.status,
            "reason": "七个模块流程已完成；后续应查看论文和证据门控产物。" if full_scope else "本次限定阶段已完成；完整科研流程仍由 framework 七阶段状态和项目门控决定。",
            "source": "framework",
        }
        save_state(ctx, state, contracts, render_markdown)
        return 0
    state.status = "max_steps_reached"
    state.next_action = {
        "stop": True,
        "stop_status": "max_steps_reached",
        "reason": "达到最大步数，需继续运行或交给 Claude Code 选择修复路线。",
        "source": "framework",
    }
    save_state(ctx, state, contracts, render_markdown)
    return 2


def print_status(args: argparse.Namespace) -> int:
    ctx = FrameworkContext.create(run_id=args.run_id, state_root=Path(args.state_root) if args.state_root else None, mode="dry-run")
    payload = read_json(ctx.public_dir / "frontend_status.json", {})
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload else 2


def print_contracts(args: argparse.Namespace) -> int:
    ctx = FrameworkContext.create(run_id=args.run_id, state_root=Path(args.state_root) if args.state_root else None, python=args.python, mode="dry-run")
    contracts = load_all_contracts(python=ctx.python, use_cli=not args.no_contract_probe)
    print(json.dumps(contracts_payload(contracts), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TASTE framework 独立后端编排器。")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="启动一次框架层科研流程编排。")
    run.add_argument("--research-goal", default="", help="自然语言研究目标。")
    run.add_argument("--project", default="", help="可选项目 ID；只作为框架状态字段，不强制传给模块。")
    run.add_argument("--venue", default="", help="可选投稿目标。")
    run.add_argument("--mode", choices=["dry-run", "execute"], default="dry-run", help="dry-run 只产生命令计划；execute 才实际调用模块。")
    run.add_argument("--strategy", choices=["deterministic", "hybrid", "claude"], default="deterministic", help="deterministic 按七阶段顺序；hybrid 优先 Claude 决策；claude 必须由 Claude 返回 JSON 决策。")
    run.add_argument("--run-id", default="", help="指定运行 ID。")
    run.add_argument("--state-root", default="", help="状态根目录，必须在 framework 内。默认 framework/workspace。")
    run.add_argument("--python", default="", help="管理 Python，默认当前解释器。")
    run.add_argument("--plan-json", default="", help="可选计划 JSON，支持 research_goal/project/venue/module_args。")
    run.add_argument("--module-arg", action="append", default=[], help="模块公开 CLI 参数，格式：stage=--flag value。可重复。")
    run.add_argument("--only-stage", action="append", default=[], help="只运行指定模块阶段，可重复或逗号分隔；用于 web/framework 单阶段链路。")
    run.add_argument("--max-steps", type=int, default=7, help="最多决策/模块调用步数。")
    run.add_argument("--timeout-sec", type=int, default=None, help="单个模块命令超时秒数。")
    run.add_argument("--run-gates", action="store_true", help="模块成功后追加框架定义的质量门控 action。")
    run.add_argument("--repair-loop", action="store_true", help="模块或门控失败后允许 Claude/Hybrid 再决策修复路线。")
    run.add_argument("--no-contract-probe", action="store_true", help="不调用模块 --contract，仅使用框架静态契约。")
    run.set_defaults(func=run_workflow)

    status = sub.add_parser("status", help="输出某个 run 的前端状态 JSON。")
    status.add_argument("--run-id", required=True)
    status.add_argument("--state-root", default="")
    status.set_defaults(func=print_status)

    contracts = sub.add_parser("contracts", help="输出七个模块公开契约。")
    contracts.add_argument("--run-id", default="contracts_probe")
    contracts.add_argument("--state-root", default="")
    contracts.add_argument("--python", default="")
    contracts.add_argument("--no-contract-probe", action="store_true")
    contracts.set_defaults(func=print_contracts)

    sync = sub.add_parser("sync-environment-handoff", help="从已有 environment run 刷新 handoff 并同步到项目 state。")
    sync.add_argument("--project", required=True, help="项目 ID。")
    sync.add_argument("--environment-run-dir", required=True, help="modules/environment/runs/<run_id> 目录。")
    sync.add_argument("--run-id", default="sync_environment_handoff", help="framework 同步命令自身的 run_id。")
    sync.add_argument("--state-root", default="")
    sync.add_argument("--python", default="")
    sync.add_argument("--no-refresh", action="store_true", help="只投影已有 decision，不重新计算 handoff。")
    sync.set_defaults(func=sync_environment_handoff)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
