#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from project_paths import CLAUDE_SKILL_ROOT, build_paths, load_project_config
from experiment_contracts import row_promotion_blockers

def _repo_root_from_script() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "framework").is_dir() and (candidate / "modules").is_dir() and (candidate / "web").is_dir():
            return candidate
    return current.parents[1]

ROOT = _repo_root_from_script()
from taste_pythonpath import script_resolver

SCRIPTS = script_resolver(ROOT)
NATIVE_SKILLS = {"experiment-loop", "evidence-gate", "writing"}
NATIVE_SKILL_LABELS = {
    "experiment-loop": "TASTE experiment-loop contract",
    "evidence-gate": "TASTE evidence-assurance contract",
    "writing": "writing contract",
}
PATH_TOKEN_RE = re.compile(r"(?:(?:/|\.?/)?(?:state|reports|scripts|projects|\.claude|raw|artifacts|experiments|datasets|repos|planning|wiki)/[^\s,;|)\]}]+|(?<![A-Za-z0-9_.-])/[^\s,;|)\]}]+|\S+\.(?:json|md|txt|csv|tsv|py|log|yaml|yml|ipynb|png|pdf))")
FAILED_BASE_SWITCH_OBJECTIVE = "Resolve current-route provenance/embedding evidence or a proposal-only candidate base-switch route before exploring new niches."
SELECTED_BASE_GATE_OBJECTIVE = "Resolve selected-base semantic provenance before exploring new niches or downstream experiments."
FAILED_GATE_REPAIR_OBJECTIVE = "Use Claude Code only on gate evidence repair nodes; do not launch behavior-only candidate experiments until the gate clears."
DEFER_NICHE_OBJECTIVE = "Keep unexplored-niche experiments deferred until current-route provenance/base-switch gate input changes."
DEFER_ELITE_OBJECTIVE = "Keep method deepening deferred until selected-base provenance/base-switch gates pass."


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_text(path: Path, max_chars: int = 12000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars] if path.exists() else ""
    except Exception:
        return ""


HELPER_MODULE_ACTIONS = {
    "ideation:arena": ("ideation", "arena"),
    "planning:review_board": ("planning", "review_board"),
    "writing:audit_evidence": ("writing", "audit_evidence"),
    "planning:method_frontier": ("planning", "method_frontier"),
}


def module_cmd(stage: str, action: str, project: str, extra: list[str]) -> list[str]:
    return [sys.executable, str(ROOT / "framework/scripts/run_module.py"), stage, "--action", action, "--project", project, *extra]


def run_helper(project: str, script: str, extra: list[str], required: bool = False) -> dict[str, Any]:
    if script in HELPER_MODULE_ACTIONS:
        stage, action = HELPER_MODULE_ACTIONS[script]
        cmd = module_cmd(stage, action, project, extra)
    else:
        cmd = [sys.executable, str(SCRIPTS / script), "--project", project, *extra]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    if required and proc.returncode != 0:
        raise RuntimeError(f"{script} failed: {proc.stderr[-2000:] or proc.stdout[-2000:]}")
    return {"script": script, "returncode": proc.returncode, "stdout_tail": proc.stdout[-1000:], "stderr_tail": proc.stderr[-1000:]}


def run_capability_audit(project: str) -> dict[str, Any]:
    cmd = [sys.executable, str(SCRIPTS / "audit_research_trajectory_capabilities.py"), "--project", project]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    return {"script": "audit_research_trajectory_capabilities.py", "returncode": proc.returncode, "stdout_tail": proc.stdout[-1000:], "stderr_tail": proc.stderr[-1000:]}


def run_end_to_end_verification(project: str) -> dict[str, Any]:
    cmd = [sys.executable, str(SCRIPTS / "verify_research_trajectory_end_to_end.py"), "--project", project]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    return {"script": "verify_research_trajectory_end_to_end.py", "returncode": proc.returncode, "stdout_tail": proc.stdout[-1000:], "stderr_tail": proc.stderr[-1000:]}


def _run_module_action(stage: str, action: str, project: str, venue: str = "") -> dict[str, Any]:
    extra = ["--venue", venue] if venue else []
    cmd = module_cmd(stage, action, project, extra)
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    return {"script": f"{stage}:{action}", "returncode": proc.returncode, "stdout_tail": proc.stdout[-1000:], "stderr_tail": proc.stderr[-1000:]}


def run_paper_orchestra_state(project: str, venue: str = "") -> dict[str, Any]:
    return _run_module_action("writing", "build_paper_orchestra_state", project, venue)


def run_paper_normality_audit(project: str, venue: str = "") -> dict[str, Any]:
    return _run_module_action("writing", "audit_normality", project, venue)


def run_third_party_stack_sync(project: str) -> dict[str, Any]:
    return _run_module_action("writing", "sync_stack", project)


def run_submission_readiness(project: str, venue: str = "") -> dict[str, Any]:
    return _run_module_action("writing", "submission_readiness", project, venue)


def run_blocker_action_plan(project: str, venue: str = "") -> dict[str, Any]:
    return _run_module_action("planning", "blocker_action", project, venue)


def one_line(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def first_text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, dict):
            value = value.get("zh") or value.get("en") or value.get("text")
        if isinstance(value, list):
            value = "; ".join(str(item) for item in value[:4])
        text = str(value or "").strip()
        if text:
            return text
    return ""


def evidence_refs(*paths: Path) -> list[str]:
    return [str(path) for path in paths if path.exists()]


def flatten_evidence(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float, bool)):
        text = str(value).strip()
        return [text] if text else []
    if isinstance(value, list):
        rows: list[str] = []
        for item in value:
            rows.extend(flatten_evidence(item))
        return rows
    if isinstance(value, dict):
        rows: list[str] = []
        for item in value.values():
            rows.extend(flatten_evidence(item))
        return rows
    text = str(value).strip()
    return [text] if text else []

def stable_hash(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def node_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("name") or row.get("title") or row.get("method") or row.get("dataset") or "")


def graph_node_ids(landscape: dict[str, Any], novelty: dict[str, Any], failed: dict[str, Any], niches: dict[str, Any]) -> dict[str, list[str]]:
    groups = landscape.get("nodes", {}) if isinstance(landscape.get("nodes", {}), dict) else {}
    result: dict[str, list[str]] = {}
    for name, rows in groups.items():
        result[f"landscape_{name}"] = sorted(node_id(row) for row in rows if isinstance(row, dict) and node_id(row))
    result["novelty"] = sorted(node_id(row) for row in novelty.get("nodes", []) if isinstance(row, dict) and node_id(row))
    result["failed_hypotheses"] = sorted(node_id(row) for row in failed.get("nodes", []) if isinstance(row, dict) and node_id(row))
    result["unexplored_niches"] = sorted(node_id(row) for row in niches.get("nodes", []) if isinstance(row, dict) and node_id(row))
    return result


def looks_like_external_ref(text: str) -> bool:
    lowered = text.lower()
    return lowered.startswith(("http://", "https://", "doi:", "arxiv:"))


def extract_path_tokens(text: str) -> list[str]:
    if not text or looks_like_external_ref(text):
        return []
    tokens = []
    for match in PATH_TOKEN_RE.finditer(text):
        token = match.group(0).strip().strip("`'\"<>.,;:")
        if token:
            tokens.append(token)
    if not tokens and text.startswith(("/", "./", "../")):
        tokens.append(text.strip().strip("`'\"<>.,;:"))
    return sorted(dict.fromkeys(tokens))


def resolve_evidence_path(paths, token: str) -> Path:
    path = Path(token)
    if path.is_absolute():
        return path
    candidates = [paths.root / path, ROOT / path]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def evidence_statuses(paths, evidence: Any) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ref in flatten_evidence(evidence):
        text = str(ref).strip()
        if not text:
            continue
        if looks_like_external_ref(text):
            key = ("external", text)
            if key not in seen:
                statuses.append({"kind": "external", "ref": text, "exists": None})
                seen.add(key)
            continue
        tokens = extract_path_tokens(text)
        if tokens:
            for token in tokens:
                resolved = resolve_evidence_path(paths, token)
                key = ("local_path", str(resolved))
                if key not in seen:
                    statuses.append({"kind": "local_path", "ref": token, "path": str(resolved), "exists": resolved.exists()})
                    seen.add(key)
            continue
        key = ("text", one_line(text, 220))
        if key not in seen:
            statuses.append({"kind": "text", "ref": one_line(text, 220), "exists": None})
            seen.add(key)
    return statuses


def load_skill_contracts() -> list[dict[str, Any]]:
    skill_root = CLAUDE_SKILL_ROOT
    contracts: list[dict[str, Any]] = []
    if not skill_root.exists():
        return contracts
    for skill_file in sorted(skill_root.glob("*/SKILL.md")):
        if skill_file.parent.name not in NATIVE_SKILLS:
            continue
        text = read_text(skill_file, 4000)
        meta: dict[str, str] = {}
        lines = text.splitlines()
        if lines and lines[0].strip() == "---":
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip()
        contracts.append({
            "name": meta.get("name") or skill_file.parent.name,
            "display_name": NATIVE_SKILL_LABELS.get(skill_file.parent.name, skill_file.parent.name),
            "description": meta.get("description", ""),
            "path": str(skill_file),
            "evidence": [str(skill_file)],
            "contract_excerpt": one_line(text, 500),
        })
    return contracts


def summarize_evoscientist_cycle(paths) -> dict[str, Any]:
    cycle_path = paths.state / "evoscientist_style_cycle.json"
    memory_path = paths.state / "evo_recoverable_memory.json"
    supervisor_path = paths.state / "autoscientist_supervisor_history.json"
    continuous_path = paths.state / "autoscientist_continuous.json"
    cycle = load_json(cycle_path, {})
    memory = load_json(memory_path, {})
    supervisor = load_json(supervisor_path, {})
    continuous = load_json(continuous_path, {})
    phases = []
    recoverable_exception_count = 0
    if isinstance(cycle, dict):
        for phase in cycle.get("phases", []) or []:
            if not isinstance(phase, dict):
                continue
            recoveries = phase.get("recoverable_exceptions", []) if isinstance(phase.get("recoverable_exceptions", []), list) else []
            recoverable_exception_count += len(recoveries)
            phases.append({
                "phase": phase.get("phase", ""),
                "role": phase.get("role", ""),
                "status": phase.get("status", ""),
                "command_count": len(phase.get("commands", [])) if isinstance(phase.get("commands", []), list) else 0,
                "recoverable_exception_count": len(recoveries),
            })
    exception_memory = memory.get("exception_memory", []) if isinstance(memory, dict) and isinstance(memory.get("exception_memory", []), list) else []
    experimentation_memory = memory.get("experimentation_memory", []) if isinstance(memory, dict) and isinstance(memory.get("experimentation_memory", []), list) else []
    latest_supervisor = supervisor.get("latest", {}) if isinstance(supervisor, dict) and isinstance(supervisor.get("latest", {}), dict) else {}
    latest_continuous = continuous.get("latest", {}) if isinstance(continuous, dict) and isinstance(continuous.get("latest", {}), dict) else {}
    evidence_files = evidence_refs(
        cycle_path,
        memory_path,
        supervisor_path,
        continuous_path,
        paths.reports / "evoscientist_style_cycle.md",
        SCRIPTS / "run_evoscientist_style_cycle.py",
        SCRIPTS / "run_autoscientist_supervisor.py",
        SCRIPTS / "run_autoscientist_continuous.py",
    )
    return {
        "status": cycle.get("final_status", "not_started") if isinstance(cycle, dict) and cycle else "not_started",
        "scientific_completion": bool(cycle.get("scientific_completion")) if isinstance(cycle, dict) else False,
        "paper_gate_summary": cycle.get("paper_gate_summary", "") if isinstance(cycle, dict) else "",
        "phase_count": len(phases),
        "phases": phases,
        "recoverable_exception_count": recoverable_exception_count,
        "exception_memory_entries": len(exception_memory),
        "experimentation_memory_entries": len(experimentation_memory),
        "latest_supervisor_action": latest_supervisor.get("supervisor_action", ""),
        "latest_continuous_status": latest_continuous,
        "evidence_files": evidence_files,
    }


def paper_nodes(paper_quality: dict[str, Any]) -> list[dict[str, Any]]:
    rows = paper_quality.get("papers", []) if isinstance(paper_quality, dict) else []
    nodes = []
    for row in rows[:80]:
        if isinstance(row, dict):
            if row.get("not_positive_support") or row.get("weak_candidate_for_critique") or row.get("foundation_demoted_from_strong"):
                continue
            if str(row.get("selection_bucket") or "").strip().lower() == "deprioritized":
                continue
            nodes.append({
                "id": str(row.get("paper_id") or row.get("id") or row.get("title") or "")[:160],
                "type": "paper",
                "title": row.get("title", ""),
                "bucket": row.get("selection_bucket", ""),
                "score": row.get("idea_worthiness_score", row.get("score", row.get("fit_score", 0))),
                "evidence": [item for item in [row.get("url", ""), row.get("source", "")] if item],
            })
    return nodes


