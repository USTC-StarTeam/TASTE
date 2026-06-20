#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path as _WritingDevPath
import sys as _writing_sys
_WRITING_SCRIPT_ROOT = next((p for p in _WritingDevPath(__file__).resolve().parents if p.name == "scripts"), _WritingDevPath(__file__).resolve().parent)
for _writing_path in [_WRITING_SCRIPT_ROOT, *[p for p in _WRITING_SCRIPT_ROOT.iterdir() if p.is_dir()]]:
    _writing_text = str(_writing_path)
    if _writing_text not in _writing_sys.path:
        _writing_sys.path.insert(0, _writing_text)


import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from paper_common import (
    count_placeholder_lines,
    ensure_paper_dirs,
    extract_summary_lines,
    load_json,
    read_text,
    summarize_experiments,
    update_pipeline_state,
    write_json,
    write_text,
)
from project_paths import build_paths, load_project_config
from experiment_contracts import row_promotion_blockers


SECTION_CONTRACTS = [
    {
        "id": "abstract",
        "title": "Abstract",
        "required": ["supported_claims", "scope_boundary", "main_result_summary"],
        "claim_policy": "Only summarize claims that pass the evidence and claim-ledger gates.",
    },
    {
        "id": "introduction",
        "title": "Introduction",
        "required": ["problem_motivation", "nearest_gap", "contribution_boundary"],
        "claim_policy": "Motivation may be literature-backed, but result claims require local artifacts.",
    },
    {
        "id": "related_work",
        "title": "Related Work",
        "required": ["citation_candidates", "nearest_neighbor_delta", "novelty_map"],
        "claim_policy": "Citations must trace to ingested metadata or verified external records.",
    },
    {
        "id": "method",
        "title": "Method",
        "required": ["implemented_code_path", "repo_adaptation", "method_artifacts"],
        "claim_policy": "Describe only implemented or explicitly planned code paths; mark missing implementation as a blocker.",
    },
    {
        "id": "experimental_setup",
        "title": "Experimental Setup",
        "required": ["dataset_registry", "split_metric_protocol", "active_repo"],
        "claim_policy": "Dataset and metric details must be grounded in registry, repo requirements, or audit files.",
    },
    {
        "id": "experiments",
        "title": "Experiments",
        "required": ["audit_ready_runs", "real_data_runs", "table_artifacts", "claim_verdicts"],
        "claim_policy": "Tables require real rows from the experiment registry and audit-ready artifact paths.",
    },
    {
        "id": "limitations",
        "title": "Limitations",
        "required": ["active_blockers", "weak_claims", "unexplored_niches"],
        "claim_policy": "Blocked claims move here or to future work until evidence improves.",
    },
    {
        "id": "reproducibility",
        "title": "Reproducibility",
        "required": ["environment_health", "manifest", "repo_data_requirements"],
        "claim_policy": "A paper cannot be submission-ready without reproducible paths and explicit missing-resource notes.",
    },
]

