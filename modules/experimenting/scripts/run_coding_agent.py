#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_state import append_agent_log, consume_guidance, mark_agent, upsert_agent
from runtime_env import find_binary as runtime_find_binary, interactive_env as project_interactive_env
from project_paths import build_paths, load_project_config, management_python
from run_project import current_find_execution_contract
from pipeline_guard import guard_fresh_base_blocker_entry

def _repo_root_from_script() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "framework").is_dir() and (candidate / "modules").is_dir() and (candidate / "web").is_dir():
            return candidate
    return current.parents[1]

ROOT = _repo_root_from_script()
from taste_pythonpath import script_resolver

SCRIPTS = script_resolver(ROOT)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def agent_cfg(cfg: dict) -> dict:
    return cfg.get("coding_agent", {}) if isinstance(cfg, dict) and isinstance(cfg.get("coding_agent", {}), dict) else {}


def backend_from_config(cfg: dict, override: str = "") -> str:
    requested = str(override or agent_cfg(cfg).get("backend", "") or "claude").strip().lower()
    return requested or "claude"


def interactive_env(project: str = "", cfg: dict | None = None) -> dict[str, str]:
    """Capture interactive bash exports such as NVM-installed Claude Code paths."""
    return project_interactive_env(project or None, cfg)


def runtime_env(extra: dict | None = None, project: str = "", cfg: dict | None = None) -> dict:
    env = interactive_env(project, cfg)
    if extra:
        env.update(extra)
    return env


def find_binary(binary: str, cfg: dict) -> str:
    project = str(cfg.get("name") or "") if isinstance(cfg, dict) else ""
    resolved = find_binary_from_runtime(binary, project, cfg)
    return resolved


def find_binary_from_runtime(binary: str, project: str, cfg: dict) -> str:
    return runtime_find_binary(binary, project=project or None, cfg=cfg)


def find_claude(cfg: dict) -> str:
    return find_binary("claude", cfg)


def resolve_backend(cfg: dict, override: str = "") -> tuple[str, str, str]:
    requested = backend_from_config(cfg, override)
    effective_backend = "claude"
    if find_claude(cfg):
        reason = "" if requested == effective_backend else f"backend-forced-claude:{requested}"
        return requested, effective_backend, reason
    return requested, effective_backend, "claude-not-found"


def run(cmd: list[str], cwd: Path, env: dict | None = None, timeout: int | None = None, project: str = "", cfg: dict | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=runtime_env(env, project, cfg), start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout or "", stderr or "")
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
            stdout, stderr = proc.communicate()
        return subprocess.CompletedProcess(cmd, 124, stdout or "", (stderr or "") + "\n" + f"timeout after {timeout}s")


def cli_env(binary_path: str, project: str = "", cfg: dict | None = None) -> dict:
    env = runtime_env(project=project, cfg=cfg)
    if binary_path:
        bindir = str(Path(binary_path).parent)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return env


def coding_timeout(cfg: dict) -> int:
    acfg = agent_cfg(cfg)
    env_value = os.environ.get("CODING_AGENT_TIMEOUT_SEC")
    value = env_value if env_value is not None else acfg.get("timeout_sec") or 14400
    try:
        parsed = max(60, int(value))
    except Exception:
        parsed = 14400
    # Older project configs used 1200s, which is too short for unattended
    # Claude Code experiment repair. Keep explicit env overrides respected.
    return parsed if env_value is not None else max(14400, parsed)


def claude_outer_timeout(cfg: dict) -> int | None:
    acfg = agent_cfg(cfg)
    value = os.environ.get("CLAUDE_CODING_OUTER_TIMEOUT_SEC") or acfg.get("claude_outer_timeout_sec", "")
    if str(value).strip():
        try:
            parsed = int(value)
            return None if parsed <= 0 else max(60, parsed)
        except Exception:
            return None
    inner = coding_timeout(cfg)
    if inner <= 0:
        return None
    return max(inner + 600, 1800)


def read_trial_context(path_text: str) -> str:
    if not path_text:
        return ""
    path = Path(path_text)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:16000]


def compact_json_file(path: Path, default: Any = None, limit: int = 10000) -> str:
    if default is None:
        default = {}
    payload = load_json(path, default)
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception:
        text = str(payload)
    return text[:limit]



