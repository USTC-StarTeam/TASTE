from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

STAGE_NAME = "writing"
DISPLAY_NAME = "Writing"
RESPONSIBILITY = "独立后端论文生成模块：根据实验 idea、plan、实验记录和目标会议/期刊，调研官方要求、下载官方模板，并让 Claude Code 生成证据受控的 venue-formatted 论文。"
REQUIRED_EXTERNAL_INPUTS = ("venue", "idea", "plan", "experimental_log", "experiment_records")
ARTIFACTS_IN = ("idea.md", "plan.md", "experimental_log.md", "records/", "venue official requirements/template")
ARTIFACTS_OUT = ("paper.tex", "paper.pdf", "refs.bib", "venue_requirements.json", "template_source.json", "audits", "provenance.json")
PRIVATE_BACKEND_ROOTS = ("modules/writing", "modules/writing/scripts", "modules/writing/skills")

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = Path(__file__).resolve().parent
SCRIPTS_ROOT = MODULE_ROOT / "scripts"
SCRIPT_CATEGORIES = ("core", "pipeline", "venue", "rendering", "audit", "repair", "review", "maintenance")


@dataclass(slots=True)
class ArtifactRef:
    name: str
    path: str = ""
    kind: str = "json"
    role: str = "input"
    required: bool = False


@dataclass(slots=True)
class StageInvocation:
    research_topic: str = ""
    research_interest: str = ""
    researcher_profile: str = ""
    artifact_root: str = ""
    llm: dict[str, Any] = field(default_factory=dict)
    inputs: list[ArtifactRef] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)

    def root_path(self) -> Path:
        return Path(self.artifact_root).expanduser() if self.artifact_root else Path.cwd()


@dataclass(slots=True)
class StageResult:
    status: str
    artifacts: list[ArtifactRef] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    message: str = ""


def _script_dirs() -> list[Path]:
    dirs = [SCRIPTS_ROOT]
    dirs.extend(SCRIPTS_ROOT / name for name in SCRIPT_CATEGORIES if (SCRIPTS_ROOT / name).is_dir())
    return dirs


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    entries: list[str] = [str(MODULE_ROOT), str(ROOT), str(ROOT / "framework"), str(ROOT / "framework" / "scripts"), str(ROOT / "web" / "backend")]
    entries.extend(str(path) for path in _script_dirs())
    modules_root = ROOT / "modules"
    for stage_dir in sorted(path for path in modules_root.iterdir() if path.is_dir() and path.name != "writing"):
        entries.append(str(stage_dir))
        scripts = stage_dir / "scripts"
        if scripts.is_dir():
            entries.append(str(scripts))
    existing = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    seen: set[str] = set()
    merged: list[str] = []
    for item in [*entries, *existing]:
        if item and item not in seen:
            seen.add(item)
            merged.append(item)
    env["PYTHONPATH"] = os.pathsep.join(merged)
    env["WORKSPACE_ROOT"] = str(ROOT)
    return env


def _contract_payload() -> dict[str, Any]:
    return {
        "stage": STAGE_NAME,
        "display_name": DISPLAY_NAME,
        "responsibility": RESPONSIBILITY,
        "required_external_inputs": list(REQUIRED_EXTERNAL_INPUTS),
        "artifacts_in": list(ARTIFACTS_IN),
        "artifacts_out": list(ARTIFACTS_OUT),
        "private_backend_roots": list(PRIVATE_BACKEND_ROOTS),
        "entrypoint": "modules/writing/main.py",
        "scripts_are_private_backend": True,
        "frontend_dependency": False,
        "intermediate_root": "modules/writing/runs",
    }


def _normalize_action(action: str) -> str:
    return str(action or "").strip().replace("-", "_")


