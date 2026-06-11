#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from agent_state import append_agent_log, consume_guidance, mark_agent, upsert_agent
from project_paths import build_paths, load_project_config
from pipeline_guard import guard_fresh_base_blocker_entry

ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _query_placeholder_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _query_looks_like_project_id(value: str, project: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    return bool(_query_placeholder_key(project) and _query_placeholder_key(text) == _query_placeholder_key(project))


def _selected_plan_topic(paths) -> str:
    plans_path = paths.planning / "finding" / "plans.json"
    state_path = paths.state / "current_find_research_plan.json"
    try:
        plans = json.loads(plans_path.read_text(encoding="utf-8")) if plans_path.exists() else {}
    except Exception:
        plans = {}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except Exception:
        state = {}
    selected_id = str((plans.get("selected_plan_id") if isinstance(plans, dict) else "") or (state.get("selected_plan_id") if isinstance(state, dict) else "") or "").strip()
    for row in (plans.get("plans") if isinstance(plans, dict) else []) or []:
        if not isinstance(row, dict):
            continue
        plan_id = str(row.get("plan_id") or row.get("id") or "").strip()
        if selected_id and plan_id != selected_id:
            continue
        for key in ["title", "idea_title", "hypothesis", "experiment_name", "summary"]:
            value = str(row.get(key) or "").strip()
            if value:
                return value
    return ""


def effective_loop_topic(project: str, args_topic: str | None, prompt: str | None, cfg: dict, paths) -> str:
    candidates = [prompt, args_topic, cfg.get("topic", ""), cfg.get("title", ""), cfg.get("research_interest", ""), cfg.get("user_prompt", ""), _selected_plan_topic(paths)]
    for value in candidates:
        text = str(value or "").strip()
        if text and not _query_looks_like_project_id(text, project):
            return text
    return ""


def run(cmd: list[str], log_path: Path, project: str = "", agent_id: str = "main", stage: str = "") -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    if project:
        upsert_agent(project, agent_id, status="running", stage=stage or "autonomous", current_step=" ".join(cmd[:4]), command=cmd)
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    if project:
        upsert_agent(project, agent_id, pid=proc.pid, status="running", current_step="subprocess started")
    assert proc.stdout is not None
    for line in proc.stdout:
        chunks.append(line)
        print(line, end="", flush=True)
        if project:
            append_agent_log(project, agent_id, line.rstrip())
    returncode = proc.wait()
    log_path.write_text("".join(chunks) + "\n--- STDERR MERGED INTO STDOUT ---\n", encoding="utf-8")
    return returncode

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--prompt")
    parser.add_argument("--topic")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--max-results", type=int)
    parser.add_argument("--discover-retries", type=int)
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--skip-semantic-scholar", action="store_true")
    parser.add_argument("--skip-github", action="store_true")
    parser.add_argument("--skip-initialization", action="store_true")
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--parallel-method", action="append", default=[])
    parser.add_argument("--benchmark")
    parser.add_argument("--metric")
    parser.add_argument("--dataset")
    parser.add_argument("--repo-name")
    parser.add_argument("--repo-path")
    parser.add_argument("--command-template")
    parser.add_argument("--execute-plan", action="store_true")
    parser.add_argument("--prepare-env", action="store_true")
    parser.add_argument("--real-bootstrap-env", action="store_true")
    parser.add_argument("--max-launches", type=int)
    parser.add_argument("--conda-env", default="")
    parser.add_argument("--coding-backend", default="")
    parser.add_argument("--venue", default="")
    parser.add_argument("--trajectory-rounds", type=int, default=1)
    parser.add_argument("--skip-trajectory-supervisor", action="store_true")
    parser.add_argument("--deep-literature-survey", action="store_true", help="Run the finding literature step in full survey mode.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name)
    if guard_rc is not None:
        return guard_rc
    agent_id = "main"
    upsert_agent(
        args.project,
        agent_id,
        name="TASTE 自主科研主控",
        role="main",
        stage="autonomous",
        status="running",
        goal=(args.prompt or args.topic or "autonomous research loop")[:500],
        current_step="initializing workspace",
    )
    guidance = consume_guidance(args.project, target_agent_id=agent_id, stage="experiment")
    if guidance:
        guidance_text = "\n".join(f"- {item.get('message', '')}" for item in guidance if item.get("message"))
        args.prompt = ((args.prompt or args.topic or "") + "\n\nQueued web guidance:\n" + guidance_text).strip()
    init_cmd = [sys.executable, str(ROOT / "scripts" / "init_workspace.py"), "--project", args.project, "--conda-env", args.conda_env]
    init_topic = args.topic if not _query_looks_like_project_id(args.topic or "", args.project) else ""
    if init_topic:
        init_cmd.extend(["--topic", init_topic])
    if args.prompt:
        init_cmd.extend(["--prompt", args.prompt])
    code = run(init_cmd, ROOT / "logs" / f"{args.project}_00_init_workspace.log", args.project, agent_id, "init")
    if code != 0:
        mark_agent(args.project, agent_id, "error", current_step=f"init_workspace failed with exit code {code}")
        return code

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    request_payload = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "project": args.project,
        "prompt": args.prompt or "",
        "topic": effective_loop_topic(args.project, args.topic, args.prompt, cfg, paths),
        "iterations": args.iterations,
        "coding_backend": args.coding_backend or cfg.get("coding_agent", {}).get("backend", ""),
    }
    requests = load_json(paths.state / "natural_language_requests.json")
    if not isinstance(requests, list):
        requests = []
    requests.append(request_payload)
    save_json(paths.state / "natural_language_requests.json", requests)

    if args.iterations <= 0:
        mark_agent(args.project, agent_id, "done", current_step="request recorded; no iterations requested")
        return 0

    topic = effective_loop_topic(args.project, args.topic, args.prompt, cfg, paths)
    for idx in range(args.iterations):
        run_cmd = [sys.executable, str(ROOT / "scripts" / "run_project.py"), "--project", args.project]
        if topic:
            run_cmd.extend(["--topic", topic])
        if args.max_results is not None:
            run_cmd.extend(["--max-results", str(args.max_results)])
        if args.discover_retries is not None:
            run_cmd.extend(["--discover-retries", str(args.discover_retries)])
        if args.skip_llm:
            run_cmd.append("--skip-llm")
        if args.skip_semantic_scholar:
            run_cmd.append("--skip-semantic-scholar")
        if args.skip_github:
            run_cmd.append("--skip-github")
        if args.skip_initialization:
            run_cmd.append("--skip-initialization")
        if args.skip_discovery:
            os.environ.setdefault("USE_EXISTING_LITERATURE_PACKET", "1")
            run_cmd.append("--skip-discovery")
        if args.deep_literature_survey:
            run_cmd.append("--deep-literature-survey")
        if args.execute_plan:
            run_cmd.append("--execute-plan")
        if args.prepare_env:
            run_cmd.append("--prepare-env")
        if args.real_bootstrap_env:
            run_cmd.append("--real-bootstrap-env")
        if args.benchmark:
            run_cmd.extend(["--benchmark", args.benchmark])
        if args.metric:
            run_cmd.extend(["--metric", args.metric])
        if args.dataset:
            run_cmd.extend(["--dataset", args.dataset])
        if args.repo_name:
            run_cmd.extend(["--repo-name", args.repo_name])
        if args.repo_path:
            run_cmd.extend(["--repo-path", args.repo_path])
        if args.command_template:
            run_cmd.extend(["--command-template", args.command_template])
        if args.max_launches is not None:
            run_cmd.extend(["--max-launches", str(args.max_launches)])
        if args.coding_backend:
            run_cmd.extend(["--coding-backend", args.coding_backend])
        if args.venue:
            run_cmd.extend(["--venue", args.venue])
        for method in args.parallel_method:
            run_cmd.extend(["--parallel-method", method])
        upsert_agent(args.project, agent_id, status="running", stage="experiment", current_step=f"running autonomous iteration {idx + 1}/{args.iterations}")
        code = run(run_cmd, paths.logs / f"run_loop_iteration_{idx + 1}.log", args.project, agent_id, "experiment")
        if code != 0:
            mark_agent(args.project, agent_id, "error", current_step=f"iteration {idx + 1} failed with exit code {code}")
            return code
        if not args.skip_trajectory_supervisor and args.trajectory_rounds > 0:
            supervisor_cmd = [
                sys.executable, str(ROOT / "scripts" / "run_research_trajectory_supervisor.py"),
                "--project", args.project,
                "--rounds", str(args.trajectory_rounds),
                "--timeout-sec", "14400",
            ]
            if args.venue:
                supervisor_cmd.extend(["--venue", args.venue])
            upsert_agent(args.project, agent_id, status="running", stage="trajectory", current_step=f"running trajectory supervisor after iteration {idx + 1}")
            code = run(supervisor_cmd, paths.logs / f"run_loop_trajectory_supervisor_{idx + 1}.log", args.project, agent_id, "trajectory")
            if code != 0:
                mark_agent(args.project, agent_id, "error", current_step=f"trajectory supervisor after iteration {idx + 1} failed with exit code {code}")
                return code

    run([sys.executable, str(ROOT / "scripts" / "generate_handoff.py"), "--project", args.project], paths.logs / "run_loop_generate_handoff.log", args.project, agent_id, "handoff")
    mark_agent(args.project, agent_id, "done", current_step="autonomous loop complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