def idea_nodes(ideas_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = ideas_payload.get("ideas", []) if isinstance(ideas_payload, dict) else []
    nodes = []
    for row in rows[:80]:
        if isinstance(row, dict):
            nodes.append({
                "id": str(row.get("idea_id") or row.get("id") or first_text(row, "title", "idea"))[:160],
                "type": "idea",
                "title": first_text(row, "title", "idea", "summary"),
                "recommendation": row.get("recommendation", ""),
                "score": row.get("idea_score", row.get("score", 0)),
                "paper": row.get("paper_id", ""),
                "repo": row.get("repo_name", ""),
                "dataset": row.get("dataset_name", ""),
            })
    return nodes


def repo_nodes(repos: Any) -> list[dict[str, Any]]:
    rows = repos if isinstance(repos, list) else []
    nodes = []
    for row in rows[:120]:
        if not isinstance(row, dict):
            continue
        notes = str(row.get("notes") or row.get("summary") or row.get("description") or "")
        task_fit = row.get("task_fit", row.get("topic_fit", ""))
        if row.get("not_positive_support") or row.get("weak_candidate_for_critique"):
            continue
        if task_fit is False or str(task_fit).strip().lower() in {"false", "no", "weak", "not_fit"}:
            continue
        if "fresh Find base candidate audit for:" in notes or bool(row.get("fresh_base_audit_only") or row.get("not_positive_support") or row.get("weak_candidate_for_critique")):
            continue
        nodes.append({
            "id": str(row.get("name") or row.get("url") or row.get("local_path") or "")[:160],
            "type": "repo",
            "name": row.get("name", ""),
            "url": row.get("url", ""),
            "local_path": row.get("local_path", ""),
            "execution_ready": bool(row.get("execution_ready") or row.get("has_entrypoint")),
            "task_fit": task_fit,
            "evidence": [item for item in [row.get("local_path", ""), row.get("url", ""), notes] if item],
        })
        if len(nodes) >= 80:
            break
    return nodes


def dataset_nodes(datasets: Any) -> list[dict[str, Any]]:
    rows = datasets if isinstance(datasets, list) else []
    nodes = []
    for row in rows[:80]:
        if isinstance(row, dict):
            nodes.append({
                "id": str(row.get("name") or row.get("dataset") or "")[:160],
                "type": "dataset",
                "name": row.get("name", row.get("dataset", "")),
                "claim_ready": bool(row.get("claim_ready")),
                "loader_probe_success": bool(row.get("loader_probe_success")),
                "available": bool(row.get("available")),
                "missing_required_files": row.get("missing_required_files") or row.get("missing_files") or [],
                "evidence": [item for item in [row.get("local_path", ""), row.get("probe_timestamp", ""), row.get("notes", "")] if item],
            })
    return nodes


def merge_dataset_evidence(paths, datasets: Any, experiments: Any) -> list[dict[str, Any]]:
    """Merge all local dataset evidence before trajectory/assurance gates.

    dataset_registry can lag behind active repo probes. The trajectory layer must
    use the same evidence family as the UI/status layer: registry entries,
    repo_data_requirements ready/blocked sets, real_dataset_probe, and real
    audit-ready experiment records.
    """
    rows: dict[str, dict[str, Any]] = {}
    for row in datasets if isinstance(datasets, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("dataset") or "").strip()
        if not name:
            continue
        item = dict(row)
        item.setdefault("name", name)
        item.setdefault("dataset", name)
        item.setdefault("evidence_sources", [])
        item["evidence_sources"].append("state/dataset_registry.json")
        rows[name] = item

    req = load_json(paths.state / "repo_data_requirements.json", {})
    if isinstance(req, dict):
        ready_names = {str(name) for name in req.get("ready_datasets", []) or [] if str(name).strip()}
        blocked_names = {str(name) for name in req.get("blocked_datasets", []) or [] if str(name).strip()}
        local_statuses = req.get("local_statuses", []) if isinstance(req.get("local_statuses", []), list) else []
        for status_row in local_statuses:
            if not isinstance(status_row, dict):
                continue
            name = str(status_row.get("dataset") or "").strip()
            if not name:
                continue
            item = rows.setdefault(name, {"name": name, "dataset": name, "evidence_sources": []})
            item.setdefault("evidence_sources", []).append("state/repo_data_requirements.json")
            item["repo_data_status"] = status_row.get("status", "")
            ready_root = str(status_row.get("ready_root") or "")
            if ready_root:
                item["local_path"] = ready_root
            if name in ready_names or status_row.get("status") == "ready":
                item["available"] = True
                item["claim_ready"] = True
                item["loader_probe_success"] = True
                item["missing_required_files"] = []
                item["claim_ready_evidence"] = "repo_data_requirements ready dataset and complete active-repo required files"
            elif name in blocked_names and "claim_ready" not in item:
                item["claim_ready"] = False
                item["loader_probe_success"] = False
        for name in ready_names:
            item = rows.setdefault(name, {"name": name, "dataset": name, "evidence_sources": []})
            item.setdefault("evidence_sources", []).append("state/repo_data_requirements.json")
            item.update({
                "available": True,
                "claim_ready": True,
                "loader_probe_success": True,
                "missing_required_files": [],
                "claim_ready_evidence": item.get("claim_ready_evidence") or "repo_data_requirements ready_datasets",
            })

    probe = load_json(paths.state / "real_dataset_probe.json", {})
    if isinstance(probe, dict):
        for row in probe.get("probes", []) or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("dataset") or "").strip()
            if not name:
                continue
            item = rows.setdefault(name, {"name": name, "dataset": name, "evidence_sources": []})
            item.setdefault("evidence_sources", []).append("state/real_dataset_probe.json")
            item.setdefault("probe_timestamp", row.get("timestamp", ""))
            if row.get("dataset_path") and not item.get("local_path"):
                item["local_path"] = row.get("dataset_path")
            loader = row.get("loader_probe", {}) if isinstance(row.get("loader_probe", {}), dict) else {}
            if row.get("claim_ready") or loader.get("success"):
                item.update({
                    "available": True,
                    "claim_ready": True,
                    "loader_probe_success": True,
                    "missing_required_files": [],
                    "claim_ready_evidence": row.get("claim_ready_reason") or "real_dataset_probe loader success",
                })
            elif "loader_probe_success" not in item:
                item["loader_probe_success"] = False

    real_experiment_datasets = {
        str(row.get("dataset"))
        for row in experiments if isinstance(row, dict)
        and str(row.get("status", "")).lower() in {"completed", "success"}
        and row.get("audit_ready")
        and row.get("dataset")
        and not str(row.get("dataset", "")).startswith("synthetic_")
    } if isinstance(experiments, list) else set()
    for name in real_experiment_datasets:
        item = rows.setdefault(name, {"name": name, "dataset": name, "evidence_sources": []})
        item.setdefault("evidence_sources", []).append("state/experiment_registry.json")
        item.update({
            "available": True,
            "claim_ready": True,
            "loader_probe_success": True,
            "missing_required_files": [],
            "real_audit_ready_experiment": True,
            "claim_ready_evidence": item.get("claim_ready_evidence") or "audit-ready real-data experiment registry entry",
        })

    for item in rows.values():
        sources = item.get("evidence_sources", [])
        item["evidence_sources"] = sorted(dict.fromkeys(str(src) for src in sources if str(src).strip()))
        notes = item.get("notes", "")
        evidence = item.get("claim_ready_evidence", "")
        if evidence and evidence not in str(notes):
            item["notes"] = f"{notes}; {evidence}".strip("; ")
    return list(rows.values())


def experiment_nodes(experiments: Any) -> list[dict[str, Any]]:
    rows = experiments if isinstance(experiments, list) else []
    nodes = []
    for row in rows[-120:]:
        if isinstance(row, dict):
            nodes.append({
                "id": str(row.get("experiment_id") or row.get("name") or "")[:160],
                "type": "experiment",
                "method": row.get("method", ""),
                "dataset": row.get("dataset", ""),
                "status": row.get("status", ""),
                "audit_ready": bool(row.get("audit_ready")),
                "claim_verdict": row.get("claim_verdict", ""),
                "counterexample_outcome": row.get("counterexample_outcome", ""),
                "metric": row.get("metric_name", ""),
                "metric_value": row.get("metric_value"),
                "artifact_path": row.get("artifact_path", ""),
                "evidence": [item for item in [row.get("audit_path", ""), row.get("artifact_path", ""), row.get("bad_case_path", "")] if item],
            })
    return nodes


def build_research_landscape(cfg: dict[str, Any], paths, paper_quality: dict[str, Any], ideas: dict[str, Any], repos: Any, datasets: Any, experiments: Any) -> dict[str, Any]:
    paper = paper_nodes(paper_quality)
    idea = idea_nodes(ideas)
    repo = repo_nodes(repos)
    dataset = dataset_nodes(datasets)
    experiment = experiment_nodes(experiments)
    edges = []
    idea_ids = {row["id"] for row in idea if row.get("id")}
    for node in idea:
        if node.get("paper"):
            edges.append({"source": node["id"], "target": node["paper"], "type": "idea_from_paper"})
        if node.get("repo"):
            edges.append({"source": node["id"], "target": node["repo"], "type": "idea_uses_repo"})
        if node.get("dataset"):
            edges.append({"source": node["id"], "target": node["dataset"], "type": "idea_uses_dataset"})
    for node in experiment:
        if node.get("method") in idea_ids:
            edges.append({"source": node["method"], "target": node["id"], "type": "tested_by"})
        if node.get("dataset"):
            edges.append({"source": node["id"], "target": node["dataset"], "type": "uses_dataset"})
    return {
        "project": paths.name,
        "topic": cfg.get("topic", ""),
        "updated_at": now_iso(),
        "nodes": {"papers": paper, "ideas": idea, "repos": repo, "datasets": dataset, "experiments": experiment},
        "edges": edges[-500:],
        "evidence_files": evidence_refs(paths.state / "paper_quality.json", paths.state / "idea_candidates.json", paths.state / "repo_candidates.json", paths.state / "dataset_registry.json", paths.state / "experiment_registry.json"),
    }


def build_novelty_map(ideas: dict[str, Any], hypothesis_arena: dict[str, Any], method_frontier: dict[str, Any]) -> dict[str, Any]:
    nodes = []
    for row in ideas.get("ideas", []) if isinstance(ideas, dict) else []:
        if isinstance(row, dict):
            nodes.append({
                "id": row.get("idea_id") or row.get("id") or first_text(row, "title", "idea")[:80],
                "title": first_text(row, "title", "idea", "summary"),
                "novelty_delta": first_text(row, "novelty", "novelty_delta", "why_new", "recommendation"),
                "score": row.get("idea_score", row.get("score", 0)),
                "nearest_neighbor": row.get("paper_id", ""),
                "status": "candidate",
                "evidence": [item for item in ["state/idea_candidates.json", row.get("paper_id", ""), row.get("repo_name", ""), row.get("dataset_name", "")] if item],
            })
    for row in hypothesis_arena.get("hypotheses", []) if isinstance(hypothesis_arena, dict) else []:
        if isinstance(row, dict):
            nodes.append({
                "id": row.get("hypothesis_id", ""),
                "title": row.get("title", ""),
                "novelty_delta": row.get("novelty_delta", ""),
                "nearest_neighbor": row.get("nearest_neighbor", ""),
                "status": "hypothesis",
                "evidence_needed": row.get("support_evidence_needed", ""),
                "evidence": [item for item in ["state/hypothesis_arena.json", row.get("repo_anchor", ""), row.get("dataset_anchor", "")] if item],
            })
    return {"updated_at": now_iso(), "nodes": nodes[:160], "elite_methods": method_frontier.get("elite_methods", []) if isinstance(method_frontier, dict) else [], "evidence_files": ["state/idea_candidates.json", "state/hypothesis_arena.json", "state/method_frontier.json"]}


def build_failed_hypothesis_graph(experiments: Any, aris_board: dict[str, Any], next_actions: dict[str, Any]) -> dict[str, Any]:
    failed = []
    edges = []
    for row in experiments if isinstance(experiments, list) else []:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status", "")).lower()
        promotion_blockers = row_promotion_blockers(row)
        if status in {"failed", "error", "incomplete_audit"} or row.get("audit_ready") is False or promotion_blockers:
            node_id = str(row.get("experiment_id") or row.get("name") or row.get("method") or "failed")
            reason_source = "; ".join(promotion_blockers) if promotion_blockers else row.get("result") or row.get("notes") or row.get("missing_audit_fields")
            failed.append({"id": node_id, "method": row.get("method", ""), "dataset": row.get("dataset", ""), "status": row.get("status", ""), "reason": one_line(reason_source), "artifact_path": row.get("artifact_path", ""), "evidence": [item for item in [row.get("audit_path", ""), row.get("bad_case_path", ""), row.get("artifact_path", ""), row.get("log_path", "")] if item]})
            if row.get("method"):
                edges.append({"source": row.get("method"), "target": node_id, "type": "failed_or_incomplete_or_non_promotable"})
    for row in aris_board.get("methods", []) if isinstance(aris_board, dict) else []:
        if row.get("recommendation") in {"block", "repair_or_prune"}:
            failed.append({"id": f"assurance:{row.get('method', 'unknown')}", "method": row.get("method", ""), "status": row.get("recommendation", ""), "reason": "; ".join(row.get("issues", []) or []), "evidence": ["state/evidence_review_board.json"]})
    for row in next_actions.get("method_summaries", []) if isinstance(next_actions, dict) else []:
        if row.get("recommendation") in {"pause_or_prune", "compare_then_prune_or_pause"}:
            failed.append({"id": f"prune:{row.get('method', 'unknown')}", "method": row.get("method", ""), "status": row.get("recommendation", ""), "reason": one_line(row.get("result_summary") or row.get("decision_reason") or row.get("recommendation")), "evidence": ["state/next_actions.json"]})
    return {"updated_at": now_iso(), "nodes": failed[-200:], "edges": edges[-500:]}


def build_unexplored_niche_graph(novelty_map: dict[str, Any], failed_graph: dict[str, Any], datasets: Any, repos: Any, experiments: Any) -> dict[str, Any]:
    tested_methods = {str(row.get("method")) for row in experiments if isinstance(row, dict) and row.get("method")} if isinstance(experiments, list) else set()
    failed_methods = {str(row.get("method")) for row in failed_graph.get("nodes", []) if row.get("method")}
    nodes = []
    for row in novelty_map.get("nodes", []):
        node_id = str(row.get("id") or "")
        if node_id and node_id not in tested_methods and node_id not in failed_methods:
            nodes.append({"id": node_id, "title": row.get("title", ""), "reason": "novelty candidate has not been tested by an audit-ready experiment", "priority": row.get("score", 0), "needed_evidence": row.get("evidence_needed") or "repo/data/experiment audit", "evidence": row.get("evidence") or ["state/novelty_map.json"]})
    ready_data = [row for row in datasets if isinstance(row, dict) and row.get("claim_ready") and row.get("loader_probe_success")] if isinstance(datasets, list) else []
    runnable_repos = [row for row in repos if isinstance(row, dict) and (row.get("execution_ready") or row.get("has_entrypoint"))] if isinstance(repos, list) else []
    if ready_data and runnable_repos:
        nodes.append({"id": "ready_repo_data_cross_product", "title": "Untested runnable-repo and loader-ready-data combinations", "reason": "TASTE evolutionary exploration pressure: test evidence-feasible combinations before inventing unsupported claims.", "priority": len(ready_data) * len(runnable_repos), "ready_datasets": [row.get("name") or row.get("dataset") for row in ready_data[:10]], "runnable_repos": [row.get("name") for row in runnable_repos[:10]], "evidence": ["state/repo_data_requirements.json", "state/repo_candidates.json", "state/research_landscape.json"]})
    return {"updated_at": now_iso(), "nodes": nodes[:120], "edges": [], "evidence_files": ["state/research_landscape.json", "state/novelty_map.json", "state/failed_hypothesis_graph.json"]}


def selected_repo_path_from_active(active_repo: dict[str, Any]) -> str:
    if not isinstance(active_repo, dict):
        return ""
    for key in ["repo_path", "local_path", "path", "selected_repo_path", "active_repo_path"]:
        text = str(active_repo.get(key) or "").strip()
        if text:
            return text.rstrip("/")
    return ""


def payload_run_id(payload: Any) -> str:
    return str(payload.get("run_id") or payload.get("source_run_id") or payload.get("find_run_id") or "").strip() if isinstance(payload, dict) else ""


def current_find_run_id(paths) -> str:
    for item in [
        paths.planning / "finding" / "find_progress.json",
        paths.state / "current_find_research_plan.json",
        paths.state / "literature_tool_packet.json",
    ]:
        run_id = payload_run_id(load_json(item, {}))
        if run_id:
            return run_id
    return ""


