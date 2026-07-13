from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from scripts.common.claude_runner import interrupt_project_controller, run_controller_message
from scripts.common.shell import isolated_runtime_env

STAGE_NAME = "environment"
DISPLAY_NAME = "Environment / 自主环境部署后端"
RESPONSIBILITY = (
    "给定实验 plan 后，在隔离工作区内让 Claude Code 自主选择/拉取 GitHub 仓库，"
    "依据 README、论文配置和本机画像生成 Conda 部署方案，执行验证与参考复现，"
    "最后输出批准、拒绝或继续修复裁决。"
)
REQUIRED_EXTERNAL_INPUTS = ("project", "experiment_plan_json", "local_runtime", "optional_paper_or_repo_hints")
ARTIFACTS_IN = ("experiment_plan.json", "paper/repo hints")
ARTIFACTS_OUT = (
    ".runtime/runs/<timestamp_action_pid>/environment_deployment_decision.json",
    ".runtime/runs/<timestamp_action_pid>/environment_chat_result.json",
    ".runtime/runs/<timestamp_action_pid>/claude_environment_plan_round_*.json",
    ".runtime/runs/<timestamp_action_pid>/round_*/command_receipts.json",
    ".runtime/runs/<timestamp_action_pid>/run_meta.json",
    ".runtime/latest_run/",
)
PRIVATE_BACKEND_ROOTS = (
    "modules/environment/scripts/orchestration/autonomous_deploy.py",
    "modules/environment/scripts/common/",
    "modules/environment/scripts/repository/",
    "modules/environment/scripts/environment/",
    "modules/environment/scripts/reproduction/",
)

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = Path(__file__).resolve().parent
SCRIPTS = MODULE_ROOT / "scripts"
RUNTIME_ROOT = MODULE_ROOT / ".runtime"
RUNTIME_RUNS_ROOT = RUNTIME_ROOT / "runs"
LATEST_RUN_REVIEW_DIR = RUNTIME_ROOT / "latest_run"
PUBLIC_ENTRYPOINT_ENV = "ENVIRONMENT_PUBLIC_ENTRYPOINT_ACTIVE"
COMPLETED_RUNS_TO_KEEP_PER_PROJECT_ACTION = 5


def contract() -> dict[str, Any]:
    return {
        "stage": STAGE_NAME,
        "display_name": DISPLAY_NAME,
        "responsibility": RESPONSIBILITY,
        "backend_only": True,
        "frontend_dependency": False,
        "required_conda_env": "taste",
        "runtime_root": "modules/environment/.runtime",
        "run_directory_policy": ".runtime/runs/<YYYYMMDDTHHMMSSffffffZ_action_pidPID>; latest_run is a human-review link only",
        "required_external_inputs": list(REQUIRED_EXTERNAL_INPUTS),
        "artifacts_in": list(ARTIFACTS_IN),
        "artifacts_out": list(ARTIFACTS_OUT),
        "actions": {
            "deploy_from_plan": "部署当前唯一实验计划，输出 environment_deployment_decision.json。",
            "chat": "向该项目唯一的 Environment 主控 Claude 会话发送或排队一条指令，输出 environment_chat_result.json。",
            "status": "读取指定或最新 Environment run 的部署裁决。",
        },
        "private_backend_roots": list(PRIVATE_BACKEND_ROOTS),
        "decision_schema": {
            "approve": "复现证据达到论文声明，allow_next_module=true，退出码 0。",
            "reject": "仓库/数据/论文不可靠，或算力不可满足且无合理降级路径，并有不可修证据，allow_next_module=false，退出码 20。",
            "continue_repair": "尚未复现且未能证明不可修，Claude Code 应继续修复，allow_next_module=false，退出码 30。",
        },
    }


def _contract_payload() -> dict[str, Any]:
    payload = contract()
    payload["entrypoint"] = "modules/environment/main.py"
    payload["scripts_are_private_backend"] = True
    return payload


