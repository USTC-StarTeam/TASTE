from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModuleBoundary:
    key: str
    directory: str
    display_name: str
    legacy_stage: str
    responsibility: str
    external_inputs: tuple[str, ...]
    artifacts_in: tuple[str, ...]
    artifacts_out: tuple[str, ...]
    private_backend_roots: tuple[str, ...]
    public_final_artifact: str = ""


STAGE_MODULES: tuple[ModuleBoundary, ...] = (
    ModuleBoundary(
        key="finding",
        directory="modules/finding",
        display_name="Finding",
        legacy_stage="Find",
        responsibility="Collect, filter, score, and rank literature/tool candidates from the research topic and profile. Full-text reading evidence is explicitly outside this module.",
        external_inputs=("llm_api", "research_topic", "research_interest", "researcher_profile", "source_selection"),
        artifacts_in=("config/profile JSON", "venue/source selection JSON"),
        artifacts_out=("find_results.json", "find.md", "source_status.md", "category/title/detail/scoring reports"),
        private_backend_roots=(
            "modules/finding/scripts/core",
            "modules/finding/scripts/flow",
            "modules/finding/scripts/sources.py",
            "modules/finding/scripts/cache",
        ),
        public_final_artifact="find.md",
    ),
    ModuleBoundary(
        key="reading",
        directory="modules/reading",
        display_name="Reading",
        legacy_stage="Read",
        responsibility="Acquire verified same-paper full text from generic paper inputs and synthesize reading notes. A title is sufficient; optional locators improve resolution.",
        external_inputs=("paper_title_or_locator", "claude_or_prepare_mode"),
        artifacts_in=("generic Reading input JSON"),
        artifacts_out=("read.md", "read_results.json", "full_text_reading/full_text_packet.json"),
        private_backend_roots=(
            "modules/reading/scripts/core",
            "modules/reading/scripts/pipeline",
            "modules/reading/scripts/acquisition",
            "modules/reading/scripts/orchestration",
        ),
        public_final_artifact="read.md",
    ),
    ModuleBoundary(
        key="ideation",
        directory="modules/ideation",
        display_name="Ideation",
        legacy_stage="Ideas",
        responsibility="Consume one normalized evidence bundle and turn it into editable research ideas without discovering project inputs or selecting an execution route.",
        external_inputs=("caller_normalized_input_bundle", "claude_code", "runtime_config"),
        artifacts_in=("ideation_input.json",),
        artifacts_out=("idea.md", "ideas.json"),
        public_final_artifact="idea.md",
        private_backend_roots=("modules/ideation/scripts/idea_pipeline.py",),
    ),
    ModuleBoundary(
        key="planning",
        directory="modules/planning",
        display_name="Planning",
        legacy_stage="Plan",
        responsibility="Turn selected approved current-Find Ideas into auditable plan candidates and one selected execution contract.",
        external_inputs=("claude_code", "framework_planning_input", "project_constraints"),
        artifacts_in=("ideas.json", "plan.md for follow-up actions", "Framework approval and selection state"),
        artifacts_out=("plan.md", "plans.json", "experiment_plan.json", "taste_plan_bridge.json"),
        public_final_artifact="plan.md",
        private_backend_roots=("modules/planning/scripts/core/plan_pipeline.py", "modules/planning/scripts/tools/planning_tools.py", "modules/planning/scripts/blockers/build_blocker_action_plan.py", "modules/planning/scripts/actions/propose_next_actions.py"),
    ),
    ModuleBoundary(
        key="environment",
        directory="modules/environment",
        display_name="Environment",
        legacy_stage="Environment",
        responsibility="Run one project-scoped Environment controller through the public module entrypoint, deploy and audit the selected plan, and emit a deterministic handoff.",
        external_inputs=("project", "selected_plan_contract", "runtime_config"),
        artifacts_in=("experiment_plan.json", "paper/repository hints"),
        artifacts_out=("environment_deployment_decision.json", "environment_chat_result.json", "command receipts"),
        private_backend_roots=("modules/environment/main.py",),
    ),
    ModuleBoundary(
        key="experimenting",
        directory="modules/experimenting",
        display_name="Experimenting",
        legacy_stage="Experiment",
        responsibility="Modify or execute selected project code, run auditable experiments, parse metrics/logs, and produce evidence gates for claims.",
        external_inputs=("selected_plan_contract", "locked_environment", "repo_path", "experiment_python"),
        artifacts_in=("environment_handoff.json", "experiment_plan.json"),
        artifacts_out=("experiment_registry.json", "experiment artifacts/logs", "runtime integrity audit", "reference/scientific progress gates"),
        private_backend_roots=(
            "modules/experimenting/main.py",
            "modules/experimenting/scripts/orchestration/controller_session.py",
            "modules/experimenting/scripts/execution/launch_experiment_run.py",
            "modules/experimenting/scripts/execution/experiment_run_watchdog.py",
            "modules/experimenting/scripts/common/experiment_contracts.py",
        ),
    ),
    ModuleBoundary(
        key="writing",
        directory="modules/writing",
        display_name="Writing",
        legacy_stage="Paper",
        responsibility="Resolve venue requirements, draft/revise/compile the manuscript, and audit citations/figures/submission readiness from experiment evidence.",
        external_inputs=("venue", "selected_plan_contract", "experiment_evidence", "paper_config"),
        artifacts_in=("experiment_registry.json", "claim ledger", "venue template/requirements"),
        artifacts_out=("paper draft/revision", "compiled PDF", "paper_pipeline.json", "submission_readiness.json"),
        private_backend_roots=("modules/writing", "modules/writing/scripts/run_paper_pipeline.py", "modules/writing/scripts/paper_common.py"),
    ),
)

