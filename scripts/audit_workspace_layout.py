#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from project_config import project_target_venue

ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except Exception:
        return str(path)


def file_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file())


def check_projects_projects(issues: list[dict[str, Any]], notes: list[dict[str, Any]]) -> None:
    bad = ROOT / "projects" / "projects"
    if bad.exists():
        issues.append({
            "severity": "block",
            "code": "nested_projects_root",
            "path": rel(bad),
            "message": "Found projects/projects; this is almost always caused by passing a path as --project instead of a project id.",
            "file_count": file_count(bad),
            "repair": "Quarantine this directory after confirming no active state references it; rerun commands with --project <project_id> only.",
        })
    quarantine = ROOT / "backups" / "quarantine_20260520_cleanup" / "projects_projects"
    if quarantine.exists():
        notes.append({
            "code": "nested_projects_root_quarantined",
            "path": rel(quarantine),
            "message": "A previous projects/projects orphan has been moved to reversible quarantine.",
            "file_count": file_count(quarantine),
        })


def check_taste_dirs(issues: list[dict[str, Any]], notes: list[dict[str, Any]]) -> None:
    integrated = ROOT / "modules" / "taste"
    upstream = ROOT / "third_party" / "TASTE"
    old_tmp = ROOT / "tmp" / "upstream"
    if not integrated.exists():
        issues.append({
            "severity": "block",
            "code": "missing_integrated_taste",
            "path": rel(integrated),
            "message": "Integrated runtime module is missing.",
        })
    if not upstream.exists():
        issues.append({
            "severity": "warn",
            "code": "missing_taste_upstream_reference",
            "path": rel(upstream),
            "message": "Upstream TASTE reference checkout is missing; this weakens provenance/sync audits but does not stop runtime if modules/taste exists.",
        })
    if old_tmp.exists():
        issues.append({
            "severity": "warn",
            "code": "stale_tmp_taste_upstream",
            "path": rel(old_tmp),
            "message": "Old temporary TASTE upstream checkout still exists outside the integrated runtime and third_party provenance locations.",
            "repair": "Move to backups/quarantine_* after confirming no references.",
        })
    auto_update = integrated / "auto_research" / "auto_update"
    if not auto_update.exists():
        issues.append({
            "severity": "warn",
            "code": "taste_auto_update_not_integrated",
            "path": rel(auto_update),
            "message": "Integrated The workflow is missing auto_update local-index builders from the newer upstream.",
            "repair": "Sync third_party/TASTE/auto_research/auto_update into modules/taste/auto_research/auto_update.",
        })
    local_index = integrated / "auto_research" / "local_database"
    if local_index.exists():
        notes.append({
            "code": "taste_local_database_present",
            "path": rel(local_index),
            "message": "Integrated The workflow has a local database directory for full venue/category-guided scans.",
            "file_count": file_count(local_index),
        })


def check_root_mirrors(issues: list[dict[str, Any]]) -> None:
    for name in ["App.tsx", "project_bridge.py", "styles.css"]:
        path = ROOT / name
        if path.exists():
            issues.append({
                "severity": "warn",
                "code": "stale_root_web_mirror",
                "path": rel(path),
                "message": f"Found root-level {name}; web code should live under modules/taste/auto_research/web, so this may be a stale mirror.",
                "repair": "Compare with canonical web file before quarantining; do not edit this mirror.",
            })