def selected_plan_context(project: str) -> str:
    paths = build_paths(project)
    try:
        selected = current_find_execution_contract(paths)
    except Exception as exc:
        selected = {
            "required": False,
            "status": "contract_error",
            "selected_plan_id": "",
            "selected_idea_id": "",
            "reason": str(exc),
            "source": "current_find_execution_contract",
        }
    if not selected.get("required"):
        sources = {
            "current_find_research_plan": load_json(paths.state / "current_find_research_plan.json", {}),
            "experiment_plan": load_json(paths.state / "experiment_plan.json", {}),
            "taste_plan_bridge": load_json(paths.state / "taste_plan_bridge.json", {}),
            "idea_candidates": load_json(paths.state / "idea_candidates.json", {}),
        }
        for label, payload in sources.items():
            if not isinstance(payload, dict):
                continue
            selected_plan_id = str(payload.get("selected_plan_id") or "").strip()
            selected_idea_id = str(payload.get("selected_idea_id") or "").strip()
            if selected_plan_id or selected_idea_id:
                selected = {
                    "source": label,
                    "run_id": payload.get("run_id") or payload.get("current_find_run_id") or "",
                    "status": payload.get("status") or "",
                    "selected_plan_id": selected_plan_id,
                    "selected_idea_id": selected_idea_id,
                    "selected_plan": payload.get("selected_plan") if isinstance(payload.get("selected_plan"), dict) else {},
                    "selected_idea": payload.get("selected_idea") if isinstance(payload.get("selected_idea"), dict) else {},
                    "execution_policy": payload.get("execution_policy") if isinstance(payload.get("execution_policy"), dict) else {},
                }
                break
    selected.setdefault("source", "current_find_execution_contract")
    selected.setdefault("execution_rule", "Only selected_plan_id may drive method implementation, experiment launches, paper text, or claim updates. Non-selected ideas/plans are backlog candidates only.")
    if selected.get("required") and not selected.get("selected_plan_id"):
        selected["hard_stop"] = "Current Find has idea/plan candidates but selected_plan_id is empty. Do not implement, launch experiments, write paper text, or promote claims until main Claude Code or human supervision selects exactly one plan."
    try:
        return json.dumps(selected, ensure_ascii=False, indent=2)[:12000]
    except Exception:
        return str(selected)[:12000]


def trajectory_context(project: str) -> str:
    paths = build_paths(project)
    files = {
        "current_find_research_plan": paths.state / "current_find_research_plan.json",
        "experiment_plan": paths.state / "experiment_plan.json",
        "taste_plan_bridge": paths.state / "taste_plan_bridge.json",
        "idea_candidates": paths.state / "idea_candidates.json",
        "fresh_base_implementation_plan": paths.state / "fresh_base_implementation_plan.json",
        "literature_tool_packet": paths.state / "literature_tool_packet.json",
        "literature_tool_last_run": paths.state / "literature_tool_last_run.json",
        "taste_literature_intermediates": paths.state / "taste_literature_intermediates.json",
        "taste_sync": paths.state / "taste_sync.json",
        "research_trajectory_system": paths.state / "research_trajectory_system.json",
        "trajectory_execution_protocol": paths.state / "trajectory_execution_protocol.json",
        "trajectory_optimization_plan": paths.state / "trajectory_optimization_plan.json",
        "evolutionary_memory_index": paths.state / "evolutionary_memory_index.json",
        "research_evidence_integrity": paths.state / "research_evidence_integrity.json",
        "research_evidence_manifest": paths.state / "research_evidence_manifest.json",
        "research_direction_memory": paths.state / "research_direction_memory.json",
        "research_graph_history": paths.state / "research_graph_history.json",
        "research_landscape_assessment": paths.state / "research_landscape_assessment.json",
        "evolutionary_memory_ledger": paths.state / "evolutionary_memory_ledger.json",
        "trajectory_checkpoints": paths.state / "trajectory_checkpoints.json",
        "research_skill_contracts": paths.state / "research_skill_contracts.json",
        "research_trajectory_capability_audit": paths.state / "research_trajectory_capability_audit.json",
        "trajectory_supervisor_state": paths.state / "trajectory_supervisor_state.json",
    }
    sections = []
    packet_md = paths.planning / "literature_tool_packet.md"
    if packet_md.exists():
        try:
            sections.append(f"## literature_tool_packet_md ({packet_md})\n{packet_md.read_text(encoding='utf-8', errors='ignore')[:9000]}")
        except Exception:
            pass
    for label, file_path in files.items():
        if file_path.exists():
            sections.append(f"## {label} ({file_path})\n{compact_json_file(file_path, {}, 9000)}")
    return "\n\n".join(sections) or "No persisted trajectory context found yet."