STAGE_MODULE_KEYS: tuple[str, ...] = tuple(module.key for module in STAGE_MODULES)
MODULE_BY_KEY: dict[str, ModuleBoundary] = {module.key: module for module in STAGE_MODULES}

TASTE_FRAMEWORK_OWNER = "taste_framework"
WEB_FRONTEND_OWNER = "web_frontend"
NON_STAGE_OWNERS = (TASTE_FRAMEWORK_OWNER, WEB_FRONTEND_OWNER)
ALL_OWNERS = STAGE_MODULE_KEYS + NON_STAGE_OWNERS

SCRIPT_OWNER_OVERRIDES: dict[str, str] = {
    "agent_state.py": TASTE_FRAMEWORK_OWNER,
    "audit_framework_content_coupling.py": TASTE_FRAMEWORK_OWNER,
    "audit_pipeline_runnability.py": TASTE_FRAMEWORK_OWNER,
    "audit_research_trajectory_capabilities.py": TASTE_FRAMEWORK_OWNER,
    "audit_workflow_connectivity.py": TASTE_FRAMEWORK_OWNER,
    "build_research_trajectory_system.py": TASTE_FRAMEWORK_OWNER,
    "build_stagnation_report.py": TASTE_FRAMEWORK_OWNER,
    "check_llm_ready.py": TASTE_FRAMEWORK_OWNER,
    "claude_project_session.py": TASTE_FRAMEWORK_OWNER,
    "compile_prompt.py": TASTE_FRAMEWORK_OWNER,
    "create_project.py": TASTE_FRAMEWORK_OWNER,
    "detect_machine_profile.py": TASTE_FRAMEWORK_OWNER,
    "generate_handoff.py": TASTE_FRAMEWORK_OWNER,
    "init_project.py": TASTE_FRAMEWORK_OWNER,
    "init_workspace.py": TASTE_FRAMEWORK_OWNER,
    "list_projects.py": TASTE_FRAMEWORK_OWNER,
    "literature_policy.py": TASTE_FRAMEWORK_OWNER,
    "llm_client.py": TASTE_FRAMEWORK_OWNER,
    "pipeline_guard.py": TASTE_FRAMEWORK_OWNER,
    "project_config.py": TASTE_FRAMEWORK_OWNER,
    "project_paths.py": TASTE_FRAMEWORK_OWNER,
    "reconcile_state.py": TASTE_FRAMEWORK_OWNER,
    "refresh_project_reports.py": TASTE_FRAMEWORK_OWNER,
    "report_status.py": TASTE_FRAMEWORK_OWNER,
    "research_healthcheck.py": TASTE_FRAMEWORK_OWNER,
    "research_manifest.py": TASTE_FRAMEWORK_OWNER,
    "run_autonomous_research.py": TASTE_FRAMEWORK_OWNER,
    "run_autoscientist_continuous.py": TASTE_FRAMEWORK_OWNER,
    "run_autoscientist_supervisor.py": TASTE_FRAMEWORK_OWNER,
    "run_evoscientist_style_cycle.py": TASTE_FRAMEWORK_OWNER,
    "run_full_research_cycle.py": TASTE_FRAMEWORK_OWNER,
    "run_project.py": TASTE_FRAMEWORK_OWNER,
    "run_research_trajectory_supervisor.py": TASTE_FRAMEWORK_OWNER,
    "run_supervision_tick.py": TASTE_FRAMEWORK_OWNER,
    "runtime_env.py": TASTE_FRAMEWORK_OWNER,
    "setup_git_guardrails.py": TASTE_FRAMEWORK_OWNER,
    "sync_outputs.py": TASTE_FRAMEWORK_OWNER,
    "taste_pythonpath.py": TASTE_FRAMEWORK_OWNER,
    "update_evolution_memory.py": TASTE_FRAMEWORK_OWNER,
    "verify_research_trajectory_end_to_end.py": TASTE_FRAMEWORK_OWNER,
    "work_status.py": TASTE_FRAMEWORK_OWNER,
    "wiki_tools.py": TASTE_FRAMEWORK_OWNER,
    "record_safe_unblock_web_job.py": WEB_FRONTEND_OWNER,
    "run_frontend.py": WEB_FRONTEND_OWNER,
    "start_web.sh": WEB_FRONTEND_OWNER,
    "build_category_summary.py": "finding",
    "build_openreview_cache.py": "finding",
    "build_venue_metadata_cache.py": "finding",
    "find_pipeline.py": "finding",
    "find_support.py": "finding",
    "update_local_database.py": "finding",
    "read_pipeline.py": "reading",
    "ensure_current_find_research_plan.py": "reading",
    "repair_current_find_full_text_evidence.py": "reading",
    "idea_pipeline.py": "ideation",
    "plan_pipeline.py": "planning",
    "planning_tools.py": "planning",
    "build_blocker_action_plan.py": "planning",
    "propose_next_actions.py": "planning",
    "experiment_contracts.py": "experimenting",
    "experiment_run_watchdog.py": "experimenting",
    "launch_experiment_run.py": "experimenting",
    "run_claude_audit.py": "experimenting",
    "audit_paper_evidence.py": "writing",
    "audit_paper_figures.py": "writing",
    "audit_paper_normality.py": "writing",
    "audit_paper_orchestra.py": "writing",
    "audit_submission_readiness.py": "writing",
    "build_claim_ledger.py": "writing",
    "build_conference_preview_paper.py": "writing",
    "build_paper_md.py": "writing",
    "build_paper_orchestra_state.py": "writing",
    "compile_paper_pdf.py": "writing",
    "fetch_latex_template.py": "writing",
    "paper_common.py": "writing",
    "paper_self_review.py": "writing",
    "render_paper_tex.py": "writing",
    "repair_paper_figures_loop.py": "writing",
    "repair_paper_orchestra_citations.py": "writing",
    "repair_paper_preview_loop.py": "writing",
    "resolve_venue_requirements.py": "writing",
    "review_response_tools.py": "writing",
    "revise_paper_citation_coverage.py": "writing",
    "revise_paper_md.py": "writing",
    "run_paper_orchestra_bridge.py": "writing",
    "run_paper_pipeline.py": "writing",
    "sync_third_party_research_stack.py": "writing",
    "sync_writing_vendor.py": "writing",
}