SECTION_ALIASES = {
    "abstract": ["abstract"],
    "introduction": ["introduction"],
    "related_work": ["related work", "related-work"],
    "method": ["method", "method snapshot"],
    "experimental_setup": ["experimental setup", "setup"],
    "experiments": ["experiments", "results"],
    "limitations": ["limitations"],
    "reproducibility": ["reproducibility", "environment"],
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def latest_markdown(*paths: Path) -> Path | None:
    existing = [path for path in paths if path.exists() and read_text(path).strip()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def section_present(text: str, section_id: str) -> bool:
    lowered = text.lower()
    for alias in SECTION_ALIASES.get(section_id, [section_id]):
        if re.search(rf"^#{{1,3}}\s+.*{re.escape(alias)}", lowered, flags=re.MULTILINE):
            return True
        if alias in lowered:
            return True
    return False


def path_exists(path_text: str) -> bool:
    try:
        return Path(path_text).exists()
    except Exception:
        return False


def evidence_item(label: str, path: Path | str, strength: str = "local_file", note: str = "") -> dict[str, Any]:
    text = str(path)
    return {
        "label": label,
        "path": text,
        "exists": path_exists(text),
        "strength": strength,
        "note": note,
    }


def citation_candidates(paths) -> list[dict[str, Any]]:
    ranking = load_json(paths.state / "ingest_ranking.json", {})
    rows: list[dict[str, Any]] = []
    if isinstance(ranking, dict):
        rows.extend(row for row in ranking.get("ingested", []) or [] if isinstance(row, dict))
        rows.extend(row for row in ranking.get("already_ingested", []) or [] if isinstance(row, dict))
    out = []
    for row in rows[:40]:
        out.append(
            {
                "title": row.get("title", ""),
                "bucket": row.get("selection_bucket", ""),
                "score": row.get("discovery_priority_score", row.get("score", "")),
                "source": row.get("url") or row.get("source") or row.get("paper_id") or "",
                "verified": bool(row.get("url") or row.get("source") or row.get("paper_id")),
            }
        )
    return out


def artifact_inventory(paths, experiments: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    rows = []
    for exp in experiments:
        if not isinstance(exp, dict):
            continue
        for key in ["artifact_path", "audit_path", "bad_case_path", "figure_path", "table_path"]:
            value = str(exp.get(key) or "").strip()
            if value:
                rows.append({"experiment": exp.get("experiment_id") or exp.get("name") or "", "kind": key, "path": value, "exists": path_exists(value)})
    file_rows = []
    for root in [paths.artifacts, paths.experiments, paths.reports]:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".csv", ".tsv", ".json", ".md"}:
                file_rows.append({"kind": path.suffix.lower().lstrip("."), "path": str(path), "exists": True})
            if len(file_rows) >= 120:
                break
    figures = [row for row in rows + file_rows if str(row.get("kind", "")).lower() in {"figure_path", "png", "jpg", "jpeg", "pdf"}]
    tables = [row for row in rows + file_rows if str(row.get("kind", "")).lower() in {"table_path", "csv", "tsv", "json"}]
    return {"linked_artifacts": rows, "figure_candidates": figures[:40], "table_candidates": tables[:40]}


def real_ready_datasets(paths, dataset_registry: list[dict[str, Any]]) -> set[str]:
    ready = {
        str(row.get("name") or row.get("dataset"))
        for row in dataset_registry
        if (
            isinstance(row, dict)
            and row.get("available")
            and row.get("claim_ready")
            and row.get("loader_probe_success")
            and not str(row.get("name") or row.get("dataset") or "").startswith("synthetic")
        )
    }
    req = load_json(paths.state / "repo_data_requirements.json", {})
    if isinstance(req, dict):
        ready.update(str(name) for name in req.get("ready_datasets", []) or [] if str(name).strip())
    return {name for name in ready if name}


def run_counts(experiments: list[dict[str, Any]], ready_real: set[str]) -> dict[str, int]:
    completed = [row for row in experiments if isinstance(row, dict) and str(row.get("status", "")).lower() in {"completed", "success"}]
    audit_ready = [row for row in completed if row.get("audit_ready") and not row_promotion_blockers(row)]
    real_runs = [row for row in audit_ready if str(row.get("dataset")) in ready_real and not str(row.get("dataset", "")).startswith("synthetic")]
    return {
        "completed": len(completed),
        "audit_ready": len(audit_ready),
        "real_audit_ready": len(real_runs),
        "claim_verdicts": sum(1 for row in audit_ready if row.get("claim_verdict")),
        "bad_case_runs": sum(1 for row in audit_ready if row.get("bad_case_path") or row.get("bad_case_slices")),
        "counterexample_runs": sum(1 for row in audit_ready if row.get("counterexample_outcome")),
    }


def claim_summary(claim_ledger: dict[str, Any]) -> dict[str, Any]:
    claims = claim_ledger.get("claims", []) if isinstance(claim_ledger, dict) and isinstance(claim_ledger.get("claims", []), list) else []
    weak = [row for row in claims if str(row.get("status", "")).lower() in {"weak", "unsupported"}]
    supported = [row for row in claims if str(row.get("status", "")).lower() in {"supported", "partially_supported", "mixed"} and str(row.get("text") or "").strip().lower() not in {"", "missing"}]
    return {
        "claim_count": len(claims),
        "supported_or_contested_count": len(supported),
        "weak_or_unsupported_count": len(weak),
        "weak_claims": [{"claim_type": row.get("claim_type", ""), "status": row.get("status", ""), "support_count": row.get("support_count", 0)} for row in weak],
        "claims": claims,
    }


def section_status(section: dict[str, Any], context: dict[str, Any]) -> tuple[str, list[str], list[str], list[dict[str, Any]]]:
    blockers: list[str] = []
    warnings: list[str] = []
    evidence: list[dict[str, Any]] = []
    section_id = section["id"]
    paths = context["paths"]
    counts = context["run_counts"]
    claims = context["claim_summary"]

    present = section_present(context["draft_text"], section_id)
    if not present:
        blockers.append("section heading/content is missing from the current draft")

    common_evidence = {
        "abstract": [evidence_item("claim ledger", paths.planning / "claim_ledger.md"), evidence_item("paper evidence audit", paths.reports / "paper_evidence_audit.md")],
        "introduction": [evidence_item("init brief", paths.planning / "init_brief.md"), evidence_item("paper quality", paths.planning / "paper_quality.md")],
        "related_work": [evidence_item("ingest ranking", paths.state / "ingest_ranking.json"), evidence_item("novelty map", paths.state / "novelty_map.json")],
        "method": [evidence_item("active repo", paths.state / "active_repo.json"), evidence_item("selected repos", paths.repos_selected)],
        "experimental_setup": [evidence_item("dataset registry", paths.state / "dataset_registry.json"), evidence_item("repo data requirements", paths.state / "repo_data_requirements.json")],
        "experiments": [evidence_item("experiment registry", paths.state / "experiment_registry.json"), evidence_item("experiment log", paths.experiments / "experiment_log.md")],
        "limitations": [evidence_item("research assurance layer", paths.state / "research_assurance_layer.json"), evidence_item("unexplored niche graph", paths.state / "unexplored_niche_graph.json")],
        "reproducibility": [evidence_item("healthcheck", paths.reports / "healthcheck.md"), evidence_item("research manifest", paths.state / "research_manifest.json")],
    }
    evidence.extend(common_evidence.get(section_id, []))
    if any(item["strength"] == "local_file" and not item["exists"] for item in evidence):
        warnings.append("one or more section evidence files are not present yet")

    if section_id in {"abstract", "experiments"} and claims["weak_or_unsupported_count"]:
        blockers.append("claim ledger contains weak or unsupported claims")
    if section_id == "abstract" and counts["real_audit_ready"] == 0:
        blockers.append("no audit-ready real-data result can support an abstract headline claim")
    if section_id == "related_work" and not context["citations"]:
        blockers.append("no citation candidates are available for related work")
    if section_id == "method" and not context["active_repo"].get("repo_path"):
        warnings.append("active repo path is missing or not recorded")
    if section_id == "experimental_setup" and not context["ready_real_datasets"]:
        blockers.append("no loader-ready real dataset is recorded")
    if section_id == "experiments":
        if counts["audit_ready"] == 0:
            blockers.append("no audit-ready experiment is recorded")
        if counts["real_audit_ready"] == 0:
            blockers.append("no audit-ready real-data experiment is recorded")
        if counts["claim_verdicts"] == 0:
            blockers.append("no experiment carries a claim verdict")
    if section_id == "reproducibility" and not (paths.reports / "healthcheck.md").exists():
        blockers.append("healthcheck report is missing")

    if blockers:
        status = "blocked"
    elif warnings:
        status = "needs_revision"
    else:
        status = "ready"
    return status, blockers, warnings, evidence


def build_state(project: str, venue: str) -> dict[str, Any]:
    cfg = load_project_config(project)
    paths = build_paths(project)
    paper = ensure_paper_dirs(project)
    draft_source = latest_markdown(paper["revised_md"], paper["draft_md"])
    draft_text = read_text(draft_source) if draft_source else ""
    experiments = load_json(paths.state / "experiment_registry.json", [])
    if not isinstance(experiments, list):
        experiments = []
    dataset_registry = load_json(paths.state / "dataset_registry.json", [])
    if not isinstance(dataset_registry, list):
        dataset_registry = []
    ready_real = real_ready_datasets(paths, dataset_registry)
    claims = claim_summary(load_json(paths.state / "claim_ledger.json", {"claims": []}))
    citations = citation_candidates(paths)
    artifacts = artifact_inventory(paths, experiments)
    assurance = load_json(paths.state / "research_assurance_layer.json", {})
    evidence_manifest = load_json(paths.state / "research_evidence_manifest.json", {})
    trajectory = load_json(paths.state / "research_trajectory_system.json", {})
    active_repo = load_json(paths.state / "active_repo.json", {})
    counts = run_counts(experiments, ready_real)
    experiment_summary = summarize_experiments(experiments)
    context = {
        "paths": paths,
        "draft_text": draft_text,
        "run_counts": counts,
        "claim_summary": claims,
        "citations": citations,
        "ready_real_datasets": ready_real,
        "assurance_issues": assurance.get("issues", []) if isinstance(assurance.get("issues", []), list) else [],
        "active_repo": active_repo if isinstance(active_repo, dict) else {},
    }

    section_rows = []
    for section in SECTION_CONTRACTS:
        status, blockers, warnings, evidence = section_status(section, context)
        section_rows.append(
            {
                **section,
                "present_in_current_draft": section_present(draft_text, section["id"]),
                "status": status,
                "blockers": blockers,
                "warnings": warnings,
                "evidence": evidence,
                "revision_actions": [
                    f"Resolve blocker: {item}" for item in blockers[:6]
                ]
                + [f"Tighten section evidence: {item}" for item in warnings[:4]],
            }
        )

    blocked_sections = [row for row in section_rows if row["status"] == "blocked"]
    revision_sections = [row for row in section_rows if row["status"] == "needs_revision"]
    placeholders = count_placeholder_lines(draft_text)
    global_blockers: list[str] = []
    if claims["weak_or_unsupported_count"]:
        global_blockers.append("claim ledger has weak or unsupported claims")
    if counts["real_audit_ready"] == 0:
        global_blockers.append("no audit-ready real-data experiment supports final claims")
    if placeholders:
        global_blockers.append(f"draft still contains {placeholders} placeholder/scaffold lines")
    if not citations:
        global_blockers.append("related-work citation candidates are missing")

    status = "hold" if blocked_sections or global_blockers else "revision_required" if revision_sections else "pass"
    promotion_gate = "hold-markdown-only" if status != "pass" else "allow-template"
    payload = {
        "project": project,
        "venue": venue,
        "topic": cfg.get("topic", "") if isinstance(cfg, dict) else "",
        "updated_at": now_iso(),
        "status": status,
        "promotion_gate_recommendation": promotion_gate,
        "draft_source": str(draft_source) if draft_source else "",
        "writing_design": {
            "borrowed_methods": [
                "structured input synthesis before generation",
                "section-wise expert contracts instead of one-shot full-paper generation",
                "claim/evidence/citation/table/figure ledgers",
                "internal audit records kept out of manuscript prose",
                "review-response-re-review loop before template promotion",
                "submission readiness gate separated from prose polish",
            ],
            "writing_method_reference": "writing module method provenance retained internally; do not expose source-method names in UI or manuscript prose.",
            "local_contract": "framework/resources/claude/skills/writing/SKILL.md",
        },
        "structured_inputs": {
            "research_trajectory": evidence_item("research trajectory system", paths.state / "research_trajectory_system.json"),
            "research_assurance_layer": evidence_item("research assurance layer", paths.state / "research_assurance_layer.json"),
            "evidence_manifest": evidence_item("research evidence manifest", paths.state / "research_evidence_manifest.json"),
            "claim_ledger": evidence_item("claim ledger", paths.state / "claim_ledger.json"),
            "experiment_registry": evidence_item("experiment registry", paths.state / "experiment_registry.json"),
            "paper_evidence_audit": evidence_item("paper evidence audit", paths.reports / "paper_evidence_audit.md"),
        },
        "sections": section_rows,
        "claims": claims,
        "citations": {
            "candidate_count": len(citations),
            "verified_candidate_count": sum(1 for row in citations if row.get("verified")),
            "candidates": citations[:20],
        },
        "artifacts": artifacts,
        "experiments": {
            **counts,
            "completed_count": experiment_summary.get("completed_count", 0),
            "failed_count": experiment_summary.get("failed_count", 0),
            "best": experiment_summary.get("best"),
            "ready_real_datasets": sorted(ready_real),
        },
        "trajectory_links": {
            "trajectory_phase": (trajectory.get("summary", {}) if isinstance(trajectory.get("summary", {}), dict) else {}).get("phase", ""),
            "assurance_status": assurance.get("status", "") if isinstance(assurance, dict) else "",
            "evidence_manifest_status": evidence_manifest.get("status", "") if isinstance(evidence_manifest, dict) else "",
            "weak_or_unsupported_claim_count": len(evidence_manifest.get("weak_or_unsupported_claims", [])) if isinstance(evidence_manifest.get("weak_or_unsupported_claims", []), list) else 0,
        },
        "global_blockers": list(dict.fromkeys(global_blockers)),
        "revision_queue": [
            {
                "section": row["id"],
                "priority": "P0" if row["status"] == "blocked" else "P1",
                "actions": row["revision_actions"],
            }
            for row in section_rows
            if row["revision_actions"]
        ],
        "principle": "The paper-writing module may improve structure and prose, but manuscript claims must stay limited to current-route supported evidence; internal audit diagnostics stay outside paper prose.",
    }
    write_json(paths.state / "paper_orchestra_state.json", payload)
    write_report(paths, payload)
    update_pipeline_state(
        project,
        {
            "paper_orchestra_state": str(paths.state / "paper_orchestra_state.json"),
            "paper_orchestra_state_report": str(paths.reports / "paper_orchestra_state.md"),
            "paper_orchestra_state_status": status,
            "paper_orchestra_promotion_gate_recommendation": promotion_gate,
        },
        venue=venue,
        promote_to_top=True,
    )
    return payload


def write_report(paths, payload: dict[str, Any]) -> Path:
    out = paths.reports / "paper_orchestra_state.md"
    lines = ["# Writing State\n\n"]
    for key in ["updated_at", "status", "promotion_gate_recommendation", "draft_source"]:
        lines.append(f"- {key}: {payload.get(key, '')}\n")
    lines.append("\n## Writing Methods\n\n")
    for item in payload.get("writing_design", {}).get("borrowed_methods", []):
        lines.append(f"- {item}\n")
    lines.append("\n## Structured Inputs\n\n")
    for key, row in payload.get("structured_inputs", {}).items():
        lines.append(f"- {key}: exists={row.get('exists')} path={row.get('path')}\n")
    lines.append("\n## Section Ledger\n\n")
    for row in payload.get("sections", []):
        lines.append(f"### {row.get('title')}\n\n")
        lines.append(f"- status: {row.get('status')}\n")
        lines.append(f"- present_in_current_draft: {row.get('present_in_current_draft')}\n")
        lines.append(f"- claim_policy: {row.get('claim_policy')}\n")
        if row.get("blockers"):
            lines.append("- blockers: " + "; ".join(row.get("blockers", [])) + "\n")
        if row.get("warnings"):
            lines.append("- warnings: " + "; ".join(row.get("warnings", [])) + "\n")
        if row.get("revision_actions"):
            lines.append("- revision_actions: " + "; ".join(row.get("revision_actions", [])[:6]) + "\n")
        lines.append("\n")
    lines.append("## Claims\n\n")
    claims = payload.get("claims", {})
    lines.append(f"- claim_count: {claims.get('claim_count', 0)}\n")
    lines.append(f"- weak_or_unsupported_count: {claims.get('weak_or_unsupported_count', 0)}\n")
    for row in claims.get("weak_claims", []):
        lines.append(f"- weak_claim: {row.get('claim_type')} status={row.get('status')} support={row.get('support_count')}\n")
    lines.append("\n## Experiments\n\n")
    for key, value in payload.get("experiments", {}).items():
        if key != "best":
            lines.append(f"- {key}: {value}\n")
    lines.append("\n## Global Blockers\n\n")
    if payload.get("global_blockers"):
        for item in payload.get("global_blockers", []):
            lines.append(f"- {item}\n")
    else:
        lines.append("- No global blocker detected.\n")
    out.write_text("".join(lines), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an writing section/evidence/citation/artifact ledger for TASTE papers.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    args = parser.parse_args()
    payload = build_state(args.project, args.venue)
    print(json.dumps({"project": args.project, "status": payload.get("status"), "report": str(build_paths(args.project).reports / "paper_orchestra_state.md")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