def _normalize_action(action: str) -> str:
    return str(action or "").strip().replace("-", "_")


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    entries: list[str] = [str(MODULE_ROOT)]
    modules_root = (ROOT / "modules").resolve()
    module_root_resolved = MODULE_ROOT.resolve()
    existing: list[str] = []
    for part in env.get("PYTHONPATH", "").split(os.pathsep):
        if not part:
            continue
        try:
            resolved = Path(part).expanduser().resolve(strict=False)
            if resolved != module_root_resolved:
                try:
                    resolved.relative_to(modules_root)
                    continue
                except ValueError:
                    pass
        except Exception:
            pass
        existing.append(part)
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*entries, *existing]:
        if item and item not in seen:
            seen.add(item)
            merged.append(item)
    env["PYTHONPATH"] = os.pathsep.join(merged)
    env["WORKSPACE_ROOT"] = str(ROOT)
    env["ENVIRONMENT_ROOT"] = str(MODULE_ROOT)
    env[PUBLIC_ENTRYPOINT_ENV] = "1"
    return env


ACTION_TO_SCRIPT = {
    "": "orchestration/autonomous_deploy.py",
    "deploy_from_plan": "orchestration/autonomous_deploy.py",
}

def _running_in_taste_conda() -> bool:
    if os.environ.get("CONDA_DEFAULT_ENV") == "taste":
        return True
    executable = Path(sys.executable).expanduser().resolve()
    return executable.parent.name == "bin" and executable.parent.parent.name == "taste" and executable.parent.parent.parent.name == "envs"


def _require_taste_conda() -> None:
    if _running_in_taste_conda():
        return
    raise SystemExit(
        "Environment must run inside the conda environment named 'taste'. "
        "Use: conda run -n taste python modules/environment/main.py ..."
    )


def _safe_slug(value: str, default: str = "run") -> str:
    text = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(value or "").strip()).strip("-").lower()
    return (text or default)[:80]


def _precise_runtime_id(action: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}_{_safe_slug(action, 'deploy')}_pid{os.getpid()}"


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _update_run_meta(run_dir: Path, **updates: Any) -> None:
    meta_path = run_dir / "run_meta.json"
    meta = _read_json(meta_path, {})
    if not isinstance(meta, dict):
        meta = {}
    meta.update(updates)
    _write_json(meta_path, meta)


def _take_arg(args: Sequence[str], name: str) -> tuple[list[str], str]:
    out: list[str] = []
    value = ""
    skip = False
    for item in args:
        if skip:
            value = str(item)
            skip = False
            continue
        text = str(item)
        if text == name:
            skip = True
            continue
        prefix = name + "="
        if text.startswith(prefix):
            value = text[len(prefix):]
            continue
        out.append(text)
    return out, value


def _arg_value(args: Sequence[str], name: str) -> str:
    for index, value in enumerate(args):
        text = str(value)
        if text == name and index + 1 < len(args):
            return str(args[index + 1])
        prefix = name + "="
        if text.startswith(prefix):
            return text[len(prefix):]
    return ""


def _new_runtime_dir(action: str) -> Path:
    RUNTIME_RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    for _ in range(20):
        run_dir = RUNTIME_RUNS_ROOT / _precise_runtime_id(action)
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            return run_dir
        except FileExistsError:
            continue
    raise RuntimeError(f"failed to create a unique Environment runtime directory for {action}")


