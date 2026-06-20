#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths, load_project_config, project_experiment_python_from_config


FORBIDDEN_LAUNCHERS = {"bash", "sh", "zsh", "fish", "nohup"}
FORBIDDEN_ENV_LAUNCHERS = {"conda", "mamba", "micromamba"}


def project_experiment_python(project: str) -> str:
    cfg = load_project_config(project)
    candidate = project_experiment_python_from_config(cfg if isinstance(cfg, dict) else {})
    if candidate:
        return candidate
    raise RuntimeError(f"project {project} does not define a usable experiment conda_env python")


def allowed_python_executables(expected_python: str) -> list[str]:
    expected = Path(expected_python).resolve()
    out = [str(expected)]
    for name in ["python3", "python3.11", "python3.10", "python3.9"]:
        candidate = expected.parent / name
        if candidate.exists():
            out.append(str(candidate.resolve()))
    return list(dict.fromkeys(out))


def command_python_contract(project: str) -> dict[str, Any]:
    expected = project_experiment_python(project)
    cfg = load_project_config(project)
    return {
        "conda_env": str(cfg.get("conda_env") or "") if isinstance(cfg, dict) else "",
        "required_python_executable": expected,
        "allowed_python_executables": allowed_python_executables(expected),
        "forbidden_launchers": sorted(FORBIDDEN_LAUNCHERS | FORBIDDEN_ENV_LAUNCHERS),
    }


def resolve_command_executable(argv0: str, cwd: Path) -> str:
    if not argv0:
        return ""
    candidate = Path(argv0).expanduser()
    if not candidate.is_absolute():
        local = (cwd / candidate).resolve()
        if local.exists():
            return str(local)
        return argv0
    try:
        return str(candidate.resolve())
    except Exception:
        return str(candidate)


def command_uses_python(argv: list[str]) -> bool:
    if not argv:
        return False
    first = Path(argv[0]).name.lower()
    if first.startswith("python"):
        return True
    return any(str(item).lower().endswith(".py") for item in argv[:3])


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def fail(message: str, *, code: int = 2, **extra: Any) -> int:
    payload = {"status": "rejected", "message": message, **extra}
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    return code


def slugify(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._-")
    return text[:120] or "experiment"


def resolve_artifact_dir(paths, artifact_name: str, artifact_dir: str, no_timestamp_suffix: bool) -> Path:
    artifacts_root = (paths.root / "artifacts").resolve()
    if artifact_dir:
        path = Path(artifact_dir).expanduser().resolve()
    else:
        base = slugify(artifact_name)
        if not no_timestamp_suffix:
            base = f"{base}_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        path = (artifacts_root / base).resolve()
    if path != artifacts_root and str(path).startswith(str(artifacts_root) + os.sep):
        return path
    raise ValueError(f"artifact_dir must be under {artifacts_root}: {path}")


def existing_contract(path: Path) -> dict[str, Any]:
    contract = load_json(path / "run_contract.json", {})
    return contract if isinstance(contract, dict) else {}


def active_pid_from_contract(path: Path) -> int | None:
    for source in [existing_contract(path), load_json(path / "launcher.pid.json", {})]:
        if not isinstance(source, dict):
            continue
        try:
            pid = int(source.get("pid") or 0)
        except Exception:
            pid = 0
        if pid > 0 and Path(f"/proc/{pid}").exists():
            return pid
    return None


def validate_fresh_artifact_dir(path: Path, allow_existing_empty: bool) -> None:
    if path.exists():
        active_pid = active_pid_from_contract(path)
        if active_pid:
            raise RuntimeError(f"artifact_dir already has an active launcher/worker pid={active_pid}: {path}")
        blockers = [
            "CONTAMINATED_DO_NOT_IMPORT.txt",
            "FAILED_DO_NOT_IMPORT.txt",
            "run_contract.json",
            "stdout_stderr.log",
            "metrics.json",
            "audit.json",
        ]
        present = [name for name in blockers if (path / name).exists()]
        if present:
            raise RuntimeError(f"artifact_dir is not fresh; found {present}: {path}")
        if not allow_existing_empty and any(path.iterdir()):
            raise RuntimeError(f"artifact_dir exists and is not empty: {path}")


def acquire_lock(path: Path) -> None:
    lock = path / "run.lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise RuntimeError(f"run.lock already exists; use a fresh artifact_dir: {lock}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"created_at": now_iso(), "pid": os.getpid()}, ensure_ascii=False) + "\n")


