#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
from pathlib import Path
from typing import Any

from llm_agent_core import llm_json, llm_text, read_text, safe_json_loads
from llm_client import llm_available, llm_disabled_reason
from project_paths import ROOT, build_paths, load_project_config


ROLE_SEQUENCE = [
    {
        "name": "planner",
        "mission": "Turn the user goal, evidence memory, and machine constraints into a staged research plan with explicit stop/deepen/prune gates.",
        "expects": ["research_questions", "parallel_tracks", "resource_budget", "decision_gates"],
    },
    {
        "name": "researcher",
        "mission": "Pressure-test the plan against recent literature, runnable repos, datasets, baselines, and novelty risks.",
        "expects": ["fresh_literature_targets", "repo_dataset_actions", "nearest_neighbors", "novelty_risks"],
    },
    {
        "name": "coder",
        "mission": "Translate the plan into minimal code-change and experiment actions that a generic LLM engineer can execute without Codex.",
        "expects": ["edit_targets", "validation_commands", "artifact_contract", "fallback_if_patch_fails"],
    },
    {
        "name": "debugger",
        "mission": "Identify likely failure modes before and after experiments, including env mismatch, bad hyperparameters, broken metrics, and module incompatibility.",
        "expects": ["preflight_checks", "failure_triage", "bad_case_slicing_plan", "repair_limits"],
    },
    {
        "name": "analyst",
        "mission": "Define how evidence will be compared across methods, when to run more attempts, when to pivot, and how to avoid shallow one-shot conclusions.",
        "expects": ["comparison_protocol", "claim_tests", "counterexamples", "prune_or_deepen_rules"],
    },
    {
        "name": "writer",
        "mission": "Maintain the paper story, claim ledger, LaTeX readiness, and venue-sensitive writing plan without overclaiming weak evidence.",
        "expects": ["paper_outline_delta", "claim_ledger_updates", "venue_template_needs", "missing_evidence"],
    },
    {
        "name": "critic",
        "mission": "Act as a harsh top-tier AI conference reviewer and veto weak novelty, weak baselines, unsupported claims, and unconvincing ablations.",
        "expects": ["vetoes", "must_fix_before_next_loop", "taste_notes", "acceptance_risk"],
    },
]


def redact_secrets(text: str) -> str:
    text = text or ''
    import os
    for key in ['OPENAI_API_KEY', 'LLM_API_KEY']:
        value = os.environ.get(key, '')
        if value:
            text = text.replace(value, '<redacted>')
    return text


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def trim(value: str, limit: int = 12000) -> str:
    value = value or ""
    return value[-limit:] if len(value) > limit else value


def load_skill_contracts() -> list[dict[str, Any]]:
    skill_root = ROOT / ".claude" / "skills"
    rows: list[dict[str, Any]] = []
    if not skill_root.exists():
        return rows
    for skill_file in sorted(skill_root.glob("*/SKILL.md")):
        text = read_text(skill_file, 1800)
        description = ""
        for line in text.splitlines():
            if line.strip().startswith("description:"):
                description = line.split(":", 1)[1].strip()
                break
        rows.append({"name": skill_file.parent.name, "path": str(skill_file), "description": description})
    return rows