def current_route_repo_context(paths, active_repo: dict[str, Any]) -> dict[str, Any]:
    # Current-run environment selection wins; active_repo.json is only fallback.
    active_repo = active_repo if isinstance(active_repo, dict) else {}
    selection = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    selected = selection.get("selected", {}) if isinstance(selection, dict) and isinstance(selection.get("selected"), dict) else {}
    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    impl_repo = impl.get("repo", {}) if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    impl_selected = impl.get("selected_base", {}) if isinstance(impl, dict) and isinstance(impl.get("selected_base"), dict) else {}
    current_run = current_find_run_id(paths)
    current_plan = load_json(paths.state / "current_find_research_plan.json", {})
    if not isinstance(current_plan, dict) or not str(current_plan.get("selected_plan_id") or "").strip():
        current_plan = load_json(paths.planning / "finding" / "plans.json", {})
    current_selected_plan_id = str(current_plan.get("selected_plan_id") or "").strip() if isinstance(current_plan, dict) else ""
    selected_run = str((selection.get("fresh_find_run_id") if isinstance(selection, dict) else "") or selected.get("fresh_find_run_id") or "").strip()
    selected_route_run = str(selected.get("fresh_find_run_id") or "").strip()
    selection_plan_id = str((selection.get("selected_plan_id") if isinstance(selection, dict) else "") or selected.get("selected_plan_id") or "").strip()
    selected_route_plan_id = str(selected.get("selected_plan_id") or "").strip()
    stage = str((selection.get("selection_stage") if isinstance(selection, dict) else "") or (selection.get("selected_by_stage") if isinstance(selection, dict) else "") or selected.get("selection_stage") or "").strip()
    run_current = bool(not current_run or (selected_run == current_run and selected_route_run == current_run))
    plan_current = bool(not current_selected_plan_id or (selection_plan_id == current_selected_plan_id and selected_route_plan_id == current_selected_plan_id))
    selected_is_current = bool(selected and stage == "environment_claude_code" and run_current and plan_current)
    if not selected_is_current:
        payload = dict(active_repo)
        payload.setdefault("current_route_source", "active_repo_fallback")
        payload.setdefault("current_find_run_id", current_run)
        payload.setdefault("current_selected_plan_id", current_selected_plan_id)
        payload.setdefault("environment_selection_current", False)
        return payload

    payload = {
        "current_route_source": "evidence_ready_repo_selection",
        "current_find_run_id": current_run,
        "selected_fresh_find_run_id": selected_run,
        "selection_stage": stage,
    }
    for key in ["name", "repo", "url"]:
        payload[key] = selected.get(key) or impl_repo.get(key) or active_repo.get(key) or ""
    selected_repo_path = selected.get("repo_path") or selected.get("local_path") or impl_repo.get("repo_path") or impl_repo.get("local_path") or ""
    if selected_repo_path:
        selected_repo_path = str(selected_repo_path).rstrip("/")
        payload["repo_path"] = selected_repo_path
        payload["local_path"] = selected_repo_path
        payload["path"] = selected_repo_path
    payload["selected_base_title"] = (
        selected.get("literature_base_title")
        or selected.get("selected_base_title")
        or selected.get("title")
        or impl_selected.get("literature_base_title")
        or impl_selected.get("selected_base_title")
        or impl_selected.get("title")
        or active_repo.get("selected_base_title")
        or ""
    )
    return payload


def experiment_repo_path(row: dict[str, Any]) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ["repo_path", "active_repo_path", "selected_repo_path", "local_path", "path"]:
        text = str(row.get(key) or "").strip()
        if text:
            return text.rstrip("/")
    return ""


def path_matches_selected_repo(path_text: str, selected_repo_path: str) -> bool:
    left = str(path_text or "").rstrip("/")
    right = str(selected_repo_path or "").rstrip("/")
    if not left or not right:
        return False
    return left == right or left.startswith(right + "/")


