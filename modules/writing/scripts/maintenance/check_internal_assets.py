#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path as _WritingDevPath
import sys as _writing_sys
_WRITING_SCRIPT_ROOT = next((p for p in _WritingDevPath(__file__).resolve().parents if p.name == "scripts"), _WritingDevPath(__file__).resolve().parent)
for _writing_path in [_WRITING_SCRIPT_ROOT, *[p for p in _WRITING_SCRIPT_ROOT.iterdir() if p.is_dir()]]:
    _writing_text = str(_writing_path)
    if _writing_text not in _writing_sys.path:
        _writing_sys.path.insert(0, _writing_text)

import argparse
import json
from pathlib import Path
from typing import Any

from writing_paths import MODULE_ROOT

REQUIRED_SKILLS = [
    "taste-paper-writing",
    "venue-intelligence",
    "citation-integrity",
    "writing-quality",
    "paper-orchestra",
    "outline-agent",
    "literature-review-agent",
    "section-writing-agent",
    "content-refinement-agent",
]
REQUIRED_HELPERS = [
    "skills/paper-orchestra/scripts/validate_inputs.py",
    "skills/paper-orchestra/scripts/check_idea_density.py",
    "skills/paper-orchestra/scripts/validate_consistency.py",
    "skills/paper-orchestra/scripts/check_tex_packages.py",
    "skills/paper-orchestra/scripts/anti_leakage_check.py",
    "skills/outline-agent/scripts/validate_outline.py",
    "skills/literature-review-agent/scripts/s2_search.py",
    "skills/literature-review-agent/scripts/validate_pool.py",
    "skills/literature-review-agent/scripts/bibtex_format.py",
    "skills/section-writing-agent/scripts/orphan_cite_gate.py",
    "skills/section-writing-agent/scripts/latex_sanity.py",
]


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(MODULE_ROOT))
    except Exception:
        return str(path)


def collect() -> dict[str, Any]:
    skills_root = MODULE_ROOT / "skills"
    skill_rows = []
    helper_rows = []
    blockers: list[str] = []
    for name in REQUIRED_SKILLS:
        path = skills_root / name / "SKILL.md"
        ok = path.exists() and path.read_text(encoding="utf-8", errors="replace").strip()
        skill_rows.append({"name": name, "path": rel(path), "ready": bool(ok)})
        if not ok:
            blockers.append(f"缺少内部 skill: {name}")
    for item in REQUIRED_HELPERS:
        path = MODULE_ROOT / item
        ok = path.exists() and path.is_file()
        helper_rows.append({"path": rel(path), "ready": bool(ok)})
        if not ok:
            blockers.append(f"缺少内部 helper: {item}")
    return {
        "module": "writing",
        "module_root": str(MODULE_ROOT),
        "status": "ok" if not blockers else "blocked",
        "required_ready": not blockers,
        "skills": skill_rows,
        "helpers": helper_rows,
        "blockers": blockers,
        "policy": "vendor 目录已移除；运行时只使用 writing/skills 和 writing/scripts 内部资产。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 writing 内部写作 skill 和 helper 是否齐全。")
    parser.add_argument("--venue", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--paper-orchestra-dir", default="")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--check", action="store_true")
    args, _unknown = parser.parse_known_args()
    payload = collect()
    if args.venue:
        payload["venue"] = args.venue
    if args.project:
        payload["project"] = args.project
    print(json.dumps(payload, ensure_ascii=False, indent=None if args.compact else 2))
    return 0 if payload["required_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
