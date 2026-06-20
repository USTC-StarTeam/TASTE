#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

CURRENT = Path(__file__).resolve()
SCRIPTS = CURRENT.parents[1]
for entry in [SCRIPTS / "common", SCRIPTS / "records", SCRIPTS / "execution"]:
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from experiment_plan import ExperimentPlan, normalize_plan, plan_prompt_block
from experiment_records import collect_metrics, load_registry, primary_metric, upsert_record, write_record_tables
from file_utils import atomic_write_json, now_iso, slugify
from runtime_environment import assert_runtime_ready, build_env, build_runtime_lock, sh_quote, write_environment_files

MODULE_ROOT = CURRENT.parents[2]
DEFAULT_OUTPUT_ROOT = MODULE_ROOT / "runtime" / "autonomous"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="根据实验计划、Conda 环境和基础代码库，驱动 Claude Code 自主迭代实验并维护记录表。")
    parser.add_argument("--plan", required=True, help="实验计划文件，支持 JSON/YAML/纯文本。")
    parser.add_argument("--repo-path", required=True, help="已有基础代码库路径。Claude 和验证命令默认只在该路径内工作。")
    parser.add_argument("--conda-env", default="", help="实验运行 Conda 环境名；为空时尝试从 plan 读取。")
    parser.add_argument("--conda-base", default="", help="Conda 安装根目录；通常可自动发现。")
    parser.add_argument("--nvm-dir", default="", help="nvm 根目录；通常可自动发现。")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="所有状态、日志、记录表和中间产物输出根目录。默认在 experimenting/runtime 内。")
    parser.add_argument("--experiment-id", default="", help="覆盖 plan 中的实验 ID。")
    parser.add_argument("--run-command", default="", help="覆盖 plan 中的验证/训练命令。")
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--claude-timeout-sec", type=int, default=14400)
    parser.add_argument("--command-timeout-sec", type=int, default=0, help="验证命令超时；0 表示不额外限制。")
    parser.add_argument("--permission-mode", default="acceptEdits", choices=["acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"])
    parser.add_argument("--model", default="")
    parser.add_argument("--extra-context", action="append", default=[], help="额外上下文文件，可重复传入。")
    parser.add_argument("--skip-claude", action="store_true", help="只做环境/记录流程自测，不调用 Claude。")
    parser.add_argument("--skip-validation-command", action="store_true", help="不由包装器执行 plan/run-command 中的验证命令。")
    parser.add_argument("--no-stop-on-success", action="store_true", help="即使某轮成功，也继续跑满 max-iterations。")
    parser.add_argument("--dry-run", action="store_true", help="只写出计划和环境锁，不调用 Claude 或验证命令。")
    return parser.parse_args(argv)


def read_extra_context(paths: list[str], limit: int = 24000) -> str:
    sections: list[str] = []
    for item in paths:
        path = Path(item).expanduser().resolve()
        if not path.exists() or not path.is_file():
            sections.append(f"## 缺失的额外上下文\n{path}")
            continue
        sections.append(f"## {path}\n" + path.read_text(encoding="utf-8", errors="replace")[:limit])
    return "\n\n".join(sections)


def build_claude_prompt(plan: ExperimentPlan, *, repo: Path, artifact_dir: Path, output_root: Path, lock: dict[str, Any], iteration: int, max_iterations: int, prior_records: list[dict[str, Any]], run_command: str, extra_context: str) -> str:
    prior = json.dumps(prior_records[-5:], ensure_ascii=False, indent=2)[:12000]
    activation = lock.get("activation_command", "")
    return f"""
你是 Experimenting 独立后端里的 Claude Code 实验代理。你的任务不是写论文，也不是改 TASTE 框架或前端，而是在给定基础代码库中围绕实验计划做最小、可审计、可回滚的实验迭代。

硬约束：
- 只能修改基础代码库: {repo}
- 只能把本轮中间产物、指标、坏例、审计摘要写入: {artifact_dir}
- 不要修改 /home/fmh/workspace/TASTE/modules/experimenting 或其它 TASTE 模块；也不要改 web/frontend/framework。
- 必须使用下面的运行环境方式，不要使用系统 python、裸 conda run 或其它未锁定解释器。
- 不要伪造指标、日志、坏例、引用或实验结论。没有跑通就如实记录失败原因。

环境激活片段：
```bash
{activation}
```

本轮信息：
- iteration: {iteration}/{max_iterations}
- repo: {repo}
- artifact_dir: {artifact_dir}
- output_root: {output_root}
- 推荐验证命令: {run_command or '计划未提供；请你根据代码库选择最小可信 smoke/validation 命令，并写入 experiment_iteration_summary.json'}

你要执行的闭环：
1. 阅读实验计划和代码库，确认本轮最小目标。
2. 做最小必要代码/config 修改；如果无需修改，明确说明复用原因。
3. 如你自己运行命令，必须把 stdout/stderr 或摘要保存到 artifact_dir；包装器之后也可能再运行推荐验证命令。
4. 在 artifact_dir 写 `experiment_iteration_summary.json`，至少包含 status、changed_files、commands、metrics、failure_reason、next_action。
5. 如产生指标，写 `metrics.json`；如发现坏例，写 `bad_cases.json` 或在 summary 中列出路径。
6. 停止前给出是否继续深化、修复、比较或剪枝的判断。

实验计划：
```json
{plan_prompt_block(plan)}
```

最近迭代记录：
```json
{prior or '[]'}
```

额外上下文：
{extra_context or '无'}
""".strip() + "\n"


