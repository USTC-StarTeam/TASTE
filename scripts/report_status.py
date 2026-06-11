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


def find_claude(cfg: dict) -> str:
    return find_cli_binary(cfg, "claude")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def count_dirs(path: Path) -> int:
    return len([entry for entry in path.iterdir() if entry.is_dir()]) if path.exists() else 0


def _repo_identity(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("repo_path") or row.get("name") or row.get("url") or "").strip()


def _blocker_matches_active_repo(blocker_packet: dict, active_repo: dict) -> bool:
    if not isinstance(blocker_packet, dict) or not blocker_packet:
        return False
    if not isinstance(active_repo, dict) or not active_repo:
        return False
    blocker_repo = blocker_packet.get("active_repo", {})
    return bool(_repo_identity(blocker_repo) and _repo_identity(blocker_repo) == _repo_identity(active_repo))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    args = parser.parse_args()

    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    report = paths.reports / "status.md"
    repo_rows = load_json(paths.state / "repo_candidates.json")
    ds_rows = load_json(paths.state / "dataset_registry.json")
    exp_rows = load_json(paths.state / "experiment_registry.json")
    requests = load_json(paths.state / "natural_language_requests.json")
    plan = load_json(paths.state / "parallel_plan.json")
    ideas = load_json(paths.state / "idea_candidates.json")
    paper_quality = load_json(paths.state / "paper_quality.json")
    active_repo = load_json(paths.state / "active_repo.json") if (paths.state / "active_repo.json").exists() else {}
    repo_data_requirements = load_json(paths.state / "repo_data_requirements.json") if (paths.state / "repo_data_requirements.json").exists() else {}
    data_policy = load_json(paths.state / "data_unavailability_policy.json") if (paths.state / "data_unavailability_policy.json").exists() else {}
    blocker_packet = load_json(paths.state / "blocker_resolution_packet.json") if (paths.state / "blocker_resolution_packet.json").exists() else {}
    repo_selection = load_json(paths.state / "evidence_ready_repo_selection.json") if (paths.state / "evidence_ready_repo_selection.json").exists() else {}
    repo_selection_blocker = load_json(paths.state / "repo_selection_blocker.json") if (paths.state / "repo_selection_blocker.json").exists() else {}
    selected_repo = repo_selection.get('selected', {}) if isinstance(repo_selection, dict) else {}
    claude_decision = {}
    if isinstance(repo_selection, dict) and isinstance(repo_selection.get('claude_topic_decision'), dict):
        claude_decision = repo_selection.get('claude_topic_decision', {})
    elif isinstance(active_repo, dict) and isinstance(active_repo.get('claude_topic_fit_decision'), dict):
        claude_decision = active_repo.get('claude_topic_fit_decision', {})
    claude_accepted_repo_ready = bool(selected_repo) and (
        str(repo_selection.get('selection_gate', '') if isinstance(repo_selection, dict) else '').startswith(('accepted_by_claude', 'accepted_by_deterministic_base_switch_gate'))
        or bool(claude_decision.get('accept_as_current_best'))
    )
    current_repo = active_repo if isinstance(active_repo, dict) else {}
    if claude_accepted_repo_ready and isinstance(selected_repo, dict) and selected_repo:
        current_repo = {**current_repo, **selected_repo}
        current_repo.setdefault('selection_source', 'evidence_ready_repo_selection')
    current_repo_path = str(current_repo.get('repo_path') or current_repo.get('local_path') or '') if isinstance(current_repo, dict) else ''
    real_probe = load_json(paths.state / "real_dataset_probe.json") if (paths.state / "real_dataset_probe.json").exists() else {}
    bootstrap = load_json(paths.state / "repo_env_bootstrap.json") if (paths.state / "repo_env_bootstrap.json").exists() else {}
    machine = load_json(paths.reports / "machine_profile.json")
    paper_state = get_active_paper_state(args.project, venue=args.venue)
    methods = plan.get("methods", []) if isinstance(plan, dict) else plan
    completed = [row for row in exp_rows if str(row.get("status", "")).lower() in {"completed", "success", "repaired"}]
    failed = [row for row in exp_rows if str(row.get("status", "")).lower() in {"failed", "error", "incomplete_audit"}]
    analyzed_failed = [row for row in failed if row.get("failure_analysis_path")]
    deps = machine.get("dependencies", {}) if isinstance(machine, dict) else {}
    idea_summary = ideas.get("summary", {}) if isinstance(ideas, dict) else {}
    paper_summary = paper_quality.get("summary", {}) if isinstance(paper_quality, dict) else {}
    coding_cfg = cfg.get("coding_agent", {}) if isinstance(cfg, dict) else {}
    coding_backend = "claude"
    coding_state_files = sorted(paths.state.glob("coding_agent_*.json"), key=lambda p: p.stat().st_mtime)
    successful_repairs = 0
    last_coding_backend = ""
    for state_file in coding_state_files:
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("repair_success"):
            successful_repairs += 1
        if payload.get("backend"):
            last_coding_backend = payload.get("backend", "")
    llm_ready = llm_available(cfg)
    llm_reason = "" if llm_ready else llm_disabled_reason(cfg)
    claude_available = bool(find_claude(cfg))
    active_env = bootstrap.get('env_name', '') if isinstance(bootstrap, dict) else ''
    active_env_status = bootstrap.get('status', '') if isinstance(bootstrap, dict) else ''
    active_env_installed = active_env_status == 'completed'
    blocked_datasets = list(repo_data_requirements.get('blocked_datasets', []) if isinstance(repo_data_requirements, dict) else [])
    ready_datasets = list(repo_data_requirements.get('ready_datasets', []) if isinstance(repo_data_requirements, dict) else [])
    if isinstance(real_probe, dict) and str(real_probe.get('repo_path') or '') == current_repo_path:
        for probe in real_probe.get('probes', []) or []:
            if isinstance(probe, dict) and probe.get('claim_ready') and probe.get('loader_probe_success'):
                dataset = str(probe.get('dataset') or '').strip()
                if dataset and dataset not in ready_datasets:
                    ready_datasets.append(dataset)
    blocked_datasets = [item for item in blocked_datasets if item not in set(ready_datasets)]
    blocker_current = _blocker_matches_active_repo(blocker_packet, current_repo)
    stale_blocker = bool(isinstance(blocker_packet, dict) and blocker_packet and not blocker_current)
    data_blocker_cleared = bool(ready_datasets)
    blocker_type = ""
    blocker_evidence_ready_count = ""
    blocker_completion_condition = ""
    if isinstance(blocker_packet, dict) and blocker_current and not data_blocker_cleared:
        blocker_type = str(blocker_packet.get('blocker_type', '') or '')
        blocker_evidence_ready_count = blocker_packet.get('evidence_ready_candidate_count', '')
        blocker_completion_condition = str(blocker_packet.get('completion_condition', '') or '')
    repo_selection_gate = repo_selection.get('selection_gate', '') if isinstance(repo_selection, dict) else ''
    repo_selection_block_reason = repo_selection_blocker.get('reason', '') if isinstance(repo_selection_blocker, dict) else ''
    lines = [
        "# Workflow Status\n\n",
        f"- project: {cfg.get('name', args.project)}\n",
        f"- topic: {cfg.get('topic', '')}\n",
        f"- conda_env: {active_env or cfg.get('conda_env', '')}\n",
        f"- configured_conda_env: {cfg.get('conda_env', '')}\n",
        f"- active_repo: {current_repo.get('name', '') if isinstance(current_repo, dict) else ''}\n",
        f"- active_repo_path: {current_repo_path}\n",
        f"- active_repo_env: {active_env}\n",
        f"- active_repo_env_status: {active_env_status or 'unknown'}\n",
        f"- active_repo_env_installed: {active_env_installed}\n",
        f"- active_repo_ready_datasets: {', '.join(ready_datasets) if ready_datasets else 'none'}\n",
        f"- active_repo_blocked_datasets: {', '.join(blocked_datasets) if blocked_datasets else 'none'}\n",
        f"- claude_accepted_transformable_repo: {claude_accepted_repo_ready}\n",
        f"- claude_repo_decision: {claude_decision.get('decision', '') if isinstance(claude_decision, dict) else ''}\n",
        f"- claude_repo_confidence: {claude_decision.get('confidence', '') if isinstance(claude_decision, dict) else ''}\n",
        f"- claude_repo_rationale: {claude_decision.get('rationale', '') if isinstance(claude_decision, dict) else ''}\n",
        f"- repo_selection_gate: {repo_selection_gate or 'not-run'}\n",
        f"- repo_selection_block_reason: {repo_selection_block_reason or 'none'}\n",
        f"- data_unavailability_decision: {data_policy.get('decision', '') if isinstance(data_policy, dict) else ''}\n",
        f"- blocker_type: {blocker_type}\n",
        f"- evidence_ready_candidate_count: {blocker_evidence_ready_count}\n",
        f"- blocker_completion_condition: {blocker_completion_condition}\n",
        f"- stale_blocker_packet_ignored: {stale_blocker}\n",
        f"- user_prompt: {cfg.get('user_prompt', '')}\n",
        f"- active_paper_venue: {paper_state.get('venue', '') if isinstance(paper_state, dict) else ''}\n",
        f"- configured_coding_backend: {coding_backend}\n",
        f"- llm_backend_ready: {llm_ready}\n",
        f"- llm_backend_reason: {llm_reason or 'ready'}\n",
        f"- claude_backend_available: {claude_available}\n",
        f"- coding_agent_runs: {len(coding_state_files)}\n",
        f"- coding_agent_successful_repairs: {successful_repairs}\n",
        f"- coding_agent_last_backend: {last_coding_backend or 'none'}\n",
        f"- discovery snapshots: {len(list(paths.discover.glob('*.json')))}\n",
        f"- ingested paper folders: {count_dirs(paths.raw_papers)}\n",
        f"- recent_high_priority_papers: {paper_summary.get('recent_high_priority_count', 0)}\n",
        f"- recent_candidate_papers: {paper_summary.get('recent_candidate_count', 0)}\n",
        f"- idea_candidates: {idea_summary.get('idea_count', 0)}\n",
        f"- pursue_ready_ideas: {idea_summary.get('pursue_count', 0)}\n",
        f"- wiki paper pages: {len(list(paths.wiki_papers.glob('*.md')))}\n",
        f"- wiki concept pages: {len(list(paths.wiki_concepts.glob('*.md')))}\n",
        f"- wiki entity pages: {len(list(paths.wiki_entities.glob('*.md')))}\n",
        f"- comparison pages: {len(list(paths.wiki_comparisons.glob('*.md')))}\n",
        f"- repo candidates: {len(repo_rows)}\n",
        f"- datasets tracked: {len(ds_rows)}\n",
        f"- methods in parallel plan: {len(methods)}\n",
        f"- experiments logged: {len(exp_rows)}\n",
        f"- experiments completed: {len(completed)}\n",
        f"- experiments failed_or_incomplete: {len(failed)}\n",
        f"- failed runs with analysis: {len(analyzed_failed)}\n",
        f"- natural-language requests logged: {len(requests) if isinstance(requests, list) else 0}\n",
        f"- environment bootstrap prepared: {(paths.state / 'repo_env_bootstrap.json').exists()}\n",
        f"- next actions generated: {(paths.state / 'next_actions.json').exists()}\n",
        f"- evolution memory ready: {(paths.state / 'evolution_memory.json').exists()}\n",
        f"- workflow blueprint ready: {(paths.planning / 'workflow_blueprint.md').exists()}\n",
        f"- workflow connectivity audit ready: {(paths.reports / 'workflow_connectivity.md').exists()}\n",
        f"- work status ready: {paths.work_status.exists()}\n",
        f"- machine profile ready: {(paths.reports / 'machine_profile.json').exists()}\n",
        f"- dependency install plan ready: {(paths.reports / 'dependency_install_plan.md').exists()}\n",
        f"- core runtime ready: {deps.get('ready_for_core_loop', False) if isinstance(deps, dict) else False}\n",
        f"- latex runtime ready: {deps.get('ready_for_latex', False) if isinstance(deps, dict) else False}\n",
        f"- required runtime gaps: {', '.join(deps.get('required_missing', [])) if isinstance(deps, dict) and deps.get('required_missing') else 'none'}\n",
        f"- recommended runtime gaps: {', '.join(deps.get('recommended_missing', [])) if isinstance(deps, dict) and deps.get('recommended_missing') else 'none'}\n",
        f"- paper draft ready: {paper_state.get('draft_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- paper review packet ready: {paper_state.get('review_packet_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- internal paper reviews ready: {paper_state.get('internal_reviews_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- aggregated review ready: {paper_state.get('paper_reviews_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- aggregated review verdict: {paper_state.get('paper_review_verdict', '') if isinstance(paper_state, dict) else ''}\n",
        f"- author response ready: {paper_state.get('author_response_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- re-review ready: {paper_state.get('re_review_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- re-review verdict: {paper_state.get('re_review_verdict', '') if isinstance(paper_state, dict) else ''}\n",
        f"- promotion gate: {paper_state.get('promotion_gate', '') if isinstance(paper_state, dict) else ''}\n",
        f"- revised draft ready: {paper_state.get('paper_revision_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- venue template fetched: {paper_state.get('template_fetched', False) if isinstance(paper_state, dict) else False}\n",
        f"- venue template format ready: {paper_state.get('venue_template_format_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- venue template format status: {paper_state.get('paper_venue_format_status', '') if isinstance(paper_state, dict) else ''}\n",
        f"- template fetch error: {paper_state.get('template_fetch_error', '') if isinstance(paper_state, dict) else ''}\n",
        f"- render ready: {paper_state.get('render_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- pdf ready: {paper_state.get('pdf_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- draft hypotheses present: {'status: draft' in (paths.wiki_gaps / 'hypotheses.md').read_text(encoding='utf-8') if (paths.wiki_gaps / 'hypotheses.md').exists() else False}\n",
        f"- ingested ids tracked: {len(load_json(paths.state / 'ingested_ids.json'))}\n",
        f"- compiled ids tracked: {len(load_json(paths.state / 'compiled_ids.json'))}\n",
        f"- loop runs tracked: {len(load_json(paths.state / 'loop_history.json'))}\n",
        "- standalone_runner: scripts/run_autonomous_research.py\n",
    ]
    report.write_text("".join(lines), encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
