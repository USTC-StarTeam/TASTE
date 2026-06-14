#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from project_paths import ROOT

VENDOR_ROOT = ROOT / "modules" / "writing" / "vendor"
THIRD_PARTY_ROOT = ROOT / "third_party"
PAPER_ORCHESTRA_URL = "https://github.com/Ar9av/PaperOrchestra.git"
ACADEMIC_SKILLS_URL = "https://github.com/Imbad0202/academic-research-skills.git"
NATURE_SKILLS_URL = "https://github.com/Yuan1z0825/nature-skills.git"

PAPER_ORCHESTRA_MARKERS = [
    "skills/paper-orchestra/SKILL.md",
    "skills/section-writing-agent/SKILL.md",
    "skills/literature-review-agent/SKILL.md",
]
ACADEMIC_SKILLS_MARKERS = [
    "academic-paper/SKILL.md",
    "academic-paper-reviewer/SKILL.md",
]
NATURE_REFERENCE_MARKERS = [
    "skills/nature-writing/SKILL.md",
    "skills/nature-polishing/SKILL.md",
    "skills/nature-data/SKILL.md",
    "skills/nature-reviewer/SKILL.md",
]
NATURE_SKILL_DIRS = [
    "_shared",
    "nature-citation",
    "nature-data",
    "nature-figure",
    "nature-polishing",
    "nature-response",
    "nature-reviewer",
    "nature-writing",
]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except Exception:
        return str(path)


def run(cmd: list[str], *, cwd: Path = ROOT, timeout: int = 600) -> dict[str, Any]:
    started_at = now_iso()
    try:
        proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
        return {
            "cmd": cmd,
            "cwd": relative(cwd),
            "started_at": started_at,
            "finished_at": now_iso(),
            "return_code": proc.returncode,
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "cmd": cmd,
            "cwd": relative(cwd),
            "started_at": started_at,
            "finished_at": now_iso(),
            "return_code": 124,
            "stdout_tail": str(exc.stdout or "")[-4000:],
            "stderr_tail": (str(exc.stderr or "") + f"\nTimed out after {timeout}s")[-4000:],
            "timed_out": True,
        }


def git_info(path: Path) -> dict[str, str]:
    if not (path / ".git").exists():
        return {"commit": "", "origin": ""}
    commit = run(["git", "rev-parse", "HEAD"], cwd=path, timeout=60)
    origin = run(["git", "remote", "get-url", "origin"], cwd=path, timeout=60)
    return {
        "commit": commit.get("stdout_tail", "").strip() if commit.get("return_code") == 0 else "",
        "origin": origin.get("stdout_tail", "").strip() if origin.get("return_code") == 0 else "",
    }


def is_nature_family_venue(venue: str) -> bool:
    normalized = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(venue or ""))
    return "nature" in normalized or "springer-nature" in normalized


def marker_status(target: Path, markers: list[str]) -> tuple[bool, list[str]]:
    missing = [marker for marker in markers if not (target / marker).exists()]
    return not missing, missing


def component_status(name: str, target: Path, markers: list[str], *, required: bool) -> dict[str, Any]:
    ready, missing = marker_status(target, markers)
    info = git_info(target)
    return {
        "name": name,
        "target": relative(target),
        "exists": target.exists(),
        "ready": ready,
        "required": required,
        "missing_markers": missing,
        "commit": info.get("commit", ""),
        "origin": info.get("origin", ""),
    }


def collect_status(
    *,
    vendor_root: Path = VENDOR_ROOT,
    third_party_root: Path = THIRD_PARTY_ROOT,
    paper_orchestra_dir: Path | None = None,
    venue: str = "",
) -> dict[str, Any]:
    paper_dir = paper_orchestra_dir or (vendor_root / "PaperOrchestra")
    nature_required = is_nature_family_venue(venue)
    components = [
        component_status("PaperOrchestra", paper_dir, PAPER_ORCHESTRA_MARKERS, required=True),
        component_status("academic-research-skills", vendor_root / "academic-research-skills", ACADEMIC_SKILLS_MARKERS, required=False),
        component_status("nature_family_writing_reference", vendor_root / "nature_family_writing_reference", NATURE_REFERENCE_MARKERS, required=nature_required),
    ]
    required_missing = [row["name"] for row in components if row.get("required") and not row.get("ready")]
    all_missing = [row["name"] for row in components if not row.get("ready")]
    return {
        "status": "ready" if not required_missing else "blocked",
        "required_ready": not required_missing,
        "all_ready": not all_missing,
        "venue": venue,
        "nature_family_required": nature_required,
        "vendor_root": relative(vendor_root),
        "third_party_root": relative(third_party_root),
        "components": components,
        "missing_required_components": required_missing,
        "missing_components": all_missing,
    }


def clone_if_needed(name: str, url: str, target: Path, markers: list[str], *, check_only: bool, commands: list[dict[str, Any]]) -> None:
    ready, _ = marker_status(target, markers)
    if ready:
        return
    if check_only:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and any(target.iterdir()) and not (target / ".git").exists():
        commands.append({
            "cmd": ["git", "clone", "--depth", "1", url, str(target)],
            "return_code": 2,
            "stderr_tail": f"{relative(target)} exists but is not a recognized {name} snapshot; refusing to overwrite it.",
        })
        return
    if not target.exists() or not any(target.iterdir()):
        commands.append(run(["git", "clone", "--depth", "1", url, str(target)], cwd=ROOT, timeout=900))
        return
    commands.append(run(["git", "pull", "--ff-only"], cwd=target, timeout=300))