def command_for_log(cmd: list[str], limit: int = 220) -> str:
    parts: list[str] = []
    for item in cmd:
        text = str(item).replace("\n", "\\n")
        if len(text) > limit:
            text = text[:limit] + "...<truncated>"
        parts.append(shlex.quote(text))
    return " ".join(parts)


def run_process(cmd: list[str], *, cwd: Path, env: dict[str, str], log_path: Path, timeout: int | None = None, stdin_text: str | None = None) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = now_iso()
    with log_path.open("w", encoding="utf-8", errors="replace") as handle:
        handle.write(f"# started_at: {started}\n# cwd: {cwd}\n# command: {command_for_log(cmd)}\n\n")
        handle.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            text=True,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            proc.communicate(input=stdin_text, timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.communicate(timeout=20)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                proc.communicate()
        finished = now_iso()
        rc = 124 if timed_out else int(proc.returncode or 0)
        handle.write(f"\n# finished_at: {finished}\n# return_code: {rc}\n")
    return {"started_at": started, "finished_at": finished, "return_code": rc, "timed_out": timed_out, "log_path": str(log_path)}


def run_claude(prompt: str, *, repo: Path, artifact_dir: Path, lock: dict[str, Any], args: argparse.Namespace, iteration: int) -> dict[str, Any]:
    claude = ((lock.get("tools") or {}).get("claude") or {}).get("path") or ""
    prompt_path = artifact_dir / "claude_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    if args.skip_claude:
        log_path = artifact_dir / "claude_stdout.log"
        log_path.write_text("skip_claude=true\n", encoding="utf-8")
        return {"return_code": 0, "log_path": str(log_path), "prompt_path": str(prompt_path), "status": "skipped"}
    cmd = [claude, "-p", "--permission-mode", args.permission_mode, "--add-dir", str(repo), "--add-dir", str(artifact_dir), "--output-format", "json"]
    if args.model:
        cmd.extend(["--model", args.model])
    cmd.append(prompt)
    env = build_env(lock, {"EXPERIMENT_ARTIFACT_DIR": str(artifact_dir), "EXPERIMENT_ITERATION": str(iteration)})
    result = run_process(cmd, cwd=repo, env=env, log_path=artifact_dir / "claude_stdout.log", timeout=max(60, args.claude_timeout_sec))
    result.update({"prompt_path": str(prompt_path), "status": "completed" if result["return_code"] == 0 else "failed"})
    return result


def run_validation(command: str, *, repo: Path, artifact_dir: Path, lock: dict[str, Any], timeout_sec: int) -> dict[str, Any]:
    if not command.strip():
        return {"return_code": 0, "status": "not_configured", "log_path": ""}
    env = build_env(lock, {"EXPERIMENT_ARTIFACT_DIR": str(artifact_dir), "EXPERIMENT_LOG_PATH": str(artifact_dir / "validation_stdout.log")})
    activation = lock.get("activation_command", "")
    script = activation + "\n" + f"cd {sh_quote(str(repo))}\n" + command + "\n"
    return run_process(["bash", "-lc", script], cwd=repo, env=env, log_path=artifact_dir / "validation_stdout.log", timeout=timeout_sec if timeout_sec > 0 else None)


def summarize_claude_log(log_path: str) -> str:
    if not log_path:
        return ""
    path = Path(log_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    body = text.split("\n\n", 1)[1] if "\n\n" in text else text
    body = body.rsplit("\n# finished_at:", 1)[0].strip()
    candidates = [body]
    json_start = body.find("{")
    json_end = body.rfind("}")
    if json_start >= 0 and json_end > json_start:
        candidates.append(body[json_start:json_end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if isinstance(payload, dict):
            for key in ["result", "summary", "text", "message"]:
                if payload.get(key):
                    return str(payload[key])[:1000]
    return " ".join(body.split())[:1000]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    plan = normalize_plan(args.plan)
    if args.experiment_id:
        plan.experiment_id = slugify(args.experiment_id)
    repo = Path(args.repo_path).expanduser().resolve()
    if not repo.exists() or not repo.is_dir():
        raise SystemExit(f"基础代码库不存在: {repo}")
    output_root = Path(args.output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    conda_env = args.conda_env or plan.conda_env
    if not conda_env:
        raise SystemExit("必须通过 --conda-env 或实验计划 conda_env 指定运行环境")

    lock = build_runtime_lock(conda_env, conda_base=args.conda_base, nvm_dir=args.nvm_dir, require_claude=not args.skip_claude)
    env_files = write_environment_files(output_root, lock)
    if not args.dry_run:
        assert_runtime_ready(lock, require_claude=not args.skip_claude)

    run_command = args.run_command or plan.run_command
    run_id = f"{plan.experiment_id}_{now_iso().replace(':', '').replace('+', 'Z')}"
    run_root = output_root / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    registry_path = output_root / "state" / "experiment_registry.json"
    registry = load_registry(registry_path)
    extra_context = read_extra_context(args.extra_context)
    summary: dict[str, Any] = {
        "run_id": run_id,
        "experiment_id": plan.experiment_id,
        "plan_path": str(plan.path),
        "repo_path": str(repo),
        "output_root": str(output_root),
        "run_command": run_command,
        "environment": env_files,
        "iterations": [],
        "status": "dry_run" if args.dry_run else "running",
    }
    atomic_write_json(run_root / "run_summary.json", summary)
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if lock.get("ready") else 2

    stop_on_success = not args.no_stop_on_success
    final_rc = 1
    max_iterations = max(1, args.max_iterations)
    for iteration in range(1, max_iterations + 1):
        artifact_dir = run_root / f"iteration_{iteration:02d}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        prompt = build_claude_prompt(plan, repo=repo, artifact_dir=artifact_dir, output_root=output_root, lock=lock, iteration=iteration, max_iterations=max_iterations, prior_records=registry, run_command=run_command, extra_context=extra_context)
        claude_result = run_claude(prompt, repo=repo, artifact_dir=artifact_dir, lock=lock, args=args, iteration=iteration)
        validation_result = {"return_code": 0, "status": "skipped", "log_path": ""}
        if not args.skip_validation_command:
            validation_result = run_validation(run_command, repo=repo, artifact_dir=artifact_dir, lock=lock, timeout_sec=args.command_timeout_sec)
        log_paths = [Path(p) for p in [claude_result.get("log_path"), validation_result.get("log_path")] if p]
        metrics = collect_metrics(artifact_dir, log_paths)
        metric_name, metric_value = primary_metric(metrics, plan.metric)
        command_rc = int(validation_result.get("return_code") or 0)
        claude_rc = int(claude_result.get("return_code") or 0)
        success = claude_rc == 0 and command_rc == 0
        status = "success" if success else "failed"
        record = {
            "timestamp": now_iso(),
            "run_id": run_id,
            "experiment_id": plan.experiment_id,
            "iteration": iteration,
            "status": status,
            "method": plan.method,
            "dataset": plan.dataset,
            "env_name": conda_env,
            "repo_path": str(repo),
            "artifact_path": str(artifact_dir),
            "command": run_command,
            "return_code": command_rc,
            "claude_return_code": claude_rc,
            "metrics": metrics,
            "metric_name": metric_name,
            "metric_value": metric_value,
            "claude_status": claude_result.get("status", ""),
            "claude_summary": summarize_claude_log(str(claude_result.get("log_path") or "")),
            "claude_log_path": claude_result.get("log_path", ""),
            "claude_prompt_path": claude_result.get("prompt_path", ""),
            "validation_log_path": validation_result.get("log_path", ""),
            "environment_lock_path": env_files.get("environment_lock_path", ""),
            "next_action": "停止并分析证据" if success else "把本轮日志和失败原因交给下一轮 Claude 修复",
        }
        registry = upsert_record(registry_path, record)
        tables = write_record_tables(output_root, registry)
        iteration_payload = {"record": record, "tables": tables, "claude_result": claude_result, "validation_result": validation_result}
        atomic_write_json(artifact_dir / "wrapper_iteration_result.json", iteration_payload)
        summary["iterations"].append(iteration_payload)
        summary["status"] = status
        summary["record_tables"] = tables
        atomic_write_json(run_root / "run_summary.json", summary)
        final_rc = 0 if success else 1
        if success and stop_on_success:
            break
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return final_rc


if __name__ == "__main__":
    raise SystemExit(main())