SCRIPT_OWNER_REASONS: dict[str, str] = {
    "finding": "literature/source discovery, source scoring, or Find artifact preparation",
    "reading": "full-text/read packet acquisition or current-Find reading bridge",
    "ideation": "idea/hypothesis candidate generation or curation",
    "planning": "plan selection, blocker planning, or execution-contract shaping",
    "environment": "repo/data/base/environment selection, probing, or runtime locking",
    "experimenting": "experiment execution, logging, repair, metric parsing, or empirical gates",
    "writing": "paper drafting, venue/template/citation/figure/submission readiness",
    TASTE_FRAMEWORK_OWNER: "TASTE orchestration, project state, runtime, supervision, or compatibility framework",
    WEB_FRONTEND_OWNER: "web server/client job bridge or frontend startup",
}


def script_name(path_or_name: str | Path) -> str:
    return Path(str(path_or_name)).name


def classify_script(path_or_name: str | Path) -> str:
    name = script_name(path_or_name)
    if name in SCRIPT_OWNER_OVERRIDES:
        return SCRIPT_OWNER_OVERRIDES[name]
    if name.startswith("discover_"):
        return "finding"
    if name.startswith(("paper_", "re_review_paper", "review_paper", "revise_paper", "repair_paper")):
        return "writing"
    if name.startswith(("audit_paper", "build_paper", "run_paper")):
        return "writing"
    if name.startswith(("audit_experiment", "experiment_")):
        return "experimenting"
    if name.startswith(("probe_", "register_", "select_")):
        return "environment"
    if name.startswith("build_") and "literature" in name:
        return "finding"
    if name.startswith("build_") and any(token in name for token in ("repo", "data", "base")):
        return "environment"
    if name.startswith("build_") and any(token in name for token in ("plan", "workflow", "method", "blocker")):
        return "planning"
    if name.startswith("run_"):
        return TASTE_FRAMEWORK_OWNER
    raise KeyError(f"No module owner for script {name}")


def script_owner_reason(owner: str) -> str:
    return SCRIPT_OWNER_REASONS.get(owner, "classified by module boundary registry")


def module_contracts_payload() -> dict[str, Any]:
    return {
        module.key: {
            "directory": module.directory,
            "display_name": module.display_name,
            "legacy_stage": module.legacy_stage,
            "responsibility": module.responsibility,
            "external_inputs": list(module.external_inputs),
            "artifacts_in": list(module.artifacts_in),
            "artifacts_out": list(module.artifacts_out),
            "public_final_artifact": module.public_final_artifact,
            "private_backend_roots": list(module.private_backend_roots),
        }
        for module in STAGE_MODULES
    }
