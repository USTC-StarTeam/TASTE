from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

STAGE_NAME = "experimenting"
DISPLAY_NAME = "Experimenting"
RESPONSIBILITY = (
    "Own one Experimenting controller Claude Code session per project, serialize its work and Web instructions, "
    "and provide deterministic experiment launch, watchdog, and audit tools through one public entrypoint."
)
REQUIRED_EXTERNAL_INPUTS = ("project",)
ARTIFACTS_IN = ("project selected Idea/Plan contract", "Environment handoff", "project-local Find/Read evidence")
ARTIFACTS_OUT = (
    ".runtime/controller_sessions.json and .runtime/controllers/<project>/controller.json",
    ".runtime/controllers/<project>/controller_request.json and controller_result.json",
    "projects/<project>/state/experimenting_controller_last_result.json",
    "projects/<project>/state/experiment_registry.json and experiments records",
    ".runtime/runs/<run_id>/audit_adjudication.json",
)
PRIVATE_BACKEND_ROOTS = (
    "modules/experimenting/scripts/orchestration/controller_session.py",
    "modules/experimenting/scripts/common/runtime_environment.py",
    "modules/experimenting/scripts/execution/launch_experiment_run.py",
    "modules/experimenting/scripts/execution/experiment_run_watchdog.py",
    "modules/experimenting/scripts/audits/run_claude_audit.py",
)

ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = Path(__file__).resolve().parent
SCRIPTS = MODULE_DIR / "scripts"
RUNTIME_DIR = MODULE_DIR / ".runtime"
RUNS_DIR = RUNTIME_DIR / "runs"
LATEST_RUN_DIR = RUNTIME_DIR / "latest_run"
LATEST_RUN_LOCK = RUNTIME_DIR / ".latest_run.lock"
ENTRYPOINT_ENV = "EXPERIMENTING_PUBLIC_ENTRYPOINT_ACTIVE"
RUN_DIR_ENV = "EXPERIMENTING_RUN_DIR"
RUN_ID_ENV = "EXPERIMENTING_RUN_ID"
RUNTIME_DIR_ENV = "EXPERIMENTING_RUNTIME_DIR"


def contract() -> dict[str, Any]:
    return {
        "stage": STAGE_NAME,
        "display_name": DISPLAY_NAME,
        "responsibility": RESPONSIBILITY,
        "backend_only": True,
        "frontend_dependency": False,
        "required_external_inputs": list(REQUIRED_EXTERNAL_INPUTS),
        "artifacts_in": list(ARTIFACTS_IN),
        "artifacts_out": list(ARTIFACTS_OUT),
        "private_backend_roots": list(PRIVATE_BACKEND_ROOTS),
        "entrypoint": f"modules/{STAGE_NAME}/main.py",
        "runtime_root": f"modules/{STAGE_NAME}/.runtime",
        "run_directory_rule": "Controller work/chat/status use the fixed project controller directory and create no per-message run. Deterministic tool actions use one fixed .runtime/runs/<timestamp_action_pid> directory; latest_run is human-only.",
        "framework_web_io": {
            "input": "Web sends Experimenting work/chat requests to Framework. Framework invokes run_module.py experimenting with only the explicit project and request controls.",
            "output": "Experimenting owns the project-to-session map, queue, interruption, controller receipts, and project experiment artifacts.",
            "project_sync": "The controller works with projects/<project> as cwd and writes scientific state there. Controller actions create no run; latest_run is only a human review copy for deterministic tool actions.",
            "web_chat": "Experimenting work and Web chat use the same project-unique Experimenting controller session. Busy messages queue in the module; an interrupting Web message runs first and interrupted Experimenting work is resumed afterward.",
        },
        "scripts_are_private_backend": True,
        "public_actions": sorted(USER_ACTIONS),
    }


def _contract_payload() -> dict[str, Any]:
    return contract()


def _normalize_action(action: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", str(action or "").strip().replace("-", "_")).strip("_")


def _script_dirs() -> list[Path]:
    dirs = [SCRIPTS]
    dirs.extend(path for path in sorted(SCRIPTS.iterdir()) if path.is_dir())
    return dirs


def _python_env(run_dir: Path | None = None, action: str = "") -> dict[str, str]:
    env = os.environ.copy()
    entries = [
        str(MODULE_DIR),
        str(SCRIPTS),
        *[str(path) for path in _script_dirs()],
        str(ROOT / "framework"),
        str(ROOT / "framework" / "scripts"),
        str(ROOT),
    ]
    existing = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*entries, *existing]:
        if item and item not in seen:
            seen.add(item)
            merged.append(item)
    env["PYTHONPATH"] = os.pathsep.join(merged)
    env["WORKSPACE_ROOT"] = str(ROOT)
    env[ENTRYPOINT_ENV] = "1"
    env[RUNTIME_DIR_ENV] = str(RUNTIME_DIR)
    if action:
        env["EXPERIMENTING_ACTION"] = action
    if run_dir is not None:
        env[RUN_DIR_ENV] = str(run_dir)
        env[RUN_ID_ENV] = run_dir.name
    return env


