#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from experiment_contracts import experiment_rows
from project_paths import build_paths


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def read_text(path: Path, limit: int = 200000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit] if path.exists() else ""
    except Exception:
        return ""


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def file_exists(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text and Path(text).exists())


def process_alive(pid: Any) -> bool:
    try:
        value = int(str(pid).strip())
    except (TypeError, ValueError):
        return False
    return value > 0 and Path(f"/proc/{value}").exists()


def running_reference_reproduction(paths) -> dict[str, Any]:
    job = load_json(paths.state / "fresh_base_reference_full_reproduction_job.json", {})
    if not isinstance(job, dict):
        return {}
    status = str(job.get("status") or "").strip().lower()
    if status not in {"queued", "running", "cancelling"}:
        return {}
    if process_alive(job.get("pid")):
        return job
    return {}


def scalmetrics(row: dict[str, Any]) -> dict[str, Any]:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    out = {str(k): v for k, v in metrics.items() if not isinstance(v, (dict, list))}
    if row.get("metric_name") and row.get("metric_value") not in {None, ""}:
        out[str(row.get("metric_name"))] = row.get("metric_value")
    return out


def latest_rows(rows: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    def key(row: dict[str, Any]) -> str:
        return str(row.get("finished_at") or row.get("timestamp") or row.get("started_at") or "")
    return sorted(rows, key=key)[-limit:]


def build_experiment_iteration_audit(project: str) -> dict[str, Any]:
    paths = build_paths(project)
    rows = experiment_rows(load_json(paths.state / "experiment_registry.json", []))
    recent = latest_rows(rows, 8)
    running_rows = latest_rows(
        [
            row for row in rows
            if str(row.get("status") or "").lower() in {"running", "queued"} or row.get("process_alive") is True
        ],
        4,
    )
    running_reference_job = running_reference_reproduction(paths)
    reference_waiting = bool(running_reference_job and not recent)
    logs_dir = paths.logs
    blockers: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    idea_files = [paths.state / "idea_candidates.json", paths.planning / "finding" / "ideas.json", paths.planning / "hypothesis_arena.md"]
    idea_ok = any(path.exists() and path.stat().st_size > 20 for path in idea_files)
    checks.append({"step": "idea", "status": "pass" if idea_ok else "block", "evidence": [str(p) for p in idea_files if p.exists()]})
    if not idea_ok:
        blockers.append("No ideation/hypothesis evidence is available before the experiment loop.")

    if running_reference_job:
        checks.append({
            "step": "reference_reproduction",
            "status": "running",
            "evidence": [str(paths.state / "fresh_base_reference_full_reproduction_job.json"), str(running_reference_job.get("log_path") or "")],
        })
        warnings.append(
            "参考工作 full reproduction 正在运行；候选实验、论文结论和下一步反思必须等该任务完成并刷新审计后再生成。"
        )

    code_evidence = []
    for row in recent:
        for key in ["implementation_log_path", "artifact_path"]:
            value = row.get(key)
            if value and Path(str(value)).exists():
                code_evidence.append(str(value))
    coding_logs = sorted(logs_dir.glob("**/*coding*"))[-8:] if logs_dir.exists() else []
    code_ok = bool(code_evidence or coding_logs or any("implementation_ready" in str(row) for row in recent))
    checks.append({"step": "code_change_or_explicit_reuse", "status": "pass" if code_ok else "pending" if reference_waiting else "warn", "evidence": code_evidence + [str(p) for p in coding_logs[-4:]]})
    if not code_ok and not reference_waiting:
        warnings.append("No recent code-change or explicit code-reuse evidence was found; The workflow should record whether it changed code or intentionally reused the baseline.")

    command_ok = any(str(row.get("command") or "").strip() for row in recent)
    checks.append({"step": "run_command", "status": "pass" if command_ok else "pending" if reference_waiting else "block", "evidence": [row.get("experiment_id") for row in recent if row.get("command")]})
    if not command_ok and not reference_waiting:
        blockers.append("Recent experiments do not record the exact run command.")

    log_paths = []
    for row in recent:
        artifact = Path(str(row.get("artifact_path") or ""))
        for candidate in [artifact / "stdout_stderr.log", artifact / "stdout_stderr_after_repair.log"]:
            if candidate.exists():
                log_paths.append(candidate)
    log_ok = bool(log_paths)
    loss_ok = False
    for path in log_paths[-8:]:
        text = read_text(path)
        if any(token in text.lower() for token in ["loss", "bpr loss", "diffloss", "sslloss", "epoch"]):
            loss_ok = True
            break
    checks.append({"step": "logs_and_loss", "status": "pass" if log_ok and loss_ok else "warn" if log_ok else "pending" if reference_waiting else "block", "evidence": [str(p) for p in log_paths[-8:]]})
    if not log_ok:
        if not reference_waiting:
            blockers.append("Recent experiments do not expose stdout/stderr logs.")
    elif not loss_ok:
        warnings.append("Recent experiment logs exist but no loss/epoch trace was detected; The workflow should capture training dynamics, not only final metrics.")

    metric_ok = any(scalmetrics(row) for row in recent)
    checks.append({"step": "result_metrics", "status": "pass" if metric_ok else "pending" if reference_waiting else "block", "evidence": [row.get("experiment_id") for row in recent if scalmetrics(row)]})
    if not metric_ok and not reference_waiting:
        blockers.append("Recent experiments do not record scalar result metrics.")

    analysis_ok = any(row.get("bad_case_path") or row.get("bad_case_slices") or row.get("failure_analysis_path") or row.get("claim_verdict") or row.get("counterexample_outcome") for row in recent)
    checks.append({"step": "analysis", "status": "pass" if analysis_ok else "pending" if reference_waiting else "block", "evidence": [row.get("experiment_id") for row in recent if row.get("bad_case_path") or row.get("failure_analysis_path") or row.get("claim_verdict") or row.get("counterexample_outcome")]})
    if not analysis_ok and not reference_waiting:
        blockers.append("Recent experiments do not include bad-case/failure/claim/counterexample analysis.")

    reflection_text = read_text(paths.reports / "iteration_reflection.md")
    next_actions = load_json(paths.state / "next_actions.json", {})
    next_ok = bool(reflection_text.strip()) and bool(next_actions.get("actions") if isinstance(next_actions, dict) else False)
    reflection_status = "pass" if next_ok else "running" if (running_rows or running_reference_job) else "block"
    checks.append({"step": "reflection_and_next_plan", "status": reflection_status, "evidence": [str(paths.reports / "iteration_reflection.md"), str(paths.state / "next_actions.json")]})
    if not next_ok:
        if running_rows:
            warnings.append("当前实验仍在运行，反思和下一步计划应在训练结束、写入本地审计后刷新。")
        elif running_reference_job:
            warnings.append("当前参考工作 full reproduction 仍在运行，实验反思和候选实验计划应在复现审计刷新后生成。")
        else:
            blockers.append("Iteration reflection or next-action plan is missing after experiments.")

    official_runs = [row for row in recent if str(row.get("status", "")).lower() in {"completed", "success", "failed", "incomplete_audit"}]
    if len(official_runs) < 2 and not (running_rows or running_reference_job):
        warnings.append("Recent cycle has fewer than two substantive experiment attempts; TASTE may be under-iterating before paper work.")

    status = "running" if (running_rows or running_reference_job) else "blocked" if blockers else "warn" if warnings else "pass"
    if status == "running":
        if running_rows:
            latest_running = running_rows[-1]
            progress_epoch = latest_running.get("progress_epoch")
            planned_epochs = latest_running.get("planned_epochs")
            progress_text = f"当前进度 epoch {progress_epoch}/{planned_epochs}" if progress_epoch is not None and planned_epochs else "当前训练仍在运行"
            human_summary = f"实验正在运行：TASTE 已登记中间指标和日志，{progress_text}；完成并写入本地审计前不能作为论文结论。"
        else:
            human_summary = (
                "参考工作 full reproduction 正在运行：TASTE 正在验证当前选中基底的论文级复现；"
                "完成并刷新 reference/scientific/paper gates 前，不启动候选实验或论文写作。"
            )
    else:
        human_summary = "实验迭代轨迹完整。" if status == "pass" else "实验迭代轨迹仍不完整：需要确认 idea-code-run-log/loss-analysis-reflection-next plan 都有落盘证据。"
    payload = {
        "project": project,
        "updated_at": now_iso(),
        "status": status,
        "human_summary": human_summary,
        "recent_experiment_count": len(recent),
        "running_experiment_count": len(running_rows),
        "running_experiments": [
            {
                "experiment_id": row.get("experiment_id") or row.get("name"),
                "method": row.get("method") or row.get("method_slug"),
                "dataset": row.get("dataset"),
                "status": row.get("status"),
                "progress_epoch": row.get("progress_epoch"),
                "planned_epochs": row.get("planned_epochs"),
                "metric": scalmetrics(row),
                "log_path": row.get("log_path"),
                "artifact_path": row.get("artifact_path"),
            }
            for row in running_rows
        ],
        "running_reference_reproduction": {
            "status": running_reference_job.get("status"),
            "pid": running_reference_job.get("pid"),
            "dataset": running_reference_job.get("dataset"),
            "artifact_dir": running_reference_job.get("artifact_dir"),
            "log_path": running_reference_job.get("log_path"),
        } if running_reference_job else {},
        "recent_experiments": [
            {
                "experiment_id": row.get("experiment_id") or row.get("name"),
                "method": row.get("method") or row.get("method_slug"),
                "dataset": row.get("dataset"),
                "status": row.get("status"),
                "audit_ready": bool(row.get("audit_ready")),
                "metric": scalmetrics(row),
                "duration_sec": row.get("duration_sec"),
            }
            for row in recent
        ],
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "required_loop": ["idea", "code change or explicit reuse", "run command", "stdout/stderr and loss logs", "metrics", "bad-case/failure analysis", "reflection", "next plan"],
    }
    save_json(paths.state / "experiment_iteration_audit.json", payload)
    lines = [
        "# Experiment Iteration Audit\n\n",
        f"- status: {status}\n",
        f"- summary: {human_summary}\n",
        f"- recent_experiment_count: {len(recent)}\n",
    ]
    if running_reference_job:
        lines.append(f"- running_reference_reproduction: pid={running_reference_job.get('pid')} dataset={running_reference_job.get('dataset')} artifact={running_reference_job.get('artifact_dir')}\n")
    lines.extend(["\n", "## Checks\n"])
    for check in checks:
        lines.append(f"- {check.get('step')}: {check.get('status')} | evidence={check.get('evidence')}\n")
    lines.append("\n## Blockers\n")
    if blockers:
        for blocker in blockers:
            lines.append(f"- {blocker}\n")
    else:
        lines.append("- No blocker.\n")
    lines.append("\n## Warnings\n")
    if warnings:
        for warning in warnings:
            lines.append(f"- {warning}\n")
    else:
        lines.append("- No warning.\n")
    (paths.reports / "experiment_iteration_audit.md").write_text("".join(lines), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit whether the TASTE experiment loop completed idea-code-run-log-analysis-reflection-next-plan trajectory.")
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    payload = build_experiment_iteration_audit(args.project)
    print(build_paths(args.project).reports / "experiment_iteration_audit.md")
    return 0 if payload.get("status") == "pass" else 2 if payload.get("status") in {"blocked", "running"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
