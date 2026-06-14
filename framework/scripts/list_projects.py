#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

def _repo_root_from_script() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "framework").is_dir() and (candidate / "modules").is_dir() and (candidate / "web").is_dir():
            return candidate
    return current.parents[1]

ROOT = _repo_root_from_script()
PROJECTS = ROOT / "projects"


def main() -> None:
    rows = []
    for project_dir in sorted([p for p in PROJECTS.iterdir() if p.is_dir()]) if PROJECTS.exists() else []:
        cfg_path = project_dir / "project.json"
        if not cfg_path.exists():
            continue
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        rows.append({
            "name": cfg.get("name", project_dir.name),
            "topic": cfg.get("topic", ""),
            "conda_env": cfg.get("conda_env", "base"),
        })
    print(json.dumps(rows, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
