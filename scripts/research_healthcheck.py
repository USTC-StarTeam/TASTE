#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from llm_client import llm_available, llm_disabled_reason
from paper_common import get_active_paper_state
from project_paths import build_paths, load_project_config


def find_cli_binary(cfg: dict, name: str) -> str:
    import glob
    import os
    import subprocess
    hints = [os.environ.get(f"{name.upper()}_BIN", "")]
    agent_cfg = cfg.get("coding_agent", {}) if isinstance(cfg, dict) else {}
    hints.append(str(agent_cfg.get(f"{name}_path_hint", "") or ""))
    found = shutil.which(name)
    if found:
        hints.append(found)
    try:
        proc = subprocess.run(["bash", "-ic", "printf %s \"$PATH\""], text=True, capture_output=True, timeout=10)
        if proc.returncode == 0:
            for part in proc.stdout.split(":"):
                if part:
                    hints.append(str(Path(part) / name))
    except Exception:
        pass
    root = Path(__file__).resolve().parents[1]
    hints.extend([
        str(root.parent / ".nvm" / "versions" / "node" / "*" / "bin" / name),
        str(Path.home() / ".nvm" / "versions" / "node" / "*" / "bin" / name),
    ])
    expanded = []
    for item in hints:
        if item and "*" in item:
            expanded.extend(glob.glob(item))
        elif item:
            expanded.append(item)
    seen = set()
    for item in expanded:
        if item in seen or not Path(item).exists():
            continue
        seen.add(item)
        env = os.environ.copy()
        env["PATH"] = str(Path(item).parent) + os.pathsep + env.get("PATH", "")
        proc = subprocess.run([item, "--version"], text=True, capture_output=True, env=env)
        if proc.returncode == 0:
            return item
    return ""


def find_codex(cfg: dict) -> str:
    return find_cli_binary(cfg, "codex")


