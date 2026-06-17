#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from agent_state import append_agent_log, mark_agent, upsert_agent
from project_paths import build_paths, management_python
from pipeline_guard import guard_fresh_base_blocker_entry
from run_full_research_cycle import current_find_full_text_gate_status
from run_project import current_find_execution_contract

def _repo_root_from_script() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "framework").is_dir() and (candidate / "modules").is_dir() and (candidate / "web").is_dir():
            return candidate
    return current.parents[1]

ROOT = _repo_root_from_script()
from taste_pythonpath import script_resolver

SCRIPTS = script_resolver(ROOT)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def compact_selected_contract(contract: dict) -> dict:
    if not isinstance(contract, dict):
        return {}
    return {
        "status": contract.get("status", ""),
        "run_id": contract.get("run_id", ""),
        "required": bool(contract.get("required")),
        "selected_plan_id": contract.get("selected_plan_id", ""),
        "selected_idea_id": contract.get("selected_idea_id", ""),
        "selected_plan": contract.get("selected_plan") if isinstance(contract.get("selected_plan"), dict) else {},
        "selected_idea": contract.get("selected_idea") if isinstance(contract.get("selected_idea"), dict) else {},
        "selected_by": contract.get("selected_by", ""),
        "selection_issue": contract.get("selection_issue", ""),
        "execution_policy": contract.get("execution_policy") if isinstance(contract.get("execution_policy"), dict) else {},
        "reason": contract.get("reason", ""),
        "candidate_counts": contract.get("candidate_counts", {}),
    }


def selected_plan_contract_ready(contract: dict) -> bool:
    if not isinstance(contract, dict) or not contract.get("required"):
        return True
    selected_plan_id = str(contract.get("selected_plan_id") or "").strip()
    selection_issue = str(contract.get("selection_issue") or "").strip()
    status = str(contract.get("status") or "").strip()
    counts = contract.get("candidate_counts") if isinstance(contract.get("candidate_counts"), dict) else {}
    candidate_count = int(counts.get("ideas") or 0) + int(counts.get("plans") or 0)
    return bool(candidate_count == 0 or (selected_plan_id and not selection_issue and status == "selected_plan_ready"))


