#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

from literature_policy import core_topic_fit_from_text
from project_paths import build_paths, load_project_config


ENVIRONMENT_SELECTION_STAGE = "environment_claude_code"


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


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def text_of(*values: Any) -> str:
    return "\n".join(str(v or "") for v in values).lower()


def title_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def has_positive(text: str, tokens: list[str]) -> bool:
    negations = [
        "缺少", "没有", "无", "不涉及", "未涉及", "完全没有",
        "does not", "do not", "without", "no ", "not ", "lack", "lacks", "missing", "absence of",
    ]
    for token in tokens:
        token = token.lower()
        start = 0
        while True:
            pos = text.find(token, start)
            if pos < 0:
                break
            before = text[max(0, pos - 64):pos]
            if not any(neg in before for neg in negations):
                return True
            start = pos + len(token)
    return False


def number(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def code_links(row: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ["code_url", "github_url", "repo_url"]:
        value = str(row.get(key) or "").strip()
        if value:
            out.append(value)
    for key in ["code_links", "links"]:
        for value in as_list(row.get(key)):
            text = str(value or "").strip()
            if text and any(token in text.lower() for token in ["github.com", "gitlab", "4open.science", "anonymous.4open"]):
                out.append(text)
    seen: list[str] = []
    for item in out:
        if item not in seen:
            seen.append(item)
    return seen[:6]


def is_non_positive_candidate(row: dict[str, Any]) -> bool:
    if row.get("not_positive_support") or row.get("weak_candidate_for_critique") or row.get("foundation_demoted_from_strong"):
        return True
    if row.get("retrieval_pool_only") or (row.get("audit_only_candidate") is True and not row.get("claim_ready_anchor")):
        return True
    tier = str(row.get("evidence_tier") or row.get("source_evidence_tier") or "").lower()
    role = str(row.get("evidence_role") or "").lower()
    pool = str(row.get("pool") or row.get("taste_pool") or "").lower()
    if any(token in tier for token in ["nethreshold", "retrieval_only", "weak", "boundary", "critique"]):
        return True
    if role in {"weak_or_boundary", "retrieval_pool_only"}:
        return True
    if pool in {"screened_ranking", "read_candidates", "arxiv_prefiltered", "evaluated_candidates", "title_candidates", "critique_candidates"}:
        return True
    return False


def is_claim_ready_anchor(row: dict[str, Any]) -> bool:
    if is_non_positive_candidate(row):
        return False
    pool = str(row.get("pool") or row.get("taste_pool") or "").lower()
    return bool(row.get("claim_ready_anchor") or row.get("positive_claim_evidence") or pool in {"strong_recommendations", "articles"})


def classify(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    text = text_of(
        row.get("title"), row.get("abstract"), row.get("abstract_en"), row.get("abstract_zh"),
        row.get("reason"), row.get("recommendation_note"), row.get("fit_explanation"), row.get("fit_explanation_zh"),
    )
    fit = core_topic_fit_from_text(text, cfg)
    hits = fit.get("topic_group_hits", {}) if isinstance(fit.get("topic_group_hits"), dict) else {}
    matched = [str(name) for name, ok in hits.items() if ok]
    required = [str(name) for name in fit.get("required_topic_groups", []) if str(name).strip()] if isinstance(fit.get("required_topic_groups"), list) else []
    return {
        "required_topic_groups": required,
        "topic_group_hits": hits,
        "matched_topic_groups": matched,
        "matched_topic_group_count": len(matched),
        "missing_topic_groups": fit.get("missing_topic_groups", []),
        "hard_topic_mismatch": bool(fit.get("hard_topic_mismatch")),
    }


def candidate_score(row: dict[str, Any], rank: int, cfg: dict[str, Any]) -> float:
    flags = classify(row, cfg)
    score = number(row.get("recommendation_score") or row.get("score") or row.get("final_score"))
    out = min(score, 10.0)
    matched_count = int(flags.get("matched_topic_group_count") or 0)
    required_count = len(flags.get("required_topic_groups") or [])
    out += min(6.0, 2.5 * matched_count)
    if required_count and matched_count >= required_count:
        out += 3.0
    elif matched_count:
        out += 1.0
    if flags.get("hard_topic_mismatch"):
        out -= 4.0
    out += 1.0 if code_links(row) else 0.0
    out -= rank * 0.03
    return round(out, 4)


def compact_row(row: dict[str, Any], rank: int, cfg: dict[str, Any]) -> dict[str, Any]:
    flags = classify(row, cfg)
    return {
        "rank": rank,
        "title": row.get("title") or row.get("name") or "",
        "authors": row.get("authors") or "",
        "venue": row.get("venue") or row.get("source") or "",
        "year": row.get("year") or "",
        "url": row.get("url") or row.get("pdf_url") or row.get("openreview_url") or "",
        "pdf_url": row.get("pdf_url") or "",
        "score": number(row.get("recommendation_score") or row.get("score") or row.get("final_score")),
        "fresh_base_score": candidate_score(row, rank, cfg),
        "signals": flags,
        "code_links": code_links(row),
        "claim_ready_anchor": is_claim_ready_anchor(row),
        "not_positive_support": is_non_positive_candidate(row),
        "pool": row.get("pool") or row.get("taste_pool") or "",
        "fit_explanation_zh": str(row.get("fit_explanation_zh") or row.get("fit_explanation") or row.get("reason") or "")[:1200],
        "abstract_zh": str(row.get("abstract_zh") or "")[:1200],
        "abstract_en": str(row.get("abstract_en") or row.get("abstract") or "")[:1200],
    }


def build_candidate_pool(find: dict[str, Any], packet: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    by_title: dict[str, dict[str, Any]] = {}
    title_order: list[str] = []
    labelled_pools = [
        ("strong_recommendations", find.get("strong_recommendations")),
        ("articles", find.get("articles")),
        ("strong_recommendations", packet.get("strong_papers")),
        ("strong_recommendations", packet.get("base_work_candidates")),
    ]
    for pool_name, pool in labelled_pools:
        for raw in as_list(pool):
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            row.setdefault("pool", pool_name)
            row.setdefault("taste_pool", pool_name)
            if pool_name in {"strong_recommendations", "articles"}:
                row.setdefault("claim_ready_anchor", True)
                row.setdefault("positive_claim_evidence", True)
            if not is_claim_ready_anchor(row):
                continue
            title = title_key(row.get("title") or row.get("name"))
            if not title:
                continue
            merged = dict(by_title.get(title, {}))
            merged.update({k: v for k, v in row.items() if v not in (None, "", [], {})})
            by_title[title] = merged
            if title not in title_order:
                title_order.append(title)
    candidates = [compact_row(by_title[title], i + 1, cfg) for i, title in enumerate(title_order)]
    candidates = [
        row for row in candidates
        if row.get("claim_ready_anchor")
        and not row.get("not_positive_support")
        and row["signals"].get("matched_topic_group_count", 0) > 0
        and not row["signals"].get("hard_topic_mismatch")
    ]
    return sorted(candidates, key=lambda row: (-number(row.get("fresh_base_score")), int(row.get("rank") or 999)))


def claim_ready_datasets(row: dict[str, Any]) -> list[str]:
    probe = row.get("probe_summary", {}) if isinstance(row.get("probe_summary"), dict) else {}
    values = row.get("claim_ready_datasets") or probe.get("claim_ready_datasets") or []
    if isinstance(values, str):
        values = [values]
    if row.get("claim_ready_dataset"):
        values = [row.get("claim_ready_dataset"), *values]
    out: list[str] = []
    iterable = values if isinstance(values, list) else []
    for value in iterable:
        item = str(value or "").strip()
        if item and item not in out:
            out.append(item)
    return out



def current_implementation_plan(paths, find_run_id: str) -> tuple[dict[str, Any], str]:
    impl_plan = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    if not isinstance(impl_plan, dict):
        return {}, "missing"
    impl_run = str(impl_plan.get("fresh_find_run_id") or impl_plan.get("current_find_run_id") or "").strip()
    if find_run_id and impl_run and impl_run != find_run_id:
        return {}, "stale_for_current_find"
    return impl_plan, str(impl_plan.get("status") or "")


def environment_selected_candidate(paths, find_run_id: str, candidates: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    selection = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    if not isinstance(selection, dict):
        return {}, {"status": "missing_repo_selection"}
    selected_repo = selection.get("selected", {}) if isinstance(selection.get("selected"), dict) else {}
    decision = selection.get("claude_topic_decision", {}) if isinstance(selection.get("claude_topic_decision"), dict) else {}
    gate = str(selection.get("selection_gate") or "")
    stage = str(selection.get("selection_stage") or selection.get("selected_by_stage") or "")
    selected_run = str(selection.get("fresh_find_run_id") or selected_repo.get("fresh_find_run_id") or "")
    ready = claim_ready_datasets(selected_repo)
    accepted = bool(decision.get("accept_as_current_best") or gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")))
    title = str(selected_repo.get("literature_base_title") or decision.get("selected_base_title") or "").strip()
    evidence = {
        "status": "checked",
        "selection_stage": stage,
        "selection_gate": gate,
        "fresh_find_run_id": selected_run,
        "accepted_by_claude": accepted,
        "claim_ready_datasets": ready,
        "selected_repo": selected_repo,
        "claude_topic_decision": decision,
        "required_stage": ENVIRONMENT_SELECTION_STAGE,
    }
    if stage != ENVIRONMENT_SELECTION_STAGE:
        evidence["blocker"] = "repo selection exists but was not made by the environment-stage Claude Code decision"
        return {}, evidence
    if find_run_id and selected_run != find_run_id:
        evidence["blocker"] = "environment-stage repo selection is stale for the current Find run"
        return {}, evidence
    if not (selected_repo and accepted and ready and selected_repo.get("repo_path") and title):
        evidence["blocker"] = "environment-stage Claude decision, repo path, selected paper title, and loader-ready real data are all required"
        return {}, evidence
    matched = next((row for row in candidates if title_key(row.get("title")) == title_key(title)), {})
    if not matched:
        evidence["blocker"] = "environment-selected paper is not present in the current strong recommendation candidate pool"
        return {}, evidence
    out = dict(matched)
    out.update({
        "environment_selected": True,
        "selection_stage": stage,
        "selection_gate": gate,
        "selected_repo": {
            "name": selected_repo.get("name", ""),
            "url": selected_repo.get("url", ""),
            "repo_path": selected_repo.get("repo_path", ""),
            "claim_ready_datasets": ready,
            "claim_ready_dataset": selected_repo.get("claim_ready_dataset", ""),
        },
        "claude_topic_decision": decision,
        "anchor_selection_policy": "Selected by environment-stage Claude Code after reading current strong recommendations/read/idea/plan artifacts and validating repo/data evidence; not selected by Find rank.",
    })
    return out, evidence


def build(project: str, top_n: int = 12) -> dict[str, Any]:
    paths = build_paths(project)
    cfg = load_project_config(project)
    find = load_json(paths.planning / "finding" / "find_results.json", {})
    packet = load_json(paths.state / "literature_tool_packet.json", {})
    audit = load_json(paths.state / "literature_base_audit.json", {})
    if not isinstance(find, dict):
        find = {}
    if not isinstance(packet, dict):
        packet = {}
    find_run_id = str(find.get("run_id") or "")
    candidates = build_candidate_pool(find, packet, cfg)
    selected, selection_evidence = environment_selected_candidate(paths, find_run_id, candidates)
    impl_plan, impl_status = current_implementation_plan(paths, find_run_id)
    impl_ready = bool(selected and impl_status == "implementation_ready_for_reference_probe")
    route_status = (
        "ready_for_reference_probe" if selected and impl_ready else
        "environment_anchor_selected_waiting_for_implementation_plan" if selected else
        "waiting_for_environment_claude_selection"
    )
    payload = {
        "project": project,
        "generated_at": now_iso(),
        "status": "environment_claude_base_selected" if selected else "fresh_base_candidates_ready_for_environment_claude_selection" if candidates else "blocked_no_fresh_base_candidates",
        "fresh_find_run_id": find_run_id,
        "policy": "Find only prepares a strong-paper candidate pool. The anchor/base work is selected later by the environment-stage Claude Code decision after reading all current strong recommendations, current Read/Idea/Plan artifacts, and validating repo/data/protocol/reproducibility evidence. Find rank alone must never write a selected base.",
        "selected": selected,
        "candidate_count": len(candidates),
        "top_candidates": candidates[:top_n],
        "all_candidate_titles": [row.get("title") for row in candidates],
        "environment_selection": selection_evidence,
        "repo_audit_status": audit.get("status", "") if isinstance(audit, dict) else "",
        "repo_evidence_ready": bool(selected),
        "legacy_active_repo_policy": "historical_control_only_until_environment_claude_code_selects_current_run_anchor",
        "implementation_route": {
            "status": route_status,
            "implementation_plan_status": impl_status,
            "implementation_plan_path": str(paths.state / "fresh_base_implementation_plan.json"),
            "ready_datasets": impl_plan.get("ready_datasets", []) if isinstance(impl_plan, dict) else [],
            "blocked_datasets": impl_plan.get("blocked_datasets", []) if isinstance(impl_plan, dict) else [],
            "steps": [
                "Do not treat a Find-ranked paper as the base work.",
                "Run/read current Find downstream artifacts: strong recommendations, Claude Code readings, ideas, plans, and targeted search notes.",
                "In the environment stage, Claude Code compares candidate papers and audited repos, then selects one anchor only if repo/data/protocol/reproduction evidence is sufficient.",
                "Only after that decision may active_repo and reference reproduction gates use the selected anchor.",
            ],
        },
        "required_next_actions": [
            "Finish the current Find and validate strong recommendations and coverage.",
            "Ensure Claude Code current-Find read/idea/plan artifacts are current for this run.",
            "Run environment-stage repo/data/protocol selection; it must write selection_stage=environment_claude_code before any selected anchor is accepted.",
        ],
        "evidence": [
            str(paths.planning / "finding" / "find_results.json"),
            str(paths.state / "literature_tool_packet.json"),
            str(paths.state / "evidence_ready_repo_selection.json"),
            str(paths.state / "current_find_research_plan.json"),
        ],
    }
    return payload


def write_report(paths, payload: dict[str, Any]) -> None:
    lines = ["# Fresh Research Base Candidates\n\n"]
    for key in ["status", "fresh_find_run_id", "candidate_count", "repo_audit_status", "repo_evidence_ready", "legacy_active_repo_policy"]:
        lines.append(f"- {key}: {payload.get(key)}\n")
    lines.append(f"- policy: {payload.get('policy')}\n")
    selected = payload.get("selected") if isinstance(payload.get("selected"), dict) else {}
    lines.append("\n## Environment-Claude Selected Anchor\n")
    if selected:
        lines.append(f"- title: {selected.get('title')}\n")
        lines.append(f"- venue/year: {selected.get('venue')} {selected.get('year')}\n")
        repo = selected.get("selected_repo", {}) if isinstance(selected.get("selected_repo"), dict) else {}
        lines.append(f"- repo: {repo.get('name', '')} | {repo.get('repo_path', '')}\n")
        lines.append(f"- claim_ready_datasets: {', '.join(repo.get('claim_ready_datasets', []) or [])}\n")
        lines.append(f"- selection_gate: {selected.get('selection_gate')}\n")
        lines.append(f"- anchor_selection_policy: {selected.get('anchor_selection_policy')}\n")
    else:
        lines.append("- none\n")
        ev = payload.get("environment_selection", {}) if isinstance(payload.get("environment_selection"), dict) else {}
        if ev.get("blocker"):
            lines.append(f"- blocker: {ev.get('blocker')}\n")
        lines.append(f"- required_stage: {ev.get('required_stage', ENVIRONMENT_SELECTION_STAGE)}\n")
    lines.append("\n## Candidate Pool From Current Find\n")
    for row in as_list(payload.get("top_candidates"))[:12]:
        lines.append(f"- score={row.get('fresh_base_score')} title={row.get('title')} venue={row.get('venue')} year={row.get('year')} signals={row.get('signals')} code={row.get('code_links')}\n")
    lines.append("\n## Required Next Actions\n")
    for item in as_list(payload.get("required_next_actions")):
        lines.append(f"- {item}\n")
    (paths.reports / "fresh_research_base.md").write_text("".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare current Find base-work candidates; do not select an anchor outside the environment-stage Claude Code decision.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--top-n", type=int, default=12)
    args = parser.parse_args()
    paths = build_paths(args.project)
    payload = build(args.project, args.top_n)
    save_json(paths.state / "fresh_research_base.json", payload)
    write_report(paths, payload)
    print(paths.reports / "fresh_research_base.md")
    return 0 if payload.get("candidate_count") else 2


if __name__ == "__main__":
    raise SystemExit(main())