def find_claude(cfg: dict) -> str:
    return find_cli_binary(cfg, "claude")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    issues: list[str] = []
    notes: list[str] = []

    required = [
        paths.agents_file,
        paths.wiki_overview,
        paths.wiki_synthesis / "field-map.md",
        paths.wiki_synthesis / "shared-assumptions.md",
        paths.wiki_gaps / "confirmed-gaps.md",
        paths.wiki_gaps / "hypotheses.md",
        paths.wiki_gaps / "questions.md",
        paths.planning / "init_brief.md",
        paths.planning / "paper_quality.md",
        paths.planning / "workflow_blueprint.md",
        paths.reports / "shared_research.md",
        paths.reports / "workflow_connectivity.md",
        paths.reports / "machine_profile.md",
        paths.root / "paper" / "drafts" / "paper_draft.md",
        paths.root / "paper" / "reviews" / "paper_review_packet.md",
    ]
    for path in required:
        if not path.exists():
            issues.append(f"Missing {path.relative_to(paths.root)}")

    machine_profile = paths.reports / "machine_profile.json"
    if machine_profile.exists():
        profile = json.loads(machine_profile.read_text(encoding="utf-8"))
        deps = profile.get("dependencies", {}) if isinstance(profile, dict) else {}
        required_missing = deps.get("required_missing", []) if isinstance(deps, dict) else []
        recommended_missing = deps.get("recommended_missing", []) if isinstance(deps, dict) else []
        optional_missing = deps.get("optional_missing", []) if isinstance(deps, dict) else []
        if required_missing:
            issues.append(f"Missing required runtime dependencies: {', '.join(required_missing)}")
        if recommended_missing:
            notes.append(f"Recommended dependencies missing: {', '.join(recommended_missing)}")
        if optional_missing:
            notes.append(f"Optional dependencies missing: {', '.join(optional_missing)}")
        install_plan = paths.reports / "dependency_install_plan.md"
        if install_plan.exists():
            notes.append(f"Install guidance available at {install_plan.relative_to(paths.root)}")
    else:
        issues.append("Missing machine profile JSON; run detect_machine_profile.py")

    llm_ready = llm_available(cfg)
    if not llm_ready:
        notes.append(f"Generic LLM backend is not configured: {llm_disabled_reason(cfg)}")
        notes.append("Run scripts/check_llm_ready.py --project <project> after setting LLM API configuration.")
    claude_path = find_claude(cfg)
    codex_path = find_codex(cfg)
    notes.append(f"Configured coding backend: {cfg.get('coding_agent', {}).get('backend', 'llm')}")
    notes.append(f"Claude backend available: {bool(claude_path)}{f' ({claude_path})' if claude_path else ''}")
    notes.append(f"Codex backend available: {bool(codex_path)}{f' ({codex_path})' if codex_path else ''}")

    takeover_required = [paths.root / "AGENTS.md", paths.work_status]
    for path in takeover_required:
        if not path.exists():
            try:
                label = path.relative_to(paths.root)
            except ValueError:
                label = path
            issues.append(f"Missing takeover/status file {label}")

    if not load_json(paths.state / "repo_candidates.json") and cfg.get("repo_selection", {}).get("enabled", False):
        issues.append("No repo candidates registered")
    if not load_json(paths.state / "dataset_registry.json") and cfg.get("dataset_check", {}).get("enabled", False):
        issues.append("No datasets registered")
    if not load_json(paths.state / "experiment_registry.json"):
        notes.append("No experiments logged yet")
    if not (paths.state / "parallel_plan.json").exists():
        notes.append("No parallel experiment plan yet")
    if not (paths.state / "natural_language_requests.json").exists():
        notes.append("No natural-language run requests logged yet")
    taste_state = load_json(paths.state / "finding_frontend.json") if (paths.state / "finding_frontend.json").exists() else {}
    taste_sync = load_json(paths.state / "taste_sync.json") if (paths.state / "taste_sync.json").exists() else {}
    taste_counts = taste_sync.get("counts", {}) if isinstance(taste_sync, dict) else {}
    if not taste_state:
        issues.append("The workflow has not been run for this project; run scripts/run_frontend.py then sync_outputs.py")
    else:
        notes.append(f"TASTE status: {taste_state.get('status', 'unknown') if isinstance(taste_state, dict) else 'unknown'}")
        if isinstance(taste_state, dict) and taste_state.get("status") in {"timeout", "failed", "error"}:
            notes.append("The workflow is in a recoverable failure state; rerun after API/network/source repair and do not treat fallback as scientific evidence.")
    if not taste_sync:
        issues.append("Outputs have not been synchronized into research state")
    elif isinstance(taste_counts, dict):
        fallback_only = False
        taste_dir = paths.planning / "finding"
        find_results = load_json(taste_dir / "find_results.json") if (taste_dir / "find_results.json").exists() else {}
        if isinstance(find_results, dict):
            articles = find_results.get("articles", []) or []
            fallback_only = bool(articles) and all(str(row.get("source", "")) == "taste_recoverable_fallback" for row in articles if isinstance(row, dict))
        if fallback_only:
            notes.append("TASTE sync contains only recoverable fallback outputs; this keeps workflow connectivity but is not scientific literature evidence.")
        if int(taste_counts.get("ideas_synced", 0) or 0) == 0:
            notes.append("The workflow has not produced synced ideas yet; rerun TASTE before idea selection.")

    paper_state = get_active_paper_state(args.project, venue=args.venue)
    if not paper_state:
        notes.append("No paper pipeline state yet")
    else:
        if not paper_state.get("draft_ready"):
            notes.append("Paper draft not prepared yet")
        if not paper_state.get("paper_reviews_ready"):
            notes.append("Internal paper review aggregation not prepared yet")
        if not paper_state.get("author_response_ready"):
            notes.append("Author response packet not prepared yet")
        if not paper_state.get("re_review_ready"):
            notes.append("Re-review summary not prepared yet")
        if not paper_state.get("paper_revision_ready"):
            notes.append("Revised Markdown paper draft not prepared yet")
        if paper_state.get("paper_review_verdict") == "blocked":
            notes.append("Paper is currently blocked by internal review; keep improving Markdown before template promotion.")
        if paper_state.get("template_fetched") and not paper_state.get("pdf_ready"):
            notes.append("Venue template fetched but PDF not compiled yet")
        if paper_state.get("template_fetch_error"):
            notes.append(f"Template fetch is currently blocked: {paper_state.get('template_fetch_error')}")

    report = paths.reports / "healthcheck.md"
    lines = ["# Research Healthcheck\n\n"]
    if issues:
        lines.append("## Issues\n")
        for issue in issues:
            lines.append(f"- {issue}\n")
    else:
        lines.append("All key project components are present.\n")
    lines.append("\n## Notes\n")
    if notes:
        for note in notes:
            lines.append(f"- {note}\n")
    else:
        lines.append("- No additional notes.\n")
    report.write_text("".join(lines), encoding="utf-8")
    print(report)
    print(f"issues={len(issues)}")


if __name__ == "__main__":
    main()
