#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
from typing import Any

from project.project_paths import build_paths, load_project_config
from reporting.work_status import append_session_snapshot_status
from runtime.framework_io import read_json as load_json


def count_json(path: Path) -> int:
    data = load_json(path, [])
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("readings", "items", "articles", "ideas", "plans", "read_results"):
            value = data.get(key)
            if isinstance(value, list):
                return len(value)
        return len(data)
    return 0


def compact(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).replace("\n", " ").strip() or default


def main() -> None:
    parser = argparse.ArgumentParser(description="Append a compact TASTE takeover snapshot to 工作状态.txt.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    state = paths.state
    planning = paths.planning / "finding"
    full = load_json(state / "full_research_cycle.json", {})
    blocker = load_json(state / "blocker_action_plan.json", {})
    find_plan = load_json(state / "current_find_research_plan.json", {})
    job = load_json(state / "fresh_base_reference_full_reproduction_job.json", {})

    full_status = "unknown"
    blocker_category = "none"
    repo = "unknown"
    if isinstance(full, dict):
        full_status = compact(full.get("status") or full.get("full_status"), "unknown")
        current_blocker = full.get("current_blocker", {}) if isinstance(full.get("current_blocker"), dict) else {}
        blocker_category = compact(current_blocker.get("category"), "none")
        fresh = full.get("fresh_base", {}) if isinstance(full.get("fresh_base"), dict) else {}
        repo = compact(fresh.get("repo_name") or fresh.get("repo_path"), "unknown")
    top_route = "none"
    if isinstance(blocker, dict):
        summary = blocker.get("summary", {}) if isinstance(blocker.get("summary"), dict) else {}
        top_route = compact(summary.get("top_route"), "none")

    paper_cfg = cfg.get("paper", {}) if isinstance(cfg.get("paper"), dict) else {}
    venue = compact(args.venue or cfg.get("target_venue") or cfg.get("venue") or paper_cfg.get("target_venue"), "ICLR").upper()
    find_run = compact(find_plan.get("find_run_id") or find_plan.get("run_id"), "unknown") if isinstance(find_plan, dict) else "unknown"
    job_status = compact(job.get("status"), "") if isinstance(job, dict) else ""
    job_pid = compact(job.get("pid"), "") if isinstance(job, dict) else ""

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "project": args.project,
        "target_venue": venue,
        "status": full_status,
        "blocker": blocker_category,
        "top_route": top_route,
        "find_run_id": find_run,
        "main_base": repo,
        "readings": count_json(planning / 'read_results.json'),
        "ideas": count_json(planning / 'ideas.json'),
        "plans": count_json(planning / 'plans.json'),
        "full_job_status": job_status,
        "full_job_pid": job_pid,
    }
    append_session_snapshot_status(args.project, payload)
    print(paths.work_status)


if __name__ == "__main__":
    main()
