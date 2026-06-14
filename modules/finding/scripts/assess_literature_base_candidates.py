#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from literature_policy import core_topic_fit_from_text
from project_paths import build_paths, load_project_config


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


def normalize_title(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return text


def has_any(text: str, tokens: list[str]) -> bool:
    low = text.lower()
    return any(token in low for token in tokens)


def has_positive_signal(text: str, tokens: list[str]) -> bool:
    low = text.lower()
    negations = [
        "缺少", "没有", "无", "不涉及", "未涉及", "完全没有",
        "does not", "do not", "without", "no ", "not ", "lack", "lacks", "missing", "absence of",
    ]
    for token in tokens:
        token_low = token.lower()
        start = 0
        while True:
            pos = low.find(token_low, start)
            if pos < 0:
                break
            local = low[max(0, pos - 64): pos + len(token_low) + 64]
            if not any(neg in local[: local.find(token_low) if token_low in local else len(local)] for neg in negations):
                return True
            start = pos + len(token_low)
    return False


def code_signals(row: dict[str, Any]) -> list[str]:
    values = []
    for key in ["code_url", "github_url", "repo_url", "url", "pdf_url", "abstract", "abstract_en", "reason", "recommendation_note", "fit_explanation"]:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value)
    text = "\n".join(values)
    signals = []
    for pattern in ["github.com", "gitlab", "4open.science", "anonymous.4open", "code", "repository", "repo"]:
        if pattern in text.lower():
            signals.append(pattern)
    return sorted(set(signals))


