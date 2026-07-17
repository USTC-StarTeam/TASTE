#!/usr/bin/env python3
from __future__ import annotations

import json
from typing import Any

from project.project_paths import ROOT
from runtime.framework_io import utc_now as now_iso

WORK_STATUS_PATH = ROOT / "工作状态.txt"
MAX_LIST_ITEMS = 8


def _clean(value: Any, default: str = "未记录", limit: int = 900) -> str:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if not text:
        return default
    return text[:limit] + ("..." if len(text) > limit else "")


def _bool_text(value: Any) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return _clean(value)


def _pid_line(label: str, job: Any) -> str:
    if not isinstance(job, dict) or not job:
        return f"- {label}: 未记录"
    pid = _clean(job.get("pid"), "-")
    status = _clean(job.get("status"), "unknown")
    alive = job.get("alive", job.get("process_alive"))
    elapsed = job.get("elapsed_sec") or job.get("etimes")
    parts = [f"状态={status}", f"PID={pid}", f"存活={_bool_text(alive)}"]
    if elapsed not in (None, ""):
        parts.append(f"运行时长={elapsed}s")
    log_path = job.get("log_path") or job.get("log")
    if log_path:
        parts.append(f"日志={_clean(log_path, limit=500)}")
    stale = job.get("stale_reason")
    if stale:
        parts.append(f"注意={_clean(stale)}")
    return f"- {label}: " + "；".join(parts)


def _list_lines(items: Any, empty: str = "- 无") -> list[str]:
    if not items:
        return [empty]
    rows: list[str] = []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return [f"- {_clean(items)}"]
    for item in items[:MAX_LIST_ITEMS]:
        if isinstance(item, dict):
            code = item.get("code") or item.get("name") or item.get("category") or item.get("route") or item.get("action") or "item"
            sev = item.get("severity") or item.get("return_code") or item.get("status") or ""
            msg = item.get("message") or item.get("summary") or item.get("stdout_tail") or item.get("stderr_tail") or item
            rows.append(f"- {_clean(code, limit=140)}" + (f" ({_clean(sev, limit=80)})" if sev != "" else "") + f": {_clean(msg, limit=700)}")
        else:
            rows.append(f"- {_clean(item)}")
    if isinstance(items, list) and len(items) > MAX_LIST_ITEMS:
        rows.append(f"- 其余 {len(items) - MAX_LIST_ITEMS} 条已省略，详见对应 state/log 文件。")
    return rows


def _step_rc_note(step: dict[str, Any]) -> str:
    rc = step.get("return_code")
    name = str(step.get("name") or "")
    timed_out = bool(step.get("timed_out"))
    if timed_out:
        return "超时，需要检查日志"
    if rc in (None, ""):
        return "未记录返回码"
    if rc == 0:
        return "执行成功"
    if rc == 2 and name == "build_blocker_action_plan":
        return "门控仍阻塞，报告已生成；这不是脚本崩溃"
    return "异常返回码，需要检查 stdout/stderr 和报告"


def _step_lines(steps: Any) -> list[str]:
    if not steps:
        return ["- 本次未执行额外子步骤。"]
    rows: list[str] = []
    if not isinstance(steps, list):
        return [f"- {_clean(steps)}"]
    for step in steps[:MAX_LIST_ITEMS]:
        if isinstance(step, dict):
            name = _clean(step.get("name"), "unnamed", 180)
            rc = _clean(step.get("return_code"), "-")
            timed_out = _bool_text(step.get("timed_out", False))
            started = _clean(step.get("started_at"), "-")
            finished = _clean(step.get("finished_at"), "-")
            note = _step_rc_note(step)
            rows.append(f"- {name}: rc={rc}；含义={note}；超时={timed_out}；开始={started}；结束={finished}")
        else:
            rows.append(f"- {_clean(step)}")
    if len(steps) > MAX_LIST_ITEMS:
        rows.append(f"- 其余 {len(steps) - MAX_LIST_ITEMS} 个步骤已省略，详见 state JSON。")
    return rows


def _process_lines(processes: Any) -> list[str]:
    if not processes:
        return ["- 未发现与该项目相关的后台进程。"]
    rows: list[str] = []
    if not isinstance(processes, list):
        return [f"- {_clean(processes)}"]
    for proc in processes[:MAX_LIST_ITEMS]:
        if isinstance(proc, dict):
            pid = _clean(proc.get("pid"), "-")
            ppid = _clean(proc.get("ppid"), "-")
            elapsed = _clean(proc.get("etimes"), "-")
            cpu = _clean(proc.get("pcpu"), "-")
            mem = _clean(proc.get("pmem"), "-")
            cmd = _clean(proc.get("cmd"), "", 500)
            rows.append(f"- PID={pid} PPID={ppid} 运行={elapsed}s CPU={cpu}% MEM={mem}% CMD={cmd}")
        else:
            rows.append(f"- {_clean(proc)}")
    if len(processes) > MAX_LIST_ITEMS:
        rows.append(f"- 其余 {len(processes) - MAX_LIST_ITEMS} 个进程已省略。")
    return rows


