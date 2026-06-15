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
    legacy_roots: tuple[str, ...]


STAGE_MODULES: tuple[ModuleBoundary, ...] = (
    ModuleBoundary(
        key="finding",
        directory="modules/finding",
        display_name="Finding",
        legacy_stage="Find",
        responsibility="Collect, filter, score, and rank literature/tool candidates from the research topic and profile. Full-text reading evidence is explicitly outside this module.",
        external_inputs=("llm_api", "research_topic", "research_interest", "researcher_profile", "source_selection"),
        artifacts_in=("config/profile JSON", "venue/source selection JSON"),
        artifacts_out=("find_results.json", "article.md", "source_status.md", "category/title/detail/scoring reports"),
        legacy_roots=("modules/finding/auto_research/auto_find", "modules/finding/scripts/discover_*.py", "modules/finding/scripts/build_literature_tool_packet.py"),
    ),
    ModuleBoundary(
        key="reading",
        directory="modules/reading",
        display_name="Reading",
        legacy_stage="Read",
        responsibility="Acquire verified paper-body text for the selected Find packet and synthesize reading notes. Same-run replacements for unavailable public full text happen here, never inside Finding.",
        external_inputs=("llm_api_or_claude", "finding_artifact_packet", "artifact_root"),
        artifacts_in=("find_results.json", "article.md", "full_text_reading/manual_full_text_sources.json"),
        artifacts_out=("read_results.json", "read.md", "full_text_reading/full_text_packet.json", "current_find_full_text_evidence_repair.json"),
        legacy_roots=("modules/reading/auto_research/auto_read", "modules/reading/scripts/repair_current_find_full_text_evidence.py", "modules/reading/scripts/ensure_current_find_research_plan.py"),
    ),
    ModuleBoundary(
        key="ideation",
        directory="modules/ideation",
        display_name="Ideation",
        legacy_stage="Ideas",
        responsibility="Turn reading/finding artifacts into editable research ideas without selecting an execution route.",
        external_inputs=("llm_api_or_claude", "reading_artifacts", "research_profile"),
        artifacts_in=("find_results.json", "read_results.json", "read.md"),
        artifacts_out=("ideas.json", "idea.md", "hypothesis_arena.md", "idea candidate audits"),
        legacy_roots=("modules/ideation/auto_research/auto_idea", "modules/ideation/scripts/assess_idea_candidates.py", "modules/ideation/scripts/build_hypothesis_arena.py"),
    ),
    ModuleBoundary(
        key="planning",
        directory="modules/planning",
        display_name="Planning",
        legacy_stage="Plan",
        responsibility="Select and repair executable research plans from approved ideas; downstream modules consume only explicit selected plan contracts.",
        external_inputs=("llm_api_or_claude", "idea_artifacts", "project_constraints"),
        artifacts_in=("ideas.json", "idea.md", "user selection/approval"),
        artifacts_out=("plans.json", "plan.md", "experiment_plan.json", "taste_plan_bridge.json", "blocker action plans"),
        legacy_roots=("modules/planning/auto_research/auto_plan", "modules/planning/scripts/plan_experiments.py", "modules/planning/scripts/build_workflow_blueprint.py"),
    ),
    ModuleBoundary(
        key="environment",
        directory="modules/environment",
        display_name="Environment",
        legacy_stage="Environment",
        responsibility="Select audited code/data bases, probe loaders, and lock the experiment runtime. It does not run novel experiments or write paper claims.",
        external_inputs=("selected_plan_contract", "candidate_repo_data_artifacts", "runtime_config"),
        artifacts_in=("plans.json", "literature_tool_packet.json", "repo/data candidates"),
        artifacts_out=("evidence_ready_repo_selection.json", "repo_env_bootstrap.json", "dataset registry", "reference/data gates"),
        legacy_roots=("modules/environment/scripts/run_environment_stage.py", "modules/environment/scripts/select_evidence_ready_repo.py", "modules/environment/scripts/bootstrap_repo_env.py"),
    ),
    ModuleBoundary(
        key="experimenting",
        directory="modules/experimenting",
        display_name="Experimenting",
        legacy_stage="Experiment",
        responsibility="Modify or execute selected project code, run auditable experiments, parse metrics/logs, and produce evidence gates for claims.",
        external_inputs=("selected_plan_contract", "locked_environment", "repo_path", "experiment_python"),
        artifacts_in=("evidence_ready_repo_selection.json", "repo_env_bootstrap.json", "experiment_plan.json"),
        artifacts_out=("experiment_registry.json", "experiment artifacts/logs", "runtime integrity audit", "reference/scientific progress gates"),
        legacy_roots=("modules/experimenting/scripts/run_coding_agent.py", "modules/experimenting/scripts/launch_experiment_run.py", "modules/experimenting/scripts/experiment_contracts.py"),
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
        legacy_roots=("modules/writing", "modules/writing/scripts/run_paper_pipeline.py", "modules/writing/scripts/paper_common.py"),
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
    "bootstrap_wiki.py": TASTE_FRAMEWORK_OWNER,
    "build_research_trajectory_system.py": TASTE_FRAMEWORK_OWNER,
    "build_stagnation_report.py": TASTE_FRAMEWORK_OWNER,
    "check_llm_ready.py": TASTE_FRAMEWORK_OWNER,
    "claude_project_session.py": TASTE_FRAMEWORK_OWNER,
    "compile_prompt.py": TASTE_FRAMEWORK_OWNER,
    "create_project.py": TASTE_FRAMEWORK_OWNER,
    "detect_machine_profile.py": TASTE_FRAMEWORK_OWNER,
    "export_obsidian.py": TASTE_FRAMEWORK_OWNER,
    "generate_handoff.py": TASTE_FRAMEWORK_OWNER,
    "init_project.py": TASTE_FRAMEWORK_OWNER,
    "init_workspace.py": TASTE_FRAMEWORK_OWNER,
    "list_projects.py": TASTE_FRAMEWORK_OWNER,
    "lint_wiki.py": TASTE_FRAMEWORK_OWNER,
    "llm_client.py": TASTE_FRAMEWORK_OWNER,
    "pipeline_guard.py": TASTE_FRAMEWORK_OWNER,
    "project_config.py": TASTE_FRAMEWORK_OWNER,
    "project_paths.py": TASTE_FRAMEWORK_OWNER,
    "reconcile_state.py": TASTE_FRAMEWORK_OWNER,
    "refresh_index_and_log.py": TASTE_FRAMEWORK_OWNER,
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
    "record_safe_unblock_web_job.py": WEB_FRONTEND_OWNER,
    "run_frontend.py": WEB_FRONTEND_OWNER,
    "start_web.sh": WEB_FRONTEND_OWNER,
    "assess_literature_base_candidates.py": "finding",
    "assess_paper_quality.py": "finding",
    "build_literature_tool_packet.py": "finding",
    "discover_arxiv.py": "finding",
    "discover_github_repos.py": "finding",
    "discover_semantic_scholar.py": "finding",
    "ingest_discovery.py": "finding",
    "literature_policy.py": "finding",
    "plan_literature_review.py": "finding",
    "run_literature_base_audit.py": "finding",
    "run_literature_tool.py": "finding",
    "update_local_database.py": "finding",
    "ensure_current_find_research_plan.py": "reading",
    "import_paper.py": "reading",
    "repair_current_find_full_text_evidence.py": "reading",
    "assess_idea_candidates.py": "ideation",
    "build_hypothesis_arena.py": "ideation",
    "prepare_initialization.py": "ideation",
    "build_aris_review_board.py": "planning",
    "build_blocker_action_plan.py": "planning",
    "build_blocker_resolution_packet.py": "planning",
    "build_method_frontier.py": "planning",
    "build_workflow_blueprint.py": "planning",
    "plan_experiments.py": "planning",
    "propose_next_actions.py": "planning",
    "reflect_iteration.py": "planning",
    "assess_repo_candidates.py": "environment",
    "attempt_data_acquisition.py": "environment",
    "audit_dataset_path.py": "environment",
    "audit_deterministic_base_switch_gate.py": "environment",
    "audit_local_repo.py": "environment",
    "audit_obsolete_baseline_cleanup.py": "environment",
    "audit_repo_candidate_pool.py": "environment",
    "audit_selected_base_viability.py": "environment",
    "bootstrap_repo_env.py": "environment",
    "build_fresh_base_implementation_plan.py": "environment",
    "build_repo_data_requirements.py": "environment",
    "data_unavailability_policy.py": "environment",
    "execute_authorized_base_switch.py": "environment",
    "guard_selected_base_route.py": "environment",
    "plan_data_acquisition.py": "environment",
    "probe_fresh_base_data_acquisition.py": "environment",
    "probe_repo_dataset.py": "environment",
    "probe_selected_base_reference.py": "environment",
    "reconcile_active_and_pool_candidates.py": "environment",
    "register_dataset.py": "environment",
    "register_repo_candidate.py": "environment",
    "repo_first_backtrack.py": "environment",
    "restart_after_data_blocker.py": "environment",
    "run_environment_stage.py": "environment",
    "run_safe_unblock.py": "environment",
    "run_selected_base_reference_reproduction_audit.py": "environment",
    "select_evidence_ready_repo.py": "environment",
    "select_fresh_research_base.py": "environment",
    "select_repo_candidate.py": "environment",
    "analyze_experiment_failures.py": "experimenting",
    "audit_experiment_iteration.py": "experimenting",
    "audit_experiment_runtime_integrity.py": "experimenting",
    "audit_reference_reproduction.py": "experimenting",
    "build_experiment_record_table.py": "experimenting",
    "experiment_contracts.py": "experimenting",
    "experiment_run_watchdog.py": "experimenting",
    "import_experiment_artifacts.py": "experimenting",
    "launch_experiment_run.py": "experimenting",
    "log_experiment.py": "experimenting",
    "reference_reproduction_state.py": "experimenting",
    "run_active_repo_smoke.py": "experimenting",
    "run_coding_agent.py": "experimenting",
    "run_loop.py": "experimenting",
    "run_real_repo_smoke.py": "experimenting",
    "aggregate_paper_reviews.py": "writing",
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
    "re_review_paper.py": "writing",
    "render_paper_tex.py": "writing",
    "repair_paper_figures_loop.py": "writing",
    "repair_paper_orchestra_citations.py": "writing",
    "repair_paper_preview_loop.py": "writing",
    "resolve_venue_requirements.py": "writing",
    "respond_to_paper_reviews.py": "writing",
    "review_paper_md.py": "writing",
    "revise_paper_citation_coverage.py": "writing",
    "revise_paper_md.py": "writing",
    "run_paper_orchestra_bridge.py": "writing",
    "run_paper_pipeline.py": "writing",
    "sync_third_party_research_stack.py": "writing",
    "sync_writing_vendor.py": "writing",
    "write_comparison.py": "writing",
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
            "legacy_roots": list(module.legacy_roots),
        }
        for module in STAGE_MODULES
    }