def _ensure_runtime_imports() -> None:
    for entry in reversed(_python_env()["PYTHONPATH"].split(os.pathsep)):
        if not entry:
            continue
        while entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)


def _load_private_script_module(relative_path: str, module_name: str) -> Any:
    _ensure_runtime_imports()
    module_path = (SCRIPTS / relative_path).resolve(strict=False)
    try:
        module_path.relative_to(SCRIPTS.resolve(strict=False))
    except ValueError as exc:
        raise ModuleNotFoundError(f"Experimenting private module path escapes scripts root: {relative_path}") from exc
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"Cannot load Experimenting private module: {module_path}")
    old_flag = os.environ.get(ENTRYPOINT_ENV)
    os.environ[ENTRYPOINT_ENV] = "1"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if old_flag is None:
            os.environ.pop(ENTRYPOINT_ENV, None)
        else:
            os.environ[ENTRYPOINT_ENV] = old_flag
    return module


PUBLIC_ACTION_SCRIPTS = {
    "work": "orchestration/controller_session.py",
    "chat": "orchestration/controller_session.py",
    "controller_status": "orchestration/controller_session.py",
    "runtime_env": "common/runtime_environment.py",
    "launch": "execution/launch_experiment_run.py",
    "watchdog": "execution/experiment_run_watchdog.py",
    "audit_iteration": "audits/run_claude_audit.py",
    "runtime_integrity": "audits/run_claude_audit.py",
    "reference_reproduction": "audits/run_claude_audit.py",
    "audit_adjudication": "audits/run_claude_audit.py",
}

ACTION_DEFAULT_ARGS = {
    "work": ["--kind", "work"],
    "chat": ["--kind", "chat"],
    "controller_status": ["--kind", "status"],
    "audit_iteration": ["--audit-kind", "experiment_iteration"],
    "runtime_integrity": ["--audit-kind", "runtime_integrity"],
    "reference_reproduction": ["--audit-kind", "reference_reproduction"],
}

USER_ACTIONS = {*PUBLIC_ACTION_SCRIPTS, "manifest"}


OUTPUT_ROOT_ACTIONS = {
    "runtime_env",
    "audit_iteration",
    "runtime_integrity",
    "reference_reproduction",
    "audit_adjudication",
}

CONTROLLER_ACTIONS = {"work", "chat", "controller_status"}


def _ensure_taste_management_env() -> None:
    env_name = os.environ.get("CONDA_DEFAULT_ENV", "")
    prefix_name = Path(sys.prefix).name
    if env_name == "taste" or prefix_name == "taste":
        return
    raise SystemExit(
        "Experimenting must be run with the conda management environment named 'taste'. "
        "Use: conda run -n taste python modules/experimenting/main.py --action <action> ..."
    )


def _run_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _create_run_dir(action: str, original_args: Sequence[str]) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    action_slug = _normalize_action(action) or "work"
    for index in range(100):
        suffix = f"_{index:02d}" if index else ""
        run_id = f"{_run_timestamp()}_{action_slug}_pid{os.getpid()}{suffix}"
        run_dir = RUNS_DIR / run_id
        try:
            run_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            continue
        meta = {
            "run_id": run_id,
            "action": action_slug,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "pid": os.getpid(),
            "cwd": str(Path.cwd()),
            "module_dir": str(MODULE_DIR),
            "argv": list(original_args),
            "status": "running",
            "latest_run_policy": "latest_run is a human review copy only; programs must use this run_dir.",
        }
        _write_json(run_dir / "run_meta.json", meta)
        _snapshot_input_files(run_dir, original_args)
        return run_dir
    raise SystemExit("Could not create a unique Experimenting run directory.")


def _arg_value(args: Sequence[str], option: str) -> str:
    for index, item in enumerate(args):
        if item == option and index + 1 < len(args):
            return str(args[index + 1])
        if item.startswith(option + "="):
            return item.split("=", 1)[1]
    return ""


def _snapshot_input_files(run_dir: Path, original_args: Sequence[str]) -> None:
    snapshots: dict[str, str] = {}
    for option, label in [("--plan", "plan")]:
        value = _arg_value(original_args, option)
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.is_file():
            continue
        target = run_dir / "input" / f"{label}{path.suffix or '.txt'}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        snapshots[label] = str(target.relative_to(run_dir))
    if snapshots:
        _update_run_meta(run_dir, input_snapshots=snapshots)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _update_run_meta(target_run_dir: Path, **updates: Any) -> None:
    path = target_run_dir / "run_meta.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        payload = {}
    payload.update(updates)
    _write_json(path, payload)


