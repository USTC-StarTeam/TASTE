#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from llm_client import llm_available, llm_disabled_reason
from project_paths import ROOT, build_paths, load_project_config
from taste_pythonpath import resolve_script_path, taste_script_dirs


def run(cmd: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)


def file_contains(path: Path, needle: str) -> bool:
    return path.exists() and needle in path.read_text(encoding="utf-8", errors="ignore")


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def discover_workspace_tool(name: str) -> str:
    candidates = []
    for base in [ROOT.parent / "miniforge", ROOT.parent / "miniforge3", ROOT.parent / "miniconda", ROOT.parent / "miniconda3", Path.home() / "miniforge", Path.home() / "miniconda3"]:
        candidates.extend([base / "bin" / name, base / "condabin" / name])
    for texroot in [ROOT.parent / "texlive", Path.home() / "texlive"]:
        candidates.append(texroot / "2026" / "bin" / "x86_64-linux" / name)
        candidates.append(texroot / "2025" / "bin" / "x86_64-linux" / name)
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return str(candidate)
    return ""


def check_script_compile() -> tuple[bool, str]:
    scripts = sorted(str(path) for directory in taste_script_dirs(ROOT) for path in directory.glob("*.py"))
    proc = run([sys.executable, "-m", "py_compile", *scripts])
    return proc.returncode == 0, (proc.stderr or proc.stdout)[-4000:]


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit whether TASTE can run with Find LLM scoring and downstream Claude Code stages.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    parser.add_argument("--run-fallback-team", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    issues: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, object]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": "" if ok else detail})
        if not ok:
            issues.append(f"{name}: {detail}")

    ok, detail = check_script_compile()
    record("all_python_scripts_compile", ok, detail)

    required_scripts = [
        "run_frontend.py", "ensure_current_find_research_plan.py", "run_autonomous_research.py", "run_full_research_cycle.py", "run_loop.py", "run_project.py",
        "claude_project_session.py", "run_coding_agent.py", "run_environment_stage.py", "run_paper_pipeline.py", "planning_tools.py",
        "bootstrap_repo_env.py", "research_healthcheck.py", "build_repo_data_requirements.py", "environment_data_tools.py",
        "restart_after_data_blocker.py", "audit_repo_candidate_pool.py",
        "probe_repo_dataset.py", "run_active_repo_smoke.py", "run_autoscientist_supervisor.py",
        "run_autoscientist_continuous.py", "build_stagnation_report.py", "run_evoscientist_style_cycle.py", "refresh_project_reports.py", "check_llm_ready.py", "llm_client.py",
    ]
    for script in required_scripts:
        record(f"script_exists:{script}", resolve_script_path(script, ROOT).exists(), "missing script")

    removed_team_script = "run_" + "llm_" + "research_team.py"
    record("autonomous_runner_accepts_venue", file_contains(ROOT / "framework/scripts/run_autonomous_research.py", "parser.add_argument(\"--venue\""), "missing --venue argument")
    record("autonomous_runner_passes_venue_to_loop", file_contains(ROOT / "framework/scripts/run_autonomous_research.py", "loop_cmd.extend([\"--venue\", args.venue])"), "run_autonomous_research.py does not pass venue into run_loop.py")
    record("run_loop_passes_venue_to_project", file_contains(ROOT / "modules/experimenting/scripts/run_loop.py", "run_cmd.extend([\"--venue\", args.venue])"), "run_loop.py does not pass venue into run_project.py")
    record("run_project_no_removed_team_route", not file_contains(ROOT / "framework/scripts/run_project.py", removed_team_script), "removed downstream team route is still referenced")
    record("run_coding_agent_forces_claude", file_contains(ROOT / "modules/experimenting/scripts/run_coding_agent.py", "effective_backend = \"claude\"") or file_contains(ROOT / "modules/experimenting/scripts/run_coding_agent.py", "return \"claude\""), "run_coding_agent.py should force Claude Code downstream")
    record("run_project_passes_venue_to_memory", file_contains(ROOT / "framework/scripts/run_project.py", "update_evolution_memory.py") and file_contains(ROOT / "framework/scripts/run_project.py", "'--venue', args.venue"), "run_project.py does not pass venue into evolution memory")

    find_llm_ready = llm_available(cfg)
    checks.append({"name": "find_llm_backend_ready", "ok": find_llm_ready, "detail": "" if find_llm_ready else llm_disabled_reason(cfg)})
    if not find_llm_ready:
        warnings.append(f"Find LLM backend not configured: {llm_disabled_reason(cfg)}; discovery can still show deterministic/source blockers, but LLM scoring and Read/Idea/Plan fallback need configuration.")

    machine = load_json(paths.reports / "machine_profile.json", {})
    deps = machine.get("dependencies", {}) if isinstance(machine, dict) else {}
    cli = deps.get("cli", {}) if isinstance(deps, dict) else {}
    conda_profile = cli.get("conda", {}) if isinstance(cli, dict) else {}
    latex_profile = cli.get("latex", {}) if isinstance(cli, dict) else {}
    latexmk_profile = cli.get("latexmk", {}) if isinstance(cli, dict) else {}
    conda = shutil.which("conda") or conda_profile.get("path", "") or discover_workspace_tool("conda")
    latex = shutil.which("latex") or latex_profile.get("path", "") or discover_workspace_tool("latex")
    latexmk = shutil.which("latexmk") or latexmk_profile.get("path", "") or discover_workspace_tool("latexmk")
    checks.append({"name": "conda_available", "ok": bool(conda_profile.get("available") or conda), "detail": conda or "not found"})
    checks.append({"name": "latex_available", "ok": bool(latex_profile.get("available") or latex), "detail": latex or "not found"})
    checks.append({"name": "latexmk_available", "ok": bool(latexmk_profile.get("available") or latexmk), "detail": latexmk or "not found"})
    if not (conda_profile.get("available") or conda):
        warnings.append("conda is unavailable; repo env bootstrap will need user installation or a configured conda path.")
    if not (latex_profile.get("available") or latex) or not (latexmk_profile.get("available") or latexmk):
        warnings.append("LaTeX tooling is unavailable; PDF stage will stay gated or fail until PATH is configured.")

    required_state = [
        paths.reports / "machine_profile.md", paths.reports / "healthcheck.md", paths.reports / "workflow_connectivity.md",
        paths.planning / "next_actions.md", paths.reports / "iteration_reflection.md",
    ]
    for path in required_state:
        record(f"artifact_exists:{path.relative_to(paths.root)}", path.exists() and path.stat().st_size > 0, "missing or empty")

    if args.run_fallback_team:
        checks.append({"name": "deprecated_run_fallback_team_option_ignored", "ok": True, "detail": "The downstream project route is Claude Code; this compatibility flag no longer launches a separate team."})

    paper_state = load_json(paths.root / "paper" / "metadata" / "paper_pipeline.json", {})
    if paper_state:
        checks.append({"name": "paper_pipeline_state_exists", "ok": True, "detail": str(paths.root / "paper" / "metadata" / "paper_pipeline.json")})
        if paper_state.get("promotion_gate") == "blocked":
            warnings.append("Paper promotion is blocked by evidence/review gates; this is safe behavior, not a runtime failure.")
    else:
        warnings.append("No paper pipeline metadata yet; run run_paper_pipeline.py when a target venue is provided.")

    report = {
        "project": args.project,
        "venue": args.venue,
        "issue_count": len(issues),
        "warning_count": len(warnings),
        "issues": issues,
        "warnings": warnings,
        "checks": checks,
    }
    paths.state.mkdir(parents=True, exist_ok=True)
    paths.reports.mkdir(parents=True, exist_ok=True)
    (paths.state / "pipeline_runnability.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# Pipeline Runnability Audit\n\n",
        f"- project: {args.project}\n",
        f"- venue: {args.venue or 'not specified'}\n",
        f"- issue_count: {len(issues)}\n",
        f"- warning_count: {len(warnings)}\n\n",
    ]
    if issues:
        lines.append("## Issues\n")
        for issue in issues:
            lines.append(f"- {issue}\n")
    else:
        lines.append("No blocking runnability issues detected.\n")
    if warnings:
        lines.append("\n## Warnings\n")
        for warning in warnings:
            lines.append(f"- {warning}\n")
    (paths.reports / "pipeline_runnability.md").write_text("".join(lines), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if issues:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