def gather_context(project: str, prompt: str, topic: str, venue: str) -> dict[str, Any]:
    paths = build_paths(project)
    cfg = load_project_config(project)
    return {
        "project": project,
        "topic": topic or cfg.get("topic", ""),
        "user_prompt": prompt or cfg.get("user_prompt", ""),
        "venue": venue,
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "project_config_excerpt": {
            "conda_env": cfg.get("conda_env", ""),
            "coding_agent": cfg.get("coding_agent", {}),
            "parallel_experiments": cfg.get("parallel_experiments", {}),
            "failure_analysis": cfg.get("failure_analysis", {}),
            "llm": {k: v for k, v in (cfg.get("llm", {}) if isinstance(cfg.get("llm", {}), dict) else {}).items() if "key" not in k.lower()},
        },
        "machine_profile": load_json(paths.reports / "machine_profile.json", {}),
        "evolution_memory": load_json(paths.state / "evolution_memory.json", {}),
        "research_trajectory_system": load_json(paths.state / "research_trajectory_system.json", {}),
        "research_memory": load_json(paths.state / "research_memory.json", {}),
        "research_direction_memory": load_json(paths.state / "research_direction_memory.json", {}),
        "research_assurance_layer": load_json(paths.state / "research_assurance_layer.json", {}),
        "research_evidence_integrity": load_json(paths.state / "research_evidence_integrity.json", {}),
        "research_evidence_manifest": load_json(paths.state / "research_evidence_manifest.json", {}),
        "research_graph_history": load_json(paths.state / "research_graph_history.json", {}),
        "research_landscape_assessment": load_json(paths.state / "research_landscape_assessment.json", {}),
        "evolutionary_memory_ledger": load_json(paths.state / "evolutionary_memory_ledger.json", {}),
        "trajectory_optimization_plan": load_json(paths.state / "trajectory_optimization_plan.json", {}),
        "trajectory_checkpoints": load_json(paths.state / "trajectory_checkpoints.json", {}),
        "evolutionary_memory_index": load_json(paths.state / "evolutionary_memory_index.json", {}),
        "research_trajectory_capability_audit": load_json(paths.state / "research_trajectory_capability_audit.json", {}),
        "recoverable_cycle": load_json(paths.state / "recoverable_cycle_summary.json", {}),
        "evo_recoverable_memory": load_json(paths.state / "evo_recoverable_memory.json", {}),
        "research_skill_contracts": load_json(paths.state / "research_skill_contracts.json", load_skill_contracts()),
        "literature_tool_packet": load_json(paths.state / "literature_tool_packet.json", {}),
        "literature_tool_last_run": load_json(paths.state / "literature_tool_last_run.json", {}),
        "taste_literature_intermediates": load_json(paths.state / "taste_literature_intermediates.json", {}),
        "taste_sync": load_json(paths.state / "taste_sync.json", {}),
        "novelty_map": load_json(paths.state / "novelty_map.json", {}),
        "failed_hypothesis_graph": load_json(paths.state / "failed_hypothesis_graph.json", {}),
        "unexplored_niche_graph": load_json(paths.state / "unexplored_niche_graph.json", {}),
        "next_actions": load_json(paths.state / "next_actions.json", {}),
        "parallel_plan": load_json(paths.state / "parallel_plan.json", {}),
        "experiment_registry_tail": load_json(paths.state / "experiment_registry.json", [])[-20:],
        "idea_candidates": load_json(paths.state / "idea_candidates.json", {}),
        "paper_quality": load_json(paths.state / "paper_quality.json", {}),
        "claim_ledger": load_json(paths.state / "claim_ledger.json", {}),
        "reports": {
            "iteration_reflection": trim(read_text(paths.reports / "iteration_reflection.md", 16000)),
            "method_frontier": trim(read_text(paths.reports / "method_frontier.md", 12000)),
            "paper_evidence_audit": trim(read_text(paths.reports / "paper_evidence_audit.md", 12000)),
            "healthcheck": trim(read_text(paths.reports / "healthcheck.md", 8000)),
        },
    }