def _copy_latest_run(run_dir: Path) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with LATEST_RUN_LOCK.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        tmp = RUNTIME_DIR / f".latest_run.tmp.{os.getpid()}.{_run_timestamp()}"
        if tmp.exists():
            shutil.rmtree(tmp)
        try:
            shutil.copytree(run_dir, tmp, symlinks=True)
            if LATEST_RUN_DIR.exists():
                shutil.rmtree(LATEST_RUN_DIR)
            tmp.rename(LATEST_RUN_DIR)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _resolve_script(action: str) -> Path:
    key = _normalize_action(action)
    mapped = PUBLIC_ACTION_SCRIPTS.get(key)
    if not mapped:
        allowed = ", ".join(sorted(k or "work" for k in USER_ACTIONS))
        raise SystemExit(f"Unknown {STAGE_NAME} action: {action}. Public actions: {allowed}")
    script = (SCRIPTS / mapped).resolve(strict=False)
    try:
        script.relative_to(SCRIPTS.resolve(strict=False))
    except ValueError as exc:
        raise SystemExit(f"Invalid private script mapping for action {action}: {mapped}") from exc
    if not script.exists():
        raise SystemExit(f"Experimenting action script is missing: {script}")
    return script


def _replace_option(args: Sequence[str], option: str, value: str) -> list[str]:
    out: list[str] = []
    replaced = False
    skip_next = False
    for index, item in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if item == option:
            out.extend([option, value])
            replaced = True
            skip_next = index + 1 < len(args)
            continue
        if item.startswith(option + "="):
            out.append(f"{option}={value}")
            replaced = True
            continue
        out.append(item)
    if not replaced:
        out.extend([option, value])
    return out


def _normalize_child_args(action: str, args: Sequence[str], run_dir: Path | None) -> list[str]:
    key = _normalize_action(action)
    normalized = list(args)
    defaults = ACTION_DEFAULT_ARGS.get(key, [])
    for index in range(0, len(defaults), 2):
        option = defaults[index]
        value = defaults[index + 1]
        if not _arg_value(normalized, option):
            normalized.extend([option, value])
    if key in OUTPUT_ROOT_ACTIONS and run_dir is not None:
        normalized = _replace_option(normalized, "--output-root", str(run_dir))
    return normalized


def _run_script(action: str, args: Sequence[str], run_dir: Path | None = None) -> int:
    script = _resolve_script(action)
    child_args = _normalize_child_args(action, args, run_dir)
    if run_dir is not None:
        _update_run_meta(run_dir, script=str(script), child_args=child_args)
    proc = subprocess.run([sys.executable, str(script), *child_args], cwd=ROOT, env=_python_env(run_dir, action), text=True)
    return int(proc.returncode)


def _manifest_payload() -> dict[str, Any]:
    scripts = sorted(str(path.relative_to(MODULE_DIR)) for path in SCRIPTS.glob("**/*.py") if path.name != "__init__.py")
    skills = sorted(str(path.relative_to(MODULE_DIR)) for path in (MODULE_DIR / "skills").glob("*/SKILL.md")) if (MODULE_DIR / "skills").exists() else []
    prompts = sorted(str(path.relative_to(MODULE_DIR)) for path in (MODULE_DIR / "prompts").glob("*.md")) if (MODULE_DIR / "prompts").exists() else []
    docs = [name for name in ["README.md", "SCRIPT_AUDIT.md"] if (MODULE_DIR / name).exists()]
    return {
        "module": STAGE_NAME,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "entrypoint": f"modules/{STAGE_NAME}/main.py",
        "public_actions": sorted(USER_ACTIONS),
        "private_script_count": len(scripts),
        "private_scripts": scripts,
        "skill_count": len(skills),
        "skills": skills,
        "prompt_count": len(prompts),
        "prompts": prompts,
        "docs": docs,
        "runtime_root": f"modules/{STAGE_NAME}/.runtime",
    }


def main(argv: Sequence[str] | None = None) -> int:
    original_args = list(argv if argv is not None else sys.argv[1:])
    parser = argparse.ArgumentParser(description="Experimenting module public backend entrypoint.", add_help=True)
    parser.add_argument("--action", default="work", help="Backend action. Default: continue the project Experimenting controller.")
    parser.add_argument("--contract", action="store_true")
    ns, rest = parser.parse_known_args(argv)

    if ns.contract:
        _ensure_taste_management_env()
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    if _normalize_action(ns.action) == "manifest":
        _ensure_taste_management_env()
        print(json.dumps(_manifest_payload(), ensure_ascii=False, indent=2))
        return 0

    _ensure_taste_management_env()
    action = _normalize_action(ns.action) or "work"
    _resolve_script(action)
    if action in CONTROLLER_ACTIONS:
        return _run_script(action, rest)
    run_dir = _create_run_dir(action, original_args)
    try:
        rc = _run_script(action, rest, run_dir)
        _update_run_meta(
            run_dir,
            status="completed" if rc == 0 else "failed",
            return_code=rc,
            finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            run_dir=str(run_dir),
        )
        return rc
    except BaseException as exc:
        _update_run_meta(
            run_dir,
            status="error",
            error=f"{type(exc).__name__}: {exc}",
            finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            run_dir=str(run_dir),
        )
        raise
    finally:
        try:
            _copy_latest_run(run_dir)
        except Exception as exc:
            print(f"warning: failed to refresh experimenting latest_run: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