def stop_for_missing_selected_plan(args, paths, agent_id: str) -> dict:
    contract = current_find_execution_contract(paths)
    compact = compact_selected_contract(contract)
    if selected_plan_contract_ready(contract):
        return compact
    selection_issue = str(contract.get("selection_issue") or "missing_selected_plan").strip() or "missing_selected_plan"
    status = "blocked_ambiguous_selected_plan" if selection_issue == "ambiguous_selected_plan" else "blocked_missing_selected_plan"
    state_path = paths.state / "autonomous_current_find_selected_plan_gate_stop.json"
    state_path.write_text(json.dumps({
        "project": args.project,
        "status": status,
        "stage": "current-find-selected-plan-gate",
        "selected_contract": compact,
        "selection_issue": selection_issue,
        "policy": "Current-Find Read/Idea/Plan may produce multiple candidates, but autonomous environment, experiment, paper, and claim stages must consume exactly one selected_plan_id chosen by the main Claude Code/human-supervised contract. Non-selected candidates are backlog only.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mark_agent(args.project, agent_id, "blocked", current_step="current Find selected_plan_id contract is not ready; execute-plan suppressed until wrapper/project Claude selects exactly one valid plan")
    run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="blocker-action-plan-selected-plan-gate")
    raise SystemExit(2)


def reference_gate_switch_prompt(project: str, venue: str, gate: dict, action_plan: dict) -> str:
    actions = action_plan.get("actions", [])[:6] if isinstance(action_plan.get("actions", []), list) else []
    return f"""
The workflow must switch or backtrack the reference base for project `{project}`.

The active reference work has not passed paper-protocol reproduction. Do not write the paper and do not tune novel methods on this base as the main route. Use the existing TASTE repo/data/env selection and experiment-loop contracts to search for a better auditable base, or record why no better base is available.

Current reference gate:
```json
{json.dumps(gate, ensure_ascii=False, indent=2)}
```

Top blocker actions:
```json
{json.dumps(actions, ensure_ascii=False, indent=2)}
```

Required trajectory:
1. Treat the current base as blocked for paper-level claims unless the paper data split/protocol becomes reproducible.
2. Run evidence-ready repo/literature backtracking using existing TASTE discovery, repo audit, loader probe, Claude repo judgment, and environment strategy.
3. Select a new base only if it has paper target evidence, runnable code/env, loader-ready real data, and feasible reproduction cost.
4. If no better base exists, persist that fact in `repo_selection_blocker.json`, research memory, failed hypothesis graph, and blocker action plan.
5. Keep `experiment_registry.json` and `experiment_records.csv` unified; do not create another record file.

Venue: {venue or 'not specified'}
Return concise Markdown with: base-switch evidence, repos/data audited, selected route or no-route blocker, files updated, and next TASTE command.
""".strip()


def reference_gate_repair_prompt(project: str, venue: str, gate: dict) -> str:
    blockers = gate.get("blockers", []) if isinstance(gate.get("blockers", []), list) else []
    actions = gate.get("required_next_actions", []) if isinstance(gate.get("required_next_actions", []), list) else []
    return f"""
TASTE experiment iteration is blocked by the reference-work reproduction gate for project `{project}`.

Do not start novel method tuning or paper writing yet. First repair the base-work reproduction evidence or switch base with evidence.

Current gate:
```json
{json.dumps(gate, ensure_ascii=False, indent=2)}
```

Blocking reasons:
```json
{json.dumps(blockers, ensure_ascii=False, indent=2)}
```

Required next actions:
```json
{json.dumps(actions, ensure_ascii=False, indent=2)}
```

Use the existing TASTE experiment-loop and evidence-gate contracts. Required trajectory:
1. Locate or record the official paper/table target metric for the active repo/dataset.
2. Run or repair reference reproduction only through TASTE audit wrappers, preserving command, env, repo, data, logs, loss/epoch traces, metrics, bad-case/missing-bad-case evidence, runtime, config, and hashes.
3. Compare reproduction results to the paper target and compute budget.
4. If the current local run is documented as protocol/data-split incomparable with the paper target, that is still a blocker. Do not edit audit scripts to turn incomparability into pass. Instead reproduce the paper protocol/data split or route to switch base with evidence.
5. If reproduction is impossible or not worth the compute, update research state to search/switch to a better base instead of tuning blindly.
6. Keep `experiment_registry.json` and `experiment_records.csv` as the only experiment record source; do not create another experiment-record file.

Venue: {venue or 'not specified'}
Return concise Markdown with: target evidence, actions taken, commands run, files changed, remaining blocker or cleared decision.
""".strip()


def experiment_evidence_repair_prompt(project: str, venue: str, progress_gate: dict, reference_gate: dict, iteration_audit: dict, selected_contract: dict | None = None) -> str:
    progress_blockers = progress_gate.get("blockers", []) if isinstance(progress_gate.get("blockers", []), list) else []
    loop_blockers = iteration_audit.get("blockers", []) if isinstance(iteration_audit.get("blockers", []), list) else []
    loop_warnings = iteration_audit.get("warnings", []) if isinstance(iteration_audit.get("warnings", []), list) else []
    selected_contract = compact_selected_contract(selected_contract or {})
    return f"""
TASTE experiment iteration has not produced evidence strong enough for paper writing in project `{project}`.

Do not start paper writing, figure repair, page-count repair, or PDF polishing. Continue the experiment trajectory only: generate/refine idea -> modify or explicitly reuse code -> run real-data experiment -> inspect logs/loss/metrics -> analyze failures/bad cases -> reflect -> plan the next action.

Live experiment safety and observability rules:
- If a training process is already alive, observe it non-invasively only. Do not send signals, attach strace/gdb/py-spy, read blocking `/proc/<pid>/fd/*` pipes, kill, restart, or launch a duplicate unless the process has exited or the artifact-local log proves a hard failure.
- For every new training launch, use unbuffered output (`PYTHONUNBUFFERED=1`, `python -u`, or an equivalent `stdbuf -oL -eL`) and redirect stdout/stderr to a project artifact log so `/api/jobs` and the taskbar can show real epoch/loss/metric progress.
- If the log is temporarily empty while the process is alive, record `running_waiting_for_output` and keep waiting; do not probe the process in a way that changes its state.
- If a run must be stopped, first write the reason, PID, command, artifact path, and evidence to an artifact-local audit or run note.

Before selecting a new method, base-work switch, or code route, read `planning/literature_tool_packet.md` or `state/literature_tool_packet.json` plus at least one raw artifact under `planning/finding/`. If the packet is stale, empty, or unrelated to the current blocker, run `{management_python()} modules/finding/main.py --action run_literature_tool --project {project} --query "<targeted research query>" --fast-mode --venue {venue}` as an internal project-agent survey and read the packet path printed under `state/internal_literature_runs/...`; do not publish it to the web-facing current Find unless the TASTE wrapper/user explicitly requests `--publish-current-find`. Use these survey outputs as planning signals only; local experiment artifacts are still required for claims.

Current-Find selected execution contract:
```json
{json.dumps(selected_contract, ensure_ascii=False, indent=2)}
```
Hard rule: current-Find Read/Idea/Plan may contain several candidates, but the main Claude Code/human-supervised contract must choose exactly one `selected_plan_id`. Environment, experiment, paper, and claim work may consume only that selected plan/idea. Non-selected ideas/plans are backlog only and must not drive implementation, launches, paper prose, or claim updates. If `selected_plan_id` is empty while candidates exist, stop and ask TASTE to rerun `modules/reading/main.py --action current_find_research_plan --project {project}` or the project-agent selection stage; do not invent an experiment route.

Reference reproduction gate:
```json
{json.dumps(reference_gate, ensure_ascii=False, indent=2)}
```

Scientific progress gate:
```json
{json.dumps(progress_gate, ensure_ascii=False, indent=2)}
```

Experiment-loop audit:
```json
{json.dumps(iteration_audit, ensure_ascii=False, indent=2)}
```

Blocking reasons:
```json
{json.dumps(progress_blockers + loop_blockers + loop_warnings, ensure_ascii=False, indent=2)}
```

Required trajectory:
1. Keep the selected reference-work reproduction intact. If it becomes incomparable or fails, return to reference reproduction repair before novel experiments.
2. Choose the next bounded real-data experiment from existing TASTE trajectory memory, failed-hypothesis graph, novelty map, evidence manifest, and the literature tool packet.
3. Use the literature packet to choose nearest papers, transformable mechanisms, base-work alternatives, and negative/boundary examples; do not hard-code topic labels. Negative/boundary examples are for internal pruning and route design only, not for paper contributions or automatic topic re-scope.
4. Use the project conda environment and existing wrappers/skills. Record exact command, env, repo, dataset, config, code diff, artifact path, stdout/stderr, loss or epoch trace, metrics, bad-case or missing-bad-case evidence, and claim verdict.
5. Compare against the audit-ready baseline/control on the same dataset, split, metric, and evaluation mode. Do not promote results that do not beat the baseline.
6. If repeated attempts fail, data cannot support the user target topic, or compute is not viable, record a blocked/prune/proposed-switch decision with evidence. Do not rewrite the target topic to a weaker paper story unless deterministic TASTE route gates and user/project configuration authorize it.
7. Keep `experiment_registry.json` and `experiment_records.csv` as the unified experiment record; do not create another record file.

Venue: {venue or 'not specified'}
Return concise Markdown with: next idea, code/command actions, log/loss findings, metric comparison, reflection, next plan, and whether the progress gate is still blocked.
""".strip()


def gate_passed(gate: dict, *, decision: str | None = None) -> bool:
    if not isinstance(gate, dict) or not gate:
        return False
    if gate.get("status") != "pass":
        return False
    return decision is None or gate.get("decision") == decision




def stop_for_current_find_full_text_gate(args, paths, agent_id: str) -> None:
    ensure_cmd = [sys.executable, str(SCRIPTS / "ensure_current_find_research_plan.py"), "--project", args.project]
    run(ensure_cmd, required=False, project=args.project, agent_id=agent_id, stage="current-find-read-idea-plan-gate")
    gate = current_find_full_text_gate_status(paths)
    if not isinstance(gate, dict) or not gate.get("blocking"):
        return
    state_path = paths.state / "autonomous_current_find_full_text_gate_stop.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "project": args.project,
        "status": "blocked_current_find_full_text_reading",
        "stage": "current-find-read-idea-plan-gate",
        "gate": gate,
        "policy": "Standalone autonomous research must not enter reference repair, experiment iteration, trajectory, paper writing, or claim repair until the Read-stage reading packet has verified full-text evidence or eligible same-run replacements plus non-placeholder deep-read synthesis.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mark_agent(args.project, agent_id, "blocked", current_step="Read-stage full-text packet gate blocked autonomous research before experiments")
    run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="blocker-action-plan-current-find-read-gate")
    raise SystemExit(2)

def selected_base_requires_base_switch_gate(paths) -> tuple[bool, dict, dict]:
    viability = load_json(paths.state / "selected_base_viability_gate.json", {})
    switch_gate = load_json(paths.state / "base_switch_gate.json", {})
    required = bool(
        isinstance(viability, dict)
        and viability.get("status") == "blocked"
        and viability.get("decision") == "base_switch_gate_required"
    )
    authorized = bool(
        isinstance(switch_gate, dict)
        and switch_gate.get("status") == "pass"
        and switch_gate.get("decision") == "authorize_base_switch"
        and switch_gate.get("switch_authorized") is True
    )
    return bool(required and not authorized), viability if isinstance(viability, dict) else {}, switch_gate if isinstance(switch_gate, dict) else {}


def stop_for_selected_base_switch_gate(args, paths, agent_id: str, *, stage: str) -> None:
    run([sys.executable, str(SCRIPTS / "audit_deterministic_base_switch_gate.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage=f"{stage}-base-switch-gate")
    run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage=f"{stage}-blocker-action-plan")
    required, viability, switch_gate = selected_base_requires_base_switch_gate(paths)
    if not required:
        return
    state_path = paths.state / "autonomous_selected_base_gate_stop.json"
    state_path.write_text(json.dumps({
        "project": args.project,
        "status": "blocked_selected_base_viability_gate",
        "stage": stage,
        "selected_base_viability_gate": viability,
        "base_switch_gate": switch_gate,
        "policy": "Autonomous research must not run candidate/alternative main-route experiments or claim repair while selected_base_viability_gate.decision=base_switch_gate_required and base_switch_gate is not authorized. Current selected-base evidence repair may continue via framework/scripts/run_module.py experimenting --action launch --route-scope selected_base_current_route; bounded candidate base-switch evidence collection may use --route-scope base_switch_evidence_collection and cannot promote claims or switch active route.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mark_agent(args.project, agent_id, "blocked", current_step="selected-base viability requires deterministic base-switch gate; candidate main-route launches suppressed, candidate gate-evidence collection requires launcher route_scope=base_switch_evidence_collection, and current-route repair requires launcher route_scope=selected_base_current_route")
    run([sys.executable, str(SCRIPTS / "generate_handoff.py"), "--project", args.project], required=False, project=args.project, agent_id=agent_id, stage="handoff")
    raise SystemExit(2)


def run(cmd: list[str], required: bool = True, project: str = "", agent_id: str = "main", stage: str = "autonomous") -> int:
    if project:
        upsert_agent(project, agent_id, status="running", stage=stage, current_step=" ".join(cmd[:5]), command=cmd)
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    if project:
        upsert_agent(project, agent_id, pid=proc.pid, status="running", current_step="subprocess started")
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        if project:
            append_agent_log(project, agent_id, line.rstrip())
    returncode = proc.wait()
    if required and returncode != 0:
        if project:
            mark_agent(project, agent_id, "error", current_step=f"subprocess failed with exit code {returncode}")
        raise SystemExit(returncode)
    return returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone autonomous research runner that uses Claude Code for downstream project work.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--prompt", default="")
    parser.add_argument("--topic", default="")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--max-results", type=int)
    parser.add_argument("--discover-retries", type=int)
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--skip-semantic-scholar", action="store_true")
    parser.add_argument("--skip-github", action="store_true")
    parser.add_argument("--skip-initialization", action="store_true")
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--parallel-method", action="append", default=[])
    parser.add_argument("--benchmark")
    parser.add_argument("--metric")
    parser.add_argument("--dataset")
    parser.add_argument("--repo-name")
    parser.add_argument("--repo-path")
    parser.add_argument("--command-template")
    parser.add_argument("--execute-plan", action="store_true")
    parser.add_argument("--prepare-env", action="store_true")
    parser.add_argument("--real-bootstrap-env", action="store_true")
    parser.add_argument("--max-launches", type=int)
    parser.add_argument("--conda-env", default="")
    parser.add_argument("--coding-backend", default="", help="Deprecated compatibility option; downstream execution uses Claude Code.")
    parser.add_argument("--venue", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--skip-paper", action="store_true")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-compile", action="store_true")
    parser.add_argument("--strict-template", action="store_true")
    parser.add_argument("--force-template", dest="force_template", action="store_true")
    parser.add_argument("--generate-inspection-paper", dest="force_template", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--generate-paper-preview", dest="force_template", action="store_true")
    parser.add_argument("--template-url", default="")
    parser.add_argument("--template-archive-path", default="")
    parser.add_argument("--auto-install-latex", action="store_true")
    parser.add_argument("--deep-literature-survey", action="store_true", help="Run finding in full literature survey mode during initialization.")
    args = parser.parse_args()
    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name)
    if guard_rc is not None:
        raise SystemExit(guard_rc)
    agent_id = "main"
    upsert_agent(
        args.project,
        agent_id,
        name="TASTE 自主科研主控",
        role="main",
        stage="autonomous",
        status="running",
        goal=(args.prompt or args.topic or "autonomous research")[:500],
        current_step="starting autonomous research",
    )

    paths = build_paths(args.project)
    stop_for_current_find_full_text_gate(args, paths, agent_id)
    selected_execution_contract = stop_for_missing_selected_plan(args, paths, agent_id) if args.execute_plan else compact_selected_contract(current_find_execution_contract(paths))
    run([sys.executable, str(SCRIPTS / "audit_reference_reproduction.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="reference-reproduction-gate")
    run([sys.executable, str(SCRIPTS / "audit_selected_base_viability.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="selected-base-viability-gate")
    run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="blocker-action-plan")
    selected_gate_required, _, _ = selected_base_requires_base_switch_gate(paths)
    if selected_gate_required:
        stop_for_selected_base_switch_gate(args, paths, agent_id, stage="selected-base-viability-gate")
    reference_gate = load_json(paths.state / "reference_reproduction_gate.json", {})
    if isinstance(reference_gate, dict) and reference_gate and not gate_passed(reference_gate, decision="continue_base"):
        if reference_gate.get("decision") == "switch_base":
            upsert_agent(
                args.project,
                agent_id,
                status="running",
                stage="reference-base-switch",
                current_step="switching or backtracking the blocked reference base before novel experiments",
            )
            action_plan = load_json(paths.state / "blocker_action_plan.json", {})
            prompt_path = paths.state / "autonomous_reference_base_switch_prompt.md"
            prompt_path.write_text(reference_gate_switch_prompt(args.project, args.venue, reference_gate, action_plan), encoding="utf-8")
            run([
                sys.executable,
                str(SCRIPTS / "claude_project_session.py"),
                "--project",
                args.project,
                "--stage",
                "reference-base-switch",
                "--message-file",
                str(prompt_path),
                "--timeout-sec",
                "14400",
                "--agent-id",
                agent_id,
            ], required=False, project=args.project, agent_id=agent_id, stage="reference-base-switch")
            env_cmd = [sys.executable, str(SCRIPTS / "run_environment_stage.py"), "--project", args.project, "--real-bootstrap-env", "--repo-search-rounds", "3", "--skip-reference-repair"]
            if args.venue:
                env_cmd.extend(["--venue", args.venue])
            run(env_cmd, required=False, project=args.project, agent_id=agent_id, stage="reference-base-switch-search")
        else:
            upsert_agent(
                args.project,
                agent_id,
                status="running",
                stage="reference-reproduction-repair",
                current_step="repairing paper-level reference reproduction before novel experiments",
            )
            prompt_path = paths.state / "autonomous_reference_reproduction_repair_prompt.md"
            prompt_path.write_text(reference_gate_repair_prompt(args.project, args.venue, reference_gate), encoding="utf-8")
            run([
                sys.executable,
                str(SCRIPTS / "claude_project_session.py"),
                "--project",
                args.project,
                "--stage",
                "reference-reproduction-repair",
                "--message-file",
                str(prompt_path),
                "--timeout-sec",
                "14400",
                "--agent-id",
                agent_id,
            ], required=False, project=args.project, agent_id=agent_id, stage="reference-reproduction-repair")
        run([sys.executable, str(SCRIPTS / "audit_reference_reproduction.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="reference-reproduction-gate-after-repair")
        run([sys.executable, str(SCRIPTS / "audit_experiment_iteration.py"), "--project", args.project], required=False, project=args.project, agent_id=agent_id, stage="experiment-iteration-audit")
        run([sys.executable, str(SCRIPTS / "audit_paper_evidence.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="paper-evidence-gate")
        run([sys.executable, str(SCRIPTS / "audit_submission_readiness.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="submission-readiness")
        run([sys.executable, str(SCRIPTS / "build_research_trajectory_system.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="trajectory")
        run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="blocker-action-plan")
        reference_gate = load_json(paths.state / "reference_reproduction_gate.json", {})
        if isinstance(reference_gate, dict) and not gate_passed(reference_gate, decision="continue_base"):
            if reference_gate.get("decision") == "switch_base":
                mark_agent(args.project, agent_id, "blocked", current_step="reference base switch/backtracking is required before novel experiment iteration")
            else:
                mark_agent(args.project, agent_id, "blocked", current_step="reference reproduction gate remains blocked; novel experiment iteration was not launched")
            run([sys.executable, str(SCRIPTS / "generate_handoff.py"), "--project", args.project], required=False, project=args.project, agent_id=agent_id, stage="handoff")
            return

    loop_cmd = [sys.executable, str(SCRIPTS / "run_loop.py"), "--project", args.project, "--iterations", str(args.iterations)]
    if args.prompt:
        loop_cmd.extend(["--prompt", args.prompt])
    if args.topic:
        loop_cmd.extend(["--topic", args.topic])
    if args.max_results is not None:
        loop_cmd.extend(["--max-results", str(args.max_results)])
    if args.discover_retries is not None:
        loop_cmd.extend(["--discover-retries", str(args.discover_retries)])
    if args.skip_llm:
        loop_cmd.append("--skip-llm")
    if args.skip_semantic_scholar:
        loop_cmd.append("--skip-semantic-scholar")
    if args.skip_github:
        loop_cmd.append("--skip-github")
    if args.skip_initialization:
        loop_cmd.append("--skip-initialization")
    if args.skip_discovery:
        os.environ.setdefault("USE_EXISTING_LITERATURE_PACKET", "1")
        loop_cmd.append("--skip-discovery")
    if args.deep_literature_survey:
        loop_cmd.append("--deep-literature-survey")
    if args.execute_plan:
        loop_cmd.append("--execute-plan")
    if args.prepare_env:
        loop_cmd.append("--prepare-env")
    if args.real_bootstrap_env:
        loop_cmd.append("--real-bootstrap-env")
    if args.benchmark:
        loop_cmd.extend(["--benchmark", args.benchmark])
    if args.metric:
        loop_cmd.extend(["--metric", args.metric])
    if args.dataset:
        loop_cmd.extend(["--dataset", args.dataset])
    if args.repo_name:
        loop_cmd.extend(["--repo-name", args.repo_name])
    if args.repo_path:
        loop_cmd.extend(["--repo-path", args.repo_path])
    if args.command_template:
        loop_cmd.extend(["--command-template", args.command_template])
    if args.max_launches is not None:
        loop_cmd.extend(["--max-launches", str(args.max_launches)])
    if args.conda_env:
        loop_cmd.extend(["--conda-env", args.conda_env])
    if args.venue:
        loop_cmd.extend(["--venue", args.venue])
    for method in args.parallel_method:
        loop_cmd.extend(["--parallel-method", method])

    run(loop_cmd, project=args.project, agent_id=agent_id, stage="experiment")

    supervisor_cmd = [
        sys.executable,
        str(SCRIPTS / "run_research_trajectory_supervisor.py"),
        "--project",
        args.project,
        "--rounds",
        str(max(1, args.iterations)),
        "--timeout-sec",
        "14400",
    ]
    if args.venue:
        supervisor_cmd.extend(["--venue", args.venue])
    run(supervisor_cmd, required=False, project=args.project, agent_id=agent_id, stage="trajectory")

    # Rebuild gates after experiments before paper production. Paper writing is
    # blocked until the reproduced base is still valid and a real-data candidate
    # beats a comparable audit-ready baseline/control.
    run([sys.executable, str(SCRIPTS / "audit_reference_reproduction.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="reference-reproduction-gate-post-experiment")
    run([sys.executable, str(SCRIPTS / "audit_experiment_iteration.py"), "--project", args.project], required=False, project=args.project, agent_id=agent_id, stage="experiment-iteration-audit-post-experiment")
    run([sys.executable, str(SCRIPTS / "audit_paper_evidence.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="paper-evidence-gate-post-experiment")
    run([sys.executable, str(SCRIPTS / "audit_submission_readiness.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="submission-readiness-post-experiment")
    run([sys.executable, str(SCRIPTS / "audit_selected_base_viability.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="selected-base-viability-gate-post-experiment")
    run([sys.executable, str(SCRIPTS / "build_research_trajectory_system.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="trajectory-post-experiment")
    run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="blocker-action-plan-post-experiment")

    reference_gate = load_json(paths.state / "reference_reproduction_gate.json", {})
    progress_gate = load_json(paths.state / "scientific_progress_gate.json", {})
    iteration_audit = load_json(paths.state / "experiment_iteration_audit.json", {})
    if not gate_passed(reference_gate, decision="continue_base"):
        mark_agent(args.project, agent_id, "blocked", current_step="reference reproduction gate became blocked after experiment checks; paper stage was not launched")
        run([sys.executable, str(SCRIPTS / "generate_handoff.py"), "--project", args.project], required=False, project=args.project, agent_id=agent_id, stage="handoff")
        return
    selected_gate_required, _, _ = selected_base_requires_base_switch_gate(paths)
    if selected_gate_required:
        stop_for_selected_base_switch_gate(args, paths, agent_id, stage="post-experiment-selected-base-viability-gate")
    if not gate_passed(progress_gate):
        upsert_agent(
            args.project,
            agent_id,
            status="running",
            stage="experiment-evidence-repair",
            current_step="repairing scientific progress evidence before paper writing",
        )
        prompt_path = paths.state / "autonomous_experiment_evidence_repair_prompt.md"
        prompt_path.write_text(experiment_evidence_repair_prompt(args.project, args.venue, progress_gate, reference_gate, iteration_audit, selected_execution_contract), encoding="utf-8")
        run([
            sys.executable,
            str(SCRIPTS / "claude_project_session.py"),
            "--project",
            args.project,
            "--stage",
            "experiment-evidence-repair",
            "--message-file",
            str(prompt_path),
            "--timeout-sec",
            "14400",
            "--agent-id",
            agent_id,
        ], required=False, project=args.project, agent_id=agent_id, stage="experiment-evidence-repair")
        run([sys.executable, str(SCRIPTS / "audit_reference_reproduction.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="reference-reproduction-gate-after-experiment-repair")
        run([sys.executable, str(SCRIPTS / "audit_experiment_iteration.py"), "--project", args.project], required=False, project=args.project, agent_id=agent_id, stage="experiment-iteration-audit-after-repair")
        run([sys.executable, str(SCRIPTS / "audit_paper_evidence.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="paper-evidence-gate-after-experiment-repair")
        run([sys.executable, str(SCRIPTS / "audit_submission_readiness.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="submission-readiness-after-experiment-repair")
        run([sys.executable, str(SCRIPTS / "audit_selected_base_viability.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="selected-base-viability-gate-after-repair")
        run([sys.executable, str(SCRIPTS / "build_research_trajectory_system.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="trajectory-after-experiment-repair")
        run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="blocker-action-plan-after-experiment-repair")
        reference_gate = load_json(paths.state / "reference_reproduction_gate.json", {})
        progress_gate = load_json(paths.state / "scientific_progress_gate.json", {})
        selected_gate_required, _, _ = selected_base_requires_base_switch_gate(paths)
        if selected_gate_required:
            stop_for_selected_base_switch_gate(args, paths, agent_id, stage="after-experiment-repair-selected-base-viability-gate")
        if not gate_passed(reference_gate, decision="continue_base") or not gate_passed(progress_gate):
            mark_agent(args.project, agent_id, "blocked", current_step="scientific progress gate remains blocked; paper stage was not launched")
            run([sys.executable, str(SCRIPTS / "generate_handoff.py"), "--project", args.project], required=False, project=args.project, agent_id=agent_id, stage="handoff")
            return

    if args.venue and not args.skip_paper:
        paper_cmd = [sys.executable, str(SCRIPTS / "run_paper_pipeline.py"), "--project", args.project, "--venue", args.venue]
        if args.title:
            paper_cmd.extend(["--title", args.title])
        if args.template_url:
            paper_cmd.extend(["--template-url", args.template_url])
        if args.template_archive_path:
            paper_cmd.extend(["--template-archive-path", args.template_archive_path])
        if args.skip_fetch:
            paper_cmd.append("--skip-fetch")
        else:
            paper_cmd.append("--refresh-current-venue")
        if args.skip_compile:
            paper_cmd.append("--skip-compile")
        if args.strict_template:
            paper_cmd.append("--strict-template")
        if args.force_template:
            paper_cmd.append("--generate-paper-preview")
        if args.auto_install_latex:
            paper_cmd.append("--auto-install-latex")
        run(paper_cmd, required=False, project=args.project, agent_id=agent_id, stage="paper")

    run([sys.executable, str(SCRIPTS / "report_status.py"), "--project", args.project] + (["--venue", args.venue] if args.venue else []), required=False, project=args.project, agent_id=agent_id, stage="status")
    run([sys.executable, str(SCRIPTS / "generate_handoff.py"), "--project", args.project], required=False, project=args.project, agent_id=agent_id, stage="handoff")
    mark_agent(args.project, agent_id, "done", current_step="autonomous research complete")


if __name__ == "__main__":
    main()