def append_entry(title: str, lines: list[str]) -> None:
    WORK_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(["", f"## {now_iso()} - {title}", *lines, ""]) + "\n"
    with WORK_STATUS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _signature_path(project: str):
    safe_project = "".join(ch for ch in str(project or "project") if ch.isalnum() or ch in "_-.") or "project"
    return ROOT / "projects" / safe_project / "state" / "work_status_signatures.json"


def _signature_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _signature_value(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_signature_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _load_signatures(project: str) -> dict[str, Any]:
    path = _signature_path(project)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def _save_signatures(project: str, data: dict[str, Any]) -> None:
    path = _signature_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _append_entry_if_changed(project: str, key: str, title: str, lines: list[str], signature: dict[str, Any]) -> bool:
    signatures = _load_signatures(project)
    normalized = _signature_value(signature)
    if signatures.get(key) == normalized:
        return False
    signatures[key] = normalized
    signatures[f"{key}_updated_at"] = now_iso()
    _save_signatures(project, signatures)
    append_entry(title, lines)
    return True


def _job_signature(job: Any) -> dict[str, Any]:
    if not isinstance(job, dict):
        return {}
    return {
        "status": job.get("status"),
        "pid": job.get("pid"),
        "process_alive": job.get("process_alive", job.get("alive")),
        "kind": job.get("kind"),
        "stage": job.get("stage"),
        "log_path": job.get("log_path") or job.get("log"),
    }


def _codes_signature(items: Any) -> list[Any]:
    if not isinstance(items, list):
        return []
    rows = []
    for item in items[:MAX_LIST_ITEMS]:
        if isinstance(item, dict):
            rows.append({
                "code": item.get("code") or item.get("id") or item.get("category") or item.get("route") or item.get("action"),
                "severity": item.get("severity") or item.get("status") or item.get("return_code"),
                "message": _clean(item.get("message") or item.get("issue") or item.get("summary"), "", 240),
            })
        else:
            rows.append(_clean(item, "", 240))
    return rows


def _supervision_signature(payload: dict[str, Any]) -> dict[str, Any]:
    packet = payload.get("packet_counts") if isinstance(payload.get("packet_counts"), dict) else {}
    return {
        "status": payload.get("status"),
        "compact_status": payload.get("compact_status"),
        "action": payload.get("action"),
        "action_rc": payload.get("action_rc"),
        "find_run_id": payload.get("find_run_id"),
        "main_base": payload.get("main_base"),
        "blocker_category": payload.get("blocker_category"),
        "blocker_plan_status": payload.get("blocker_plan_status"),
        "top_route": payload.get("top_route"),
        "top_action": _clean(payload.get("top_action"), "", 300),
        "data_status": payload.get("data_status"),
        "loader_status": payload.get("loader_status"),
        "reference_gate_decision": payload.get("reference_gate_decision"),
        "packet_counts": {"readings": packet.get("readings"), "ideas": packet.get("ideas"), "plans": packet.get("plans")},
        "full_cycle_job": _job_signature(payload.get("full_cycle_job")),
        "full_reference_job": _job_signature(payload.get("full_reference_job")),
        "issues": _codes_signature(payload.get("issues")),
        "repairs": _codes_signature(payload.get("repairs")),
        "observations": _codes_signature(payload.get("observations")),
    }


def _session_snapshot_signature(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "blocker": payload.get("blocker"),
        "top_route": payload.get("top_route"),
        "main_base": payload.get("main_base"),
        "find_run_id": payload.get("find_run_id"),
        "readings": payload.get("readings"),
        "ideas": payload.get("ideas"),
        "plans": payload.get("plans"),
        "full_job_status": payload.get("full_job_status"),
        "full_job_pid": payload.get("full_job_pid"),
    }


def append_supervision_status(project: str, payload: dict[str, Any]) -> None:
    packet = payload.get("packet_counts") if isinstance(payload.get("packet_counts"), dict) else {}
    literature = payload.get("literature") if isinstance(payload.get("literature"), dict) else {}
    literature_counts = literature.get("counts") if isinstance(literature.get("counts"), dict) else {}
    api = payload.get("api") if isinstance(payload.get("api"), dict) else {}
    lines = [
        "### 当前结论",
        f"- 项目: {project}",
        f"- 目标会议/期刊: {_clean(payload.get('target_venue'))}",
        f"- 当前环境阶段选出的基底/仓库: {_clean(payload.get('main_base'))}",
        f"- Find run: {_clean(payload.get('find_run_id'))}",
        f"- 当前状态: {_clean(payload.get('status'))}",
        f"- 页面/API 状态: compact={_clean(payload.get('compact_status'))}；blocker={_clean(payload.get('blocker_category'))}",
        f"- 本次监督动作: {_clean(payload.get('action'))}；返回码={_clean(payload.get('action_rc'), '-')}",
        f"- 文献包计数: 精读={_clean(packet.get('readings'), '0')}；想法={_clean(packet.get('ideas'), '0')}；计划={_clean(packet.get('plans'), '0')}",
        "",
        "### 文献 / Find 质量",
        f"- 当前 Find 选择项: {_clean(literature.get('selection'), '{}')}",
        f"- 强推荐={_clean(literature_counts.get('strong_recommendations'), '0')}；文章输出={_clean(literature_counts.get('articles'), '0')}；精读={_clean(literature_counts.get('read_candidates') or literature_counts.get('strong_recommendations'), '0')}；内部审计候选={_clean(literature_counts.get('triage_candidates') or literature_counts.get('audit_candidates'), '0')}；已评分候选={_clean(literature_counts.get('evaluated_candidates'), '0')}；批判候选={_clean(literature_counts.get('critique_candidates'), '0')}",
        f"- 已降级或仅作边界/反例的候选数: {_clean(literature.get('demoted_or_not_positive_support'), '0')}",
        "- 当前强推荐前列:",
        *_list_lines(literature.get('top_strong'), "- 暂无强推荐条目。"),
        f"- 关键文件: {_clean(literature.get('files'), '{}')}",
        "",
        "### 关键门控",
        f"- 数据门控: status={_clean(payload.get('data_status'))}；decision={_clean(payload.get('data_decision'))}",
        f"- Loader 门控: status={_clean(payload.get('loader_status'))}；decision={_clean(payload.get('loader_decision'))}",
        f"- 参考复现门控: decision={_clean(payload.get('reference_gate_decision'))}",
        f"- 当前修复路线: {_clean(payload.get('top_route'))}",
        f"- 当前首要阻塞: {_clean(payload.get('top_action'))}",
        f"- blocker plan 状态: {_clean(payload.get('blocker_plan_status'))}",
        "",
        "### 后台进程",
        _pid_line("full reference reproduction", payload.get("full_reference_job")),
        _pid_line("full research cycle", payload.get("full_cycle_job")),
        *_process_lines(payload.get("processes")),
        "",
        "### API 验收",
        f"- /api/jobs?compact=1&limit=20: {'失败 - ' + _clean(api.get('jobs_error')) if api.get('jobs_error') else '正常；返回条数=' + _clean(api.get('jobs_count'), '0')}",
        f"- /api/projects/{project}?compact=1: {'失败 - ' + _clean(api.get('compact_error')) if api.get('compact_error') else '正常；已读取项目 compact 状态'}",
        "",
        "### 本次执行和修复",
        *_step_lines(payload.get("steps")),
        "- 修复/同步项:",
        *_list_lines(payload.get("repairs"), "- 无修复项。"),
        "",
        "### 需要人类关注的问题",
        *_list_lines(payload.get("issues"), "- 未发现新的严重问题。"),
        "- 观察项:",
        *_list_lines(payload.get("observations"), "- 无额外观察项。"),
        "",
        "### 下一步",
        f"- {_clean(payload.get('next_action'))}",
        "",
        "### 安全约束",
        *_list_lines(payload.get("guardrails"), "- 未记录。"),
    ]
    _append_entry_if_changed(project, "supervision", f"TASTE 统一监督 tick / {project}", lines, _supervision_signature(payload))


def append_session_snapshot_status(project: str, payload: dict[str, Any]) -> None:
    lines = [
        "### 接管快照",
        f"- 项目: {project}",
        f"- 目标会议/期刊: {_clean(payload.get('target_venue'))}",
        f"- 当前状态: {_clean(payload.get('status'))}",
        f"- 当前 blocker: {_clean(payload.get('blocker'))}",
        f"- 修复路线: {_clean(payload.get('top_route'))}",
        f"- 当前环境阶段选出的基底/仓库: {_clean(payload.get('main_base'))}",
        f"- Find run: {_clean(payload.get('find_run_id'))}",
        f"- 文献包计数: 精读={_clean(payload.get('readings'), '0')}；想法={_clean(payload.get('ideas'), '0')}；计划={_clean(payload.get('plans'), '0')}",
        _pid_line("full reference reproduction", {"status": payload.get("full_job_status"), "pid": payload.get("full_job_pid"), "alive": None}),
        "",
        "### 用途",
        "- 这是接管/刷新工作状态时的人类可读快照；完整推进仍以统一监督 tick 和 主控任务为准。",
    ]
    _append_entry_if_changed(project, "session_snapshot", f"TASTE 接管快照 / {project}", lines, _session_snapshot_signature(payload))


def append_guard_status(project: str, entrypoint: str, venue: str, action: str, rc: int | str = "") -> None:
    lines = [
        "### Guard 触发",
        f"- 项目: {project}",
        f"- 目标会议/期刊: {_clean(venue)}",
        f"- 入口: {_clean(entrypoint)}",
        f"- 动作: {_clean(action)}",
        f"- 返回码: {_clean(rc, '-')}",
        "",
        "### 决策",
        "- fresh-base 硬门控未过时，只允许统一 safe-unblock/恢复；不启动训练、论文写作、claim promotion、第二条 Find、pair_compare 或历史主线回退。",
    ]
    append_entry(f"TASTE guard / {project}", lines)