def check_paper_outputs(project: str, venue: str, issues: list[dict[str, Any]], notes: list[dict[str, Any]]) -> None:
    project_root = ROOT / "projects" / project
    metadata = load_json(project_root / "paper" / "metadata" / "paper_pipeline.json", {})
    output_root = project_root / "paper" / "output"
    active_slug = str(metadata.get("venue_slug") or venue or "").lower() if isinstance(metadata, dict) else str(venue or "").lower()
    pdf_path = Path(str(metadata.get("pdf_path") or metadata.get("conference_preview_pdf") or "")) if isinstance(metadata, dict) else Path("")
    if pdf_path and str(pdf_path) != "." and pdf_path.exists():
        notes.append({
            "code": "active_pdf_exists",
            "path": rel(pdf_path),
            "message": "Active paper metadata points to an existing PDF.",
            "bytes": pdf_path.stat().st_size,
        })
    elif isinstance(metadata, dict):
        fallback_pdfs = [
            output_root / active_slug / "paper.pdf" if active_slug else None,
            output_root / str(venue or "").lower() / "paper.pdf" if venue else None,
        ]
        fallback_pdf = next((candidate for candidate in fallback_pdfs if candidate and candidate.exists()), None)
        if fallback_pdf:
            notes.append({
                "code": "active_pdf_preview_found_without_metadata_pointer",
                "path": rel(fallback_pdf),
                "message": "Paper metadata lacks an active PDF pointer, but the current venue preview PDF exists. This is project runtime state, not a GitHub release warning.",
                "bytes": fallback_pdf.stat().st_size,
            })
        else:
            notes.append({
                "code": "active_pdf_preview_not_found",
                "path": str(pdf_path) if str(pdf_path) != "." else "",
                "message": "No current paper preview PDF was found. This affects the local project demo state only and is not a framework release warning.",
            })
    active_output = output_root / active_slug if active_slug else None
    legacy_cikm = output_root / "cikm-2026"
    if legacy_cikm.exists() and (not active_output or legacy_cikm != active_output):
        notes.append({
            "code": "legacy_venue_output_present",
            "path": rel(legacy_cikm),
            "message": "Legacy CIKM-2026 output exists. Keep only if needed for historical audit; current active output should match the configured target venue output directory.",
            "file_count": file_count(legacy_cikm),
        })
    raw_pdf = output_root / (active_slug or venue.lower()) / "paper_orchestra_raw.pdf"
    final_pdf = output_root / (active_slug or venue.lower()) / "paper.pdf"
    if raw_pdf.exists() and final_pdf.exists():
        notes.append({
            "code": "raw_and_final_pdf_present",
            "raw_pdf": rel(raw_pdf),
            "final_pdf": rel(final_pdf),
            "message": "Raw writing PDF and TASTE final/preview PDF both exist; UI should prefer paper.pdf and expose raw only as audit evidence.",
        })


def git_lines(args: list[str]) -> list[str]:
    try:
        proc = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=ROOT, text=True, capture_output=True, timeout=30)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _framework_change_paths(project: str, rows: list[str]) -> list[dict[str, str]]:
    prefixes = (
        ".claude/",
        "automation/",
        "modules/taste/",
        "modules/writing/",
        "prompts/",
        "scripts/",
        "templates/",
    )
    exact = {
        ".gitignore",
        "AGENTS.md",
        "README.md",
        "START_HERE.md",
        "config.example.json",
    }
    changes: list[dict[str, str]] = []
    for row in rows:
        parts = row.split("	")
        if not parts:
            continue
        status = parts[0].strip()
        candidate = parts[-1].strip()
        if candidate in exact or candidate.startswith(prefixes):
            changes.append({"status": status, "path": candidate})
    return changes


