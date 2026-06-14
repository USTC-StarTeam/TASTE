#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_state import append_agent_log, consume_guidance, mark_agent, upsert_agent
from project_paths import ROOT, build_paths, load_project_config
from pipeline_guard import guard_fresh_base_blocker_entry

from taste_pythonpath import script_resolver

SCRIPTS = script_resolver(ROOT)
TRAJECTORY_SUPERVISOR_ENTRY_ENV = "TRAJECTORY_SUPERVISOR_ENTRY"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def proc_cmdline(pid: int) -> str:
    proc_root = Path("/proc") / str(pid)
    try:
        raw = (proc_root / "cmdline").read_bytes()
        text = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if text:
            return " ".join(text.split())
    except Exception:
        pass
    try:
        return (proc_root / "comm").read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


def proc_ppid(pid: int) -> int:
    try:
        text = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        end = text.rfind(")")
        if end < 0:
            return 0
        fields = text[end + 2 :].split()
        return safe_int(fields[1]) if len(fields) > 1 else 0
    except Exception:
        return 0


def ancestor_processes(limit: int = 40) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pid = proc_ppid(os.getpid())
    seen: set[int] = set()
    for _ in range(limit):
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        cmdline = proc_cmdline(pid)
        rows.append({"pid": pid, "cmdline": cmdline})
        pid = proc_ppid(pid)
    return rows


def is_claude_control_process(cmdline: str) -> bool:
    text = " ".join(str(cmdline or "").split())
    return bool(
        "claude_project_session.py" in text
        or " claude -p" in f" {text}"
        or "/claude -p" in text
    )


def guard_recursive_supervisor_entry(paths, args: argparse.Namespace) -> int | None:
    ancestors = ancestor_processes()
    if not any(is_claude_control_process(str(row.get("cmdline") or "")) for row in ancestors):
        return None
    report = {
        "status": "blocked_recursion_guard",
        "reason": "trajectory_supervisor_invoked_from_claude_session",
        "project": args.project,
        "venue": args.venue,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "command": sys.argv,
        "entry_env": os.environ.get(TRAJECTORY_SUPERVISOR_ENTRY_ENV, ""),
        "ancestor_tail": ancestors[:12],
        "blocked_at": now_iso(),
        "policy": "Trajectory supervisor is owned by the wrapper/web entrypoint. Claude Code workers must execute the assigned queue item and update state, not spawn nested supervisors.",
    }
    save_json(paths.state / "trajectory_supervisor_recursion_guard.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2


def acquire_supervisor_lock(paths, args: argparse.Namespace) -> bool:
    lock_path = paths.state / "trajectory_supervisor.lock.json"
    payload = {
        "project": args.project,
        "venue": args.venue,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "command": sys.argv,
        "entry_env": os.environ.get(TRAJECTORY_SUPERVISOR_ENTRY_ENV, ""),
        "acquired_at": now_iso(),
    }
    while True:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            return True
        except FileExistsError:
            existing = load_json(lock_path, {})
            existing_pid = safe_int(existing.get("pid") if isinstance(existing, dict) else 0)
            if existing_pid and process_alive(existing_pid):
                report = {
                    "status": "blocked_duplicate_supervisor",
                    "reason": "trajectory_supervisor_already_running_for_project",
                    "project": args.project,
                    "venue": args.venue,
                    "pid": os.getpid(),
                    "existing_lock": existing,
                    "blocked_at": now_iso(),
                    "lock_path": str(lock_path),
                }
                save_json(paths.state / "trajectory_supervisor_duplicate_guard.json", report)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return False
            try:
                lock_path.unlink()
            except FileNotFoundError:
                continue
            except Exception as exc:
                report = {
                    "status": "blocked_lock_unavailable",
                    "reason": "stale_trajectory_supervisor_lock_could_not_be_removed",
                    "project": args.project,
                    "pid": os.getpid(),
                    "lock_path": str(lock_path),
                    "error": str(exc),
                    "blocked_at": now_iso(),
                }
                save_json(paths.state / "trajectory_supervisor_duplicate_guard.json", report)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return False


def release_supervisor_lock(paths) -> None:
    lock_path = paths.state / "trajectory_supervisor.lock.json"
    existing = load_json(lock_path, {})
    if isinstance(existing, dict) and safe_int(existing.get("pid")) == os.getpid():
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop(TRAJECTORY_SUPERVISOR_ENTRY_ENV, None)
    env.pop("TRAJECTORY_SUPERVISOR_PARENT_PID", None)
    return env


def run(cmd: list[str], timeout: int | None = None) -> dict[str, Any]:
    started = now_iso()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, env=child_env(), text=True, capture_output=True, timeout=timeout)
        rc = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        rc = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "ignore")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "ignore")
        stderr += f"\nTIMEOUT after {timeout}s"
    return {
        "command": cmd,
        "return_code": rc,
        "started_at": started,
        "finished_at": now_iso(),
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
    }