def copytree_merge(src: Path, dst: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {name for name in names if name in {".git", "__pycache__", ".pytest_cache"} or name.endswith((".pyc", ".pyo"))}

    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=ignore)


def ensure_nature_reference(third_party_root: Path, vendor_root: Path, *, check_only: bool, refresh: bool, commands: list[dict[str, Any]]) -> None:
    dst = vendor_root / "nature_family_writing_reference"
    ready, _ = marker_status(dst, NATURE_REFERENCE_MARKERS)
    if ready and not refresh:
        return
    if check_only:
        return
    src_repo = third_party_root / "nature-skills"
    if not (src_repo / "skills" / "nature-writing" / "SKILL.md").exists():
        src_repo.parent.mkdir(parents=True, exist_ok=True)
        commands.append(run(["git", "clone", "--depth", "1", NATURE_SKILLS_URL, str(src_repo)], cwd=ROOT, timeout=900))
    if not (src_repo / "skills" / "nature-writing" / "SKILL.md").exists():
        return
    skills_dst = dst / "skills"
    skills_dst.mkdir(parents=True, exist_ok=True)
    for skill_name in NATURE_SKILL_DIRS:
        src = src_repo / "skills" / skill_name
        if src.exists():
            copytree_merge(src, skills_dst / skill_name)
    for filename in ["README.md", "LICENSE", "install.md"]:
        src_file = src_repo / filename
        if src_file.exists():
            shutil.copy2(src_file, dst / filename)


def compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compact = dict(payload)
    commands = []
    for row in payload.get("commands", []):
        if not isinstance(row, dict):
            continue
        commands.append({
            "cmd": row.get("cmd", []),
            "cwd": row.get("cwd", ""),
            "return_code": row.get("return_code", 0),
            "timed_out": bool(row.get("timed_out", False)),
            "stderr_tail": str(row.get("stderr_tail") or "")[-600:],
        })
    compact["commands"] = commands
    return compact


def provenance_from_status(status: dict[str, Any]) -> dict[str, Any]:
    sources: dict[str, Any] = {}
    for row in status.get("components", []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        sources[name] = {
            "target": row.get("target", ""),
            "commit": row.get("commit", ""),
            "origin": row.get("origin", ""),
            "ready": bool(row.get("ready")),
            "required": bool(row.get("required")),
        }
    return {
        "generated_at": now_iso(),
        "policy": "writing vendor references are runtime-only, ignored by Git, and restored by modules/writing/scripts/sync_writing_vendor.py.",
        "sources": sources,
    }


def sync_vendor(
    *,
    vendor_root: Path = VENDOR_ROOT,
    third_party_root: Path = THIRD_PARTY_ROOT,
    paper_orchestra_dir: Path | None = None,
    venue: str = "",
    check_only: bool = False,
    refresh: bool = False,
) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    paper_dir = paper_orchestra_dir or (vendor_root / "PaperOrchestra")
    clone_if_needed("PaperOrchestra", PAPER_ORCHESTRA_URL, paper_dir, PAPER_ORCHESTRA_MARKERS, check_only=check_only, commands=commands)
    clone_if_needed("academic-research-skills", ACADEMIC_SKILLS_URL, vendor_root / "academic-research-skills", ACADEMIC_SKILLS_MARKERS, check_only=check_only, commands=commands)
    ensure_nature_reference(third_party_root, vendor_root, check_only=check_only, refresh=refresh, commands=commands)
    status = collect_status(vendor_root=vendor_root, third_party_root=third_party_root, paper_orchestra_dir=paper_dir, venue=venue)
    status["commands"] = commands
    status["checked_at"] = now_iso()
    if not check_only:
        vendor_root.mkdir(parents=True, exist_ok=True)
        (vendor_root / "WRITING_VENDOR_PROVENANCE.json").write_text(json.dumps(provenance_from_status(status), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore or check runtime-only writing vendor references.")
    parser.add_argument("--check", action="store_true", help="Only inspect required vendor references; do not clone or copy.")
    parser.add_argument("--refresh", action="store_true", help="Refresh the mirrored Nature-family reference from third_party/nature-skills.")
    parser.add_argument("--compact", action="store_true", help="Print compact JSON without long command output tails.")
    parser.add_argument("--venue", default="", help="Current target venue; Nature-family venues require the Nature reference.")
    parser.add_argument("--vendor-root", default=str(VENDOR_ROOT))
    parser.add_argument("--third-party-root", default=str(THIRD_PARTY_ROOT))
    parser.add_argument("--paper-orchestra-dir", default="")
    args = parser.parse_args()

    vendor_root = Path(args.vendor_root).expanduser().resolve()
    third_party_root = Path(args.third_party_root).expanduser().resolve()
    paper_orchestra_dir = Path(args.paper_orchestra_dir).expanduser().resolve() if args.paper_orchestra_dir else vendor_root / "PaperOrchestra"
    payload = sync_vendor(
        vendor_root=vendor_root,
        third_party_root=third_party_root,
        paper_orchestra_dir=paper_orchestra_dir,
        venue=args.venue,
        check_only=args.check,
        refresh=args.refresh,
    )
    if args.compact:
        payload = compact_payload(payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload.get("required_ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