def parse_env(items: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--env must be KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            raise ValueError(f"invalid env key: {key!r}")
        env[key] = value
    return env


def command_to_display(argv: list[str]) -> str:
    return " ".join(shlex.quote(item) for item in argv)


def validate_command(
    argv: list[str],
    *,
    allow_shell: bool,
    allow_nonproject_python: bool,
    python_contract: dict[str, Any],
    cwd: Path,
) -> dict[str, Any]:
    if not argv:
        raise ValueError("missing command after --")
    launcher = Path(argv[0]).name.lower()
    if not allow_shell and launcher in FORBIDDEN_LAUNCHERS:
        raise ValueError(
            "new experiments must not be launched via shell/nohup; pass the training executable argv directly "
            "or create a repo wrapper script and launch that wrapper through this the launcher"
        )
    if launcher in FORBIDDEN_ENV_LAUNCHERS and "run" in [str(item).lower() for item in argv[:4]] and not allow_nonproject_python:
        raise ValueError("new experiments must not be launched through conda/mamba run; use the project experiment Python executable directly")
    joined = " ".join(argv)
    if not allow_shell and any(token in joined for token in [" nohup ", " > ", " 2>&1", " &"]):
        raise ValueError("command appears to contain shell background/redirection syntax; launcher owns logging and backgrounding")

    actual_executable = resolve_command_executable(argv[0], cwd)
    allowed = {str(Path(item).resolve()) for item in python_contract.get("allowed_python_executables", []) if item}
    actual_resolved = str(Path(actual_executable).resolve()) if Path(actual_executable).exists() else actual_executable
    python_like = command_uses_python(argv)
    if python_like and actual_resolved not in allowed and not allow_nonproject_python:
        raise ValueError(
            "training command must use the project experiment Python executable after `--`; "
            f"expected one of {sorted(allowed)}, got {actual_executable!r}. "
            "Do not use system python, bare python3, or conda run for experiments."
        )
    return {
        "actual_executable": actual_executable,
        "actual_executable_resolved": actual_resolved,
        "python_like": python_like,
        "policy": "pass" if (not python_like or actual_resolved in allowed) else "allowed_nonproject_python_escape_hatch",
    }


def parse_metadata(items: list[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--metadata must be KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*$", key):
            raise ValueError(f"invalid metadata key: {key!r}")
        metadata[key] = value
    return metadata


def normalize_command(argv: list[str], artifact_dir: Path) -> list[str]:
    text = str(artifact_dir)
    normalized: list[str] = []
    skip_next = False
    for index, item in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        value = item.replace("{artifact_dir}", text).replace("{artifact-dir}", text)
        if value in {"--artifact_dir", "--artifact-dir"}:
            normalized.extend([value, text])
            skip_next = index + 1 < len(argv)
            continue
        if value.startswith("--artifact_dir="):
            normalized.append("--artifact_dir=" + text)
            continue
        if value.startswith("--artifact-dir="):
            normalized.append("--artifact-dir=" + text)
            continue
        normalized.append(value)
    return normalized


def run_watchdog(project: str) -> dict[str, Any]:
    script = Path(__file__).resolve().parents[1] / "execution" / "experiment_run_watchdog.py"
    if not script.exists():
        return {"status": "missing", "active_run_count": 0, "issues": []}
    proc = subprocess.run(
        [sys.executable, str(script), "--project", project],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return {"status": "error", "active_run_count": 0, "issues": [{"stderr": proc.stderr[-2000:]}]}
    try:
        payload = json.loads(proc.stdout)
        return payload if isinstance(payload, dict) else {"status": "invalid", "active_run_count": 0}
    except Exception:
        return {"status": "invalid", "active_run_count": 0, "stdout_tail": proc.stdout[-2000:]}


def experiment_launch_gate(paths, project: str, *, route_scope: str = "unspecified", save: bool = True) -> dict[str, Any]:
    viability = load_json(paths.state / "selected_base_viability_gate.json", {})
    switch_gate = load_json(paths.state / "base_switch_gate.json", {})
    environment_selection = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    viability_requires_gate = bool(
        isinstance(viability, dict)
        and viability.get("status") == "blocked"
        and viability.get("decision") == "base_switch_gate_required"
    )
    environment_requires_gate = bool(
        isinstance(environment_selection, dict)
        and environment_selection.get("selection_gate") == "blocked_candidate_base_switch_gate_required"
    )
    switch_gate_blocked = bool(
        isinstance(switch_gate, dict)
        and switch_gate.get("status") == "blocked"
        and switch_gate.get("decision") == "base_switch_not_authorized"
    )
    required = bool(viability_requires_gate or environment_requires_gate or switch_gate_blocked)
    authorized = bool(
        isinstance(switch_gate, dict)
        and switch_gate.get("status") == "pass"
        and switch_gate.get("decision") == "authorize_base_switch"
        and switch_gate.get("switch_authorized") is True
    )
    selected_base_reference_required = bool(isinstance(switch_gate, dict) and switch_gate.get("selected_base_reference_required") is True)
    normalized_scope = str(route_scope or "unspecified").strip().lower().replace("-", "_")
    current_route_scopes = {"selected_base_current_route", "current_route", "current_route_evidence_repair", "selected_base_evidence_repair", "selected_base_prune_diagnostic"}
    base_switch_evidence_scopes = {"base_switch_evidence_collection", "candidate_base_evaluation", "candidate_route_evidence_collection", "candidate_loader_probe", "candidate_reference_protocol", "candidate_reference_smoke", "candidate_reference_reproduction"}
    candidate_route_scopes = {"candidate_route", "alternative_route", "base_switch_candidate", "new_base", "route_switch_candidate"}
    if required and not authorized:
        if normalized_scope in current_route_scopes:
            if viability_requires_gate or selected_base_reference_required:
                status = "pass"
                reason = "selected-base current-route evidence repair remains allowed under launcher/audit contract; candidate-route switching and claim promotion remain blocked"
            else:
                status = "blocked"
                reason = "current-route experiment launch is blocked because the current Find/Plan has no authoritative selected Environment; old active_repo evidence is historical only"
        elif normalized_scope in base_switch_evidence_scopes:
            status = "pass"
            reason = "candidate base-switch evidence collection is allowed only to satisfy deterministic gate checks; it cannot switch the active route or support paper/claim promotion"
        elif normalized_scope in candidate_route_scopes:
            status = "blocked"
            reason = "candidate/alternative-route main-route launch requires an authorized deterministic base-switch gate; use --route-scope base_switch_evidence_collection only for bounded gate evidence collection"
        else:
            status = "blocked"
            reason = "route_scope is unspecified while deterministic base-switch gating is required by selected-base viability or Environment selection; pass --route-scope base_switch_evidence_collection only for bounded candidate-gate evidence collection"
    else:
        status = "pass"
        reason = "base-switch gate does not block this launch"
    payload = {
        "project": project,
        "status": status,
        "route_scope": normalized_scope,
        "generated_at": now_iso(),
        "selected_base_viability_gate": {
            "status": viability.get("status", "") if isinstance(viability, dict) else "",
            "decision": viability.get("decision", "") if isinstance(viability, dict) else "",
            "switch_authorized": viability.get("switch_authorized", False) if isinstance(viability, dict) else False,
        },
        "base_switch_gate": {
            "status": switch_gate.get("status", "") if isinstance(switch_gate, dict) else "",
            "decision": switch_gate.get("decision", "") if isinstance(switch_gate, dict) else "",
            "switch_authorized": switch_gate.get("switch_authorized", False) if isinstance(switch_gate, dict) else False,
            "selected_base_reference_required": selected_base_reference_required,
        },
        "environment_selection": {
            "selection_gate": environment_selection.get("selection_gate", "") if isinstance(environment_selection, dict) else "",
            "fresh_find_run_id": environment_selection.get("fresh_find_run_id", "") if isinstance(environment_selection, dict) else "",
            "selected_plan_id": environment_selection.get("selected_plan_id", "") if isinstance(environment_selection, dict) else "",
        },
        "base_switch_gate_required": required,
        "viability_requires_gate": viability_requires_gate,
        "environment_requires_gate": environment_requires_gate,
        "switch_gate_blocked": switch_gate_blocked,
        "policy": "When selected_base_viability_gate or Environment selection requires deterministic base-switch gating and base_switch_gate is not authorized, candidate/alternative-route main-route launches and claim promotion are blocked. Explicit current selected-base evidence repair may continue only when a current authoritative selected base exists. Bounded candidate evidence collection may continue with route_scope=base_switch_evidence_collection only to satisfy deterministic gate checks; it must not edit active_repo/evidence_ready_repo_selection or support paper/claim promotion.",
        "evidence": [
            str(paths.state / "selected_base_viability_gate.json"),
            str(paths.state / "base_switch_gate.json"),
            str(paths.state / "evidence_ready_repo_selection.json"),
            str(paths.state / "blocker_action_plan.json"),
        ],
    }
    payload["reason"] = reason
    if status == "pass" and required and not authorized and normalized_scope in current_route_scopes:
        payload["guardrail"] = "current selected-base remains authoritative; this launch cannot switch active_repo/evidence_ready_repo_selection or support paper/claim promotion until evidence gates pass"
    if status == "pass" and required and not authorized and normalized_scope in base_switch_evidence_scopes:
        payload["guardrail"] = "candidate evidence collection only; this launch cannot switch active_repo/evidence_ready_repo_selection, cannot become the main route, and cannot support paper/claim promotion until deterministic base-switch gate and execution receipts pass"
    if save:
        save_json(paths.state / "experiment_launch_gate.json", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch one TASTE experiment with a deterministic artifact/PID/log contract.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--artifact-name", default="")
    parser.add_argument("--artifact-dir", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--env", action="append", default=[], help="Environment override KEY=VALUE; may be repeated.")
    parser.add_argument("--allow-existing-empty", action="store_true")
    parser.add_argument("--allow-concurrent", action="store_true", help="Allow another active experiment run; default rejects to avoid duplicates.")
    parser.add_argument("--allow-shell", action="store_true", help="Emergency escape hatch; should not be used by autonomous Claude runs.")
    parser.add_argument("--allow-nonproject-python", action="store_true", help="Emergency escape hatch; autonomous Claude runs must not use it.")
    parser.add_argument("--method", default="", help="Generic experiment method slug recorded in the artifact contract; required for audit-ready import.")
    parser.add_argument("--dataset", default="", help="Dataset/benchmark slug recorded in the artifact contract; required for audit-ready import when not present in command args.")
    parser.add_argument("--role", default="candidate", help="Generic comparison role such as candidate/control/reference.")
    parser.add_argument("--route-scope", default="unspecified", choices=["unspecified", "selected_base_current_route", "current_route_evidence_repair", "selected_base_prune_diagnostic", "base_switch_evidence_collection", "candidate_base_evaluation", "candidate_route_evidence_collection", "candidate_loader_probe", "candidate_reference_protocol", "candidate_reference_smoke", "candidate_reference_reproduction", "candidate_route", "alternative_route", "base_switch_candidate"], help="Route authorization scope for selected-base/base-switch gates; default remains conservative.")
    parser.add_argument("--metadata", action="append", default=[], help="Additional generic artifact metadata KEY=VALUE; may be repeated.")
    parser.add_argument("--no-timestamp-suffix", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    try:
        paths = build_paths(args.project)
        artifact_dir = resolve_artifact_dir(paths, args.artifact_name, args.artifact_dir, args.no_timestamp_suffix)
        command = normalize_command(command, artifact_dir)
        cwd = Path(args.cwd).expanduser().resolve() if args.cwd else paths.root.resolve()
        if not cwd.exists() or not cwd.is_dir():
            raise ValueError(f"cwd does not exist or is not a directory: {cwd}")
        if not (cwd == paths.root.resolve() or str(cwd).startswith(str(paths.root.resolve()) + os.sep)):
            raise ValueError(f"cwd must stay inside project root: {cwd}")
        python_contract = command_python_contract(args.project)
        executable_contract = validate_command(
            command,
            allow_shell=args.allow_shell,
            allow_nonproject_python=args.allow_nonproject_python,
            python_contract=python_contract,
            cwd=cwd,
        )
        extra_env = parse_env(args.env)
        extra_metadata = parse_metadata(args.metadata)
        validate_fresh_artifact_dir(artifact_dir, args.allow_existing_empty)
        launch_gate = experiment_launch_gate(paths, args.project, route_scope=args.route_scope, save=not args.dry_run)
        if launch_gate.get("status") == "blocked" and not args.dry_run:
            raise RuntimeError(f"experiment launch gate blocked: {launch_gate.get('reason')}")
        watchdog = run_watchdog(args.project)
        if watchdog.get("status") == "blocked":
            raise RuntimeError(f"experiment watchdog has blocking issues: {watchdog.get('issues')}")
        if int(watchdog.get("active_run_count") or 0) > 0 and not args.allow_concurrent and not args.dry_run:
            raise RuntimeError("another experiment is already active; wait for it to finish or pass --allow-concurrent with evidence")
    except Exception as exc:
        return fail(str(exc))

    log_path = artifact_dir / "stdout_stderr.log"
    contract_path = artifact_dir / "run_contract.json"
    pid_path = artifact_dir / "launcher.pid.json"
    payload = {
        "contract_schema_version": 2,
        "project": args.project,
        "status": "dry_run" if args.dry_run else "launching",
        "created_at": now_iso(),
        "artifact_dir": str(artifact_dir),
        "stdout_path": str(log_path),
        "contract_path": str(contract_path),
        "cwd": str(cwd),
        "command": command,
        "command_display": command_to_display(command),
        "env_overrides": sorted(extra_env.keys()),
        "experiment_metadata": {
            "method": str(args.method or "").strip(),
            "dataset": str(args.dataset or "").strip(),
            "role": str(args.role or "candidate").strip() or "candidate",
            "route_scope": str(args.route_scope or "unspecified").strip(),
            "extra": extra_metadata,
        },
        "launcher": str(Path(__file__).resolve()),
        "python_executable": python_contract.get("required_python_executable"),
        "actual_executable": executable_contract.get("actual_executable"),
        "environment_contract": {**python_contract, "allow_nonproject_python": bool(args.allow_nonproject_python)},
        "expected_outputs": {
            "contract": str(contract_path),
            "pid": str(pid_path),
            "lock": str(artifact_dir / "run.lock"),
            "stdout": str(log_path),
            "optional_metrics": str(artifact_dir / "metrics.json"),
            "optional_audit": str(artifact_dir / "audit.json"),
        },
        "audit_refresh_required": [
            "python modules/experimenting/main.py --action watchdog",
            "framework/scripts/run_module.py writing --action audit_evidence",
            "framework/scripts/run_module.py writing --action submission_readiness",
            "framework/scripts/run_module.py planning --action blocker_action",
        ],
        "artifact_contract_required_fields": [
            "contract_schema_version",
            "artifact_dir",
            "stdout_path",
            "contract_path",
            "command",
            "experiment_metadata.method",
            "experiment_metadata.dataset",
            "launcher",
            "python_executable",
            "environment_contract.required_python_executable",
            "expected_outputs.stdout",
        ],
        "registry_import_policy": "Import only after the process exits cleanly, the artifact has no CONTAMINATED_DO_NOT_IMPORT/FAILED_DO_NOT_IMPORT marker, stdout_stderr.log is audit-clean, artifact-local method/dataset metadata or experiment.json is present, and deterministic audit/registry refresh scripts have run.",
        "policy": "one launcher, one artifact_dir, one stdout_stderr.log, one project-env python worker; no reused/contaminated artifacts; no system python or conda run",
        "experiment_launch_gate": launch_gate,
    }
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    try:
        artifact_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        pass
    try:
        acquire_lock(artifact_dir)
        save_json(contract_path, payload)
        env = os.environ.copy()
        env.update(extra_env)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env["EXPERIMENT_ARTIFACT_DIR"] = str(artifact_dir)
        env["EXPERIMENT_LOG_PATH"] = str(log_path)
        env["EXPERIMENT_CONTRACT_PATH"] = str(contract_path)
        with log_path.open("ab", buffering=0) as log_handle, open(os.devnull, "rb") as devnull:
            proc = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                stdin=devnull,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        payload.update({"status": "running", "pid": proc.pid, "started_at": now_iso()})
        save_json(contract_path, payload)
        save_json(pid_path, {"pid": proc.pid, "artifact_dir": str(artifact_dir), "started_at": payload["started_at"], "command": command, "python_executable": payload.get("python_executable")})
        save_json(paths.state / "experiment_launcher_last_run.json", payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        payload.update({"status": "launch_failed", "error": str(exc), "failed_at": now_iso()})
        try:
            save_json(contract_path, payload)
        except Exception:
            pass
        return fail(str(exc), contract=payload)


if __name__ == "__main__":
    raise SystemExit(main())