def check_git_hygiene(project: str, issues: list[dict[str, Any]], notes: list[dict[str, Any]]) -> None:
    root_gitignore = ROOT / ".gitignore"
    if not root_gitignore.exists():
        issues.append({"severity": "warn", "code": "missing_root_gitignore", "message": "module root .gitignore is missing."})
    else:
        gitignore_text = root_gitignore.read_text(encoding="utf-8")
        if "!projects/*/HANDOFF.md" in gitignore_text:
            issues.append({
                "severity": "warn",
                "code": "handoff_reallowed_in_gitignore",
                "path": rel(root_gitignore),
                "message": "Root .gitignore still allows project HANDOFF.md; takeover state should use 工作状态.txt.",
            })

    tracked = git_lines(["ls-files"])
    tracked_set = set(tracked)
    forbidden_prefixes = (
        "local_imports_from_mac/",
        "third_party/",
        "modules/taste/auto_research/web/client/dist/",
        f"projects/{project}/state/",
        f"projects/{project}/planning/",
        f"projects/{project}/paper/",
        f"projects/{project}/artifacts/",
        f"projects/{project}/logs/",
        f"projects/{project}/repos/",
        f"projects/{project}/datasets/",
        f"projects/{project}/experiments/",
        f"projects/{project}/discover/",
        f"projects/{project}/reports/",
        f"projects/{project}/wiki/",
        f"projects/{project}/raw/",
    )
    forbidden_suffixes = (".orig", ".rej", ".pyc", ".log", ".pt", ".pth", ".ckpt", ".pdf", ".zip", ".tar", ".tar.gz")
    bad_tracked = [
        path for path in tracked
        if path.startswith(forbidden_prefixes) or path.endswith(forbidden_suffixes) or path == f"projects/{project}/HANDOFF.md"
    ]
    if bad_tracked:
        issues.append({
            "severity": "warn",
            "code": "generated_or_external_files_tracked",
            "message": "Git is tracking generated, external, or project research-object files that should stay out of the framework history.",
            "paths": bad_tracked[:40],
            "count": len(bad_tracked),
        })

    required_tracked = [
        ".gitignore",
        "AGENTS.md",
        "modules/taste/auto_research/web/project_bridge.py",
        "modules/taste/auto_research/web/client/src/App.tsx",
        "modules/taste/auto_research/web/client/src/styles.css",
        "scripts/project_config.py",
        "scripts/pipeline_guard.py",
        "scripts/run_safe_unblock.py",
        "scripts/run_supervision_tick.py",
        "scripts/run_full_research_cycle.py",
        "scripts/audit_reference_reproduction.py",
        "scripts/audit_workspace_layout.py",
        "scripts/build_blocker_action_plan.py",
        "scripts/create_project.py",
        "scripts/setup_git_guardrails.py",
        "templates/project.json",
        "config.example.json",
        "README.md",
        "START_HERE.md",
    ]
    missing_tracked = [path for path in required_tracked if path not in tracked_set]
    if missing_tracked:
        issues.append({
            "severity": "warn",
            "code": "important_files_not_tracked",
            "message": "Important framework/config/status files are not tracked by Git.",
            "paths": missing_tracked,
        })

    private_runtime_paths = [
        f"projects/{project}/project.json",
        f"projects/{project}/AGENTS.md",
        f"projects/{project}/activate_env.sh",
        "工作状态.txt",
        "config.json",
    ]
    existing_private = [item for item in private_runtime_paths if (ROOT / item).exists()]
    ignored_private = set(git_lines(["check-ignore", "--", *existing_private])) if existing_private else set()
    not_ignored_private = [item for item in existing_private if item not in ignored_private]
    if not_ignored_private:
        issues.append({
            "severity": "warn",
            "code": "private_runtime_files_not_ignored",
            "message": "Machine/project-private runtime files exist but are not ignored by Git; publishing would risk leaking local project state or secrets.",
            "paths": not_ignored_private,
        })
    else:
        notes.append({
            "code": "private_runtime_files_ignored",
            "message": "Project-private config/status files are intentionally ignored for GitHub release. Use templates/project.json and scripts/create_project.py after cloning.",
            "paths": existing_private,
        })

    tracked_changes = _framework_change_paths(project, git_lines(["diff", "--name-status"]))
    if tracked_changes:
        issues.append({
            "severity": "warn",
            "code": "tracked_framework_changes_uncommitted",
            "message": "Tracked framework/config files contain an uncommitted repair batch. This is formal source/config work, not disposable scratch; commit it after review or explicitly revert selected paths.",
            "paths": [f"{row['status']} {row['path']}" for row in tracked_changes[:80]],
            "count": len(tracked_changes),
        })
        deleted = [row["path"] for row in tracked_changes if row.get("status", "").startswith("D")]
        if deleted:
            notes.append({
                "code": "deleted_legacy_framework_entries",
                "message": "Deleted tracked framework entries are part of the repair batch. Verify they have no live references before committing; current audit keeps them visible instead of silently hiding the deletion.",
                "paths": deleted[:40],
                "count": len(deleted),
            })

    untracked_source = [
        path for path in git_lines(["ls-files", "--others", "--exclude-standard"])
        if path.startswith(("scripts/", "modules/taste/auto_research/", "modules/taste/tests/", "modules/writing/", ".claude/", "automation/", "prompts/", "templates/"))
        and not path.endswith((".orig", ".rej"))
    ]
    if untracked_source:
        issues.append({
            "severity": "warn",
            "code": "untracked_framework_source",
            "message": "Formal framework source candidates are not yet tracked by Git; review them as new framework modules/tests and add them to version control when accepting this repair batch. Do not ignore or quarantine them unless a later audit proves a file is obsolete.",
            "paths": untracked_source[:40],
            "count": len(untracked_source),
        })

    ignored_temp_all = [
        path for path in git_lines(["ls-files", "--others", "--ignored", "--exclude-standard"])
        if path.endswith((".orig", ".rej"))
    ]
    historical_prefixes = (
        "backups/",
        f"projects/{project}/artifacts/",
        f"projects/{project}/archive/",
    )
    current_ignored_temp = [
        path for path in ignored_temp_all
        if not path.startswith(historical_prefixes)
    ]
    historical_ignored_temp = [
        path for path in ignored_temp_all
        if path.startswith(historical_prefixes)
    ]
    if current_ignored_temp:
        notes.append({
            "code": "current_ignored_patch_temps_present",
            "message": "Current workspace .orig/.rej patch leftovers are present outside quarantine/evidence directories and can confuse manual inspection.",
            "paths": current_ignored_temp[:20],
            "count": len(current_ignored_temp),
        })
    if historical_ignored_temp:
        notes.append({
            "code": "historical_quarantine_patch_temps_present",
            "message": "Historical .orig/.rej files remain only inside quarantine or project evidence directories; keep them as audit evidence unless an authorized cleanup receipt removes them.",
            "paths": historical_ignored_temp[:20],
            "count": len(historical_ignored_temp),
        })

    project_root = ROOT / "projects" / project
    selected = project_root / "repos" / "selected"
    git_repos = sorted(path for path in selected.glob("*/.git") if path.is_dir()) if selected.exists() else []
    notes.append({
        "code": "selected_repo_git_count",
        "path": rel(selected),
        "message": "Selected research repos with their own git repositories.",
        "count": len(git_repos),
    })
    missing_repo_gitignore = []
    for repo_git in git_repos[:20]:
        repo = repo_git.parent
        if not (repo / ".gitignore").exists():
            missing_repo_gitignore.append(rel(repo))
    if missing_repo_gitignore:
        notes.append({
            "code": "selected_repo_missing_gitignore_runtime_note",
            "message": "Some project-local downloaded research repos lack a repo-local .gitignore guardrail. They are ignored by the framework GitHub release; add guardrails before long local experiment iteration, but do not publish them.",
            "paths": missing_repo_gitignore[:20],
            "count": len(missing_repo_gitignore),
        })