def priority_key(item: dict[str, Any]) -> tuple[int, str]:
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    return (order.get(str(item.get("priority") or "P9"), 9), str(item.get("id") or ""))


def selected_method_contracts(third_party_stack: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
    bindings = third_party_stack.get("capability_bindings", []) if isinstance(third_party_stack.get("capability_bindings", []), list) else []
    haystack = " ".join(str(item.get(key) or "") for key in ["id", "owner_role", "skill_contract", "objective"]).lower()
    selected: list[dict[str, Any]] = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        text = " ".join(
            [
                str(binding.get("capability") or ""),
                " ".join(str(value) for value in binding.get("uses", []) if value) if isinstance(binding.get("uses", []), list) else str(binding.get("uses") or ""),
            ]
        ).lower()
        if any(token in text or token in haystack for token in ["assurance", "evidence", "memory", "trajectory", "paper", "novelty", "hypothesis"]):
            selected.append(binding)
    return selected[:8]


def queue_item_message(item: dict[str, Any], guidance_text: str, round_index: int, method_contracts: list[dict[str, Any]]) -> str:
    return f"""
You are executing research trajectory supervisor round {round_index}.

Trajectory queue item:
{json.dumps(item, ensure_ascii=False, indent=2)}

native method contracts for this stage:
{json.dumps(method_contracts, ensure_ascii=False, indent=2) if method_contracts else 'none available; first refresh state/third_party_research_stack.json if method contracts are needed'}

Queued web guidance:
{guidance_text or 'none'}

Required behavior:
- Read state/evolutionary_memory_index.json, state/trajectory_optimization_plan.json, state/research_evidence_integrity.json, state/research_direction_memory.json, and state/trajectory_checkpoints.json before deciding.
- If this queue item contains blocker_action_id, read state/blocker_action_plan.json first and execute the listed repair_strategy, recommended_commands, and success_checks unless local evidence proves a safer route.
- Follow the item skill_contract if it points at a local .claude skill, and use the native contracts above as the execution style for this stage. Do not present source projects as separate agents, roles, or modules.
- Repair or execute only evidence-backed actions. Do not fabricate metrics, data availability, citations, or paper readiness.
- If the current repo/env/data should be changed, decide from local evidence and record the reason; do not hard-code topic gates.
- Do not call framework/scripts/run_research_trajectory_supervisor.py from this Claude worker. You are already inside a wrapper-owned trajectory item; finish the item, update state, or return a blocked/remaining-queue report.
- After action, ensure the relevant state/report/artifact files are updated or explain exactly why blocked.
- Return concise Markdown with Conclusion, Evidence Inspected, Actions Taken, Validation, Remaining Queue/Blockers.
""".strip()


def load_supervisor_state(paths) -> dict[str, Any]:
    return load_json(paths.state / "trajectory_supervisor_state.json", {"project": paths.name, "rounds": []})


def main() -> int:
    parser = argparse.ArgumentParser(description="Trajectory-level TASTE supervisor that repeatedly delegates queue items to Claude Code and rebuilds evidence memory.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--timeout-sec", type=int, default=14400)
    parser.add_argument("--dry-run", action="store_true", help="Record selected queue items without calling Claude Code.")
    parser.add_argument("--stop-on-pass", action="store_true", help="Stop early when assurance and evidence integrity both pass.")
    args = parser.parse_args()
    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name)
    if guard_rc is not None:
        return guard_rc

    paths = build_paths(args.project)
    recursion_rc = guard_recursive_supervisor_entry(paths, args)
    if recursion_rc is not None:
        return recursion_rc
    if not acquire_supervisor_lock(paths, args):
        return 2
    atexit.register(release_supervisor_lock, paths)
    cfg = load_project_config(args.project)
    state_path = paths.state / "trajectory_supervisor_state.json"
    state = load_supervisor_state(paths)
    rounds = state.get("rounds", []) if isinstance(state.get("rounds", []), list) else []
    completed_ids = {str(row.get("queue_item", {}).get("id")) for row in rounds if row.get("status") == "completed"}

    upsert_agent(
        args.project,
        "main",
        name="TASTE 轨迹主控 Agent",
        role="main",
        stage="trajectory",
        status="running",
        goal="Execute trajectory optimization queue through evidence-gated Claude Code rounds",
        current_step="refreshing research trajectory state",
    )

    for local_round in range(1, max(1, args.rounds) + 1):
        build_cmd = [sys.executable, str(SCRIPTS / "build_research_trajectory_system.py"), "--project", args.project]
        if args.venue:
            build_cmd.extend(["--venue", args.venue])
        build_result = run(build_cmd, timeout=900)
        append_agent_log(args.project, "main", f"trajectory refresh rc={build_result['return_code']}")
        if build_result["return_code"] != 0:
            mark_agent(args.project, "main", "error", current_step="trajectory refresh failed", result={"state_path": str(state_path)})
            rounds.append({
                "round_index": len(rounds) + 1,
                "started_at": build_result["started_at"],
                "finished_at": build_result["finished_at"],
                "status": "refresh_failed",
                "build_result": build_result,
            })
            state.update({"updated_at": now_iso(), "rounds": rounds[-200:], "latest": rounds[-1]})
            save_json(state_path, state)
            return int(build_result["return_code"] or 1)

        trajectory = load_json(paths.state / "research_trajectory_system.json", {})
        plan = load_json(paths.state / "trajectory_optimization_plan.json", {})
        integrity = load_json(paths.state / "research_evidence_integrity.json", {})
        assurance = load_json(paths.state / "research_assurance_layer.json", {})
        third_party_stack = load_json(paths.state / "third_party_research_stack.json", {})
        blocker_plan = load_json(paths.state / "blocker_action_plan.json", {})
        queue = plan.get("queue", []) if isinstance(plan, dict) and isinstance(plan.get("queue", []), list) else []
        remaining = [item for item in queue if isinstance(item, dict) and str(item.get("id")) not in completed_ids]
        remaining.sort(key=priority_key)
        if args.stop_on_pass and assurance.get("status") == "pass" and integrity.get("status") == "pass":
            latest = {
                "round_index": len(rounds) + 1,
                "started_at": now_iso(),
                "finished_at": now_iso(),
                "status": "stopped_passed",
                "reason": "assurance and evidence integrity both passed",
                "assurance_status": assurance.get("status"),
                "evidence_integrity_status": integrity.get("status"),
            }
            rounds.append(latest)
            state.update({"updated_at": now_iso(), "rounds": rounds[-200:], "latest": latest})
            save_json(state_path, state)
            break
        if not remaining:
            latest = {
                "round_index": len(rounds) + 1,
                "started_at": now_iso(),
                "finished_at": now_iso(),
                "status": "queue_exhausted",
                "assurance_status": assurance.get("status"),
                "evidence_integrity_status": integrity.get("status"),
                "trajectory_phase": trajectory.get("summary", {}).get("phase") if isinstance(trajectory, dict) else "",
            }
            rounds.append(latest)
            state.update({"updated_at": now_iso(), "rounds": rounds[-200:], "latest": latest})
            save_json(state_path, state)
            append_agent_log(args.project, "main", "trajectory queue exhausted")
            break

        item = remaining[0]
        round_index = len(rounds) + 1
        guidance = consume_guidance(args.project, target_agent_id="main", stage="experiment") + consume_guidance(args.project, target_agent_id="main", stage="trajectory")
        guidance_text = "\n".join(f"- {row.get('message', '')}" for row in guidance if row.get("message"))
        worker_id = f"trajectory_{str(item.get('id') or round_index).replace('/', '_')[:60]}"
        upsert_agent(
            args.project,
            worker_id,
            name=f"Claude 轨迹 Worker: {item.get('id', round_index)}",
            role="claude-worker",
            stage="trajectory",
            status="running" if not args.dry_run else "queued",
            parent_id="main",
            goal=str(item.get("objective") or "trajectory queue item")[:500],
            current_step="selected from trajectory optimization queue",
            extra={"queue_item": item},
        )
        upsert_agent(args.project, "main", children=[worker_id], current_step=f"delegating {item.get('id')} to Claude Code")
        append_agent_log(args.project, "main", f"selected trajectory queue item {item.get('id')} priority={item.get('priority')}")
        append_agent_log(args.project, worker_id, f"objective: {item.get('objective')}")
        method_contracts = selected_method_contracts(third_party_stack if isinstance(third_party_stack, dict) else {}, item)
        append_agent_log(args.project, worker_id, f"method contracts: {', '.join(str(row.get('capability')) for row in method_contracts if isinstance(row, dict)) or 'none'}")

        if args.dry_run:
            claude_result = {"return_code": 0, "status": "dry_run", "stdout_tail": "Claude call skipped by --dry-run."}
            status = "dry_run_recorded"
            mark_agent(args.project, worker_id, "done", current_step="dry-run queue item recorded")
        else:
            message = queue_item_message(item, guidance_text, round_index, method_contracts)
            prompt_path = paths.state / f"trajectory_prompt_{worker_id}.md"
            prompt_path.write_text(message, encoding="utf-8")
            cmd = [
                sys.executable,
                str(SCRIPTS / "claude_project_session.py"),
                "--project",
                args.project,
                "--stage",
                "trajectory",
                "--message-file",
                str(prompt_path),
                "--timeout-sec",
                str(args.timeout_sec),
                "--agent-id",
                worker_id,
            ]
            claude_result = run(cmd, timeout=None if args.timeout_sec <= 0 else max(args.timeout_sec + 600, 1800))
            status = "completed" if claude_result["return_code"] == 0 else "claude_failed"
            mark_agent(args.project, worker_id, "done" if status == "completed" else "error", current_step=f"trajectory Claude round {status}")

        rebuild = run(build_cmd, timeout=900)
        checkpoint = load_json(paths.state / "trajectory_checkpoints.json", {})
        latest = {
            "round_index": round_index,
            "started_at": build_result["started_at"],
            "finished_at": now_iso(),
            "status": status,
            "queue_item": item,
            "method_contracts_applied": method_contracts,
            "guidance_consumed": guidance,
            "claude_result": claude_result,
            "post_rebuild_return_code": rebuild["return_code"],
            "checkpoint_delta": checkpoint.get("latest", {}).get("delta_status") if isinstance(checkpoint, dict) and isinstance(checkpoint.get("latest", {}), dict) else "",
            "assurance_status": load_json(paths.state / "research_assurance_layer.json", {}).get("status"),
            "evidence_integrity_status": load_json(paths.state / "research_evidence_integrity.json", {}).get("status"),
        }
        rounds.append(latest)
        if status == "completed":
            completed_ids.add(str(item.get("id")))
        state.update({
            "project": args.project,
            "updated_at": now_iso(),
            "status": status,
            "dry_run": args.dry_run,
            "rounds": rounds[-200:],
            "latest": latest,
            "method_contract_source": str(paths.state / "third_party_research_stack.json"),
            "protocol_path": str(paths.state / "trajectory_execution_protocol.json"),
            "optimization_plan_path": str(paths.state / "trajectory_optimization_plan.json"),
            "blocker_action_plan_path": str(paths.state / "blocker_action_plan.json"),
            "blocker_action_plan_summary": blocker_plan.get("summary", {}) if isinstance(blocker_plan, dict) else {},
        })
        save_json(state_path, state)
        append_agent_log(args.project, "main", f"trajectory round {round_index} {status}; checkpoint={latest.get('checkpoint_delta')}")
        if status not in {"completed", "dry_run_recorded"}:
            mark_agent(args.project, "main", "error", current_step=f"trajectory round {round_index} failed", result={"state_path": str(state_path)})
            return int(claude_result.get("return_code") or 1)

    mark_agent(args.project, "main", "done", current_step="trajectory supervisor complete", result={"state_path": str(state_path)})
    print(state_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