def compact_context(context: dict[str, Any], limit: int) -> dict[str, Any]:
    if limit <= 0:
        return context
    text = json.dumps(context, ensure_ascii=False)
    if len(text) <= limit:
        return context
    compact = dict(context)
    reports = compact.get("reports", {}) if isinstance(compact.get("reports", {}), dict) else {}
    compact["reports"] = {k: trim(str(v), max(1200, limit // 12)) for k, v in reports.items()}
    compact["_context_compacted"] = f"Original context exceeded {limit} chars; reports were trimmed. Inspect project artifacts before acting on uncertain details."
    return compact


def role_prompt(role: dict[str, Any], context: dict[str, Any], shared_state: dict[str, Any], context_limit: int = 50000) -> str:
    schema = {
        "role": role["name"],
        "summary": "one-paragraph role conclusion",
        "decisions": ["concrete decisions this role makes"],
        "actions": [
            {
                "priority": "P0/P1/P2",
                "owner": "script-or-role",
                "action": "specific next action",
                "success_check": "how the next loop knows it worked",
            }
        ],
        "risks": ["failure modes or scientific risks"],
        "handoff": "what the next role must use",
    }
    return (
        "You are one specialist in TASTE's generic-LLM automated research trajectory system. "
        "Use only the supplied state. Do not pretend experiments were run. Be harsh about novelty, baselines, bad-case evidence, and claim strength. "
        "Treat research_trajectory_system, research_direction_memory, research_evidence_integrity, research_evidence_manifest, research_graph_history, research_landscape_assessment, evolutionary_memory_ledger, trajectory_optimization_plan, trajectory_checkpoints, evolutionary_memory_index, research_trajectory_capability_audit, TASTE recoverable-cycle memory, and local skill contracts as persistent trajectory state, not optional notes. "
        "Treat literature_tool_packet and finding intermediate files as TASTE's internal literature-survey memory: use them for nearest-work, base-switch, code/repo, and experiment-planning decisions, and request a targeted run_literature_tool.py refresh when the packet is stale or insufficient. Literature signals are not local experiment evidence. "
        "Do not discuss the schema or describe what you are about to do. Start immediately with the JSON object. "
        "Every action must be an executable research-loop step, not meta-commentary about planning. "
        "Return strict JSON only matching this schema:\n"
        + json.dumps(schema, ensure_ascii=False, indent=2)
        + "\n\nRole mission:\n"
        + json.dumps(role, ensure_ascii=False, indent=2)
        + "\n\nCurrent shared state from earlier roles:\n"
        + json.dumps(shared_state, ensure_ascii=False, indent=2)[:30000]
        + "\n\nProject context:\n"
        + json.dumps(compact_context(context, context_limit), ensure_ascii=False, indent=2)[:context_limit]
    )




def heuristic_role_json(role: dict[str, Any], raw_content: str) -> dict[str, Any]:
    text = " ".join((raw_content or "").replace("\r", "\n").split())
    if not text:
        return {}
    lowered = text.lower()
    keywords = [
        "literature", "paper", "repo", "codebase", "dataset", "baseline", "experiment", "ablation",
        "bad-case", "counterexample", "novelty", "prune", "deepen", "gate", "method", "evidence",
        "llm", "large language model", "sota", "hyperparameter", "metric",
    ]
    sentences = []
    for raw in raw_content.replace("\r", "\n").replace(";", ".").split("."):
        sentence = " ".join(raw.strip().split())
        if 24 <= len(sentence) <= 320 and any(k in sentence.lower() for k in keywords):
            sentences.append(sentence)
        if len(sentences) >= 8:
            break
    if not sentences:
        sentences = [text[:280]]
    role_name = role["name"]
    actions = []
    for index, sentence in enumerate(sentences[:5]):
        actions.append({
            "priority": "P0" if index < 2 else "P1",
            "owner": role_name,
            "action": sentence,
            "success_check": "A subsequent loop records concrete artifacts, commands, evidence, and a prune/deepen decision for this item.",
        })
    decisions = sentences[:4]
    risks = []
    for risk_kw in ["novelty", "baseline", "dataset", "counterexample", "bad-case", "overclaim"]:
        if risk_kw in lowered:
            risks.append(f"Raw role draft flags {risk_kw} as a claim-strength risk that must be audited before paper writing.")
    if not risks:
        risks = ["The model produced prose instead of schema; treat this as a weak structured signal and verify against artifacts before acting."]
    return {
        "role": role_name,
        "summary": f"Heuristically structured from non-JSON {role_name} draft: " + text[:420],
        "decisions": decisions,
        "actions": actions,
        "risks": risks[:5],
        "handoff": "Use these recovered actions as tentative guidance; prefer raw artifacts and rerun this role with stricter/smaller prompts when possible.",
        "_heuristic_from_raw": True,
    }


def repair_role_json_with_llm(role: dict[str, Any], raw_content: str, cfg: dict[str, Any]) -> dict[str, Any]:
    schema = {
        "role": role["name"],
        "summary": "one-paragraph role conclusion",
        "decisions": ["concrete decisions this role makes"],
        "actions": [{"priority": "P0/P1/P2", "owner": "script-or-role", "action": "specific next action", "success_check": "how the next loop knows it worked"}],
        "risks": ["failure modes or scientific risks"],
        "handoff": "what the next role must use",
    }
    prompt = (
        "Convert the following role draft into strict JSON only. Do not add analysis or meta-commentary. "
        "Actions must be executable research-loop steps rather than descriptions of the schema. "
        "Use this exact schema and preserve concrete technical details when possible:\n"
        + json.dumps(schema, ensure_ascii=False, indent=2)
        + "\n\nRole draft:\n"
        + (raw_content or '')[-12000:]
    )
    parsed, _ = llm_json(prompt, cfg, system_prompt="You are a strict JSON repair tool. Output JSON only.")
    return parsed if isinstance(parsed, dict) else {}


def fallback_role_output(role: dict[str, Any], reason: str) -> dict[str, Any]:
    name = role["name"]
    base_actions = {
        "planner": "Run discovery, repo selection, dataset audit, parallel method planning, execution, reflection, and paper audit in that order.",
        "researcher": "Refresh recent literature and runnable repo/dataset evidence before creating or deepening a method.",
        "coder": "Use scripts/run_coding_agent.py --backend llm for code edits; validate with the exact experiment command and audit artifacts.",
        "debugger": "On bad results, inspect hyperparameters, implementation correctness, metric logging, bad-case slices, and environment mismatch before retrying.",
        "analyst": "Compare several methods in parallel, deepen only methods with claim support and bad-case evidence, and prune repeated weak paths.",
        "writer": "Draft only claims supported by experiment artifacts; fetch venue template only after the research evidence gate is plausible.",
        "critic": "Veto unsupported novelty, missing baselines, absent bad-case slicing, weak counterexamples, and overclaiming.",
    }
    return {
        "role": name,
        "summary": f"Fallback guidance because LLM role execution was unavailable: {reason}",
        "decisions": [base_actions.get(name, "Continue with evidence-driven workflow.")],
        "actions": [
            {
                "priority": "P0",
                "owner": name,
                "action": base_actions.get(name, "Continue with evidence-driven workflow."),
                "success_check": "Next healthcheck/reflection records concrete artifacts instead of only prose.",
            }
        ],
        "risks": ["This fallback is rule-based and should be superseded by configured LLM API outputs."],
        "handoff": "Proceed to the next role using available project artifacts and script outputs.",
    }


def build_markdown(team_state: dict[str, Any]) -> str:
    lines = [
        "# LLM Research Team State\n\n",
        f"- created_at: {team_state.get('created_at', '')}\n",
        f"- llm_available: {team_state.get('llm_available')}\n",
        f"- llm_reason: {team_state.get('llm_reason', '')}\n\n",
        "## Role Outputs\n",
    ]
    for role in team_state.get("roles", []):
        lines.extend([
            f"\n### {role.get('role', 'role')}\n",
            f"{role.get('summary', '')}\n\n",
            "Decisions:\n",
        ])
        for item in role.get("decisions", []) or []:
            lines.append(f"- {item}\n")
        lines.append("\nActions:\n")
        for action in role.get("actions", []) or []:
            lines.append(f"- [{action.get('priority', 'P1')}] {action.get('action', '')} | owner={action.get('owner', '')} | check={action.get('success_check', '')}\n")
        lines.append("\nRisks:\n")
        for risk in role.get("risks", []) or []:
            lines.append(f"- {risk}\n")
        if role.get("handoff"):
            lines.append(f"\nHandoff: {role.get('handoff')}\n")
    lines.extend([
        "\n## Operating Contract\n",
        "- Generic LLM API is the primary agent substrate; Codex is optional and must not be required for the loop to progress.\n",
        "- Scripts are scaffolding and auditors, not sources of truth; role outputs must be checked against raw logs, artifacts, code, and datasets.\n",
        "- Failed or weak experiments trigger debug/analyze feedback before retry; repeated weak paths enter prune or pause queues.\n",
        "- Paper writing is gated by claim strength, novelty, counterexample survival, and bad-case evidence.\n",
    ])
    return "".join(lines)


class RoleTimeout(Exception):
    pass


class role_time_limit:
    def __init__(self, seconds: int):
        self.seconds = seconds
        self.previous = None

    def __enter__(self):
        if self.seconds <= 0 or not hasattr(signal, 'SIGALRM'):
            return self
        self.previous = signal.getsignal(signal.SIGALRM)
        def handler(_signum, _frame):
            raise RoleTimeout(f'role exceeded {self.seconds}s')
        signal.signal(signal.SIGALRM, handler)
        signal.alarm(self.seconds)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.seconds > 0 and hasattr(signal, 'SIGALRM'):
            signal.alarm(0)
            if self.previous is not None:
                signal.signal(signal.SIGALRM, self.previous)
        return False


def write_team_state(paths, team_state: dict[str, Any]) -> None:
    out_json = paths.state / "llm_research_team_state.json"
    out_md = paths.planning / "llm_research_team.md"
    out_json.write_text(json.dumps(team_state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_md.write_text(build_markdown(team_state), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Codex-free generic-LLM TASTE research team pass.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--prompt", default="")
    parser.add_argument("--topic", default="")
    parser.add_argument("--venue", default="")
    parser.add_argument("--roles", default="", help="Comma-separated subset of roles, e.g. planner,researcher,critic. Empty means all roles.")
    parser.add_argument("--context-limit", type=int, default=30000)
    parser.add_argument("--allow-fallback", action="store_true", default=True)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    context = gather_context(args.project, args.prompt, args.topic, args.venue)
    available = llm_available(cfg)
    reason = "" if available else llm_disabled_reason(cfg)
    team_state: dict[str, Any] = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "project": args.project,
        "llm_available": available,
        "llm_reason": reason,
        "roles": [],
    }

    requested_roles = {item.strip() for item in args.roles.split(",") if item.strip()}
    selected_roles = [role for role in ROLE_SEQUENCE if not requested_roles or role["name"] in requested_roles]
    if requested_roles and len(selected_roles) != len(requested_roles):
        known = {role["name"] for role in ROLE_SEQUENCE}
        missing = sorted(requested_roles - known)
        if missing:
            print(f"unknown roles ignored: {missing}")

    role_timeout = int(os.environ.get('LLM_ROLE_TIMEOUT_SEC', '120'))
    write_team_state(paths, team_state)
    for role in selected_roles:
        if available:
            try:
                prompt_text = role_prompt(role, context, team_state, args.context_limit)
                with role_time_limit(role_timeout):
                    parsed, raw = llm_json(prompt_text, cfg, system_prompt="Return strict JSON only.")
                if not parsed:
                    with role_time_limit(role_timeout):
                        text = llm_text(prompt_text, cfg, system_prompt="Return strict JSON only.")
                    parsed = safe_json_loads(text.get("content", ""), {})
                    raw = text
                if not parsed:
                    failure_dir = paths.logs / 'llm_role_failures'
                    failure_dir.mkdir(parents=True, exist_ok=True)
                    raw_content = raw.get('content', '') if isinstance(raw, dict) else ''
                    debug = {k: raw.get(k, '') for k in ['provider', 'model', 'finish_reason', 'message_keys'] if isinstance(raw, dict)}
                    saved = redact_secrets((raw_content or '')[:24000])
                    if not saved:
                        saved = json.dumps({'empty_content_debug': debug}, ensure_ascii=False, indent=2)
                    raw_path = failure_dir / f"{role['name']}_raw.txt"
                    raw_path.write_text(saved, encoding='utf-8')
                    parsed = repair_role_json_with_llm(role, raw_content, cfg) if raw_content else {}
                    if parsed:
                        parsed['_json_repaired_from_raw'] = str(raw_path)
                    else:
                        parsed = heuristic_role_json(role, raw_content)
                        if parsed:
                            parsed['_heuristic_raw_path'] = str(raw_path)
                        else:
                            raise RuntimeError(f"empty-or-invalid-role-json; raw_saved={raw_path}")
                parsed.setdefault("role", role["name"])
                parsed["_raw_model"] = {"provider": raw.get("provider", ""), "model": raw.get("model", "")}
                team_state["roles"].append(parsed)
            except Exception as exc:
                team_state["roles"].append(fallback_role_output(role, f"role-error:{exc}"))
        else:
            team_state["roles"].append(fallback_role_output(role, reason))

    out_json = paths.state / "llm_research_team_state.json"
    out_md = paths.planning / "llm_research_team.md"
    out_json.write_text(json.dumps(team_state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_md.write_text(build_markdown(team_state), encoding="utf-8")
    print(out_md)


if __name__ == "__main__":
    main()