def build_prompt(agent_name: str, args: argparse.Namespace, repo: Path, trial_context: str) -> str:
    guidance = consume_guidance(args.project, target_agent_id="main", stage="experiment")
    guidance_text = "\n".join(f"- {item.get('message', '')}" for item in guidance if item.get("message")) if guidance else "none"
    return (
        f"You are the {agent_name} coding backend inside an autonomous AI research workflow. "
        "Use the TASTE Claude Code research pack if available: experiment-coordinator, experiment-loop, and evidence-gate. "
        "Act like a workflow trajectory executor plus skeptical evidence panel, not a lone coder. "
        "Operate only inside the selected repository/work directory and research project artifacts. Do not fabricate results, metrics, logs, citations, or paper claims. "
        "Before changing a method because of a paper or idea, inspect TASTE's current-Find bridge (`state/current_find_research_plan.json`, `state/experiment_plan.json`, `state/taste_plan_bridge.json`), then the literature tool packet (`planning/literature_tool_packet.md` or `state/literature_tool_packet.json`) and raw `planning/finding/` files when present. "
        "The bridge may contain several idea/plan candidates, but downstream work must use exactly the `selected_plan_id` and `selected_idea_id` chosen by the main Claude Code/human-supervised selection contract. Non-selected ideas and plans are backlog only; do not implement, launch experiments for, cite as the current method, or promote claims from them unless TASTE rewrites the selection contract. "
        f"If the packet is missing or unrelated, run `{management_python()} modules/finding/scripts/run_literature_tool.py --project {args.project} --query \"<targeted query>\" --fast-mode` from the module root, then rebuild the packet. "
        "Use literature only to choose code routes and baselines; validation still requires local logs/metrics/audits. "
        "Your job is to autonomously execute this loop: inspect the research method contract and code, make the smallest evidence-driven code/config changes needed for the method, "
        "run the validation command, analyze failures or bad-case/audit outputs, repair and rerun within the allowed rounds, then stop. "
        "If the method is already implemented, run the command and only fix real failures. "
        "Preserve git hygiene: do not add bulky artifacts, do not revert unrelated user changes, and keep edits minimal. "
        "The final answer must explicitly state whether the validation command succeeded, where metrics/audit/bad-case files were written, the weakest slice, and whether to deepen/repair/compare/prune.\n\n"
        f"Mode: {args.mode}\n"
        f"Project: {args.project}\n"
        f"Method: {args.method}\n"
        f"Repo: {repo}\n"
        f"Conda env: {args.env_name or 'project/default'}\n"
        f"Validation command from repo root: {args.command}\n"
        f"Max repair/validation rounds: {args.max_rounds}\n"
        f"Research/coding request: {args.request}\n\n"
        f"Queued web guidance for the autonomous main agent:\n{guidance_text}\n\n"
        f"Trial context JSON, if any:\n{trial_context}\n\n"
        f"Selected current-Find execution contract:\n{selected_plan_context(args.project)}\n\n"
        f"Persisted native trajectory context:\n{trajectory_context(args.project)}\n"
    )



def run_claude_backend(args: argparse.Namespace, cfg: dict, repo: Path) -> dict:
    claude = find_claude(cfg)
    if not claude:
        return {"return_code": 2, "stderr": "claude-not-found", "repair_success": False}
    prompt = build_prompt("Claude Code", args, repo, read_trial_context(args.trial_json))
    agent_id = f"claude_coding_{args.method}".replace("/", "_")[:80]
    upsert_agent(
        args.project,
        agent_id,
        name=f"Claude Code: {args.method}",
        role="claude-worker",
        stage="experiment",
        status="running",
        goal=args.request[:500] or f"{args.mode} {args.method}",
        parent_id="main",
        current_step="starting Claude coding backend",
    )
    prompt_path = build_paths(args.project).state / f"claude_coding_prompt_{args.method.replace('/', '_')[:80]}.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    proc = run([
        sys.executable, str(SCRIPTS / "claude_project_session.py"),
        "--project", args.project,
        "--stage", f"coding:{args.mode}:{args.method}",
        "--message-file", str(prompt_path),
        "--timeout-sec", str(coding_timeout(cfg)),
        "--agent-id", agent_id,
        "--repo-path", str(repo),
    ], ROOT, timeout=claude_outer_timeout(cfg), project=args.project, cfg=cfg)
    for line in (proc.stdout or "").splitlines()[-80:]:
        append_agent_log(args.project, agent_id, line)
    state_path = build_paths(args.project).state / "claude_project_session_last_result.json"
    state = load_json(state_path, {})
    mark_agent(args.project, agent_id, "done" if proc.returncode == 0 else "error", current_step=f"Claude coding backend return_code={proc.returncode}")
    return {
        "return_code": proc.returncode,
        "stdout": (state.get("stdout") or proc.stdout)[-6000:],
        "stderr": (state.get("stderr") or proc.stderr)[-6000:],
        "repair_success": proc.returncode == 0,
        "binary": claude,
        "persistent_session": True,
        "session_id": state.get("session_id", ""),
        "session_state_path": str(state_path),
        "resume_command": state.get("resume_command", ""),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--method", required=True)
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--env-name", default="")
    parser.add_argument("--request", default="")
    parser.add_argument("--mode", choices=["implement", "repair"], default="repair")
    parser.add_argument("--trial-json", default="")
    parser.add_argument("--max-rounds", type=int, default=2)
    parser.add_argument("--backend", default="")
    args = parser.parse_args()
    guard_rc = guard_fresh_base_blocker_entry(args.project, '', Path(__file__).name)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    requested_backend, backend, fallback_reason = resolve_backend(cfg, args.backend)
    repo = Path(args.repo_path).resolve()
    out = {
        "project": args.project,
        "method": args.method,
        "requested_backend": requested_backend,
        "backend": backend,
        "backend_fallback_reason": fallback_reason,
        "repo_path": str(repo),
        "request": args.request,
        "mode": args.mode,
        "trial_json": args.trial_json,
        "timeout_sec": coding_timeout(cfg),
    }

    out.update(run_claude_backend(args, cfg, repo))

    out_path = paths.state / f"coding_agent_{args.method}.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(out_path)
    if int(out.get("return_code", 1) or 0) != 0:
        raise SystemExit(int(out.get("return_code", 1) or 1))


if __name__ == "__main__":
    main()