def _start_runtime_run(action: str, args: Sequence[str], *, consume_project: bool = True) -> tuple[Path, list[str]]:
    _require_taste_conda()
    cleaned, requested_run_id = _take_arg(args, "--run-id")
    requested_project = ""
    if consume_project:
        cleaned, requested_project = _take_arg(cleaned, "--project")
    else:
        requested_project = _arg_value(cleaned, "--project")
    cleaned, requested_venue = _take_arg(cleaned, "--venue")
    cleaned, requested_work_root = _take_arg(cleaned, "--work-root")
    if requested_work_root:
        raise SystemExit("Environment no longer accepts --work-root; all outputs stay under modules/environment/.runtime/runs.")
    run_dir = _new_runtime_dir(action)
    _update_run_meta(
        run_dir,
        environment_run_id=run_dir.name,
        requested_run_id=str(requested_run_id or ""),
        requested_project=str(requested_project or ""),
        requested_venue=str(requested_venue or ""),
        action=action,
        status="running",
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        pid=os.getpid(),
        workspace_root=str(ROOT),
        module_root=str(MODULE_ROOT),
        python=sys.executable,
        conda_env=os.environ.get("CONDA_DEFAULT_ENV", ""),
    )
    return run_dir, [*cleaned, "--run-dir", str(run_dir)]


def _refresh_latest_run_review_link(run_dir: Path) -> Path | None:
    if not run_dir.exists():
        return None
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    temp = RUNTIME_ROOT / f".latest_run.tmp.{os.getpid()}"
    try:
        if temp.exists() or temp.is_symlink():
            temp.unlink()
        temp.symlink_to(run_dir, target_is_directory=True)
        if LATEST_RUN_REVIEW_DIR.is_symlink() or LATEST_RUN_REVIEW_DIR.is_file():
            LATEST_RUN_REVIEW_DIR.unlink()
        elif LATEST_RUN_REVIEW_DIR.exists():
            shutil.rmtree(LATEST_RUN_REVIEW_DIR)
        os.replace(temp, LATEST_RUN_REVIEW_DIR)
        return LATEST_RUN_REVIEW_DIR
    except OSError:
        try:
            if temp.exists() or temp.is_symlink():
                temp.unlink()
        except OSError:
            pass
        return None


def _complete_runtime_run(run_dir: Path, return_code: int) -> None:
    decision = _read_json(run_dir / "environment_deployment_decision.json", {})
    has_decision = isinstance(decision, dict) and bool(decision)
    _update_run_meta(
        run_dir,
        status="complete" if has_decision else ("complete" if return_code == 0 else "failed"),
        return_code=return_code,
        completed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        decision=str(decision.get("decision") or "") if isinstance(decision, dict) else "",
    )
    review_dir = _refresh_latest_run_review_link(run_dir)
    _update_run_meta(run_dir, latest_run_review_dir=str(review_dir or ""))
    if review_dir:
        _update_run_meta(review_dir, latest_run_review_dir=str(review_dir))
    _prune_completed_runs()


def _protected_run_ids() -> set[str]:
    protected: set[str] = set()
    if LATEST_RUN_REVIEW_DIR.is_symlink():
        try:
            protected.add(LATEST_RUN_REVIEW_DIR.resolve().name)
        except OSError:
            pass
    projects_root = ROOT / "projects"
    for name in ("environment_handoff.json", "environment_latest_run.json"):
        for path in projects_root.glob(f"*/state/{name}") if projects_root.exists() else []:
            payload = _read_json(path, {})
            if not isinstance(payload, dict):
                continue
            run_id = str(payload.get("environment_run_id") or "").strip()
            if run_id:
                protected.add(run_id)
            run_dir = str(payload.get("module_run_dir") or "").strip()
            if run_dir:
                protected.add(Path(run_dir).name)
    return protected


def _prune_completed_runs() -> None:
    if not RUNTIME_RUNS_ROOT.exists():
        return
    protected = _protected_run_ids()
    grouped: dict[tuple[str, str], list[Path]] = {}
    for run_dir in RUNTIME_RUNS_ROOT.iterdir():
        if not run_dir.is_dir():
            continue
        meta = _read_json(run_dir / "run_meta.json", {})
        if not isinstance(meta, dict) or str(meta.get("status") or "") not in {"complete", "failed"}:
            continue
        key = (str(meta.get("requested_project") or "_unscoped"), str(meta.get("action") or "unknown"))
        grouped.setdefault(key, []).append(run_dir)
    for (project, _action), run_dirs in grouped.items():
        ordered = sorted(run_dirs, key=lambda path: path.stat().st_mtime_ns, reverse=True)
        keep_count = 0 if project == "_unscoped" else COMPLETED_RUNS_TO_KEEP_PER_PROJECT_ACTION
        for run_dir in ordered[keep_count:]:
            if run_dir.name not in protected:
                shutil.rmtree(run_dir, ignore_errors=True)