def write_report(project: str, venue: str, issues: list[dict[str, Any]], notes: list[dict[str, Any]]) -> Path:
    report_dir = ROOT / "reports"
    state_dir = ROOT / "state"
    report_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "project": project,
        "venue": venue,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "blocked" if any(item.get("severity") == "block" for item in issues) else ("warn" if issues else "pass"),
        "issue_count": len(issues),
        "block_count": sum(1 for item in issues if item.get("severity") == "block"),
        "warning_count": sum(1 for item in issues if item.get("severity") == "warn"),
        "issues": issues,
        "notes": notes,
    }
    (state_dir / "workspace_layout_audit.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = report_dir / "workspace_layout_audit.md"
    lines = ["# Workspace Layout Audit\n\n", f"- status: {payload['status']}\n", f"- project: {project}\n", f"- venue: {venue}\n", f"- generated_at: {payload['generated_at']}\n", f"- issues: {payload['issue_count']} (blocks={payload['block_count']}, warnings={payload['warning_count']})\n\n"]
    if issues:
        lines.append("## Issues\n")
        for item in issues:
            lines.append(f"- [{item.get('severity')}] {item.get('code')}: {item.get('message')} path={item.get('path', '')}\n")
            if item.get("repair"):
                lines.append(f"  repair: {item['repair']}\n")
            issue_paths = item.get("paths")
            if isinstance(issue_paths, list) and issue_paths:
                for issue_path in issue_paths[:40]:
                    lines.append(f"  - {issue_path}\n")
                count = int(item.get("count") or len(issue_paths))
                if count > min(40, len(issue_paths)):
                    lines.append(f"  - ... {count - min(40, len(issue_paths))} more\n")
    else:
        lines.append("No layout issues detected.\n")
    if notes:
        lines.append("\n## Notes\n")
        for item in notes:
            path = item.get("path") or item.get("final_pdf") or ""
            lines.append(f"- {item.get('code')}: {item.get('message')} {path}\n")
    report.write_text("".join(lines), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit TASTE workspace layout for duplicate roots, stale mirrors, TASTE integration, and paper-output pointer risks.")
    parser.add_argument("--project", default=os.environ.get("PROJECT_ID") or os.environ.get("DEFAULT_PROJECT_ID") or "")
    parser.add_argument("--venue", default="")
    args = parser.parse_args()
    if not str(args.project or "").strip():
        raise SystemExit("project is required; pass --project or set PROJECT_ID")
    venue = str(args.venue or "").strip() or project_target_venue(args.project, "ICLR")

    issues: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    check_projects_projects(issues, notes)
    check_taste_dirs(issues, notes)
    check_root_mirrors(issues)
    check_paper_outputs(args.project, venue, issues, notes)
    check_git_hygiene(args.project, issues, notes)
    report = write_report(args.project, venue, issues, notes)
    print(report)
    print(f"status={'blocked' if any(item.get('severity') == 'block' for item in issues) else ('warn' if issues else 'pass')} issues={len(issues)}")


if __name__ == "__main__":
    main()