def current_route_experiment_rows(rows: list[dict[str, Any]], active_repo: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep assurance blockers scoped to the environment-stage selected repo.

    Historical route experiments remain in the failed-hypothesis graph and
    registry audit, but they must not become current-route promotion blockers.
    """
    selected_repo_path = selected_repo_path_from_active(active_repo)
    if not selected_repo_path:
        return rows, []
    current: list[dict[str, Any]] = []
    legacy: list[dict[str, Any]] = []
    for row in rows:
        repo_path = experiment_repo_path(row)
        if repo_path and path_matches_selected_repo(repo_path, selected_repo_path):
            current.append(row)
        elif repo_path:
            legacy.append(row)
    return current, legacy


def build_assurance_layer(paths, experiments: Any, datasets: Any, active_repo: dict[str, Any], evidence_audit_text: str, aris_board: dict[str, Any], claim_ledger: dict[str, Any] | None = None) -> dict[str, Any]:
    issues = []
    all_rows = experiments if isinstance(experiments, list) else []
    rows, legacy_rows = current_route_experiment_rows(all_rows, active_repo)
    completed = [row for row in rows if str(row.get("status", "")).lower() in {"completed", "success"}]
    audit_ready = [row for row in completed if row.get("audit_ready")]
    non_promotable_audit_ready = [row for row in audit_ready if row_promotion_blockers(row)]
    promotable_audit_ready = [row for row in audit_ready if row not in non_promotable_audit_ready]
    real_ready = {str(row.get("name") or row.get("dataset")) for row in datasets if isinstance(row, dict) and row.get("claim_ready") and row.get("loader_probe_success")} if isinstance(datasets, list) else set()
    real_completed = [row for row in promotable_audit_ready if str(row.get("dataset")) in real_ready and not str(row.get("dataset", "")).startswith("synthetic_")]
    if not promotable_audit_ready:
        issues.append({"severity": "block", "issue": "No audit-ready promotable experiment exists.", "evidence": "state/experiment_registry.json"})
    if not real_completed:
        issues.append({"severity": "block", "issue": "No promotable audit-ready experiment on loader-ready real data exists.", "evidence": "state/experiment_registry.json + merged dataset evidence from dataset_registry/repo_data_requirements/real_dataset_probe"})
    for row in non_promotable_audit_ready[:12]:
        issues.append({"severity": "block", "issue": f"Experiment `{row.get('experiment_id') or row.get('name')}` is audit-ready but not promotable: {', '.join(row_promotion_blockers(row)[:4])}", "evidence": row.get("audit_path") or row.get("artifact_path") or "state/experiment_registry.json"})
    for row in promotable_audit_ready:
        is_smoke = str(row.get("claim_verdict", "")).lower() == "weak"
        if not row.get("claim_verdict"):
            issues.append({"severity": "warn", "issue": f"{row.get('experiment_id')} lacks claim_verdict.", "evidence": row.get("audit_path", "")})
        if not is_smoke and not row.get("counterexample_outcome"):
            issues.append({"severity": "warn", "issue": f"{row.get('experiment_id')} lacks counterexample_outcome.", "evidence": row.get("audit_path", "")})
        if not is_smoke and not (row.get("bad_case_path") or row.get("bad_case_slices")):
            issues.append({"severity": "warn", "issue": f"{row.get('experiment_id')} lacks bad-case evidence.", "evidence": row.get("artifact_path", "")})
    for blocker in aris_board.get("blockers", []) if isinstance(aris_board, dict) else []:
        issues.append({"severity": "block", "issue": str(blocker), "evidence": "state/evidence_review_board.json"})
    if "promotion_gate_recommendation: hold-markdown-only" in evidence_audit_text:
        issues.append({"severity": "block", "issue": "Paper evidence audit recommends hold-markdown-only.", "evidence": "reports/paper_evidence_audit.md"})
    reference_reproduction = load_json(paths.state / "reference_reproduction_gate.json", {})
    if isinstance(reference_reproduction, dict) and reference_reproduction.get("status") not in {"", "pass"}:
        for blocker in reference_reproduction.get("blockers", [])[:8] if isinstance(reference_reproduction.get("blockers", []), list) else []:
            issues.append({"severity": "block", "issue": f"Reference reproduction gate: {blocker}", "evidence": "state/reference_reproduction_gate.json"})
    weak_claims = [row for row in (claim_ledger or {}).get("claims", []) if isinstance(row, dict) and str(row.get("status", "")).lower() in {"weak", "unsupported"}]
    if weak_claims:
        claim_types = ", ".join(str(row.get("claim_type") or row.get("text") or "claim") for row in weak_claims[:8])
        issues.append({"severity": "block", "issue": f"Claim ledger contains weak or unsupported claims: {claim_types}", "evidence": "state/claim_ledger.json"})
    return {"updated_at": now_iso(), "status": "blocked" if any(row["severity"] == "block" for row in issues) else "warn" if issues else "pass", "principles": ["Every claim must point to local files or verifiable external records.", "Synthetic smoke proves plumbing only, not scientific conclusions.", "Claude/LLM summaries are routing hints until checked against artifacts.", "Paper promotion requires real-data, audit-ready, bad-case, claim, and counterexample evidence."], "issues": issues[:200], "active_repo": active_repo.get("repo_path", "") if isinstance(active_repo, dict) else "", "route_experiment_count": len(rows), "legacy_excluded_experiment_count": len(legacy_rows), "legacy_excluded_experiments": [row.get("experiment_id") or row.get("name") for row in legacy_rows[:40]], "real_ready_datasets": sorted(real_ready), "real_audit_ready_experiments": [row.get("experiment_id") or row.get("name") for row in real_completed], "weak_claim_count": len(weak_claims), "evidence_files": evidence_refs(paths.state / "experiment_registry.json", paths.state / "dataset_registry.json", paths.state / "repo_data_requirements.json", paths.state / "real_dataset_probe.json", paths.state / "evidence_review_board.json", paths.reports / "paper_evidence_audit.md", paths.state / "claim_ledger.json")}


def base_switch_candidate_has_identity(candidate: Any) -> bool:
    if not isinstance(candidate, dict):
        return False
    for key in ["repo", "name", "title", "repo_path", "local_path", "path", "proposed_path_hint", "proposal_path"]:
        if str(candidate.get(key) or "").strip():
            return True
    return False


def selected_base_semantic_gate_required(gate: Any) -> bool:
    if not isinstance(gate, dict):
        return False
    if str(gate.get("status") or "").lower() not in {"blocked", "fail", "failed"}:
        return False
    review = gate.get("semantic_data_provenance_review", {}) if isinstance(gate.get("semantic_data_provenance_review", {}), dict) else {}
    return bool(
        str(gate.get("decision") or "").lower() == "base_switch_gate_required"
        or review.get("deterministic_gate_required")
        or str(review.get("status") or "").lower() in {"blocked", "fail", "failed"}
    )


def blocked_check_ids(gate: Any) -> list[str]:
    rows = gate.get("failed_checks", []) if isinstance(gate, dict) and isinstance(gate.get("failed_checks", []), list) else []
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        check_id = str(row.get("id") or row.get("name") or "").strip()
        if check_id and str(row.get("status") or "blocked").lower() in {"blocked", "fail", "failed"}:
            ids.append(check_id)
    return sorted(dict.fromkeys(ids))


def trajectory_gate_context(paths) -> dict[str, Any]:
    selected_gate = load_json(paths.state / "selected_base_viability_gate.json", {})
    base_switch_gate = load_json(paths.state / "base_switch_gate.json", {})
    selected_gate_required = selected_base_semantic_gate_required(selected_gate)
    base_status = str(base_switch_gate.get("status") or "").lower() if isinstance(base_switch_gate, dict) else ""
    base_decision = str(base_switch_gate.get("decision") or "").lower() if isinstance(base_switch_gate, dict) else ""
    switch_authorized = bool(base_switch_gate.get("switch_authorized")) if isinstance(base_switch_gate, dict) else False
    candidate_present = base_switch_candidate_has_identity(base_switch_gate.get("candidate_route", {}) if isinstance(base_switch_gate, dict) else {})
    failed_checks = blocked_check_ids(base_switch_gate)
    base_switch_failed = bool(
        base_status == "blocked"
        and (base_decision == "base_switch_not_authorized" or not switch_authorized)
        and (failed_checks or not candidate_present)
    )
    return {
        "selected_base_gate_required": selected_gate_required,
        "base_switch_failed": base_switch_failed,
        "blocks_downstream": bool(selected_gate_required or base_switch_failed),
        "base_switch_gate_status": base_status,
        "base_switch_gate_decision": base_decision,
        "base_switch_candidate_route_present": candidate_present,
        "base_switch_failed_checks": failed_checks,
    }


def trajectory_controller(assurance: dict[str, Any], failed: dict[str, Any], niches: dict[str, Any], evolution_memory: dict[str, Any], method_frontier: dict[str, Any], evo_cycle: dict[str, Any], skill_contracts: list[dict[str, Any]], gate_context: dict[str, Any] | None = None) -> dict[str, Any]:
    repair_queue = evolution_memory.get("repair_queue", []) if isinstance(evolution_memory, dict) else []
    elite = evolution_memory.get("elite_pool", []) if isinstance(evolution_memory, dict) else []
    gate_context = gate_context if isinstance(gate_context, dict) else {}
    blocks_downstream = bool(gate_context.get("blocks_downstream"))
    objectives = []
    if assurance.get("status") == "blocked":
        objectives.append("Resolve evidence blockers before claiming or promoting paper output.")
    if repair_queue or failed.get("nodes"):
        objectives.append(FAILED_GATE_REPAIR_OBJECTIVE if blocks_downstream else "Use Claude Code on the highest-priority failed/repair node, then rerun the validation trial in the same trajectory.")
    if blocks_downstream:
        objectives.append(FAILED_BASE_SWITCH_OBJECTIVE if gate_context.get("base_switch_failed") else SELECTED_BASE_GATE_OBJECTIVE)
    if niches.get("nodes"):
        objectives.append(DEFER_NICHE_OBJECTIVE if blocks_downstream else "Select one unexplored niche with loader-ready data and runnable repo evidence for the next bounded experiment.")
    if evo_cycle.get("phase_count"):
        objectives.append("Use TASTE recoverable cycle trace and long-horizon memory before starting the next experiment.")
    if skill_contracts:
        objectives.append("Route Claude Code through local TASTE skills for experiment-loop, evidence-gate, and paper-production work.")
    if elite:
        objectives.append(DEFER_ELITE_OBJECTIVE if blocks_downstream else "Deepen only elite methods that pass TASTE evidence-assurance checks.")
    if not objectives:
        objectives.append("Refresh literature/repo landscape and generate a new evidence-feasible experiment plan.")
    return {
        "phase": "assurance_blocked" if assurance.get("status") == "blocked" else "repair_or_explore" if repair_queue or failed.get("nodes") else "explore_or_deepen",
        "gate_context": gate_context,
        "agent_roles": [
            {"role": "landscape_cartographer", "responsibility": "Maintain research landscape, novelty map, failed hypothesis graph, and unexplored niche graph."},
            {"role": "evolutionary_memory_curator", "responsibility": "Persist ideation, experimentation, assurance, and trajectory memory to disk."},
            {"role": "evidence_assurance_reviewer", "responsibility": "Reject unsupported claims and require local evidence paths."},
            {"role": "trajectory_experiment_executor", "responsibility": "Run plan -> execute -> evaluate -> repair loops with bounded retries."},
            {"role": "skill_bound_claude_executor", "responsibility": "Use local Claude skills as executable contracts for experiment-loop, evidence-gate, and paper-production work."},
            {"role": "paper_production_controller", "responsibility": "Only write claims after evidence gates pass; otherwise keep paper at draft/preview."},
            {"role": "submission_readiness_auditor", "responsibility": "Run section, evidence, citation, artifact, venue, and reproducibility gates before any submission-ready claim."},
        ],
        "next_objectives": objectives,
        "stop_conditions": [
            "No loader-ready real dataset or runnable repo is available after bounded search.",
            "All candidate hypotheses are pruned by TASTE evidence assurance and failed-hypothesis memory.",
            "Compute/account limits make required experiments impossible; record blocker and ask for resources.",
        ],
        "native_capabilities": {
            "evidence_assurance": "adversarial evidence review and prune/deepen gates",
            "evolutionary_memory": "persistent evolutionary memory and recoverable multi-phase cycles",
            "paper_production": "section-wise paper construction, review/revision orchestration, and submission-readiness gates separated from prose polish",
            "local_claude_skills": [row.get("path", "") for row in skill_contracts],
        },
        "method_frontier": {
            "elite_count": len(method_frontier.get("elite_methods", [])) if isinstance(method_frontier, dict) else 0,
            "repair_count": len(method_frontier.get("repair_queue", [])) if isinstance(method_frontier, dict) else 0,
            "prune_count": len(method_frontier.get("prune_queue", [])) if isinstance(method_frontier, dict) else 0,
        },
        "evoscientist_cycle": {
            "status": evo_cycle.get("status", ""),
            "phase_count": evo_cycle.get("phase_count", 0),
            "recoverable_exception_count": evo_cycle.get("recoverable_exception_count", 0),
        },
        "skill_contract_count": len(skill_contracts),
    }

def build_trajectory_execution_protocol(paths, controller: dict[str, Any], optimization_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist the durable main-agent loop that optimizes the whole trajectory."""
    queue = optimization_plan.get("queue", []) if isinstance(optimization_plan, dict) and isinstance(optimization_plan.get("queue", []), list) else []
    return {
        "project": paths.name,
        "updated_at": now_iso(),
        "status": "ready" if queue else "waiting_for_queue",
        "main_agent": {
            "id": "main",
            "role": "trajectory_supervisor",
            "goal": "Continuously optimize the research trajectory through evidence-gated Claude Code calls, validation, memory updates, and checkpoint comparison.",
            "state_path": str(paths.state / "trajectory_supervisor_state.json"),
            "entrypoint": "framework/scripts/run_research_trajectory_supervisor.py",
        },
        "loop_steps": [
            {
                "step": "refresh_state",
                "action": "Run build_research_trajectory_system.py and read evolutionary_memory_index before selecting work.",
                "evidence": ["state/research_trajectory_system.json", "state/evolutionary_memory_index.json", "state/research_graph_history.json", "state/evolutionary_memory_ledger.json"],
            },
            {
                "step": "select_queue_item",
                "action": "Select the highest-priority non-completed P0/P1/P2 trajectory queue item; prefer assurance/evidence blockers over new exploration.",
                "evidence": ["state/trajectory_optimization_plan.json"],
            },
            {
                "step": "delegate_to_claude_code",
                "action": "Call the persistent Claude Code project session with the queue objective, local skill contract, evidence inputs, success checks, and queued web guidance.",
                "evidence": ["state/claude_project_session.json", "state/claude_project_session_last_result.json"],
            },
            {
                "step": "validate_and_rebuild",
                "action": "Re-run audits/builders after Claude returns; compare checkpoint deltas and never treat text-only claims as scientific evidence.",
                "evidence": ["state/research_evidence_integrity.json", "state/research_evidence_manifest.json", "state/trajectory_checkpoints.json", "reports/paper_evidence_audit.md"],
            },
            {
                "step": "continue_or_stop",
                "action": "Continue until gates pass, the queue is exhausted, bounded rounds are used, or a real missing-resource blocker is recorded.",
                "evidence": ["state/trajectory_supervisor_state.json", "state/research_memory.json", "state/research_direction_memory.json", "state/research_graph_history.json", "state/evolutionary_memory_ledger.json"],
            },
        ],
        "worker_contract": {
            "claude_worker_parent": "main",
            "required_context_reads": [
                "state/evolutionary_memory_index.json",
                "state/trajectory_optimization_plan.json",
                "state/research_evidence_integrity.json",
                "state/research_evidence_manifest.json",
                "state/research_direction_memory.json",
                "state/research_graph_history.json",
                "state/research_landscape_assessment.json",
                "state/evolutionary_memory_ledger.json",
                "state/trajectory_checkpoints.json",
                "state/research_skill_contracts.json",
            ],
            "must_update_or_preserve": [
                "state/research_memory.json",
                "state/research_direction_memory.json",
                "state/failed_hypothesis_graph.json",
                "state/unexplored_niche_graph.json",
                "state/research_evidence_integrity.json",
                "state/research_evidence_manifest.json",
                "state/research_graph_history.json",
                "state/research_landscape_assessment.json",
                "state/evolutionary_memory_ledger.json",
                "state/trajectory_checkpoints.json",
            ],
        },
        "queue_preview": queue[:8],
        "source_objectives": controller.get("next_objectives", []),
        "stop_conditions": controller.get("stop_conditions", []),
    }


def update_research_memory(paths, trajectory: dict[str, Any], ideas: dict[str, Any], experiments: Any) -> dict[str, Any]:
    path = paths.state / "research_memory.json"
    memory = load_json(path, {"project": paths.name, "ideation_memory": [], "experimentation_memory": [], "assurance_memory": [], "trajectory_memory": []})
    idea_rows = ideas.get("ideas", []) if isinstance(ideas, dict) else []
    exp_rows = experiments if isinstance(experiments, list) else []
    entries = {
        "ideation_memory": {"updated_at": now_iso(), "top_candidates": [{"idea_id": row.get("idea_id") or row.get("id"), "title": first_text(row, "title", "idea", "summary"), "score": row.get("idea_score", row.get("score", 0)), "recommendation": row.get("recommendation", "")} for row in idea_rows[:10] if isinstance(row, dict)], "unexplored_niches": trajectory.get("unexplored_niche_graph", {}).get("nodes", [])[:10]},
        "experimentation_memory": {"updated_at": now_iso(), "completed": sum(1 for row in exp_rows if str(row.get("status", "")).lower() in {"completed", "success"}), "failed_or_incomplete": sum(1 for row in exp_rows if str(row.get("status", "")).lower() in {"failed", "error", "incomplete_audit"}), "latest": exp_rows[-10:]},
        "assurance_memory": {"updated_at": now_iso(), "status": trajectory.get("assurance_layer", {}).get("status", ""), "issues": trajectory.get("assurance_layer", {}).get("issues", [])[:20]},
        "trajectory_memory": {"updated_at": now_iso(), "phase": trajectory.get("trajectory_controller", {}).get("phase", ""), "next_objectives": trajectory.get("trajectory_controller", {}).get("next_objectives", []), "stop_conditions": trajectory.get("trajectory_controller", {}).get("stop_conditions", []), "recoverable_cycle": trajectory.get("recoverable_cycle", trajectory.get("evoscientist_cycle", {})), "skill_contracts": [{"name": row.get("display_name") or row.get("name"), "path": row.get("path")} for row in trajectory.get("skill_contracts", [])], "optimization_queue_size": trajectory.get("trajectory_optimization_plan", {}).get("queue_size", 0)},
    }
    for key, entry in entries.items():
        rows = memory.get(key, []) if isinstance(memory.get(key), list) else []
        rows.append(entry)
        memory[key] = rows[-120:]
    save_json(path, memory)
    return memory


def update_research_direction_memory(paths, landscape: dict[str, Any], novelty: dict[str, Any], failed: dict[str, Any], niches: dict[str, Any], assurance: dict[str, Any], controller: dict[str, Any]) -> dict[str, Any]:
    path = paths.state / "research_direction_memory.json"
    memory = load_json(path, {"project": paths.name, "history": []})
    node_groups = landscape.get("nodes", {}) if isinstance(landscape.get("nodes", {}), dict) else {}
    entry = {
        "updated_at": now_iso(),
        "topic": landscape.get("topic", ""),
        "phase": controller.get("phase", ""),
        "assurance_status": assurance.get("status", ""),
        "counts": {
            "papers": len(node_groups.get("papers", [])),
            "ideas": len(node_groups.get("ideas", [])),
            "repos": len(node_groups.get("repos", [])),
            "datasets": len(node_groups.get("datasets", [])),
            "experiments": len(node_groups.get("experiments", [])),
            "novelty_nodes": len(novelty.get("nodes", [])),
            "failed_hypotheses": len(failed.get("nodes", [])),
            "unexplored_niches": len(niches.get("nodes", [])),
        },
        "top_novelty_candidates": novelty.get("nodes", [])[:8],
        "top_unexplored_niches": niches.get("nodes", [])[:8],
        "recent_failed_hypotheses": failed.get("nodes", [])[-8:],
        "active_assurance_issues": assurance.get("issues", [])[:8],
        "next_objectives": controller.get("next_objectives", []),
        "evidence_files": [
            "state/research_landscape.json",
            "state/novelty_map.json",
            "state/failed_hypothesis_graph.json",
            "state/unexplored_niche_graph.json",
            "state/research_assurance_layer.json",
        ],
    }
    history = memory.get("history", []) if isinstance(memory.get("history", []), list) else []
    history.append(entry)
    memory.update({
        "project": paths.name,
        "updated_at": entry["updated_at"],
        "latest": entry,
        "history": history[-200:],
        "purpose": "Persistent long-term research direction memory for landscape, novelty, failed hypotheses, and unexplored niches.",
    })
    save_json(path, memory)
    return memory


def update_research_graph_history(paths, landscape: dict[str, Any], novelty: dict[str, Any], failed: dict[str, Any], niches: dict[str, Any], evidence_integrity: dict[str, Any], assurance: dict[str, Any]) -> dict[str, Any]:
    path = paths.state / "research_graph_history.json"
    ledger = load_json(path, {"project": paths.name, "history": []})
    ids = graph_node_ids(landscape, novelty, failed, niches)
    counts = {key: len(values) for key, values in ids.items()}
    snapshot_hash = stable_hash({"ids": ids, "assurance_status": assurance.get("status", ""), "evidence_integrity_status": evidence_integrity.get("status", "")})
    history = ledger.get("history", []) if isinstance(ledger.get("history", []), list) else []
    previous = history[-1] if history else {}
    prev_ids = previous.get("node_ids", {}) if isinstance(previous.get("node_ids", {}), dict) else {}
    deltas = {}
    for key, values in ids.items():
        prev_values = set(prev_ids.get(key, []) if isinstance(prev_ids.get(key, []), list) else [])
        current_values = set(values)
        deltas[key] = {
            "added": sorted(current_values - prev_values)[:80],
            "removed": sorted(prev_values - current_values)[:80],
            "added_count": len(current_values - prev_values),
            "removed_count": len(prev_values - current_values),
        }
    entry = {
        "updated_at": now_iso(),
        "snapshot_hash": snapshot_hash,
        "counts": counts,
        "node_ids": ids,
        "deltas_from_previous": deltas,
        "assurance_status": assurance.get("status", ""),
        "assurance_issue_count": len(assurance.get("issues", [])) if isinstance(assurance.get("issues", []), list) else 0,
        "evidence_integrity_status": evidence_integrity.get("status", ""),
        "evidence_integrity_issue_count": len(evidence_integrity.get("issues", [])) if isinstance(evidence_integrity.get("issues", []), list) else 0,
        "evidence_files": [
            "state/research_landscape.json",
            "state/novelty_map.json",
            "state/failed_hypothesis_graph.json",
            "state/unexplored_niche_graph.json",
            "state/research_evidence_integrity.json",
            "state/research_assurance_layer.json",
        ],
    }
    history.append(entry)
    repeated_hash_count = sum(1 for row in history[-20:] if isinstance(row, dict) and row.get("snapshot_hash") == snapshot_hash)
    landscape_assessment = {
        "project": paths.name,
        "updated_at": entry["updated_at"],
        "status": "stagnant" if repeated_hash_count >= 3 else "active",
        "history_entries": len(history[-200:]),
        "latest_snapshot_hash": snapshot_hash,
        "repeated_hash_count_last_20": repeated_hash_count,
        "counts": counts,
        "latest_deltas": deltas,
        "risk_notes": [
            "Landscape appears unchanged across repeated builds; prioritize literature/repo refresh or document why search is exhausted."
        ] if repeated_hash_count >= 3 else [],
        "principle": "Research direction quality is tracked as a trajectory of graph snapshots, not only the latest graph file.",
    }
    ledger.update({
        "project": paths.name,
        "updated_at": entry["updated_at"],
        "history_count": len(history[-200:]),
        "latest": entry,
        "history": history[-200:],
        "landscape_assessment": landscape_assessment,
        "principle": "Long-horizon The workflow must maintain graph history for landscape, novelty, failed hypotheses, and unexplored niches.",
    })
    save_json(path, ledger)
    save_json(paths.state / "research_landscape_assessment.json", landscape_assessment)
    return ledger


def update_evolutionary_memory_ledger(paths, memory: dict[str, Any], direction_memory: dict[str, Any], evolutionary_index: dict[str, Any], graph_history: dict[str, Any], evidence_integrity: dict[str, Any]) -> dict[str, Any]:
    path = paths.state / "evolutionary_memory_ledger.json"
    ledger = load_json(path, {"project": paths.name, "history": []})
    counts = {
        "ideation_entries": len(memory.get("ideation_memory", [])) if isinstance(memory.get("ideation_memory", []), list) else 0,
        "experimentation_entries": len(memory.get("experimentation_memory", [])) if isinstance(memory.get("experimentation_memory", []), list) else 0,
        "assurance_entries": len(memory.get("assurance_memory", [])) if isinstance(memory.get("assurance_memory", []), list) else 0,
        "trajectory_entries": len(memory.get("trajectory_memory", [])) if isinstance(memory.get("trajectory_memory", []), list) else 0,
        "direction_entries": len(direction_memory.get("history", [])) if isinstance(direction_memory.get("history", []), list) else 0,
        "graph_history_entries": int(graph_history.get("history_count", 0) or 0),
        "evolutionary_index_items": int(evolutionary_index.get("indexed_item_count", 0) or 0),
    }
    latest_memory_hash = stable_hash({
        "research_memory_tail": {key: (memory.get(key, [])[-3:] if isinstance(memory.get(key, []), list) else []) for key in ["ideation_memory", "experimentation_memory", "assurance_memory", "trajectory_memory"]},
        "direction_latest": direction_memory.get("latest", {}) if isinstance(direction_memory, dict) else {},
        "graph_hash": (graph_history.get("latest", {}) if isinstance(graph_history.get("latest", {}), dict) else {}).get("snapshot_hash", ""),
        "index_count": evolutionary_index.get("indexed_item_count", 0) if isinstance(evolutionary_index, dict) else 0,
    })
    history = ledger.get("history", []) if isinstance(ledger.get("history", []), list) else []
    previous = history[-1] if history else {}
    prev_counts = previous.get("counts", {}) if isinstance(previous.get("counts", {}), dict) else {}
    deltas = {key: counts[key] - int(prev_counts.get(key, 0) or 0) for key in counts}
    entry = {
        "updated_at": now_iso(),
        "memory_hash": latest_memory_hash,
        "counts": counts,
        "delta_from_previous": deltas,
        "evidence_integrity_status": evidence_integrity.get("status", ""),
        "evidence_integrity_issue_count": len(evidence_integrity.get("issues", [])) if isinstance(evidence_integrity.get("issues", []), list) else 0,
        "required_next_reads": [
            "state/research_memory.json",
            "state/research_direction_memory.json",
            "state/research_graph_history.json",
            "state/evolutionary_memory_index.json",
            "state/research_evidence_integrity.json",
        ],
    }
    history.append(entry)
    ledger.update({
        "project": paths.name,
        "updated_at": entry["updated_at"],
        "history_count": len(history[-200:]),
        "latest": entry,
        "history": history[-200:],
        "principle": "Evolutionary memory must be a durable inheritance ledger across ideation, experimentation, assurance, direction, and trajectory state.",
    })
    save_json(path, ledger)
    return ledger


def audit_evidence_integrity(paths, trajectory: dict[str, Any], claim_ledger: dict[str, Any] | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add_check(scope: str, node_id: str, evidence: Any, negative_evidence: bool = False) -> None:
        statuses = evidence_statuses(paths, evidence)
        has_any = bool(flatten_evidence(evidence))
        missing_local = [row for row in statuses if row.get("kind") == "local_path" and row.get("exists") is False]
        local_count = sum(1 for row in statuses if row.get("kind") == "local_path")
        existing_local_count = sum(1 for row in statuses if row.get("kind") == "local_path" and row.get("exists") is True)
        external_count = sum(1 for row in statuses if row.get("kind") == "external")
        text_count = sum(1 for row in statuses if row.get("kind") == "text")
        severity = "pass"
        issue = ""
        if not has_any:
            severity = "warn"
            issue = "missing evidence reference"
        elif missing_local:
            severity = "warn" if negative_evidence else "block"
            issue = "negative evidence path is absent" if negative_evidence else "local evidence path does not exist"
        checks.append({
            "scope": scope,
            "id": node_id,
            "severity": severity,
            "issue": issue,
            "local_refs": local_count,
            "existing_local_refs": existing_local_count,
            "external_refs": external_count,
            "text_refs": text_count,
            "missing_local_refs": missing_local[:8],
            "sample_evidence": flatten_evidence(evidence)[:8],
            "evidence_statuses": statuses[:12],
            "negative_evidence": bool(negative_evidence),
        })

    landscape = trajectory.get("research_landscape", {}) if isinstance(trajectory.get("research_landscape", {}), dict) else {}
    for group, rows in (landscape.get("nodes", {}) if isinstance(landscape.get("nodes", {}), dict) else {}).items():
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                negative_evidence = group == "datasets" and (not row.get("claim_ready") or not row.get("loader_probe_success"))
                add_check(f"research_landscape.{group}", str(row.get("id") or row.get("name") or row.get("title") or "unknown"), row.get("evidence"), negative_evidence=negative_evidence)
    for graph_key in ["novelty_map", "failed_hypothesis_graph", "unexplored_niche_graph"]:
        graph = trajectory.get(graph_key, {}) if isinstance(trajectory.get(graph_key, {}), dict) else {}
        for row in graph.get("nodes", []) if isinstance(graph.get("nodes", []), list) else []:
            if isinstance(row, dict):
                add_check(graph_key, str(row.get("id") or row.get("title") or "unknown"), row.get("evidence") or row.get("evidence_needed") or row.get("needed_evidence"))
    assurance = trajectory.get("assurance_layer", {}) if isinstance(trajectory.get("assurance_layer", {}), dict) else {}
    for row in assurance.get("issues", []) if isinstance(assurance.get("issues", []), list) else []:
        if isinstance(row, dict):
            add_check("assurance_layer.issue", str(row.get("issue") or "issue"), row.get("evidence"))
    controller = trajectory.get("trajectory_controller", {}) if isinstance(trajectory.get("trajectory_controller", {}), dict) else {}
    for row in controller.get("executable_queue", []) if isinstance(controller.get("executable_queue", []), list) else []:
        if isinstance(row, dict):
            add_check("trajectory_controller.executable_queue", str(row.get("id") or row.get("objective") or "queue_item"), row.get("evidence_inputs"))

    for claim in (claim_ledger or {}).get("claims", []) if isinstance((claim_ledger or {}).get("claims", []), list) else []:
        if not isinstance(claim, dict):
            continue
        claim_status = str(claim.get("status", "")).lower()
        severity = "block" if claim_status in {"weak", "unsupported"} else "pass"
        issue = "claim ledger claim is weak or unsupported" if severity == "block" else ""
        statuses = evidence_statuses(paths, ["state/claim_ledger.json"])
        checks.append({
            "scope": "claim_ledger.claim",
            "id": str(claim.get("claim_type") or claim.get("text") or "claim"),
            "severity": severity,
            "issue": issue,
            "local_refs": 1,
            "existing_local_refs": sum(1 for row in statuses if row.get("kind") == "local_path" and row.get("exists") is True),
            "external_refs": 0,
            "text_refs": 0,
            "missing_local_refs": [row for row in statuses if row.get("kind") == "local_path" and row.get("exists") is False],
            "sample_evidence": ["state/claim_ledger.json"],
            "evidence_statuses": statuses[:12],
            "claim_status": claim_status,
            "support_count": int(claim.get("support_count", 0) or 0),
            "weak_only_count": int(claim.get("weak_only_count", 0) or 0),
        })

    # Derive the marker from the current Find environment-stage repo, not stale active_repo.json.
    _current_repo = current_route_repo_context(paths, load_json(paths.state / "active_repo.json", {}))
    _active_repo_path = selected_repo_path_from_active(_current_repo)
    _active_repo_marker = _active_repo_path.rstrip("/").split("/")[-1] if _active_repo_path else "__no_active_repo__"

    def _is_stale_repo_check(row: dict) -> bool:
        # Case 1: missing local refs all from non-active repos
        missing = row.get("missing_local_refs", []) or []
        if missing and not any(_active_repo_marker in str(r.get("ref", "")) for r in missing):
            return True
        # Case 2: no evidence from research_landscape scope with non-active repo
        if not missing and row.get("issue") == "missing evidence reference" and row.get("scope", "").startswith("research_landscape"):
            node_id = str(row.get("id", ""))
            # Ideas from non-selected repos and stale historical-route datasets
            if node_id and _active_repo_marker not in node_id:
                return True
        return False

    active_checks = [row for row in checks if not _is_stale_repo_check(row)]
    block_count = sum(1 for row in active_checks if row.get("severity") == "block")
    warn_count = sum(1 for row in active_checks if row.get("severity") == "warn")
    pass_count = sum(1 for row in active_checks if row.get("severity") == "pass")
    auditable_count = sum(1 for row in active_checks if row.get("existing_local_refs", 0) or row.get("external_refs", 0) or row.get("text_refs", 0))
    score = round(auditable_count / len(active_checks), 4) if active_checks else 1.0
    status = "blocked" if block_count else "warn" if warn_count else "pass"
    manifest_refs = []
    for row in checks:
        for status_row in row.get("evidence_statuses", []) if isinstance(row.get("evidence_statuses", []), list) else []:
            item = dict(status_row)
            item.update({"scope": row.get("scope", ""), "id": row.get("id", ""), "check_severity": row.get("severity", "")})
            manifest_refs.append(item)
    weak_claim_checks = [row for row in checks if row.get("scope") == "claim_ledger.claim" and row.get("severity") == "block"]
    # Filter out missing local refs from pruned/inactive repos so they
    # don't produce stale warnings in the evidence manifest.
    manifest_missing = [
        row for row in manifest_refs
        if row.get("kind") == "local_path" and row.get("exists") is False
        and _active_repo_marker in str(row.get("ref", ""))
    ][:200]
    manifest = {
        "project": paths.name,
        "updated_at": now_iso(),
        "status": status,
        "ref_count": len(manifest_refs),
        "existing_local_refs": sum(1 for row in manifest_refs if row.get("kind") == "local_path" and row.get("exists") is True),
        "missing_local_refs": manifest_missing,
        "external_refs": sum(1 for row in manifest_refs if row.get("kind") == "external"),
        "text_refs": sum(1 for row in manifest_refs if row.get("kind") == "text"),
        "weak_or_unsupported_claims": [{"id": row.get("id"), "claim_status": row.get("claim_status"), "support_count": row.get("support_count"), "weak_only_count": row.get("weak_only_count")} for row in weak_claim_checks],
        "refs": manifest_refs[:1000],
        "principle": "Evidence references are tracked separately from claims so TASTE can audit whether a claim is supported by local artifacts, external records, or only weak text references.",
    }
    save_json(paths.state / "research_evidence_manifest.json", manifest)
    payload = {
        "project": paths.name,
        "updated_at": now_iso(),
        "status": status,
        "score": score,
        "checked_nodes": len(checks),
        "pass_count": pass_count,
        "warn_count": warn_count,
        "block_count": block_count,
        "issues": [row for row in checks if row.get("severity") != "pass"][:200],
        "checks": checks[:500],
        "manifest_path": str(paths.state / "research_evidence_manifest.json"),
        "principle": "Every trajectory node should expose evidence; missing local files are blockers, while text/external references are preserved as lower-strength evidence.",
    }
    save_json(paths.state / "research_evidence_integrity.json", payload)
    return payload


def build_trajectory_optimization_plan(paths, controller: dict[str, Any], assurance: dict[str, Any], failed: dict[str, Any], niches: dict[str, Any], evidence_integrity: dict[str, Any], skills: list[dict[str, Any]], blocker_action_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    skill_by_name = {str(row.get("name") or ""): row for row in skills if isinstance(row, dict)}

    def skill_path(name: str) -> str:
        return str(skill_by_name.get(name, {}).get("path") or "")

    queue: list[dict[str, Any]] = []
    blocker_actions = blocker_action_plan.get("actions", []) if isinstance(blocker_action_plan, dict) and isinstance(blocker_action_plan.get("actions", []), list) else []
    for index, action in enumerate(blocker_actions[:16]):
        if not isinstance(action, dict):
            continue
        queue.append({
            "id": f"blocker-action-{index + 1}",
            "priority": action.get("priority") or "P1",
            "owner_role": action.get("route") or "blocker_action_router",
            "skill_contract": action.get("skill_contract") or "",
            "objective": f"Resolve routed blocker: {action.get('issue', '')}",
            "blocker_action_id": action.get("id", ""),
            "blocker_category": action.get("category", ""),
            "autonomy": action.get("autonomy", ""),
            "repair_strategy": action.get("repair_strategy", ""),
            "recommended_commands": action.get("recommended_commands", []) if isinstance(action.get("recommended_commands", []), list) else [],
            "evidence_inputs": flatten_evidence(action.get("evidence")) or [action.get("source", ""), "state/blocker_action_plan.json"],
            "success_checks": action.get("success_checks", []) if isinstance(action.get("success_checks", []), list) else ["The routed blocker is removed from blocker_action_plan or preserved with a concrete missing-resource reason."],
            "stop_condition": "If autonomy=manual_required, do not fake completion; keep the submission gate blocked and record the required human action.",
        })
    for index, issue in enumerate(assurance.get("issues", []) if isinstance(assurance.get("issues", []), list) else []):
        if not isinstance(issue, dict):
            continue
        queue.append({
            "id": f"assurance-{index + 1}",
            "priority": "P0" if issue.get("severity") == "block" else "P1",
            "owner_role": "evidence_assurance_reviewer",
            "skill_contract": skill_path("evidence-gate"),
            "objective": f"Resolve or truthfully document evidence issue: {issue.get('issue', '')}",
            "evidence_inputs": flatten_evidence(issue.get("evidence")) or ["state/research_assurance_layer.json"],
            "success_checks": ["Issue is removed from research_assurance_layer or downgraded with new local evidence.", "No paper claim is promoted before the gate passes."],
            "stop_condition": "If evidence cannot be produced locally, keep the claim blocked and record the missing resource.",
        })
    for index, node in enumerate(failed.get("nodes", [])[:10] if isinstance(failed.get("nodes", []), list) else []):
        if not isinstance(node, dict):
            continue
        queue.append({
            "id": f"repair-{index + 1}",
            "priority": "P1",
            "owner_role": "trajectory_experiment_executor",
            "skill_contract": skill_path("experiment-loop"),
            "objective": f"Repair, retry, or prune failed hypothesis: {node.get('method') or node.get('id')}",
            "evidence_inputs": flatten_evidence(node.get("evidence")) or ["state/failed_hypothesis_graph.json", "state/experiment_registry.json"],
            "success_checks": ["A rerun produces an audit-ready registry entry or a prune decision with evidence.", "Recoverable exception memory is updated."],
            "stop_condition": "Prune after bounded retries if the same failure recurs with local evidence.",
        })
    for index, node in enumerate(niches.get("nodes", [])[:10] if isinstance(niches.get("nodes", []), list) else []):
        if not isinstance(node, dict):
            continue
        queue.append({
            "id": f"explore-{index + 1}",
            "priority": "P2" if assurance.get("status") == "blocked" else "P1",
            "owner_role": "landscape_cartographer",
            "skill_contract": skill_path("experiment-loop"),
            "objective": f"Turn unexplored niche into one bounded experiment: {node.get('title') or node.get('id')}",
            "evidence_inputs": flatten_evidence(node.get("evidence")) or ["state/unexplored_niche_graph.json", "state/novelty_map.json"],
            "success_checks": ["A concrete repo/data/metric plan is recorded before execution.", "The experiment result updates novelty, failed, and memory graphs."],
            "stop_condition": "Do not execute if repo/data evidence is missing or assurance P0 issues would invalidate the result.",
        })
    if evidence_integrity.get("status") != "pass":
        queue.append({
            "id": "evidence-integrity-sweep",
            "priority": "P0" if evidence_integrity.get("status") == "blocked" else "P1",
            "owner_role": "evolutionary_memory_curator",
            "skill_contract": skill_path("evidence-gate"),
            "objective": "Repair trajectory nodes that lack auditable evidence references or point at missing local files.",
            "evidence_inputs": ["state/research_evidence_integrity.json"],
            "success_checks": ["research_evidence_integrity.status becomes pass or remaining warnings are explicitly justified."],
            "stop_condition": "Never convert an unsupported claim into a stronger claim just to pass the audit.",
        })
    if not queue:
        queue.append({
            "id": "refresh-landscape",
            "priority": "P1",
            "owner_role": "landscape_cartographer",
            "skill_contract": skill_path("experiment-loop"),
            "objective": "Refresh landscape and propose the next evidence-feasible trajectory branch.",
            "evidence_inputs": ["state/research_landscape.json", "state/research_memory.json"],
            "success_checks": ["At least one evidence-feasible niche or prune decision is recorded."],
            "stop_condition": "Ask for resources only after local repo/data/literature search paths are exhausted.",
        })
    payload = {
        "project": paths.name,
        "updated_at": now_iso(),
        "phase": controller.get("phase", ""),
        "queue": queue[:40],
        "queue_size": len(queue[:40]),
        "highest_priority": queue[0].get("priority") if queue else "",
        "agent_handoff": {
            "main_agent_goal": "Optimize the whole research trajectory through evidence-gated plan/execute/evaluate/repair loops, not a single response.",
            "queue_policy": "Run routed P0 blocker actions first, then assurance/evidence work, then P1 repair/explore work; update persistent memory after every material action.",
            "required_memory_outputs": ["state/research_memory.json", "state/research_direction_memory.json", "state/research_evidence_integrity.json", "state/trajectory_optimization_plan.json"],
        },
        "blocker_action_plan_summary": (blocker_action_plan.get("summary", {}) if isinstance(blocker_action_plan, dict) else {}),
        "source_objectives": controller.get("next_objectives", []),
    }
    save_json(paths.state / "trajectory_optimization_plan.json", payload)
    return payload


def build_evolutionary_memory_index(paths, trajectory: dict[str, Any], research_memory: dict[str, Any], direction_memory: dict[str, Any], evolution_memory: dict[str, Any], evo_cycle: dict[str, Any], method_frontier: dict[str, Any], next_actions: dict[str, Any], aris_board: dict[str, Any], ideas: dict[str, Any], experiments: Any, evidence_integrity: dict[str, Any], optimization_plan: dict[str, Any]) -> dict[str, Any]:
    """Unify native assurance and recoverable-cycle memories into one inheritance index for the next agent turn."""
    exp_rows = experiments if isinstance(experiments, list) else []
    idea_rows = ideas.get("ideas", []) if isinstance(ideas, dict) else []
    latest_research_memory = {
        key: (research_memory.get(key, [])[-1] if isinstance(research_memory.get(key, []), list) and research_memory.get(key, []) else {})
        for key in ["ideation_memory", "experimentation_memory", "assurance_memory", "trajectory_memory"]
    } if isinstance(research_memory, dict) else {}
    aris_methods = aris_board.get("methods", []) if isinstance(aris_board, dict) and isinstance(aris_board.get("methods", []), list) else []
    method_summaries = next_actions.get("method_summaries", []) if isinstance(next_actions, dict) and isinstance(next_actions.get("method_summaries", []), list) else []
    exception_memory_entries = evo_cycle.get("exception_memory_entries", 0) if isinstance(evo_cycle, dict) else 0
    indexed_items = []
    for row in idea_rows[:12]:
        if isinstance(row, dict):
            indexed_items.append({"type": "idea", "id": row.get("idea_id") or row.get("id") or first_text(row, "title", "idea"), "score": row.get("idea_score", row.get("score", 0)), "evidence": ["state/idea_candidates.json"]})
    for row in exp_rows[-20:]:
        if isinstance(row, dict):
            indexed_items.append({"type": "experiment", "id": row.get("experiment_id") or row.get("name"), "status": row.get("status", ""), "audit_ready": bool(row.get("audit_ready")), "evidence": [item for item in [row.get("audit_path", ""), row.get("artifact_path", "")] if item] or ["state/experiment_registry.json"]})
    for row in aris_methods[:12]:
        if isinstance(row, dict):
            indexed_items.append({"type": "assurance_method_verdict", "id": row.get("method", ""), "recommendation": row.get("recommendation", ""), "issues": row.get("issues", []), "evidence": ["state/aris_review_board.json"]})
    for row in optimization_plan.get("queue", [])[:12] if isinstance(optimization_plan.get("queue", []), list) else []:
        if isinstance(row, dict):
            indexed_items.append({"type": "trajectory_queue", "id": row.get("id", ""), "priority": row.get("priority", ""), "owner_role": row.get("owner_role", ""), "evidence": row.get("evidence_inputs", [])})
    payload = {
        "project": paths.name,
        "updated_at": now_iso(),
        "status": "blocked" if trajectory.get("assurance_layer", {}).get("status") == "blocked" else "active",
        "indexed_item_count": len(indexed_items),
        "indexed_items": indexed_items[:80],
        "memory_counts": {
            "research_ideation_entries": len(research_memory.get("ideation_memory", [])) if isinstance(research_memory, dict) and isinstance(research_memory.get("ideation_memory", []), list) else 0,
            "research_experimentation_entries": len(research_memory.get("experimentation_memory", [])) if isinstance(research_memory, dict) and isinstance(research_memory.get("experimentation_memory", []), list) else 0,
            "direction_entries": len(direction_memory.get("history", [])) if isinstance(direction_memory, dict) and isinstance(direction_memory.get("history", []), list) else 0,
            "evo_exception_entries": exception_memory_entries,
            "evo_experimentation_entries": evo_cycle.get("experimentation_memory_entries", 0) if isinstance(evo_cycle, dict) else 0,
        },
        "latest_research_memory": latest_research_memory,
        "method_frontier": {
            "elite_methods": method_frontier.get("elite_methods", [])[:5] if isinstance(method_frontier, dict) and isinstance(method_frontier.get("elite_methods", []), list) else [],
            "repair_queue": method_frontier.get("repair_queue", [])[:8] if isinstance(method_frontier, dict) and isinstance(method_frontier.get("repair_queue", []), list) else [],
            "prune_queue": method_frontier.get("prune_queue", [])[:8] if isinstance(method_frontier, dict) and isinstance(method_frontier.get("prune_queue", []), list) else [],
        },
        "assurance_snapshot": {
            "assurance_status": trajectory.get("assurance_layer", {}).get("status", ""),
            "assurance_issue_count": len(trajectory.get("assurance_layer", {}).get("issues", [])),
            "evidence_integrity_status": evidence_integrity.get("status", ""),
            "evidence_integrity_issue_count": len(evidence_integrity.get("issues", [])) if isinstance(evidence_integrity.get("issues", []), list) else 0,
        },
        "inheritance_rules": [
            "Read this index before asking Claude Code to switch repo, change environment, or launch experiments.",
            "Prefer P0 trajectory queue items over new ideation unless the queue is blocked by missing resources.",
            "Never promote claims from experiments that are not audit-ready and evidence-linked.",
            "Treat TASTE assurance prune/repair verdicts and recoverable exceptions as persistent constraints, not transient chat context.",
        ],
        "required_next_reads": [
            "state/evolutionary_memory_index.json",
            "state/trajectory_optimization_plan.json",
            "state/research_evidence_integrity.json",
            "state/research_direction_memory.json",
            "state/research_memory.json",
            "state/recoverable_cycle_summary.json",
            "state/evidence_review_board.json",
        ],
        "source_files": evidence_refs(
            paths.state / "research_memory.json",
            paths.state / "research_direction_memory.json",
            paths.state / "trajectory_optimization_plan.json",
            paths.state / "research_evidence_integrity.json",
            paths.state / "evolution_memory.json",
            paths.state / "recoverable_cycle_summary.json",
            paths.state / "evidence_review_board.json",
            paths.state / "method_frontier.json",
            paths.state / "next_actions.json",
        ),
    }
    save_json(paths.state / "evolutionary_memory_index.json", payload)
    return payload


def update_trajectory_checkpoints(paths, trajectory: dict[str, Any]) -> dict[str, Any]:
    path = paths.state / "trajectory_checkpoints.json"
    checkpoints = load_json(path, {"project": paths.name, "history": []})
    history = checkpoints.get("history", []) if isinstance(checkpoints.get("history", []), list) else []
    summary = trajectory.get("summary", {}) if isinstance(trajectory.get("summary", {}), dict) else {}
    counts = {
        "landscape_nodes": int(summary.get("landscape_nodes", 0) or 0),
        "novelty_nodes": int(summary.get("novelty_nodes", 0) or 0),
        "failed_hypotheses": int(summary.get("failed_hypotheses", 0) or 0),
        "unexplored_niches": int(summary.get("unexplored_niches", 0) or 0),
        "assurance_issue_count": int(summary.get("assurance_issue_count", 0) or 0),
        "evidence_integrity_issue_count": int(summary.get("evidence_integrity_issue_count", 0) or 0),
        "optimization_queue_size": int(summary.get("optimization_queue_size", 0) or 0),
        "direction_memory_entries": int(summary.get("direction_memory_entries", 0) or 0),
    }
    prev = history[-1] if history else {}
    prev_counts = prev.get("counts", {}) if isinstance(prev.get("counts", {}), dict) else {}
    delta = {key: counts.get(key, 0) - int(prev_counts.get(key, 0) or 0) for key in counts}
    regressions = []
    improvements = []
    continuity = []
    if not history:
        continuity.append("baseline checkpoint captured")
    else:
        if delta.get("assurance_issue_count", 0) > 0:
            regressions.append("assurance issues increased")
        elif delta.get("assurance_issue_count", 0) < 0:
            improvements.append("assurance issues decreased")
        if delta.get("evidence_integrity_issue_count", 0) > 0:
            regressions.append("evidence integrity issues increased")
        elif delta.get("evidence_integrity_issue_count", 0) < 0:
            improvements.append("evidence integrity issues decreased")
        if delta.get("novelty_nodes", 0) > 0 or delta.get("unexplored_niches", 0) > 0:
            improvements.append("search space expanded")
        if delta.get("direction_memory_entries", 0) > 0:
            continuity.append("direction memory extended")
    delta_status = "initial_checkpoint" if not history else "regressed" if regressions else "improved" if improvements else "memory_extended" if continuity else "stable"
    latest = {
        "updated_at": trajectory.get("updated_at", now_iso()),
        "phase": summary.get("phase", ""),
        "assurance_status": summary.get("assurance_status", ""),
        "evidence_integrity_status": summary.get("evidence_integrity_status", ""),
        "counts": counts,
        "delta_from_previous": delta,
        "delta_status": delta_status,
        "improvements": improvements,
        "regressions": regressions,
        "continuity": continuity,
        "next_queue_head": (trajectory.get("trajectory_optimization_plan", {}).get("queue", [])[:3] if isinstance(trajectory.get("trajectory_optimization_plan", {}).get("queue", []), list) else []),
        "evidence": ["state/research_trajectory_system.json", "state/trajectory_optimization_plan.json", "state/research_evidence_integrity.json"],
    }
    history.append(latest)
    payload = {
        "project": paths.name,
        "updated_at": latest["updated_at"],
        "checkpoint_count": len(history[-200:]),
        "latest": latest,
        "history": history[-200:],
        "principle": "Trajectory quality is evaluated across checkpoints; stable blockers must become explicit queue items or resource escalations.",
    }
    save_json(path, payload)
    return payload

def write_markdown(paths, trajectory: dict[str, Any]) -> Path:
    out = paths.reports / "research_trajectory_system.md"
    controller = trajectory.get("trajectory_controller", {})
    assurance = trajectory.get("assurance_layer", {})
    landscape_count = sum(len(v) for v in trajectory.get("research_landscape", {}).get("nodes", {}).values())
    integrity = trajectory.get("research_evidence_integrity", {}) if isinstance(trajectory.get("research_evidence_integrity", {}), dict) else {}
    plan = trajectory.get("trajectory_optimization_plan", {}) if isinstance(trajectory.get("trajectory_optimization_plan", {}), dict) else {}
    direction = trajectory.get("direction_memory", {}) if isinstance(trajectory.get("direction_memory", {}), dict) else {}
    checkpoints = trajectory.get("trajectory_checkpoints", {}) if isinstance(trajectory.get("trajectory_checkpoints", {}), dict) else {}
    evo_index = trajectory.get("evolutionary_memory_index", {}) if isinstance(trajectory.get("evolutionary_memory_index", {}), dict) else {}
    protocol = trajectory.get("trajectory_execution_protocol", {}) if isinstance(trajectory.get("trajectory_execution_protocol", {}), dict) else {}
    lines = ["# Research Trajectory System\n\n", f"- updated_at: {trajectory.get('updated_at', '')}\n", f"- phase: {controller.get('phase', '')}\n", f"- assurance_status: {assurance.get('status', '')}\n", f"- evidence_integrity_status: {integrity.get('status', '')}\n", f"- optimization_queue_size: {plan.get('queue_size', 0)}\n", f"- trajectory_checkpoint_count: {checkpoints.get('checkpoint_count', 0)}\n", f"- trajectory_delta_status: {checkpoints.get('latest', {}).get('delta_status', '') if isinstance(checkpoints.get('latest', {}), dict) else ''}\n", f"- evolutionary_index_items: {evo_index.get('indexed_item_count', 0)}\n", f"- direction_memory_entries: {direction.get('entries', 0)}\n", f"- landscape_nodes: {landscape_count}\n", f"- novelty_nodes: {len(trajectory.get('novelty_map', {}).get('nodes', []))}\n", f"- failed_hypotheses: {len(trajectory.get('failed_hypothesis_graph', {}).get('nodes', []))}\n", f"- unexplored_niches: {len(trajectory.get('unexplored_niche_graph', {}).get('nodes', []))}\n\n", "## Next Objectives\n"]
    for item in controller.get("next_objectives", []):
        lines.append(f"- {item}\n")
    lines.append("\n## Agent Roles\n")
    for row in controller.get("agent_roles", []):
        lines.append(f"- {row.get('role')}: {row.get('responsibility')}\n")
    evo_cycle = trajectory.get("recoverable_cycle", trajectory.get("evoscientist_cycle", {}))
    lines.append("\n## Recoverable Cycle\n")
    lines.append(f"- status: {evo_cycle.get('status', '')}\n")
    lines.append(f"- phases: {evo_cycle.get('phase_count', 0)}\n")
    lines.append(f"- recoverable_exceptions: {evo_cycle.get('recoverable_exception_count', 0)}\n")
    for row in evo_cycle.get("phases", [])[:12]:
        lines.append(f"- {row.get('phase')}: role={row.get('role')} status={row.get('status')} recoveries={row.get('recoverable_exception_count', 0)}\n")
    lines.append("\n## Local Claude Skills\n")
    for row in trajectory.get("skill_contracts", []):
        lines.append(f"- {row.get('display_name') or row.get('name')}: {row.get('description', '')}\n")
    third_party_stack = trajectory.get("third_party_research_stack", {}) if isinstance(trajectory.get("third_party_research_stack", {}), dict) else {}
    stack_summary = third_party_stack.get("summary", {}) if isinstance(third_party_stack.get("summary", {}), dict) else {}
    lines.append("\n## Method Provenance Audit\n")
    lines.append(f"- status: {third_party_stack.get('status', '')}\n")
    lines.append(f"- source_count: {stack_summary.get('source_count', 0)}\n")
    lines.append(f"- selected_module_count: {stack_summary.get('selected_module_count', 0)}\n")
    lines.append(f"- synced_skill_count: {stack_summary.get('synced_skill_count', 0)}\n")
    lines.append(f"- report: {third_party_stack.get('report', '')}\n")
    for binding in third_party_stack.get("capability_bindings", []) if isinstance(third_party_stack.get("capability_bindings", []), list) else []:
        contract = binding.get("native_contract") or binding.get("contract") or binding.get("capability", "")
        lines.append(f"- binding: {binding.get('capability')} -> {contract}\n")
    lines.append("\n## Trajectory Execution Protocol\n")
    main_agent = protocol.get("main_agent", {}) if isinstance(protocol.get("main_agent", {}), dict) else {}
    lines.append(f"- status: {protocol.get('status', '')}\n")
    lines.append(f"- entrypoint: {main_agent.get('entrypoint', '')}\n")
    lines.append(f"- supervisor_state: {main_agent.get('state_path', '')}\n")
    for row in protocol.get("loop_steps", [])[:10]:
        lines.append(f"- {row.get('step')}: {row.get('action')}\n")
    lines.append("\n## Trajectory Optimization Queue\n")
    for row in plan.get("queue", [])[:20]:
        lines.append(f"- [{row.get('priority')}] {row.get('id')}: {row.get('objective')} | owner={row.get('owner_role')} | skill={row.get('skill_contract', '')}\n")
        if row.get("recommended_commands"):
            commands = row.get("recommended_commands", []) if isinstance(row.get("recommended_commands", []), list) else []
            if commands:
                lines.append(f"  verify: `{commands[-1]}`\n")
    if not plan.get("queue"):
        lines.append("- No queued trajectory action.\n")
    blocker_plan = trajectory.get("blocker_action_plan", {}) if isinstance(trajectory.get("blocker_action_plan", {}), dict) else {}
    blocker_summary = blocker_plan.get("summary", {}) if isinstance(blocker_plan.get("summary", {}), dict) else {}
    lines.append("\n## Blocker Action Routing\n")
    lines.append(f"- status: {blocker_plan.get('status', '')}\n")
    lines.append(f"- actions: {blocker_summary.get('action_count', 0)}\n")
    lines.append(f"- autonomous_actions: {blocker_summary.get('autonomous_action_count', 0)}\n")
    lines.append(f"- manual_actions: {blocker_summary.get('manual_action_count', 0)}\n")
    lines.append(f"- top_route: {blocker_summary.get('top_route', '')}\n")
    for row in blocker_plan.get("actions", [])[:12] if isinstance(blocker_plan.get("actions", []), list) else []:
        lines.append(f"- [{row.get('priority')}] {row.get('route')}: {row.get('issue')} | autonomy={row.get('autonomy')}\n")
    lines.append("\n## Trajectory Checkpoints\n")
    latest_checkpoint = checkpoints.get("latest", {}) if isinstance(checkpoints.get("latest", {}), dict) else {}
    lines.append(f"- checkpoint_count: {checkpoints.get('checkpoint_count', 0)}\n")
    lines.append(f"- delta_status: {latest_checkpoint.get('delta_status', '')}\n")
    for item in latest_checkpoint.get("improvements", [])[:10]:
        lines.append(f"- improvement: {item}\n")
    for item in latest_checkpoint.get("regressions", [])[:10]:
        lines.append(f"- regression: {item}\n")
    lines.append("\n## Evolutionary Memory Index\n")
    lines.append(f"- indexed_items: {evo_index.get('indexed_item_count', 0)}\n")
    for row in evo_index.get("indexed_items", [])[:20]:
        lines.append(f"- {row.get('type')}: {row.get('id')} | evidence={row.get('evidence', [])}\n")
    graph_history = trajectory.get("research_graph_history", {}) if isinstance(trajectory.get("research_graph_history", {}), dict) else {}
    landscape_assessment = trajectory.get("research_landscape_assessment", {}) if isinstance(trajectory.get("research_landscape_assessment", {}), dict) else {}
    memory_ledger = trajectory.get("evolutionary_memory_ledger", {}) if isinstance(trajectory.get("evolutionary_memory_ledger", {}), dict) else {}
    manifest = trajectory.get("research_evidence_manifest", {}) if isinstance(trajectory.get("research_evidence_manifest", {}), dict) else {}
    lines.append("\n## Research Graph History\n")
    lines.append(f"- history_entries: {graph_history.get('history_count', 0)}\n")
    lines.append(f"- landscape_assessment_status: {landscape_assessment.get('status', '')}\n")
    lines.append(f"- latest_snapshot_hash: {landscape_assessment.get('latest_snapshot_hash', '')}\n")
    for item in landscape_assessment.get("risk_notes", [])[:8]:
        lines.append(f"- risk: {item}\n")
    lines.append("\n## Evolutionary Memory Ledger\n")
    lines.append(f"- history_entries: {memory_ledger.get('history_count', 0)}\n")
    latest_memory = memory_ledger.get("latest", {}) if isinstance(memory_ledger.get("latest", {}), dict) else {}
    lines.append(f"- memory_hash: {latest_memory.get('memory_hash', '')}\n")
    lines.append("\n## Evidence Manifest\n")
    lines.append(f"- refs: {manifest.get('ref_count', 0)}\n")
    lines.append(f"- missing_local_refs: {len(manifest.get('missing_local_refs', [])) if isinstance(manifest.get('missing_local_refs', []), list) else 0}\n")
    lines.append(f"- weak_or_unsupported_claims: {len(manifest.get('weak_or_unsupported_claims', [])) if isinstance(manifest.get('weak_or_unsupported_claims', []), list) else 0}\n")
    orchestra = trajectory.get("paper_orchestra_state", {}) if isinstance(trajectory.get("paper_orchestra_state", {}), dict) else {}
    readiness = trajectory.get("submission_readiness", {}) if isinstance(trajectory.get("submission_readiness", {}), dict) else {}
    lines.append("\n## TASTE Paper Production System\n")
    lines.append(f"- paper_orchestra_state_status: {orchestra.get('status', '')}\n")
    lines.append(f"- paper_orchestra_promotion_gate: {orchestra.get('promotion_gate_recommendation', '')}\n")
    lines.append(f"- submission_readiness_status: {readiness.get('status', '')}\n")
    lines.append(f"- submission_ready: {readiness.get('submission_ready', False)}\n")
    bridge = trajectory.get("paper_orchestra_bridge", {}) if isinstance(trajectory.get("paper_orchestra_bridge", {}), dict) else {}
    normality = trajectory.get("paper_normality_audit", {}) if isinstance(trajectory.get("paper_normality_audit", {}), dict) else {}
    lines.append(f"- paper_bridge_status: {bridge.get('status', '')}\n")
    lines.append(f"- paper_normality_status: {normality.get('status', '')}\n")
    lines.append(f"- normal_preview_ready: {normality.get('normal_preview_ready', False)}\n")
    lines.append(f"- section_count: {len(orchestra.get('sections', [])) if isinstance(orchestra.get('sections', []), list) else 0}\n")
    for row in orchestra.get("sections", [])[:12] if isinstance(orchestra.get("sections", []), list) else []:
        lines.append(f"- {row.get('id')}: status={row.get('status')} blockers={len(row.get('blockers', [])) if isinstance(row.get('blockers', []), list) else 0}\n")
    for item in readiness.get("blockers", [])[:12] if isinstance(readiness.get("blockers", []), list) else []:
        lines.append(f"- submission_blocker: {item}\n")
    verification = trajectory.get("research_trajectory_end_to_end_verification", {}) if isinstance(trajectory.get("research_trajectory_end_to_end_verification", {}), dict) else {}
    lines.append("\n## End-to-End Verification\n")
    lines.append(f"- overall_status: {verification.get('overall_status', '')}\n")
    lines.append(f"- capability_status: {verification.get('capability_status', '')}\n")
    lines.append(f"- total_checks: {verification.get('total_checks', 0)}\n")
    lines.append(f"- passed_checks: {verification.get('passed_checks', 0)}\n")
    lines.append(f"- failed_checks: {verification.get('failed_checks', 0)}\n")
    lines.append(f"- warning_checks: {verification.get('warning_checks', 0)}\n")
    for module in verification.get("modules", [])[:12] if isinstance(verification.get("modules", []), list) else []:
        lines.append(f"- {module.get('module')}: status={module.get('status')} checks={len(module.get('checks', [])) if isinstance(module.get('checks', []), list) else 0}\n")
    lines.append("\n## Evidence Integrity\n")
    lines.append(f"- status: {integrity.get('status', '')}\n")
    lines.append(f"- checked_nodes: {integrity.get('checked_nodes', 0)}\n")
    lines.append(f"- issues: {len(integrity.get('issues', [])) if isinstance(integrity.get('issues', []), list) else 0}\n")
    for row in integrity.get("issues", [])[:20]:
        lines.append(f"- [{row.get('severity')}] {row.get('scope')}::{row.get('id')} | {row.get('issue')}\n")
    lines.append("\n## Assurance Issues\n")
    issues = assurance.get("issues", [])
    if issues:
        for row in issues[:30]:
            lines.append(f"- [{row.get('severity')}] {row.get('issue')} | evidence={row.get('evidence')}\n")
    else:
        lines.append("- No hard issue detected.\n")
    lines.append("\n## Unexplored Niche Graph\n")
    for row in trajectory.get("unexplored_niche_graph", {}).get("nodes", [])[:25]:
        lines.append(f"- {row.get('id')}: {row.get('reason')} | needed={row.get('needed_evidence', '')}\n")
    lines.append("\n## Failed Hypothesis Graph\n")
    for row in trajectory.get("failed_hypothesis_graph", {}).get("nodes", [])[-25:]:
        lines.append(f"- {row.get('id')}: method={row.get('method')} status={row.get('status')} reason={row.get('reason')}\n")
    out.write_text("".join(lines), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Build native trajectory memory for research direction, evolutionary memory, evidence assurance, and paper production.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    parser.add_argument("--skip-helpers", action="store_true")
    args = parser.parse_args()
    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    helper_runs = []
    if not args.skip_helpers:
        venue_extra = ["--venue", args.venue] if args.venue else []
        helper_runs.extend([run_helper(args.project, "ideation:arena", []), run_helper(args.project, "planning:review_board", []), run_helper(args.project, "writing:audit_evidence", venue_extra), run_helper(args.project, "planning:method_frontier", []), run_helper(args.project, "update_evolution_memory.py", venue_extra)])
    paper_quality = load_json(paths.state / "paper_quality.json", {})
    ideas = load_json(paths.state / "idea_candidates.json", {})
    repos = load_json(paths.state / "repo_candidates.json", [])
    raw_datasets = load_json(paths.state / "dataset_registry.json", [])
    experiments = load_json(paths.state / "experiment_registry.json", [])
    if isinstance(experiments, dict) and isinstance(experiments.get("experiments"), list):
        experiments = experiments.get("experiments", [])
    datasets = merge_dataset_evidence(paths, raw_datasets, experiments)
    hypothesis_arena = load_json(paths.state / "hypothesis_arena.json", {})
    method_frontier = load_json(paths.state / "method_frontier.json", {})
    aris_board = load_json(paths.state / "aris_review_board.json", {})
    evolution_memory = load_json(paths.state / "evolution_memory.json", {})
    next_actions = load_json(paths.state / "next_actions.json", {})
    claim_ledger = load_json(paths.state / "claim_ledger.json", {"claims": []})
    active_repo = current_route_repo_context(paths, load_json(paths.state / "active_repo.json", {}))
    reference_reproduction = load_json(paths.state / "reference_reproduction_gate.json", {})
    experiment_iteration_audit = load_json(paths.state / "experiment_iteration_audit.json", {})
    evidence_audit = read_text(paths.reports / "paper_evidence_audit.md")
    paper_orchestra_state_run = run_paper_orchestra_state(args.project, args.venue)
    # Run capability audit and end-to-end verification before submission_readiness
    # so capability_status is available in the audit file when the check runs.
    end_to_end_verification_run = run_end_to_end_verification(args.project)
    capability_audit_run = run_capability_audit(args.project)
    submission_readiness_run = run_submission_readiness(args.project, args.venue)
    paper_normality_run = run_paper_normality_audit(args.project, args.venue)
    third_party_stack_run = run_third_party_stack_sync(args.project)
    paper_orchestra_state = load_json(paths.state / "paper_orchestra_state.json", {})
    paper_orchestra_bridge = load_json(paths.state / "paper_orchestra_bridge.json", {})
    paper_normality_audit = load_json(paths.state / "paper_normality_audit.json", {})
    submission_readiness = load_json(paths.state / "submission_readiness.json", {})
    third_party_stack = load_json(paths.state / "third_party_research_stack.json", {})
    evo_cycle = summarize_evoscientist_cycle(paths)
    skills = load_skill_contracts()
    landscape = build_research_landscape(cfg, paths, paper_quality, ideas, repos, datasets, experiments)
    novelty = build_novelty_map(ideas, hypothesis_arena, method_frontier)
    failed = build_failed_hypothesis_graph(experiments, aris_board, next_actions)
    niches = build_unexplored_niche_graph(novelty, failed, datasets, repos, experiments)
    assurance = build_assurance_layer(paths, experiments, datasets, active_repo, evidence_audit, aris_board, claim_ledger)
    gate_context = trajectory_gate_context(paths)
    controller = trajectory_controller(assurance, failed, niches, evolution_memory, method_frontier, evo_cycle, skills, gate_context)
    direction_memory = update_research_direction_memory(paths, landscape, novelty, failed, niches, assurance, controller)
    trajectory = {
        "project": args.project,
        "topic": cfg.get("topic", "") if isinstance(cfg, dict) else "",
        "updated_at": now_iso(),
        "trajectory_gate_context": gate_context,
        "research_landscape": landscape,
        "novelty_map": novelty,
        "failed_hypothesis_graph": failed,
        "unexplored_niche_graph": niches,
        "assurance_layer": assurance,
        "trajectory_controller": controller,
        "evoscientist_cycle": evo_cycle,
        "recoverable_cycle": evo_cycle,
        "skill_contracts": skills,
        "direction_memory": {
            "entries": len(direction_memory.get("history", [])) if isinstance(direction_memory, dict) else 0,
            "latest": direction_memory.get("latest", {}) if isinstance(direction_memory, dict) else {},
        },
        "helper_runs": helper_runs,
        "paper_orchestra_state": paper_orchestra_state,
        "paper_orchestra_bridge": paper_orchestra_bridge,
        "paper_normality_audit": paper_normality_audit,
        "submission_readiness": submission_readiness,
        "reference_reproduction_gate": reference_reproduction if isinstance(reference_reproduction, dict) else {},
        "experiment_iteration_audit": experiment_iteration_audit if isinstance(experiment_iteration_audit, dict) else {},
        "third_party_research_stack": third_party_stack,
        "paper_orchestra_state_run": paper_orchestra_state_run,
        "paper_normality_run": paper_normality_run,
        "submission_readiness_run": submission_readiness_run,
        "third_party_stack_run": third_party_stack_run,
        "source_methods": {
            "MethodReferenceAudit": ["framework/scripts/run_module.py writing --action sync_stack", "state/third_party_research_stack.json", "reports/third_party_research_stack.md"],
            "ResearchDirectionManagement": ["state/research_landscape.json", "state/novelty_map.json", "state/unexplored_niche_graph.json"],
            "EvidenceAssurance": ["state/research_assurance_layer.json", "state/research_evidence_manifest.json", "framework/scripts/run_module.py writing --action audit_evidence"],
            "TrajectoryOptimization": ["state/trajectory_optimization_plan.json", "state/evolutionary_memory_index.json", "framework/scripts/run_research_trajectory_supervisor.py"],
            "PaperProduction": ["state/paper_orchestra_state.json", "framework/scripts/run_module.py writing --action run", "framework/scripts/run_module.py writing --action submission_readiness", "framework/scripts/run_module.py writing --action audit_normality", "framework/scripts/run_module.py writing --action audit_evidence"],
            "BlockerActionRouting": ["state/blocker_action_plan.json", "reports/blocker_action_plan.md", "framework/scripts/run_module.py planning --action blocker_action"],
            "LocalClaudeSkills": [row.get("path", "") for row in skills],
        },
    }
    evidence_integrity = audit_evidence_integrity(paths, trajectory, claim_ledger)
    blocker_action_plan_run = run_blocker_action_plan(args.project, args.venue)
    blocker_action_plan = load_json(paths.state / "blocker_action_plan.json", {})
    trajectory["blocker_action_plan"] = blocker_action_plan
    trajectory["blocker_action_plan_run"] = blocker_action_plan_run
    optimization_plan = build_trajectory_optimization_plan(paths, controller, assurance, failed, niches, evidence_integrity, skills, blocker_action_plan)
    controller["executable_queue"] = optimization_plan.get("queue", [])
    trajectory["trajectory_controller"] = controller
    trajectory["research_evidence_integrity"] = evidence_integrity
    trajectory["trajectory_optimization_plan"] = optimization_plan
    execution_protocol = build_trajectory_execution_protocol(paths, controller, optimization_plan)
    trajectory["trajectory_execution_protocol"] = execution_protocol
    memory = update_research_memory(paths, trajectory, ideas, experiments)
    evolutionary_index = build_evolutionary_memory_index(paths, trajectory, memory, direction_memory, evolution_memory, evo_cycle, method_frontier, next_actions, aris_board, ideas, experiments, evidence_integrity, optimization_plan)
    trajectory["evolutionary_memory_index"] = evolutionary_index
    graph_history = update_research_graph_history(paths, landscape, novelty, failed, niches, evidence_integrity, assurance)
    evolutionary_memory_ledger = update_evolutionary_memory_ledger(paths, memory, direction_memory, evolutionary_index, graph_history, evidence_integrity)
    trajectory["research_graph_history"] = {"history_count": graph_history.get("history_count", 0), "latest": graph_history.get("latest", {}), "landscape_assessment": graph_history.get("landscape_assessment", {})}
    trajectory["research_landscape_assessment"] = graph_history.get("landscape_assessment", {})
    trajectory["evolutionary_memory_ledger"] = {"history_count": evolutionary_memory_ledger.get("history_count", 0), "latest": evolutionary_memory_ledger.get("latest", {})}
    trajectory["research_evidence_manifest"] = load_json(paths.state / "research_evidence_manifest.json", {})
    trajectory["research_memory_summary"] = {
        "ideation_entries": len(memory.get("ideation_memory", [])) if isinstance(memory, dict) else 0,
        "experimentation_entries": len(memory.get("experimentation_memory", [])) if isinstance(memory, dict) else 0,
        "assurance_entries": len(memory.get("assurance_memory", [])) if isinstance(memory, dict) else 0,
        "trajectory_entries": len(memory.get("trajectory_memory", [])) if isinstance(memory, dict) else 0,
        "evo_exception_entries": evo_cycle.get("exception_memory_entries", 0),
        "evo_experimentation_entries": evo_cycle.get("experimentation_memory_entries", 0),
        "direction_entries": len(direction_memory.get("history", [])) if isinstance(direction_memory, dict) else 0,
        "evolutionary_index_items": evolutionary_index.get("indexed_item_count", 0),
        "graph_history_entries": graph_history.get("history_count", 0),
        "evolutionary_memory_ledger_entries": evolutionary_memory_ledger.get("history_count", 0),
    }
    trajectory["summary"] = {
        "phase": controller.get("phase", ""),
        "assurance_status": assurance.get("status", ""),
        "landscape_nodes": sum(len(v) for v in landscape.get("nodes", {}).values()) if isinstance(landscape.get("nodes"), dict) else 0,
        "novelty_nodes": len(novelty.get("nodes", [])),
        "failed_hypotheses": len(failed.get("nodes", [])),
        "unexplored_niches": len(niches.get("nodes", [])),
        "next_objectives": controller.get("next_objectives", []),
        "assurance_issue_count": len(assurance.get("issues", [])),
        "evo_phase_count": evo_cycle.get("phase_count", 0),
        "recoverable_exception_count": evo_cycle.get("recoverable_exception_count", 0),
        "skill_contract_count": len(skills),
        "direction_memory_entries": trajectory["research_memory_summary"].get("direction_entries", 0),
        "evidence_integrity_status": evidence_integrity.get("status", ""),
        "evidence_integrity_issue_count": len(evidence_integrity.get("issues", [])),
        "evidence_integrity_score": evidence_integrity.get("score", 0),
        "optimization_queue_size": optimization_plan.get("queue_size", 0),
        "highest_optimization_priority": optimization_plan.get("highest_priority", ""),
        "evolutionary_index_items": evolutionary_index.get("indexed_item_count", 0),
        "graph_history_entries": graph_history.get("history_count", 0),
        "evolutionary_memory_ledger_entries": evolutionary_memory_ledger.get("history_count", 0),
        "landscape_assessment_status": trajectory["research_landscape_assessment"].get("status", ""),
        "evidence_manifest_ref_count": trajectory["research_evidence_manifest"].get("ref_count", 0) if isinstance(trajectory.get("research_evidence_manifest", {}), dict) else 0,
        "weak_or_unsupported_claim_count": len(trajectory["research_evidence_manifest"].get("weak_or_unsupported_claims", [])) if isinstance(trajectory.get("research_evidence_manifest", {}), dict) and isinstance(trajectory["research_evidence_manifest"].get("weak_or_unsupported_claims", []), list) else 0,
        "paper_orchestra_state_status": paper_orchestra_state.get("status", "") if isinstance(paper_orchestra_state, dict) else "",
        "paper_orchestra_bridge_status": paper_orchestra_bridge.get("status", "") if isinstance(paper_orchestra_bridge, dict) else "",
        "paper_normality_status": paper_normality_audit.get("status", "") if isinstance(paper_normality_audit, dict) else "",
        "normal_preview_ready": bool(paper_normality_audit.get("normal_preview_ready")) if isinstance(paper_normality_audit, dict) else False,
        "paper_orchestra_blocked_sections": len([row for row in paper_orchestra_state.get("sections", []) if isinstance(row, dict) and row.get("status") == "blocked"]) if isinstance(paper_orchestra_state, dict) and isinstance(paper_orchestra_state.get("sections", []), list) else 0,
        "submission_readiness_status": submission_readiness.get("status", "") if isinstance(submission_readiness, dict) else "",
        "reference_reproduction_status": reference_reproduction.get("status", "") if isinstance(reference_reproduction, dict) else "",
        "reference_reproduction_decision": reference_reproduction.get("decision", "") if isinstance(reference_reproduction, dict) else "",
        "experiment_iteration_audit_status": experiment_iteration_audit.get("status", "") if isinstance(experiment_iteration_audit, dict) else "",
        "submission_ready": bool(submission_readiness.get("submission_ready")) if isinstance(submission_readiness, dict) else False,
        "third_party_stack_status": third_party_stack.get("status", "") if isinstance(third_party_stack, dict) else "",
        "third_party_source_count": third_party_stack.get("summary", {}).get("source_count", 0) if isinstance(third_party_stack, dict) and isinstance(third_party_stack.get("summary", {}), dict) else 0,
        "third_party_synced_skill_count": third_party_stack.get("summary", {}).get("synced_skill_count", 0) if isinstance(third_party_stack, dict) and isinstance(third_party_stack.get("summary", {}), dict) else 0,
        "third_party_selected_module_count": third_party_stack.get("summary", {}).get("selected_module_count", 0) if isinstance(third_party_stack, dict) and isinstance(third_party_stack.get("summary", {}), dict) else 0,
        "memory": trajectory["research_memory_summary"],
    }
    checkpoints = update_trajectory_checkpoints(paths, trajectory)
    trajectory["trajectory_checkpoints"] = checkpoints
    trajectory["summary"]["trajectory_checkpoint_count"] = checkpoints.get("checkpoint_count", 0)
    trajectory["summary"]["trajectory_delta_status"] = checkpoints.get("latest", {}).get("delta_status", "")
    outputs = {
        "research_landscape": paths.state / "research_landscape.json",
        "novelty_map": paths.state / "novelty_map.json",
        "failed_hypothesis_graph": paths.state / "failed_hypothesis_graph.json",
        "unexplored_niche_graph": paths.state / "unexplored_niche_graph.json",
        "research_assurance_layer": paths.state / "research_assurance_layer.json",
        "research_trajectory_system": paths.state / "research_trajectory_system.json",
        "research_direction_memory": paths.state / "research_direction_memory.json",
        "research_evidence_integrity": paths.state / "research_evidence_integrity.json",
        "research_evidence_manifest": paths.state / "research_evidence_manifest.json",
        "research_graph_history": paths.state / "research_graph_history.json",
        "research_landscape_assessment": paths.state / "research_landscape_assessment.json",
        "evolutionary_memory_ledger": paths.state / "evolutionary_memory_ledger.json",
        "trajectory_optimization_plan": paths.state / "trajectory_optimization_plan.json",
        "trajectory_checkpoints": paths.state / "trajectory_checkpoints.json",
        "evolutionary_memory_index": paths.state / "evolutionary_memory_index.json",
        "evoscientist_cycle_summary": paths.state / "evoscientist_cycle_summary.json",
        "recoverable_cycle_summary": paths.state / "recoverable_cycle_summary.json",
        "evidence_review_board": paths.state / "evidence_review_board.json",
        "research_skill_contracts": paths.state / "research_skill_contracts.json",
        "trajectory_execution_protocol": paths.state / "trajectory_execution_protocol.json",
        "research_trajectory_capability_audit": paths.state / "research_trajectory_capability_audit.json",
        "research_trajectory_end_to_end_verification": paths.state / "research_trajectory_end_to_end_verification.json",
        "paper_orchestra_state": paths.state / "paper_orchestra_state.json",
        "paper_orchestra_bridge": paths.state / "paper_orchestra_bridge.json",
        "paper_normality_audit": paths.state / "paper_normality_audit.json",
        "submission_readiness": paths.state / "submission_readiness.json",
        "blocker_action_plan": paths.state / "blocker_action_plan.json",
        "third_party_research_stack": paths.state / "third_party_research_stack.json",
    }
    save_json(outputs["research_landscape"], landscape)
    save_json(outputs["novelty_map"], novelty)
    save_json(outputs["failed_hypothesis_graph"], failed)
    save_json(outputs["unexplored_niche_graph"], niches)
    save_json(outputs["research_assurance_layer"], assurance)
    save_json(outputs["research_direction_memory"], direction_memory)
    save_json(outputs["research_evidence_integrity"], evidence_integrity)
    save_json(outputs["research_evidence_manifest"], trajectory["research_evidence_manifest"])
    save_json(outputs["research_graph_history"], graph_history)
    save_json(outputs["research_landscape_assessment"], trajectory["research_landscape_assessment"])
    save_json(outputs["evolutionary_memory_ledger"], evolutionary_memory_ledger)
    save_json(outputs["trajectory_optimization_plan"], optimization_plan)
    save_json(outputs["trajectory_checkpoints"], checkpoints)
    save_json(outputs["evolutionary_memory_index"], evolutionary_index)
    save_json(outputs["evoscientist_cycle_summary"], evo_cycle)
    save_json(outputs["recoverable_cycle_summary"], evo_cycle)
    save_json(outputs["evidence_review_board"], aris_board)
    save_json(outputs["research_skill_contracts"], skills)
    save_json(outputs["trajectory_execution_protocol"], execution_protocol)
    save_json(outputs["research_trajectory_system"], trajectory)
    save_json(outputs["paper_orchestra_state"], paper_orchestra_state)
    save_json(outputs["paper_orchestra_bridge"], paper_orchestra_bridge)
    save_json(outputs["paper_normality_audit"], paper_normality_audit)
    save_json(outputs["submission_readiness"], submission_readiness)
    save_json(outputs["blocker_action_plan"], blocker_action_plan)
    save_json(outputs["third_party_research_stack"], third_party_stack)
    end_to_end_verification = load_json(outputs["research_trajectory_end_to_end_verification"], {})
    capability_audit = load_json(outputs["research_trajectory_capability_audit"], {})
    trajectory["research_trajectory_capability_audit"] = capability_audit
    trajectory["research_trajectory_end_to_end_verification"] = end_to_end_verification
    trajectory["summary"]["capability_audit_status"] = capability_audit.get("overall_status", "") if isinstance(capability_audit, dict) else ""
    trajectory["summary"]["capability_status"] = capability_audit.get("capability_status", "") if isinstance(capability_audit, dict) else ""
    trajectory["summary"]["end_to_end_verification_status"] = end_to_end_verification.get("overall_status", "") if isinstance(end_to_end_verification, dict) else ""
    trajectory["summary"]["end_to_end_verification_capability_status"] = end_to_end_verification.get("capability_status", "") if isinstance(end_to_end_verification, dict) else ""
    trajectory["summary"]["end_to_end_verification_failed_checks"] = end_to_end_verification.get("failed_checks", 0) if isinstance(end_to_end_verification, dict) else 0
    trajectory["summary"]["end_to_end_verification_warning_checks"] = end_to_end_verification.get("warning_checks", 0) if isinstance(end_to_end_verification, dict) else 0
    trajectory["capability_audit_run"] = capability_audit_run
    trajectory["end_to_end_verification_run"] = end_to_end_verification_run
    save_json(outputs["research_trajectory_system"], trajectory)
    report = write_markdown(paths, trajectory)
    print(json.dumps({
        "project": args.project,
        "phase": controller.get("phase", ""),
        "assurance_status": assurance.get("status", ""),
        "outputs": {key: str(path) for key, path in outputs.items()},
        "memory": str(paths.state / "research_memory.json"),
        "report": str(report),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