SCRIPT_ACTIONS = {
    "run_paper_pipeline": "pipeline/run_paper_pipeline.py",
    "run_paper_orchestra_bridge": "pipeline/run_paper_orchestra_bridge.py",
    "run_standalone_paper": "pipeline/run_standalone_paper.py",
    "build_project_input_pack": "pipeline/build_project_input_pack.py",
    "project_input_pack": "pipeline/build_project_input_pack.py",
    "standalone": "pipeline/run_standalone_paper.py",
    "build_paper_orchestra_state": "pipeline/build_paper_orchestra_state.py",
    "build_paper_md": "pipeline/build_paper_md.py",
    "build_conference_preview_paper": "pipeline/build_conference_preview_paper.py",
    "resolve_venue_requirements": "venue/resolve_venue_requirements.py",
    "venue_requirements": "venue/resolve_venue_requirements.py",
    "fetch_latex_template": "venue/fetch_latex_template.py",
    "render_paper_tex": "rendering/render_paper_tex.py",
    "compile_paper_pdf": "rendering/compile_paper_pdf.py",
    "audit_paper_evidence": "audit/audit_paper_evidence.py",
    "audit_paper_figures": "audit/audit_paper_figures.py",
    "audit_paper_normality": "audit/audit_paper_normality.py",
    "audit_paper_orchestra": "audit/audit_paper_orchestra.py",
    "audit_submission_readiness": "audit/audit_submission_readiness.py",
    "audit_standalone_paper": "audit/audit_standalone_paper.py",
    "build_claim_ledger": "audit/build_claim_ledger.py",
    "repair_paper_figures_loop": "repair/repair_paper_figures_loop.py",
    "repair_paper_orchestra_citations": "repair/repair_paper_orchestra_citations.py",
    "repair_paper_preview_loop": "repair/repair_paper_preview_loop.py",
    "revise_paper_citation_coverage": "repair/revise_paper_citation_coverage.py",
    "revise_paper_md": "repair/revise_paper_md.py",
    "review_response_tools": "review/review_response_tools.py",
    "check_internal_assets": "maintenance/check_internal_assets.py",
    "check_assets": "maintenance/check_internal_assets.py",
    "sync_vendor": "maintenance/check_internal_assets.py",
    "sync_stack": "maintenance/check_internal_assets.py",
}
ACTION_ALIASES = {
    "": "run_paper_pipeline",
    "run": "run_paper_pipeline",
    "paper": "run_paper_pipeline",
    "paper_pipeline": "run_paper_pipeline",
    "pipeline": "run_paper_pipeline",
    "preview": "build_conference_preview_paper",
    "audit_evidence": "audit_paper_evidence",
    "submission_readiness": "audit_submission_readiness",
    "repair_figures": "repair_paper_figures_loop",
    "repair_preview": "repair_paper_preview_loop",
    "audit_normality": "audit_paper_normality",
    "audit_figures": "audit_paper_figures",
}
REVIEW_TOOL_ACTIONS = {
    "respond_to_paper_reviews": "respond",
    "review_paper": "review_paper",
    "aggregate_reviews": "aggregate_reviews",
    "respond_reviews": "respond",
    "respond_to_reviews": "respond",
    "re_review_paper": "re_review",
    "re_review": "re_review",
    "write_comparison": "comparison",
    "comparison": "comparison",
}


def _script_path(script_stem: str) -> Path:
    key = _normalize_action(script_stem)
    mapped = SCRIPT_ACTIONS.get(key)
    candidates = [SCRIPTS_ROOT / mapped] if mapped else []
    candidates.extend(directory / f"{key}.py" for directory in _script_dirs())
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    raise SystemExit(f"Unknown {STAGE_NAME} module action: {script_stem}")


def _run_script(script_stem: str, args: Sequence[str]) -> int:
    script = _script_path(script_stem)
    proc = subprocess.run([sys.executable, str(script), *args], cwd=ROOT, env=_python_env(), text=True)
    return int(proc.returncode)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="writing 独立后端入口。", add_help=True)
    parser.add_argument("--action", default="run", help="Backend action. Default: run.")
    parser.add_argument("--contract", action="store_true")
    ns, rest = parser.parse_known_args(argv)
    if ns.contract:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    action = _normalize_action(ns.action)
    if action in REVIEW_TOOL_ACTIONS:
        return _run_script("review_response_tools", ["--tool-action", REVIEW_TOOL_ACTIONS[action], *rest])
    target = ACTION_ALIASES.get(action, action)
    if target in REVIEW_TOOL_ACTIONS:
        return _run_script("review_response_tools", ["--tool-action", REVIEW_TOOL_ACTIONS[target], *rest])
    return _run_script(target, rest)


if __name__ == "__main__":
    raise SystemExit(main())