def _run_script(relative_script: str, args: Sequence[str], *, run_dir: Path | None = None) -> int:
    script = SCRIPTS / relative_script
    if not script.exists():
        raise SystemExit(f"未知 environment 模块动作脚本：{relative_script}")
    env = _python_env()
    if run_dir is not None:
        env["ENVIRONMENT_RUN_DIR"] = str(run_dir)
    proc = subprocess.run([sys.executable, str(script), *args], cwd=ROOT, env=env, text=True)
    return int(proc.returncode)


def _read_text_file(path_text: str) -> str:
    if not str(path_text or "").strip():
        return ""
    path = Path(path_text).expanduser()
    return path.read_text(encoding="utf-8")


def _project_root(project: str) -> Path:
    name = str(project or "").strip()
    if not name or not re.match(r"^[A-Za-z0-9_.-]+$", name):
        raise SystemExit("Environment chat requires a valid --project name.")
    root = (ROOT / "projects" / name).resolve()
    try:
        root.relative_to((ROOT / "projects").resolve())
    except ValueError as exc:
        raise SystemExit(f"Environment chat project escapes projects/: {project}") from exc
    if not root.is_dir():
        raise SystemExit(f"Environment chat project does not exist: {project}")
    return root


@contextmanager
def _interrupt_controller_on_exit(project: str):
    if not project:
        yield
        return
    project_root = _project_root(project)
    previous_handlers: dict[int, Any] = {}

    def stop_controller(signum, _frame) -> None:
        interrupt_project_controller(project, project_root)
        raise SystemExit(128 + int(signum))

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, stop_controller)
    try:
        yield
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _run_chat(args: Sequence[str], *, run_dir: Path) -> int:
    parser = argparse.ArgumentParser(description="向项目唯一的 Environment 主控 Claude Code 发送一次指令。")
    parser.add_argument("--project", required=True)
    parser.add_argument("--message", default="")
    parser.add_argument("--message-file", default="")
    parser.add_argument("--interrupt-current", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-dir", default=argparse.SUPPRESS)
    ns = parser.parse_args(list(args))
    project_root = _project_root(ns.project)
    message = (str(ns.message or "").strip() or _read_text_file(ns.message_file).strip())
    if not message:
        raise SystemExit("Environment chat requires --message or --message-file.")
    message_path = run_dir / "environment_chat_message.md"
    message_path.write_text(message + "\n", encoding="utf-8")
    result_path = run_dir / "environment_chat_result.json"
    if ns.dry_run:
        payload = {
            "schema_version": "environment.chat_result.v1",
            "status": "dry_run",
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "project": ns.project,
            "stage": "environment",
            "message_path": str(message_path),
            "interrupt_current": bool(ns.interrupt_current),
        }
        _write_json(result_path, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    def announce_queued(event: dict[str, Any]) -> None:
        _write_json(result_path, {
            "schema_version": "environment.chat_result.v1",
            "status": "queued",
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "project": ns.project,
            "message_path": str(message_path),
            **event,
        })
        print(json.dumps(event, ensure_ascii=False), flush=True)

    controller_result = run_controller_message(
        project=ns.project,
        project_root=project_root,
        message=message,
        interrupt_current=bool(ns.interrupt_current),
        env=isolated_runtime_env(run_dir, isolate_home=False),
        on_queued=announce_queued,
    )
    code = int(controller_result.get("return_code") or 0)
    payload = {
        "schema_version": "environment.chat_result.v1",
        "status": str(controller_result.get("status") or ("completed" if code == 0 else "failed")),
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "project": ns.project,
        "stage": "environment",
        "return_code": code,
        "message_path": str(message_path),
        "message_id": str(controller_result.get("message_id") or ""),
        "queued": bool(controller_result.get("queued")),
        "interrupted_current": bool(controller_result.get("interrupted_current")),
        "controller_result": controller_result,
    }
    _write_json(result_path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def _newest_decision_path(project: str) -> Path | None:
    if not RUNTIME_RUNS_ROOT.exists():
        return None
    candidates = []
    for path in RUNTIME_RUNS_ROOT.glob("*/environment_deployment_decision.json"):
        meta = _read_json(path.parent / "run_meta.json", {})
        if path.exists() and isinstance(meta, dict) and str(meta.get("requested_project") or "") == project:
            candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _print_status(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="查看 environment 最近一次自主部署状态。")
    parser.add_argument("--project", required=True, help="只查看该项目的 Environment run。")
    parser.add_argument("--run-id", default="", help="指定 run_id；不指定则读取 .runtime/runs 中最新完成的 run。")
    ns = parser.parse_args(list(args))
    _project_root(ns.project)
    _prune_completed_runs()
    decision_path = RUNTIME_RUNS_ROOT / ns.run_id / "environment_deployment_decision.json" if ns.run_id else _newest_decision_path(ns.project)
    run_meta = _read_json(decision_path.parent / "run_meta.json", {}) if isinstance(decision_path, Path) else {}
    if isinstance(run_meta, dict) and run_meta and str(run_meta.get("requested_project") or "") != ns.project:
        payload = {}
        read_status = "project_mismatch"
    else:
        payload = _read_json(decision_path, {}) if isinstance(decision_path, Path) else {}
        read_status = "ok" if payload else "missing"
    handoff = payload.get("environment_handoff") if isinstance(payload, dict) and isinstance(payload.get("environment_handoff"), dict) else {}
    gate = handoff.get("handoff_gate") if isinstance(handoff.get("handoff_gate"), dict) else {}
    ready = bool(handoff.get("ready_for_experimenting") is True and gate.get("passed") is True)
    status = "ready_for_experimenting" if ready else "not_ready_for_experimenting" if payload else read_status
    print(
        json.dumps(
            {
                "status": status,
                "read_status": read_status,
                "project": ns.project,
                "environment_run_id": decision_path.parent.name if isinstance(decision_path, Path) else "",
                "decision_path": str(decision_path or ""),
                "decision": payload,
                "runtime_runs_root": str(RUNTIME_RUNS_ROOT),
                "latest_run_review_dir": str(LATEST_RUN_REVIEW_DIR),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if read_status == "ok" else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Environment module public backend entrypoint.")
    parser.add_argument("--action", default="deploy_from_plan", help="后端动作，默认 deploy_from_plan。")
    parser.add_argument("--contract", action="store_true", help="输出模块契约。")
    ns, rest = parser.parse_known_args(argv)
    _require_taste_conda()
    if ns.contract:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    action = _normalize_action(ns.action)
    if action == "status":
        return _print_status(rest)
    if action == "chat":
        run_dir, runtime_args = _start_runtime_run(action, rest, consume_project=False)
        try:
            with _interrupt_controller_on_exit(_arg_value(runtime_args, "--project")):
                code = _run_chat(runtime_args, run_dir=run_dir)
        finally:
            _complete_runtime_run(run_dir, int(locals().get("code", 1)))
        return int(code)
    script = ACTION_TO_SCRIPT.get(action)
    if not script:
        raise SystemExit(f"未知 environment 模块动作：{action}")
    run_dir, runtime_args = _start_runtime_run(action or "deploy_from_plan", rest, consume_project=False)
    try:
        with _interrupt_controller_on_exit(_arg_value(runtime_args, "--project")):
            code = _run_script(script, runtime_args, run_dir=run_dir)
    finally:
        _complete_runtime_run(run_dir, int(locals().get("code", 130)))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
