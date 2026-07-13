#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from project_paths import build_paths, load_project_config, project_experiment_python_from_config


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def proc_link(pid: int, name: str) -> str:
    try:
        return str(Path(f"/proc/{pid}/{name}").resolve())
    except Exception:
        return ""


def fd_target(pid: int, fd: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/fd/{fd}")
    except Exception:
        return ""


def is_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except Exception:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def proc_ppid(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except Exception:
        pass
    return 0


def proc_resource_usage(pid: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etimes=,pcpu=,pmem="],
            text=True,
            capture_output=True,
            timeout=3,
        )
    except Exception:
        return {"elapsed_sec": 0, "pcpu": "", "pmem": ""}
    if proc.returncode != 0:
        return {"elapsed_sec": 0, "pcpu": "", "pmem": ""}
    parts = proc.stdout.strip().split(None, 2)
    try:
        elapsed = int(parts[0]) if parts else 0
    except ValueError:
        elapsed = 0
    return {
        "elapsed_sec": elapsed,
        "pcpu": parts[1] if len(parts) > 1 else "",
        "pmem": parts[2] if len(parts) > 2 else "",
    }


def is_python_worker(pid: int, cmd: str) -> bool:
    exe = Path(proc_link(pid, "exe")).name.lower()
    if exe.startswith("python"):
        return True
    text = cmd.strip().lower()
    return text.startswith("python") or "/python" in text.split(" ", 1)[0]

def project_experiment_python(project: str) -> str:
    cfg = load_project_config(project)
    return project_experiment_python_from_config(cfg if isinstance(cfg, dict) else {})


def allowed_python_executables(project: str) -> set[str]:
    expected = project_experiment_python(project)
    if not expected:
        return set()
    base = Path(expected).resolve()
    out = {str(base)}
    for name in ["python3", "python3.11", "python3.10", "python3.9"]:
        candidate = base.parent / name
        if candidate.exists():
            out.add(str(candidate.resolve()))
    return out


def process_python_policy(row: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    exe = str(row.get("exe") or "").strip()
    cmd = str(row.get("cmd") or "").strip()
    first = cmd.split(None, 1)[0] if cmd else ""
    resolved = exe or first
    if resolved and Path(resolved).exists():
        resolved = str(Path(resolved).resolve())
    cmd_l = cmd.lower()
    if " conda run " in f" {cmd_l} " or cmd_l.startswith("conda run"):
        return {"status": "reject", "reason": "experiment process uses conda run wrapper", "resolved_executable": resolved}
    if row.get("is_python_worker") and allowed and resolved not in allowed:
        return {"status": "reject", "reason": "experiment process does not use project experiment Python", "resolved_executable": resolved, "allowed": sorted(allowed)}
    return {"status": "pass", "resolved_executable": resolved, "allowed": sorted(allowed)}


def contract_python_policy(contract: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    command = contract.get("command") if isinstance(contract.get("command"), list) else []
    if not command:
        return {"status": "unknown", "reason": "missing command"}
    first = str(command[0] or "")
    launcher = Path(first).name.lower()
    if launcher in {"conda", "mamba", "micromamba"} and "run" in [str(item).lower() for item in command[:4]]:
        return {"status": "reject", "reason": "contract command uses conda/mamba run"}
    if launcher.startswith("python") or first.endswith(".py"):
        resolved = str(Path(first).resolve()) if Path(first).exists() else first
        if allowed and resolved not in allowed:
            return {"status": "reject", "reason": "contract command does not use project experiment Python", "resolved_executable": resolved, "allowed": sorted(allowed)}
    recorded = str(contract.get("python_executable") or "").strip()
    if recorded and allowed and str(Path(recorded).resolve()) not in allowed:
        return {"status": "reject", "reason": "contract recorded python_executable is outside project env", "resolved_executable": recorded, "allowed": sorted(allowed)}
    return {"status": "pass", "allowed": sorted(allowed)}


def looks_like_experiment(cmd: str) -> bool:
    lowered = cmd.lower()
    if not lowered:
        return False
    skip_terms = [
        "experiment_run_watchdog.py",
        "audit_",
        "grep ",
        "rg ",
        "tail -",
        "sed -n",
        "curl ",
        "api/jobs",
    ]
    if any(term in lowered for term in skip_terms):
        return False
    if "finetune.py" in lowered or "finetune_llm" in lowered:
        return True
    if "exp_text_init" in lowered or "exp_text_init_standard_train.py" in lowered:
        return True
    if re.search(r"(?:^|\s)(?:\S*/)?main\.py\b", lowered) and re.search(r"(?:^|\s)--data(?:=|\s+)", lowered):
        return True
    if re.search(r"(?:^|\s)(?:\S*/)?(?:finetune|train)[\w.-]*\.py\b", lowered) and "python" in lowered:
        return True
    if "python" in lowered and "--artifact_dir" in lowered and "/artifacts/" in lowered:
        training_tokens = ["train_data", "evaluate(model", "optimizer", "loss.backward", "torch.save", "backward()"]
        return any(token in lowered for token in training_tokens)
    return False


def clean_path_token(raw: str) -> str:
    token = raw.strip().strip("'\"")
    token = token.rstrip(";,)")
    return token


def artifact_dirs_from_command(cmd: str, project_root: Path) -> set[Path]:
    out: set[Path] = set()
    patterns = [
        r"--artifact_dir(?:=|\s+)(['\"]?)(?P<path>/[^\s'\";]+)",
        r"ARTIFACT_DIR=(['\"])(?P<path>/.*?)(?:\1)",
        r"ARTIFACT_DIR=(?P<path>/[^\s;]+)",
        r"(?:>|>>|tee\s+-a)\s+(?P<path>/[^\s'\";]+stdout_stderr\.log)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, cmd):
            path_text = clean_path_token(match.group("path"))
            if not path_text:
                continue
            path = Path(path_text)
            if path.name == "stdout_stderr.log":
                path = path.parent
            if "artifacts" in path.parts:
                out.add(path)
    return {path for path in out if str(path).startswith(str(project_root))}


def artifact_dir_from_fd(path_text: str, project_root: Path) -> Path | None:
    if not path_text:
        return None
    # Linux marks deleted fd targets with " (deleted)"; keep the original path.
    cleaned = path_text.replace(" (deleted)", "")
    path = Path(cleaned)
    if path.name != "stdout_stderr.log":
        return None
    if "artifacts" not in path.parts:
        return None
    parent = path.parent
    return parent if str(parent).startswith(str(project_root)) else None


def command_matches_contract(cmd: str, command: Any) -> bool:
    if not isinstance(command, list) or not command:
        return False
    lowered = f" {cmd.lower()} "
    meaningful: list[str] = []
    for item in command[:4]:
        value = str(item or "").strip()
        if not value or value == "-u":
            continue
        meaningful.append(Path(value).name.lower() if value.endswith(".py") else value.lower())
    return bool(meaningful) and all(token in lowered for token in meaningful)


def contract_process_rows(project_root: Path) -> list[dict[str, Any]]:
    artifacts_root = project_root / "artifacts"
    if not artifacts_root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for contract_path in artifacts_root.glob("**/run_contract.json"):
        contract = load_json(contract_path, {})
        if not isinstance(contract, dict):
            continue
        artifact_dir = Path(str(contract.get("artifact_dir") or contract_path.parent)).resolve()
        if not str(artifact_dir).startswith(str(artifacts_root.resolve())):
            continue
        pid_raw = contract.get("pid")
        if not pid_raw:
            pid_sidecar = load_json(artifact_dir / "launcher.pid.json", {})
            pid_raw = pid_sidecar.get("pid") if isinstance(pid_sidecar, dict) else None
        try:
            pid = int(pid_raw or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or not is_alive(pid):
            continue
        cmd = proc_cmdline(pid)
        fd1 = fd_target(pid, 1)
        fd2 = fd_target(pid, 2)
        expected_stdout = str(contract.get("stdout_path") or nested_value(contract, "expected_outputs.stdout") or artifact_dir / "stdout_stderr.log")
        fd_artifacts = {path for path in (artifact_dir_from_fd(fd1, project_root), artifact_dir_from_fd(fd2, project_root)) if path is not None}
        stdout_matches = any(str(path.resolve()) == str(artifact_dir) for path in fd_artifacts)
        contract_matches = command_matches_contract(cmd, contract.get("command"))
        if not stdout_matches and not contract_matches:
            continue
        usage = proc_resource_usage(pid)
        rows.append({
            "pid": pid,
            "ppid": proc_ppid(pid),
            "elapsed_sec": usage["elapsed_sec"],
            "pcpu": usage["pcpu"],
            "pmem": usage["pmem"],
            "cwd": proc_link(pid, "cwd"),
            "exe": proc_link(pid, "exe"),
            "cmd": cmd,
            "is_python_worker": is_python_worker(pid, cmd),
            "stdout_fd": fd1,
            "stderr_fd": fd2,
            "artifact_dirs": [str(artifact_dir)],
            "contract_detected": True,
            "contract_path": str(contract_path),
            "contract_stdout_path": expected_stdout,
        })
    return rows


def log_contains_nul(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    return False
                if b"\x00" in chunk:
                    return True
    except Exception:
        return False


def mark_contaminated(artifact_dir: Path, reason: str, pids: list[int]) -> dict[str, Any]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    marker = artifact_dir / "CONTAMINATED_DO_NOT_IMPORT.txt"
    log_path = artifact_dir / "stdout_stderr.log"
    backup = ""
    if log_path.exists():
        backup_path = artifact_dir / f"stdout_stderr.log.contaminated_{ts}"
        try:
            shutil.copy2(log_path, backup_path)
            backup = str(backup_path)
        except Exception:
            backup = ""
    marker.write_text(
        "\n".join([
            f"contaminated_at_utc={ts}",
            f"reason={reason}",
            "pids=" + ",".join(str(pid) for pid in pids),
            "action=artifact must not be imported; relaunch must use a fresh unique artifact_dir",
            f"backup={backup}",
            "",
        ]),
        encoding="utf-8",
    )
    return {"artifact_dir": str(artifact_dir), "marker": str(marker), "backup": backup, "reason": reason, "pids": pids}


def stop_pids(pids: list[int]) -> dict[str, Any]:
    stopped: list[int] = []
    killed: list[int] = []
    for pid in pids:
        if not is_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    time.sleep(2)
    for pid in pids:
        if not is_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    return {"sigterm": stopped, "sigkill": killed}


def process_rows(project_root: Path) -> list[dict[str, Any]]:
    proc = subprocess.run(["ps", "-eo", "pid=,ppid=,etimes=,pcpu=,pmem=,cmd="], text=True, capture_output=True, timeout=10)
    rows: list[dict[str, Any]] = []
    if proc.returncode != 0:
        return rows
    own = os.getpid()
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 5)
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            etimes = int(parts[2])
        except ValueError:
            continue
        if pid == own:
            continue
        cmd = parts[5]
        cwd = proc_link(pid, "cwd")
        project_owned = str(project_root) in cmd or str(project_root) in cwd
        if not project_owned or not looks_like_experiment(cmd):
            continue
        fd1 = fd_target(pid, 1)
        fd2 = fd_target(pid, 2)
        artifact_dirs = artifact_dirs_from_command(cmd, project_root)
        for fd_path in [fd1, fd2]:
            inferred = artifact_dir_from_fd(fd_path, project_root)
            if inferred is not None:
                artifact_dirs.add(inferred)
        rows.append({
            "pid": pid,
            "ppid": ppid,
            "elapsed_sec": etimes,
            "pcpu": parts[3],
            "pmem": parts[4],
            "cwd": cwd,
            "exe": proc_link(pid, "exe"),
            "cmd": cmd,
            "is_python_worker": is_python_worker(pid, cmd),
            "stdout_fd": fd1,
            "stderr_fd": fd2,
            "artifact_dirs": sorted(str(path) for path in artifact_dirs),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit live TASTE experiment processes and artifact contracts.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--stop-duplicate-writers", action="store_true")
    parser.add_argument(
        "--manual-stop-confirm",
        default="",
        help="Required together with WATCHDOG_ALLOW_STOP_DUPLICATE_WRITERS=1 before duplicate writers can be killed.",
    )
    args = parser.parse_args()
    manual_stop_confirmed = args.manual_stop_confirm == "ALLOW_WATCHDOG_TO_STOP_DUPLICATE_WRITERS"
    allow_stop = bool(
        args.stop_duplicate_writers
        and os.environ.get("WATCHDOG_ALLOW_STOP_DUPLICATE_WRITERS") == "1"
        and manual_stop_confirmed
    )
    paths = build_paths(args.project)
    allowed_pythons = allowed_python_executables(args.project)
    rows = process_rows(paths.root)
    seen_rows = {(int(row.get("pid") or 0), artifact) for row in rows for artifact in (row.get("artifact_dirs") or [])}
    for row in contract_process_rows(paths.root):
        artifacts = row.get("artifact_dirs") or []
        if any((int(row.get("pid") or 0), artifact) not in seen_rows for artifact in artifacts):
            rows.append(row)
            for artifact in artifacts:
                seen_rows.add((int(row.get("pid") or 0), artifact))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for artifact in row.get("artifact_dirs") or []:
            grouped.setdefault(artifact, []).append(row)

    issues: list[dict[str, Any]] = []
    contaminated: list[dict[str, Any]] = []
    stopped: list[dict[str, Any]] = []
    for artifact_text, group in sorted(grouped.items()):
        artifact_dir = Path(artifact_text)
        workers = [row for row in group if row.get("is_python_worker")]
        worker_pids = sorted({int(row["pid"]) for row in workers})
        for row in workers:
            policy = process_python_policy(row, allowed_pythons)
            row["python_policy"] = policy
            if policy.get("status") == "reject":
                reason = str(policy.get("reason") or "wrong experiment python")
                issue = {"severity": "block", "type": "wrong_experiment_python", "artifact_dir": artifact_text, "worker_pids": [int(row["pid"])], "reason": reason, "python_policy": policy}
                issues.append(issue)
                contaminated.append(mark_contaminated(artifact_dir, reason, [int(row["pid"])]))
        if len(worker_pids) > 1:
            reason = "multiple python experiment workers are writing or targeting the same artifact_dir"
            issue = {"severity": "block", "type": "duplicate_artifact_writers", "artifact_dir": artifact_text, "worker_pids": worker_pids, "process_count": len(group)}
            issues.append(issue)
            contaminated.append(mark_contaminated(artifact_dir, reason, worker_pids))
            if allow_stop:
                stopped.append({"artifact_dir": artifact_text, **stop_pids(worker_pids)})
        log_path = artifact_dir / "stdout_stderr.log"
        if log_path.exists() and log_contains_nul(log_path):
            reason = "stdout_stderr.log contains NUL bytes; artifact is not audit-clean"
            issue = {"severity": "block", "type": "nul_log", "artifact_dir": artifact_text, "log_path": str(log_path)}
            if issue not in issues:
                issues.append(issue)
            if not (artifact_dir / "CONTAMINATED_DO_NOT_IMPORT.txt").exists():
                contaminated.append(mark_contaminated(artifact_dir, reason, worker_pids))
                if allow_stop and worker_pids:
                    stopped.append({"artifact_dir": artifact_text, **stop_pids(worker_pids)})

    active_runs = []
    for artifact_text, group in sorted(grouped.items()):
        workers = [row for row in group if row.get("is_python_worker")]
        artifact_dir = Path(artifact_text)
        contract_path = artifact_dir / "run_contract.json"
        contract = load_json(contract_path, {})
        contract_policy = contract_python_policy(contract if isinstance(contract, dict) else {}, allowed_pythons)
        if contract_policy.get("status") == "reject":
            reason = str(contract_policy.get("reason") or "wrong experiment python in contract")
            issues.append({"severity": "block", "type": "wrong_experiment_python_contract", "artifact_dir": artifact_text, "worker_pids": worker_pids, "reason": reason, "python_policy": contract_policy})
            if not (artifact_dir / "CONTAMINATED_DO_NOT_IMPORT.txt").exists():
                contaminated.append(mark_contaminated(artifact_dir, reason, worker_pids))
        active_runs.append({
            "artifact_dir": artifact_text,
            "worker_pids": sorted({int(row["pid"]) for row in workers}),
            "process_pids": sorted({int(row["pid"]) for row in group}),
            "stdout_path": str(artifact_dir / "stdout_stderr.log"),
            "contract_path": str(contract_path) if contract_path.exists() else "",
            "contract_status": contract.get("status", "") if isinstance(contract, dict) else "",
            "python_executable": contract.get("python_executable", "") if isinstance(contract, dict) else "",
            "environment_contract": contract.get("environment_contract", {}) if isinstance(contract, dict) else {},
            "python_policy": contract_policy,
            "launcher_pid": contract.get("pid") if isinstance(contract, dict) else None,
            "status": "running" if workers else "wrapper_only",
        })

    by_worker: dict[tuple[int, ...], list[dict[str, Any]]] = {}
    for run in active_runs:
        worker_key = tuple(run.get("worker_pids") or [])
        if worker_key:
            by_worker.setdefault(worker_key, []).append(run)
    for worker_key, runs in by_worker.items():
        if len(runs) <= 1:
            continue
        contract_dirs = [run for run in runs if run.get("contract_path")]
        no_contract_dirs = [run for run in runs if not run.get("contract_path")]
        if contract_dirs and no_contract_dirs:
            issues.append({
                "severity": "warn",
                "type": "split_artifact_contract",
                "worker_pids": list(worker_key),
                "artifact_dirs": [run.get("artifact_dir") for run in runs],
                "message": "one worker references multiple artifact dirs; importer must reconcile companion stdout/contract without promoting claims",
            })
    active_worker_keys = {tuple(run.get("worker_pids") or []) for run in active_runs if run.get("worker_pids")}
    blocking_issues = [item for item in issues if str(item.get("severity") or "block") == "block"]

    payload = {
        "project": args.project,
        "generated_at": now_iso(),
        "status": "blocked" if blocking_issues else "ok",
        "active_run_count": len(active_worker_keys),
        "project_experiment_python": project_experiment_python(args.project),
        "allowed_python_executables": sorted(allowed_pythons),
        "active_runs": active_runs,
        "issues": issues,
        "contaminated": contaminated,
        "stopped": stopped,
        "stop_duplicate_writers_requested": bool(args.stop_duplicate_writers),
        "stop_duplicate_writers_enabled": allow_stop,
        "stop_duplicate_writers_confirmed": manual_stop_confirmed,
        "stop_duplicate_writers_policy": "disabled unless --stop-duplicate-writers, WATCHDOG_ALLOW_STOP_DUPLICATE_WRITERS=1, and --manual-stop-confirm=ALLOW_WATCHDOG_TO_STOP_DUPLICATE_WRITERS are all present",
        "processes": rows,
        "policy": "One clean artifact_dir may have one launcher contract, one stdout_stderr.log, and one project-env python experiment worker. Duplicate writers, wrong interpreters, conda-run launches, or NUL logs are contaminated and must not be imported. Stop mode is manual-only and never enabled by automatic research loops.",
    }
    save_json(paths.state / "experiment_run_manifest.json", payload)
    save_json(paths.state / "experiment_run_watchdog.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    _common = _Path(__file__).resolve().parents[1] / "common"
    if str(_common) not in _sys.path:
        _sys.path.insert(0, str(_common))
    from entrypoint_guard import ensure_main_entrypoint

    ensure_main_entrypoint()
    raise SystemExit(main())