def candidate_url(row: dict[str, Any]) -> str:
    for key in ["code_url", "github_url", "repo_url", "url", "pdf_url", "openreview_url", "link"]:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def merge_rows(packet_rows: list[dict[str, Any]], find_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_title: dict[str, dict[str, Any]] = {}
    for row in find_rows + packet_rows:
        if not isinstance(row, dict):
            continue
        title = normalize_title(row.get("title") or row.get("name"))
        if not title:
            continue
        merged = dict(by_title.get(title, {}))
        merged.update({k: v for k, v in row.items() if v not in (None, "", [], {})})
        by_title[title] = merged
    return list(by_title.values())


def assess_candidate(row: dict[str, Any], rank: int, cfg: dict[str, Any]) -> dict[str, Any]:
    title = str(row.get("title") or row.get("name") or "")
    abstract = str(row.get("abstract") or row.get("abstract_en") or row.get("summary") or "")
    reason = str(row.get("reason") or row.get("recommendation_note") or row.get("fit_explanation") or "")
    text = "\n".join([title, abstract, reason])
    topic_fit = core_topic_fit_from_text(text, cfg)
    hits = topic_fit.get("topic_group_hits", {}) if isinstance(topic_fit.get("topic_group_hits"), dict) else {}
    matched_groups = [str(name) for name, ok in hits.items() if ok]
    required_groups = [str(name) for name in topic_fit.get("required_topic_groups", []) if str(name).strip()] if isinstance(topic_fit.get("required_topic_groups"), list) else []
    matched_count = len(matched_groups)
    signals = code_signals(row)
    score = float(row.get("score") or row.get("final_score") or row.get("total_score") or 0)
    base_priority = 0.0
    base_priority += min(7.0, 2.5 * matched_count)
    if required_groups and matched_count >= len(required_groups):
        base_priority += 3.0
    base_priority += min(score, 10.0) / 10.0
    base_priority += 2.0 if signals else 0.0
    if topic_fit.get("hard_topic_mismatch"):
        base_priority -= 5.0
    if rank <= 10:
        base_priority += 1.0
    if required_groups and matched_count >= len(required_groups):
        fit = "direct_topic_candidate"
    elif matched_count:
        fit = "topic_component_candidate"
    else:
        fit = "weak_or_background_candidate"
    needs = []
    if not signals:
        needs.append("code_or_repo_lookup")
    needs.extend(["dataset_protocol_check", "paper_target_metric_check", "local_runnability_probe"])
    route = "needs_repo_data_env_audit" if matched_count and not topic_fit.get("hard_topic_mismatch") else "background_only"
    return {
        "rank": rank,
        "title": title,
        "venue": row.get("venue") or row.get("source") or "",
        "year": row.get("year") or row.get("published") or "",
        "score": score,
        "url": candidate_url(row),
        "fit": fit,
        "route": route,
        "required_topic_groups": required_groups,
        "topic_group_hits": hits,
        "matched_topic_groups": matched_groups,
        "matched_topic_group_count": matched_count,
        "missing_topic_groups": topic_fit.get("missing_topic_groups", []),
        "hard_topic_mismatch": bool(topic_fit.get("hard_topic_mismatch")),
        "code_signals": signals,
        "base_priority": round(base_priority, 4),
        "needs_audit": needs,
        "reason": reason[:1000],
    }


def build(project: str, top_n: int) -> dict[str, Any]:
    paths = build_paths(project)
    cfg = load_project_config(project)
    packet = load_json(paths.state / "literature_tool_packet.json", {})
    find_results = load_json(paths.planning / "finding" / "find_results.json", {})
    repo_selection = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    repo_blocker = load_json(paths.state / "repo_selection_blocker.json", {})
    literature_audit = load_json(paths.state / "literature_base_audit.json", {})
    strong = as_list(find_results.get("strong_recommendations")) or as_list(find_results.get("articles"))
    packet_base = as_list(packet.get("base_work_candidates"))
    packet_strong = as_list(packet.get("strong_papers"))
    merged = merge_rows(packet_base + packet_strong, strong)
    assessed = [assess_candidate(row, i + 1, cfg) for i, row in enumerate(merged[:max(top_n, 1)])]
    assessed = sorted(assessed, key=lambda x: (-float(x.get("base_priority") or 0), int(x.get("rank") or 999)))
    audit_required = [row for row in assessed if row.get("route") == "needs_repo_data_env_audit"]
    fresh_find_run_id = find_results.get("run_id") or ""
    current_selection_generated = str(repo_selection.get("generated_at") or "") if isinstance(repo_selection, dict) else ""
    current_blocker_updated = str(repo_blocker.get("updated_at") or repo_blocker.get("search_timestamp") or "") if isinstance(repo_blocker, dict) else ""
    stale_reason = ""
    stale_existing_base_decision = bool(audit_required)
    if audit_required:
        stale_reason = (
            f"Fresh Find {fresh_find_run_id} produced {len(audit_required)} literature base candidates requiring repo/data/env audit. "
            "Existing repo_selection_blocker/evidence_ready_repo_selection cannot prove no better base unless these candidates are audited."
        )
    status = "blocked_pending_literature_base_audit" if audit_required else "no_literature_base_candidates"
    audit_matches_current_find = (
        isinstance(literature_audit, dict)
        and bool(literature_audit.get("audit_complete"))
        and str(literature_audit.get("fresh_find_run_id") or "") == str(fresh_find_run_id or "")
    )
    audit_selected = literature_audit.get("selected") if isinstance(literature_audit, dict) and isinstance(literature_audit.get("selected"), dict) else {}
    if audit_matches_current_find:
        status = (
            "fresh_literature_base_audit_completed_selected_base"
            if audit_selected
            else "fresh_literature_base_audit_completed_no_evidence_ready_base"
        )
        stale_existing_base_decision = False
        stale_reason = (
            "Fresh literature base audit already completed for this Find run and selected an evidence-ready base; "
            "downstream gates should evaluate that selected fresh route."
            if audit_selected
            else "Fresh literature base audit already completed for this Find run, but no evidence-ready repo/data/env route was selected. "
            "Downstream gates must continue fresh-base implementation/code/data work instead of resetting to pending audit or a historical route."
        )
    payload = {
        "project": project,
        "generated_at": now_iso(),
        "status": status,
        "fresh_find_run_id": fresh_find_run_id,
        "literature_packet_generated_at": packet.get("generated_at") if isinstance(packet, dict) else "",
        "strong_count": len(strong),
        "packet_base_work_candidates": len(packet_base),
        "assessed_count": len(assessed),
        "audit_required_count": len(audit_required),
        "current_repo_selection_generated_at": current_selection_generated,
        "current_repo_blocker_updated_at": current_blocker_updated,
        "stale_existing_base_decision": stale_existing_base_decision,
        "stale_reason": stale_reason,
        "top_candidates": assessed[:top_n],
        "audit_required_candidates": audit_required[:top_n],
        "policy": {
            "fresh_find_must_drive_base_selection": True,
            "positive_anchor_pools": ["articles", "strong_recommendations"],
            "audit_pool_policy": "screened/read/evaluated/title candidates can propose repo/base audits but cannot support claims.",
            "historical_route_policy": "A historical route cannot remain the main route after a fresh Find until the fresh literature base candidates are audited or rejected with evidence.",
        },
        "required_next_actions": [
            "For each audit_required_candidate, search/resolve code repository and dataset/protocol availability.",
            "Run repo/data/env selector on discovered repos; update evidence_ready_repo_selection.json with fresh_find_run_id.",
            "Only after this fresh assessment may reference_reproduction_gate decide switch_base/no_viable_base_switch_route/continue_base.",
        ],
    }
    if audit_matches_current_find and isinstance(literature_audit, dict):
        payload.update({
            "last_audit_generated_at": literature_audit.get("generated_at", ""),
            "last_audit_status": literature_audit.get("status", ""),
            "last_audit_repo_candidates_discovered_count": literature_audit.get("repo_candidates_discovered_count", 0),
            "last_audit_selection_gate": literature_audit.get("selection_gate", ""),
            "last_audit_selected": audit_selected,
            "last_audit_candidate_count": literature_audit.get("candidate_count", 0),
            "last_audit_total_required_count": literature_audit.get("total_audit_required_count", 0),
            "last_audit_complete": bool(literature_audit.get("audit_complete")),
            "last_audit_remaining_candidate_count": literature_audit.get("remaining_candidate_count", 0),
        })
    return payload


def write_report(paths, payload: dict[str, Any]) -> Path:
    lines = ["# Literature Base Candidate Assessment\n\n"]
    for key in ["status", "fresh_find_run_id", "strong_count", "packet_base_work_candidates", "audit_required_count", "stale_existing_base_decision"]:
        lines.append(f"- {key}: {payload.get(key)}\n")
    if payload.get("stale_reason"):
        lines.append(f"- stale_reason: {payload.get('stale_reason')}\n")
    lines.append("\n## Audit Required Candidates\n")
    for row in payload.get("audit_required_candidates", [])[:30]:
        lines.append(f"- rank={row.get('rank')} priority={row.get('base_priority')} fit={row.get('fit')} title={row.get('title')} venue={row.get('venue')} year={row.get('year')} url={row.get('url')} needs={row.get('needs_audit')}\n")
    out = paths.reports / "literature_base_candidate_assessment.md"
    out.write_text("".join(lines), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Assess fresh TASTE/Find literature candidates before TASTE can keep or switch a base work.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--top-n", type=int, default=30)
    args = parser.parse_args()
    paths = build_paths(args.project)
    payload = build(args.project, args.top_n)
    save_json(paths.state / "literature_base_candidate_assessment.json", payload)
    report = write_report(paths, payload)
    print(report)
    return 2 if payload.get("status") == "blocked_pending_literature_base_audit" else 0


if __name__ == "__main__":
    raise SystemExit(main())
