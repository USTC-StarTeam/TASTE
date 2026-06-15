#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths
from taste_pythonpath import resolve_script_path, taste_pythonpath_string


def _cmd(script: str, *args: str) -> list[str]:
    return [sys.executable, str(resolve_script_path(script, ROOT)), *args]


def build_steps(project: str, venue: str = "") -> list[tuple[str, list[str]]]:
    health = _cmd("research_healthcheck.py", "--project", project)
    status = _cmd("report_status.py", "--project", project)
    trajectory = _cmd("build_research_trajectory_system.py", "--project", project, "--skip-helpers")
    if venue:
        health.extend(["--venue", venue])
        status.extend(["--venue", venue])
        trajectory.extend(["--venue", venue])
    return [
        ("healthcheck", health),
        ("status", status),
        ("next_actions", _cmd("propose_next_actions.py", "--project", project)),
        ("trajectory", trajectory),
        ("reflection", _cmd("reflect_iteration.py", "--project", project)),
        ("shared_research", _cmd("compile_prompt.py", "--project", project)),
    ]


def _refresh_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = taste_pythonpath_string(ROOT, env.get("PYTHONPATH", ""))
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _normalize_taste_root_keys(value: Any, canonical_root: str) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if key == "taste_root" and isinstance(item, str) and item != canonical_root:
                normalized[key] = canonical_root
                changed = True
                continue
            new_item, item_changed = _normalize_taste_root_keys(item, canonical_root)
            normalized[key] = new_item
            changed = changed or item_changed
        return normalized, changed
    if isinstance(value, list):
        changed = False
        normalized_items: list[Any] = []
        for item in value:
            new_item, item_changed = _normalize_taste_root_keys(item, canonical_root)
            normalized_items.append(new_item)
            changed = changed or item_changed
        return normalized_items, changed
    return value, False


def normalize_project_metadata(paths: Any, *, canonical_root: Path = ROOT) -> dict[str, Any]:
    canonical = str(canonical_root)
    changed_files: list[str] = []
    for name in ["finding_frontend.json", "taste_sync.json"]:
        path = paths.state / name
        if not path.exists():
            continue
        payload = _load_json(path)
        normalized, changed = _normalize_taste_root_keys(payload, canonical)
        if changed:
            path.write_text(json.dumps(normalized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            changed_files.append(str(path))
    return {"canonical_taste_root": canonical, "changed_files": changed_files}


def run_steps(steps: list[tuple[str, list[str]]], *, cwd: Path = ROOT) -> list[dict[str, Any]]:
    env = _refresh_env()
    results: list[dict[str, Any]] = []
    for name, cmd in steps:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, text=True, capture_output=True)
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        print(f"[{name}] rc={proc.returncode}: " + " ".join(cmd), flush=True)
        if stdout:
            print(stdout, flush=True)
        if stderr:
            print(stderr, file=sys.stderr, flush=True)
        results.append({
            "step": name,
            "returncode": proc.returncode,
            "command": cmd,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
        })
        if proc.returncode != 0:
            break
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh project-facing status reports and their derived reflection artifacts in dependency order.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    args = parser.parse_args()

    project = str(args.project or "").strip()
    venue = str(args.venue or "").strip()
    paths = build_paths(project)
    paths.state.mkdir(parents=True, exist_ok=True)
    paths.reports.mkdir(parents=True, exist_ok=True)

    metadata_normalization = normalize_project_metadata(paths)
    if metadata_normalization["changed_files"]:
        print("[metadata] normalized taste_root in " + ", ".join(metadata_normalization["changed_files"]), flush=True)
    results = run_steps(build_steps(project, venue))
    payload = {"project": project, "venue": venue, "metadata_normalization": metadata_normalization, "steps": results}
    (paths.state / "project_report_refresh.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    failed = next((row for row in results if row.get("returncode") != 0), None)
    if failed:
        raise SystemExit(int(failed.get("returncode") or 1))
    print(paths.state / "project_report_refresh.json")


if __name__ == "__main__":
    main()
