#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths, configured_max_ideas, load_project_config, management_python, project_experiment_python_from_config
from agent_state import append_agent_log, mark_agent, upsert_agent
from auto_research.paths import CONFIG_PATH
from paper_common import get_active_paper_state
from project_config import project_target_venue
from pipeline_guard import fresh_base_state_names, guard_fresh_base_blocker_entry
from guard_selected_base_route import repair_project as guard_selected_base_route
from run_project import current_find_execution_contract

from taste_pythonpath import script_resolver

SCRIPTS = script_resolver(ROOT)

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(Authorization:\s*Bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*[\"']?)[A-Za-z0-9._\-]+"),
]


def redact_secrets(value: Any) -> str:
    text = str(value or "")
    for pattern in SECRET_PATTERNS:
        def repl(match: re.Match[str]) -> str:
            if match.lastindex:
                return match.group(1) + "[REDACTED]"
            return "[REDACTED]"
        text = pattern.sub(repl, text)
    return text


def _secret_like_key(key: Any) -> bool:
    lowered = str(key or "").strip().lower()
    return lowered in {"api_key", "apikey", "authorization", "password", "secret", "access_token", "auth_token", "github_token"} or lowered.endswith(("_api_key", "_apikey", "_password", "_secret", "_access_token", "_auth_token"))


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): ("[REDACTED]" if _secret_like_key(key) and item else redact_payload(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_payload(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        try:
            return redact_payload(asdict(value))
        except Exception:
            return redact_secrets(value)
    if isinstance(value, str):
        return redact_secrets(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        json.dumps(value)
        return value
    except TypeError:
        return redact_secrets(value)
    return value


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()



def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def read_json_if_small(path: Path, default: Any, *, max_bytes: int = 2_000_000) -> Any:
    try:
        if not path.exists() or path.stat().st_size > max_bytes:
            return default
        return read_json(path, default)
    except Exception:
        return default


def payload_run_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ["run_id", "source_run_id", "taste_run_id", "find_run_id", "current_find_run_id"]:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def current_find_light_payload(paths) -> dict[str, Any]:
    """Return current Find summary rows without loading huge find_results.json."""
    progress = read_json(paths.planning / "finding" / "find_progress.json", {})
    frontend = read_json(paths.state / "finding_frontend.json", {})
    current_plan = read_json(paths.state / "current_find_research_plan.json", {})
    projection = read_json_if_small(paths.state / "current_find_recommendation_projection.json", {}, max_bytes=5_000_000)
    find_results = read_json_if_small(paths.planning / "finding" / "find_results.json", {})
    payloads = [progress, frontend, current_plan, projection, find_results]
    current_run = ""
    for payload in payloads:
        current_run = payload_run_id(payload)
        if current_run:
            break

    def matches(payload: Any) -> bool:
        run_id = payload_run_id(payload)
        return isinstance(payload, dict) and (not current_run or not run_id or run_id == current_run)

    result = projection if matches(projection) and projection else {}
    if not result and matches(find_results):
        result = find_results
    if not isinstance(result, dict):
        result = {}
    if current_run and not payload_run_id(result):
        result = {**result, "run_id": current_run}
    counts: dict[str, Any] = {}
    for payload in [progress, frontend, result]:
        if isinstance(payload, dict):
            for key in ["counts", "survey_stats"]:
                value = payload.get(key)
                if isinstance(value, dict):
                    counts.update(value)
    if counts:
        result_counts = result.get("counts", {}) if isinstance(result.get("counts"), dict) else {}
        result = {**result, "counts": {**result_counts, **counts}}
    return result


def current_find_light_run_id(paths) -> str:
    payload = current_find_light_payload(paths)
    run_id = payload_run_id(payload)
    if run_id:
        return run_id
    for candidate in [
        paths.planning / "finding" / "find_progress.json",
        paths.state / "current_find_research_plan.json",
        paths.state / "literature_tool_packet.json",
        paths.state / "supervision_tick.json",
    ]:
        run_id = payload_run_id(read_json(candidate, {}))
        if run_id:
            return run_id
    return ""


def read_text(path: Path, limit: int = 24000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit] if path.exists() else ""
    except Exception:
        return ""


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact_payload(payload), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def one_line(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def slug(value: str, limit: int = 80) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value or ""))
    safe = "_".join(part for part in safe.split("_") if part)
    return (safe or "stage")[:limit]


def sha256_file(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return ""


def is_int(value: Any) -> bool:
    try:
        int(str(value).strip())
        return True
    except Exception:
        return False


def count_rows(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


CLAUDE_IDEATION_COMPLETED_STATUSES = {"completed", "success", "ok"}


def claude_ideation_block_status(result: Any) -> str:
    if not isinstance(result, dict):
        return "failed"
    status = str(result.get("status") or "").strip().lower()
    if not status or status in CLAUDE_IDEATION_COMPLETED_STATUSES:
        return ""
    return status


CURRENT_FIND_FULL_TEXT_POLICY_VERSION = "full_text_required_v5_detailed_deep_read"


def current_find_validation_ready(validation: Any, run_id: str, expected_count: int) -> bool:
    if not isinstance(validation, dict):
        return False
    if str(validation.get("run_id") or "").strip() != str(run_id or "").strip():
        return False
    if validation.get("valid") is not True:
        return False
    if str(validation.get("policy_version") or "").strip() != CURRENT_FIND_FULL_TEXT_POLICY_VERSION:
        return False
    full_text_count = safe_int(validation.get("full_text_reading_count"), 0)
    pending_count = safe_int(validation.get("pending_full_text_reading_count"), 0)
    actual_count = safe_int(validation.get("actual_reading_count"), 0)
    expected_from_validation = safe_int(validation.get("expected_recommendation_count"), 0)
    required = expected_count or expected_from_validation
    if not required:
        return False
    if actual_count != required or full_text_count < required:
        return False
    if pending_count > 0:
        return False
    if validation.get("blockers"):
        return False
    return True


def current_find_full_text_gate_status(paths) -> dict[str, Any]:
    taste_dir = paths.planning / "finding"
    find_results = current_find_light_payload(paths)
    run_id = payload_run_id(find_results) or current_find_light_run_id(paths)
    expected = count_rows(find_results.get("strong_recommendations")) or count_rows(find_results.get("articles"))
    validation = read_json(paths.state / "current_find_claude_reading_validation.json", {})
    current_plan = read_json(paths.state / "current_find_research_plan.json", {})
    read_payload = read_json(taste_dir / "read_results.json", {})
    validation_ready = current_find_validation_ready(validation, run_id, expected)
    validation_run = str(validation.get("run_id") or "").strip() if isinstance(validation, dict) else ""
    plan_status = str(current_plan.get("status") or "").strip() if isinstance(current_plan, dict) else ""
    read_status = str(read_payload.get("status") or "").strip() if isinstance(read_payload, dict) else ""
    blockers = []
    if isinstance(validation, dict) and isinstance(validation.get("blockers"), list):
        blockers.extend(str(item) for item in validation.get("blockers", []) if str(item or "").strip())
    full_text_count = safe_int(validation.get("full_text_reading_count"), 0) if isinstance(validation, dict) else 0
    pending_count = safe_int(validation.get("pending_full_text_reading_count"), 0) if isinstance(validation, dict) else 0
    actual_count = safe_int(validation.get("actual_reading_count"), 0) if isinstance(validation, dict) else count_rows(read_payload.get("readings")) if isinstance(read_payload, dict) else 0
    if expected and (not validation_ready):
        if validation_run and run_id and validation_run != run_id:
            blockers.append("Read-stage full-text packet validation is stale for this run_id")
        if not blockers:
            blockers.append("Read-stage reading packet has not passed full-text/PDF/page evidence validation")
        return {
            "status": "blocked_current_find_full_text_reading",
            "blocking": True,
            "run_id": run_id,
            "expected_recommendation_count": expected,
            "actual_reading_count": actual_count,
            "full_text_reading_count": full_text_count,
            "pending_full_text_reading_count": pending_count if pending_count else max(0, expected - full_text_count),
            "policy_version": CURRENT_FIND_FULL_TEXT_POLICY_VERSION,
            "current_find_plan_status": plan_status,
            "read_results_status": read_status,
            "blockers": blockers,
            "evidence": [
                str(taste_dir / "find_results.json"),
                str(taste_dir / "read_results.json"),
                str(paths.state / "current_find_claude_reading_validation.json"),
                str(paths.state / "current_find_research_plan.json"),
            ],
            "next_action": "Run the Read-stage full-text repair packet: acquire or prove missing title/author-verified PDF/HTML/page evidence, or let Read use a same-run ranked replacement in full_text_packet only; do not rewrite Find outputs or rerun Claude over unchanged missing evidence.",
        }
    return {
        "status": "pass" if expected else "unknown",
        "blocking": False,
        "run_id": run_id,
        "expected_recommendation_count": expected,
        "actual_reading_count": actual_count,
        "full_text_reading_count": full_text_count,
        "pending_full_text_reading_count": pending_count,
        "policy_version": CURRENT_FIND_FULL_TEXT_POLICY_VERSION,
        "blockers": [],
    }


def current_find_plan_bridge_gate_status(paths, bridge_summary: Any | None = None) -> dict[str, Any]:
    """Validate that current-Find Read/Idea/Plan artifacts belong to the latest Find run.

    This is stricter than checking a worker return code. Full-cycle must not feed
    stale or blocked Read/Idea/Plan state into environment, experiment, or Claude
    repair stages.
    """
    bridge_summary = bridge_summary if isinstance(bridge_summary, dict) else {}
    taste_dir = paths.planning / "finding"
    find_results = current_find_light_payload(paths)
    run_id = payload_run_id(find_results) or current_find_light_run_id(paths)
    expected_readings = count_rows(find_results.get("strong_recommendations")) or count_rows(find_results.get("articles"))
    read_results = read_json(taste_dir / "read_results.json", {})
    ideas = read_json(taste_dir / "ideas.json", {})
    plans = read_json(taste_dir / "plans.json", {})
    current_plan = read_json(paths.state / "current_find_research_plan.json", {})
    validation = read_json(paths.state / "current_find_claude_reading_validation.json", {})
    selected_contract = current_find_execution_contract(paths)
    required_idea_count = configured_max_ideas(getattr(paths, "name", ""), default=5)

    plan_run_id = str(current_plan.get("run_id") or "").strip() if isinstance(current_plan, dict) else ""
    plan_status = str(current_plan.get("status") or "").strip() if isinstance(current_plan, dict) else ""
    read_count = count_rows(read_results.get("readings")) if isinstance(read_results, dict) else 0
    idea_count = count_rows(ideas.get("ideas")) if isinstance(ideas, dict) else 0
    plan_count = count_rows(plans.get("plans")) if isinstance(plans, dict) else 0
    bridge_return_code = bridge_summary.get("bridge_return_code")
    if bridge_return_code is None and "return_code" in bridge_summary:
        bridge_return_code = bridge_summary.get("return_code")

    blockers: list[str] = []
    if not run_id:
        blockers.append("missing latest Find run_id")
    if bridge_return_code not in (None, 0):
        blockers.append(f"current-Find Read/Idea/Plan bridge returned {bridge_return_code}")
    if not isinstance(current_plan, dict) or not current_plan:
        blockers.append("state/current_find_research_plan.json is missing")
    elif run_id and plan_run_id != run_id:
        blockers.append("state/current_find_research_plan.json is stale for the latest Find run_id")
    if plan_status.startswith("blocked") or plan_status.startswith("failed") or plan_status.startswith("error"):
        blockers.append(f"current-Find plan bridge status is {plan_status}")
    if expected_readings and read_count != expected_readings:
        blockers.append(f"read_results count {read_count} does not match current Find recommendation count {expected_readings}")
    if isinstance(read_results, dict) and run_id and str(read_results.get("run_id") or "").strip() != run_id:
        blockers.append("read_results.json is stale for the latest Find run_id")
    if isinstance(ideas, dict) and run_id and str(ideas.get("run_id") or "").strip() != run_id:
        blockers.append("ideas.json is stale for the latest Find run_id")
    if isinstance(plans, dict) and run_id and str(plans.get("run_id") or "").strip() != run_id:
        blockers.append("plans.json is stale for the latest Find run_id")
    if idea_count < required_idea_count:
        blockers.append(f"current-Find idea count {idea_count} is below required {required_idea_count}")
    if plan_count < required_idea_count:
        blockers.append(f"current-Find plan count {plan_count} is below required {required_idea_count}")
    if not current_find_validation_ready(validation, run_id, expected_readings):
        blockers.append("current-Find full-text reading validation is not ready for the latest Find run")
    if isinstance(current_plan, dict):
        if current_plan.get("read_idea_plan_ready") is not True:
            blockers.append("current_find_research_plan.read_idea_plan_ready is not true")
        if current_plan.get("claude_current_find_ready") is not True:
            blockers.append("current_find_research_plan.claude_current_find_ready is not true")
    if selected_contract.get("required") and not str(selected_contract.get("selected_plan_id") or "").strip():
        blockers.append(str(selected_contract.get("reason") or "current Find plan selection is missing"))
    contract_run_id = str(selected_contract.get("run_id") or "").strip()
    if run_id and contract_run_id and contract_run_id != run_id:
        blockers.append("current-Find selected plan contract is stale for the latest Find run_id")

    status = "pass" if not blockers else "blocked_current_find_plan_bridge"
    return {
        "status": status,
        "blocking": bool(blockers),
        "run_id": run_id,
        "plan_run_id": plan_run_id,
        "bridge_return_code": bridge_return_code,
        "bridge_status": plan_status,
        "readings": read_count,
        "ideas": idea_count,
        "plans": plan_count,
        "expected_readings": expected_readings,
        "selected_plan_id": str(selected_contract.get("selected_plan_id") or ""),
        "selection_issue": str(selected_contract.get("selection_issue") or ""),
        "blockers": blockers,
        "evidence": [
            str(taste_dir / "find_results.json"),
            str(taste_dir / "read_results.json"),
            str(taste_dir / "ideas.json"),
            str(taste_dir / "plans.json"),
            str(paths.state / "current_find_claude_reading_validation.json"),
            str(paths.state / "current_find_research_plan.json"),
            str(paths.state / "taste_plan_bridge.json"),
        ],
        "next_action": "Repair the Read-stage full-text packet and rerun modules/reading/scripts/ensure_current_find_research_plan.py --project <project>; do not continue environment, experiment, paper, or Claude repair stages with stale or blocked plan artifacts.",
    }


def enabled_literature_source_count(selection: Any) -> int:
    if not isinstance(selection, dict):
        return 0
    count = len(selection.get("venue_ids") or []) if isinstance(selection.get("venue_ids"), list) else 0
    for key in [
        "include_arxiv",
        "include_biorxiv",
        "include_nature",
        "include_science",
        "include_huggingface",
        "include_github",
    ]:
        if selection.get(key):
            count += 1
    return count


def looks_like_llm_quota_blocker(value: Any) -> bool:
    if not value:
        return False
    text = json.dumps(value, ensure_ascii=False).lower() if isinstance(value, (dict, list)) else str(value).lower()
    markers = [
        "llm http 429",
        "quota_exceeded",
        "quota exceeded",
        "token plan limit exhausted",
        "rpm exhausted",
        "too many requests",
        "llm quota",
        "rate-limit",
        "rate limit",
    ]
    return any(marker in text for marker in markers)


def latest_targeted_literature_tool_status(paths) -> dict[str, Any]:
    current_plan = read_json(paths.state / "current_find_research_plan.json", {})
    status = current_plan.get("targeted_search_tool_status") if isinstance(current_plan, dict) and isinstance(current_plan.get("targeted_search_tool_status"), dict) else {}
    progress = read_json(paths.planning / "finding" / "find_progress.json", {})
    current_run = str(progress.get("run_id") or "").strip() if isinstance(progress, dict) else ""
    status_run = str(status.get("current_find_run_id") or status.get("run_id") or status.get("find_run_id") or "").strip() if isinstance(status, dict) else ""
    if current_run and status_run and status_run != current_run:
        status = {}
    latest = read_json(paths.state / "taste_targeted_queries.json", {})
    if isinstance(latest, dict):
        latest_run = str(latest.get("current_find_run_id") or latest.get("run_id") or latest.get("find_run_id") or "").strip()
        latest_status = str(latest.get("status") or "")
        latest_has_failure = bool(latest.get("failure_summary") or latest.get("return_codes"))
        if current_run and latest_run and latest_run != current_run:
            return status
        if latest_has_failure or latest_status.startswith("failed"):
            merged = dict(status)
            for key in ["status", "venue", "packet_return_code", "return_codes", "failure_summary", "guardrail", "record_only_requested", "new_find_allowed", "current_find_run_id"]:
                if key in latest:
                    merged[key] = latest.get(key)
            status = merged
    return status


def recommendation_target_status(paths) -> dict[str, Any]:
    """Read the current Find recommendation target without loading find_results.json.

    `strong_recommendation_count` is the ranked Top-N output after mandatory
    title+abstract LLM scoring over papers with real abstracts. It is not the
    smaller strict/claim-anchor count used later by Read/Idea/Paper gates.
    """
    progress = read_json(paths.planning / "finding" / "find_progress.json", {})
    packet = read_json(paths.state / "literature_tool_packet.json", {})
    frontend = read_json(paths.state / "finding_frontend.json", {})
    find_results = current_find_light_payload(paths)
    current_plan = read_json(paths.state / "current_find_research_plan.json", {})
    find_run_id = payload_run_id(find_results) or current_find_light_run_id(paths)
    plan_run_id = str(current_plan.get("run_id") or current_plan.get("current_find_run_id") or "").strip() if isinstance(current_plan, dict) else ""
    plan_status = str(current_plan.get("status") or "").strip().lower() if isinstance(current_plan, dict) else ""
    current_plan_ready = bool(find_run_id and plan_run_id == find_run_id and not plan_status.startswith("blocked"))
    if current_plan_ready and isinstance(find_results, dict):
        recommendation_count = len(find_results.get("strong_recommendations") or find_results.get("articles") or [])
        read_candidate_count = len(find_results.get("read_candidates") or []) or recommendation_count
        progress_matches_current = isinstance(progress, dict) and str(progress.get("run_id") or "").strip() == find_run_id
        packet_matches_current = isinstance(packet, dict) and str(packet.get("run_id") or packet.get("source_run_id") or "").strip() == find_run_id
        packet_summary_current = packet.get("summary", {}) if packet_matches_current and isinstance(packet.get("summary", {}), dict) else {}
        target = max(
            safe_int(progress.get("recommendation_target_count"), 0) if progress_matches_current else 0,
            safe_int(packet_summary_current.get("recommendation_target_count"), 0),
            recommendation_count,
        )
        shortfall = max(0, target - recommendation_count) if target else 0
        return {
            "run_id": find_run_id,
            "actual": recommendation_count,
            "target": target,
            "shortfall": shortfall,
            "source_count": 0,
            "selection": {},
            "blocking": bool(target and shortfall > 0),
            "status": "shortfall" if shortfall else "pass" if target else "unknown",
            "llm_blocked": False,
            "counts": {
                "strong_recommendations": recommendation_count,
                "read_candidates": read_candidate_count,
                "readings": safe_int(current_plan.get("current_find_reading_count"), 0) if isinstance(current_plan, dict) else 0,
                "ideas": safe_int(current_plan.get("current_find_idea_count"), 0) if isinstance(current_plan, dict) else 0,
                "plans": safe_int(current_plan.get("current_find_plan_count"), 0) if isinstance(current_plan, dict) else 0,
            },
            "blockers": [] if not shortfall else [f"Find recommended papers are below the project target: {recommendation_count}/{target}; shortfall={shortfall}."]
        }
    sources = [item for item in [progress, packet, frontend] if isinstance(item, dict)]
    selection: dict[str, Any] = {}
    for item in sources:
        candidate = item.get("selection")
        if isinstance(candidate, dict) and candidate:
            selection = candidate
            break
    run_id = ""
    for item in sources:
        run_id = str(item.get("run_id") or item.get("source_run_id") or item.get("find_run_id") or "").strip()
        if run_id:
            break
    targeted_tool_status = latest_targeted_literature_tool_status(paths)
    progress_status = str(progress.get("status") or progress.get("phase") or "").lower() if isinstance(progress, dict) else ""
    blocked_reason = str(
        (progress.get("blocked_reason") or progress.get("error") or "") if isinstance(progress, dict) else ""
    ) or str(targeted_tool_status.get("failure_summary") or targeted_tool_status.get("error") or "")
    llm_blocked = (
        "blocked_llm" in progress_status
        or "quota" in progress_status
        or looks_like_llm_quota_blocker(blocked_reason)
        or looks_like_llm_quota_blocker(targeted_tool_status)
    )
    packet_summary = packet.get("summary", {}) if isinstance(packet.get("summary"), dict) else {}
    packet_layer = packet.get("candidate_layer_summary", {}) if isinstance(packet.get("candidate_layer_summary"), dict) else {}
    packet_counts = packet_layer.get("pool_counts", {}) if isinstance(packet_layer.get("pool_counts"), dict) else {}
    counts = progress.get("counts", {}) if isinstance(progress.get("counts"), dict) else {}
    actual_candidates = [
        progress.get("strong_recommendation_count"),
        packet_summary.get("strong_paper_anchors"),
        packet_counts.get("strong_papers"),
        packet_counts.get("claim_ready_strong_papers"),
        packet_counts.get("strong_recommendations"),
    ]
    actual = safe_int(progress.get("strong_recommendation_count"), 0) if llm_blocked else max(safe_int(value, 0) for value in actual_candidates)
    source_count = enabled_literature_source_count(selection)
    explicit_target = max(
        safe_int(progress.get("recommendation_target_count"), 0),
        safe_int(packet_summary.get("recommendation_target_count"), 0),
        safe_int(packet_counts.get("recommendation_target_count"), 0),
    )
    target = explicit_target or (source_count * 5 if source_count else 0)
    explicit_shortfall = max(
        safe_int(progress.get("recommendation_shortfall"), 0),
        safe_int(packet_summary.get("recommendation_shortfall"), 0),
        safe_int(packet_counts.get("recommendation_shortfall"), 0),
    )
    shortfall = explicit_shortfall if (explicit_shortfall or llm_blocked) else max(0, target - actual) if target else 0
    blocking = bool(target and shortfall > 0)
    blockers = []
    if blocking:
        blockers.append(
            f"Find recommended papers are below the project target: {actual}/{target}; shortfall={shortfall}. Continue title+abstract scoring repair before base/claim/paper promotion."
        )
    return {
        "run_id": run_id,
        "actual": actual,
        "target": target,
        "shortfall": shortfall,
        "source_count": source_count,
        "selection": selection,
        "blocking": blocking,
        "status": "blocked_llm_quota_exhausted" if llm_blocked else "shortfall" if blocking else "pass" if target else "unknown",
        "llm_blocked": llm_blocked,
        "blocked_reason": blocked_reason,
        "targeted_search_tool_status": targeted_tool_status,
        "counts": counts,
        "blockers": blockers,
        "evidence": [
            str(paths.planning / "finding" / "find_progress.json"),
            str(paths.state / "literature_tool_packet.json"),
        ],
    }


def current_find_run_id(paths) -> str:
    """Return the active Find run id from project-owned state only."""
    return current_find_light_run_id(paths)



def title_key_for_current_find(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def current_find_recommended_title_keys(paths_or_root) -> set[str]:
    payload = current_find_light_payload(paths_or_root)
    if not isinstance(payload, dict):
        return set()
    keys: set[str] = set()
    for pool in ["articles", "strong_recommendations"]:
        rows = payload.get(pool)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                key = title_key_for_current_find(row.get("title") or row.get("paper_title"))
                if key:
                    keys.add(key)
    return keys


def selected_title_in_current_find(paths_or_root, selected: dict[str, Any], decision: dict[str, Any] | None = None) -> bool:
    decision = decision if isinstance(decision, dict) else {}
    title = selected.get("title") or selected.get("literature_base_title") or selected.get("selected_base_title") or decision.get("selected_base_title") or selected.get("name") or ""
    key = title_key_for_current_find(title)
    if key and key in current_find_recommended_title_keys(paths_or_root):
        return True
    root = Path(paths_or_root.root) if hasattr(paths_or_root, "root") else Path(paths_or_root)
    audit = read_json(root / "state" / "fresh_base_reference_reproduction_audit.json", {})
    audit_selected = audit.get("selected_base") if isinstance(audit, dict) and isinstance(audit.get("selected_base"), dict) else {}
    selected_repo = str(selected.get("repo_path") or selected.get("local_path") or "").strip()
    audit_repo = str(audit.get("repo_path") or audit.get("active_repo_path") or audit_selected.get("repo_path") or audit_selected.get("local_path") or "").strip() if isinstance(audit, dict) else ""
    audit_title = audit_selected.get("literature_base_title") or audit_selected.get("title") or audit.get("paper_title") or audit.get("base_title") or "" if isinstance(audit, dict) else ""
    audit_run = str(audit_selected.get("fresh_find_run_id") or "").strip()
    selected_run = str(selected.get("fresh_find_run_id") or "").strip()
    if selected_repo and audit_repo and selected_repo == audit_repo and (
        (audit_run and selected_run == audit_run)
        or (key and title_key_for_current_find(audit_title) == key)
    ):
        return True
    gate = read_json(root / "state" / "base_switch_gate.json", {})
    execution = read_json(root / "state" / "base_switch_execution.json", {})
    candidate = gate.get("candidate_route") if isinstance(gate, dict) and isinstance(gate.get("candidate_route"), dict) else {}
    candidate_repo = str(candidate.get("repo_path") or "").strip()
    return bool(
        selected_repo
        and candidate_repo
        and selected_repo == candidate_repo
        and isinstance(gate, dict)
        and gate.get("status") == "pass"
        and gate.get("decision") == "authorize_base_switch"
        and isinstance(execution, dict)
        and str(execution.get("status") or "").startswith("authorized_by_deterministic_base_switch_gate")
    )

def _selection_accepted_by_claude(selection: dict[str, Any]) -> bool:
    decision = selection.get("claude_topic_decision") if isinstance(selection.get("claude_topic_decision"), dict) else {}
    return bool(
        selection.get("accepted_by_claude")
        or str(selection.get("selection_gate") or "").startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate"))
        or decision.get("accept_as_current_best")
    )


def environment_selection_context(paths) -> dict[str, Any]:
    """Validate the current environment-stage base selection.

    Find recommends papers; it does not define the experiment base. The
    experiment repo/path is usable only after the environment Claude Code stage
    accepts a base for the current Find run. This keeps project state portable
    across topics and prevents any paper-specific path from becoming a global
    TASTE constant.
    """
    run_id = current_find_run_id(paths)
    selection = read_json(paths.state / "evidence_ready_repo_selection.json", {})
    if not isinstance(selection, dict):
        return {
            "valid": False,
            "current_find_run_id": run_id,
            "fresh_find_run_id": "",
            "selection_stage": "",
            "accepted_by_claude": False,
            "selected": {},
            "selection_gate": "",
            "reason": "missing_evidence_ready_repo_selection",
        }
    selected = selection.get("selected", {}) if isinstance(selection.get("selected"), dict) else {}
    selected_run = str(selected.get("fresh_find_run_id") or selection.get("fresh_find_run_id") or "").strip()
    stage = str(selection.get("selection_stage") or selection.get("selected_by_stage") or "").strip()
    accepted = _selection_accepted_by_claude(selection)
    decision = selection.get("claude_topic_decision") if isinstance(selection.get("claude_topic_decision"), dict) else {}
    in_current_find = selected_title_in_current_find(paths, selected, decision)
    valid = bool(selected and stage == "environment_claude_code" and accepted and in_current_find)
    return {
        "valid": valid,
        "current_find_run_id": run_id,
        "fresh_find_run_id": selected_run,
        "selection_stage": stage,
        "accepted_by_claude": accepted,
        "selected": selected,
        "selection_gate": str(selection.get("selection_gate") or ""),
        "reason": "current_environment_base_selected" if valid else ("selected_base_not_in_current_find_recommendations" if not in_current_find else "environment_base_selection_pending_or_stale"),
    }


def fresh_base_data_required(plan: Any) -> bool:
    """Return True when the fresh-base route is blocked by real data/loader evidence."""
    if not isinstance(plan, dict):
        return False
    if str(plan.get("status") or "") == "blocked_fresh_base_data_required":
        return True
    for key in ["fresh_base_data_acquisition", "data_acquisition"]:
        data = plan.get(key)
        if isinstance(data, dict) and str(data.get("decision") or "") == "blocked_external_data_required":
            return True
    blocked_datasets = plan.get("blocked_datasets", [])
    blockers = plan.get("blocker_reasons", []) if isinstance(plan.get("blocker_reasons"), list) else []
    text = "\\n".join(str(item).lower() for item in blockers)
    data_terms = ["dataset", "loader", "google drive", "required file", "required_files", "dataset_contract", "missing_required_files"]
    return bool(blocked_datasets) and any(term in text for term in data_terms)


def fresh_base_block_category(
    plan: Any,
    *,
    reference_probe_required: bool = False,
    reference_smoke_required: bool = False,
    reference_reproduction_required: bool = False,
) -> str:
    if reference_reproduction_required:
        return "blocked_fresh_base_reference_reproduction_required"
    if reference_smoke_required:
        return "blocked_fresh_base_reference_smoke_required"
    if reference_probe_required:
        return "blocked_fresh_base_reference_probe_required"
    return "blocked_fresh_base_data_required" if fresh_base_data_required(plan) else "blocked_fresh_base_implementation_required"


def fresh_base_route_context(paths, project: str = "") -> dict[str, Any]:
    fresh = read_json(paths.state / "fresh_research_base.json", {})
    current = read_json(paths.state / "current_find_research_plan.json", {})
    plan = read_json(paths.state / "fresh_base_implementation_plan.json", {})
    active = read_json(paths.state / "active_repo.json", {})
    env = environment_selection_context(paths)
    env_selected = env.get("selected", {}) if isinstance(env.get("selected"), dict) and env.get("valid") else {}
    selected = fresh.get("selected", {}) if isinstance(fresh, dict) and isinstance(fresh.get("selected"), dict) else {}
    if env_selected:
        selected = {**selected, **env_selected}
    base_switch = read_json(paths.state / "reference_reproduction_gate.json", {})
    if isinstance(base_switch, dict) and isinstance(base_switch.get("base_switch"), dict):
        maybe = base_switch["base_switch"].get("fresh_paper_base")
        if isinstance(maybe, dict) and maybe:
            selected = {**selected, **maybe}
    repo = plan.get("repo", {}) if isinstance(plan, dict) and isinstance(plan.get("repo"), dict) else {}
    if env_selected:
        repo = {
            **repo,
            "name": env_selected.get("name") or repo.get("name") or repo.get("repo"),
            "repo": env_selected.get("name") or repo.get("repo") or repo.get("name"),
            "repo_path": env_selected.get("repo_path") or env_selected.get("local_path") or repo.get("repo_path") or repo.get("local_path"),
            "local_path": env_selected.get("local_path") or env_selected.get("repo_path") or repo.get("local_path") or repo.get("repo_path"),
        }
    title = one_line(selected.get("title") or (current.get("selected_base_title") if isinstance(current, dict) else ""))
    if not title:
        title = one_line(active.get("title") or active.get("name") or active.get("repo") or "environment-stage selected anchor") if isinstance(active, dict) else "environment-stage selected anchor"
    repo_name = one_line(repo.get("name") or repo.get("repo") or active.get("name") or active.get("repo") or "environment-stage selected repo") if isinstance(active, dict) else one_line(repo.get("name") or repo.get("repo") or "environment-stage selected repo")
    repo_path = str(repo.get("repo_path") or repo.get("local_path") or (active.get("repo_path") if isinstance(active, dict) else "") or (active.get("local_path") if isinstance(active, dict) else "") or "")
    return {
        "title": title,
        "repo_name": repo_name,
        "repo_path": repo_path,
        "selected": selected,
        "repo": repo,
        "current_route_source": "evidence_ready_repo_selection" if env_selected else "active_repo_or_implementation_fallback",
        "environment_selection": env,
        "legacy_policy": "Historical or previous routes are legacy/control only unless the current project explicitly selects them as the active main route.",
        "legacy_policy_zh": "历史路线或旧项目路线只作为 legacy/control，除非当前项目明确把它选为 active main route。",
    }


def fresh_base_status_text(category: str, ctx: dict[str, Any], *, zh: bool = False) -> tuple[str, str, str]:
    title = str(ctx.get("title") or "environment-stage selected anchor")
    repo_name = str(ctx.get("repo_name") or "environment-stage selected repo")
    legacy = "旧路线仅作为历史/内部对照，不是当前主线。" if zh else "Previous routes are legacy/control only, not the current main route."
    if category == "blocked_fresh_base_data_required":
        if zh:
            return (
                f"环境阶段 Claude Code 已选择 {title}；{repo_name} 的真实数据/loader 合同尚未 evidence-ready",
                "补齐当前基底需要的真实数据文件和 loader/import probe；通过前不训练、不写论文、不提升 claim。" + legacy,
                "fresh paper base lacks loader-ready real data",
            )
        return (
            f"Environment-stage Claude Code selected {title}; real data/loader contract for {repo_name} is not evidence-ready.",
            "Resolve the selected base's real dataset files and loader/import probes before training, paper writing, or claim promotion. " + legacy,
            "fresh paper base lacks loader-ready real data",
        )
    if category == "blocked_fresh_base_reference_probe_required":
        if zh:
            return (
                f"{title} 的数据/loader 已就绪；参考协议/环境 manifest 探针仍未通过",
                "运行当前基底的有界只读参考协议/环境探针；通过前不训练、不写论文、不提升 claim。" + legacy,
                "fresh base reference protocol/env manifest is not audited",
            )
        return (
            f"Data/loader is ready for {title}; reference protocol/env manifest probe is still required.",
            "Run bounded read-only reference-protocol/env probes for the selected base before training, paper writing, or claim promotion. " + legacy,
            "fresh base reference protocol/env manifest is not audited",
        )
    if category == "blocked_fresh_base_reference_smoke_required":
        if zh:
            return (
                f"{title} 的参考协议探针已通过；有界 no-training reference smoke/audit 仍未通过",
                "运行当前基底的有界 no-training reference smoke/audit；通过前不完整训练、不写论文、不提升 claim。" + legacy,
                "fresh base bounded reference smoke is not audited",
            )
        return (
            f"Reference protocol passed for {title}; bounded no-training reference smoke/audit is still required.",
            "Run bounded no-training reference smoke/audit for the selected base before full training, paper writing, or claim promotion. " + legacy,
            "fresh base bounded reference smoke is not audited",
        )
    if category == "blocked_fresh_base_reference_reproduction_required":
        if zh:
            return (
                f"{title} 的 bounded audit 已通过；论文级 full reference reproduction 仍需完成或监督",
                "继续监督当前基底的受审计 full reference reproduction；通过前不写论文、不提升 claim。" + legacy,
                "fresh base reference reproduction is not audited",
            )
        return (
            f"Bounded audit passed for {title}; paper-level full reference reproduction is still required or running.",
            "Continue audited full reference reproduction for the selected base before paper writing or claim promotion. " + legacy,
            "fresh base reference reproduction is not audited",
        )
    if zh:
        return (
            f"环境阶段 Claude Code 已选择 {title}；仍需补齐代码/实现/数据协议",
            "继续找官方代码/产物，或在当前项目内实现该基底并建立真实数据/协议证据；通过前不训练、不写论文、不提升 claim。" + legacy,
            "fresh paper base needs code/data/protocol implementation route",
        )
    return (
        f"Environment-stage Claude Code selected {title}; code/artifact search or implementation route is still required.",
        "Continue official-code/artifact search or implement the selected base with real data/protocol evidence before experiments or paper writing. " + legacy,
        "fresh paper base needs code/data/protocol implementation route",
    )


def current_impl_repo_path(paths) -> str:
    impl = read_json(paths.state / "fresh_base_implementation_plan.json", {})
    repo = impl.get("repo", {}) if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    return str(repo.get("repo_path") or repo.get("local_path") or repo.get("path") or "").strip()


def artifact_matches_current_repo(paths, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    current_repo = current_impl_repo_path(paths)
    if not current_repo:
        return False
    for key in ["repo_path", "active_repo_path", "local_path", "path"]:
        value = str(payload.get(key) or "").strip()
        if value and value != current_repo:
            return False
    return True


def fresh_base_reference_protocol_passed(paths) -> bool:
    for name in fresh_base_state_names(paths, "reference_protocol_probe"):
        probe = read_json(paths.state / name, {})
        if artifact_matches_current_repo(paths, probe):
            return bool(probe.get("status") == "reference_protocol_probe_passed" and probe.get("decision") == "ready_for_bounded_reference_smoke")
    return False


def fresh_base_reference_smoke_passed(paths) -> bool:
    for name in fresh_base_state_names(paths, "reference_smoke"):
        smoke = read_json(paths.state / name, {})
        if artifact_matches_current_repo(paths, smoke):
            return bool(smoke.get("status") == "reference_smoke_passed" and smoke.get("decision") == "ready_for_reference_reproduction_audit")
    return False


def fresh_base_reference_audit_recorded(paths) -> bool:
    for name in fresh_base_state_names(paths, "reference_reproduction_audit"):
        audit = read_json(paths.state / name, {})
        if artifact_matches_current_repo(paths, audit):
            return bool(audit.get("mode") == "bounded" and audit.get("return_code") == 0 and audit.get("audit_ready"))
    return False


def project_probe_python(project: str) -> str:
    paths = build_paths(project)
    cfg = read_json(paths.config, {})
    return project_experiment_python_from_config(cfg if isinstance(cfg, dict) else {}, fallback_to_current=True)



class FullCycle:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.paths = build_paths(args.project)
        self.state_path = self.paths.state / "full_research_cycle.json"
        self.report_path = self.paths.reports / "full_research_cycle.md"
        self.state: dict[str, Any] = read_json(self.state_path, {})
        if not isinstance(self.state, dict):
            self.state = {}
        self.state.update(
            {
                "project": args.project,
                "topic": args.topic,
                "venue": args.venue,
                "title": args.title,
                "max_cycles": args.max_cycles,
                "iterations_per_cycle": args.iterations_per_cycle,
                "trajectory_rounds": args.trajectory_rounds,
                "status": "running",
                "started_at": self.state.get("started_at") or now_iso(),
                "updated_at": now_iso(),
                "principle": (
                    "This supervisor orchestrates TASTE, Claude Code, trajectory memory, "
                    "evidence gates, and TASTE paper production. It never fabricates research "
                    "content or marks a paper complete unless local gates pass."
                ),
            }
        )
        # A fresh full-cycle must not keep terminal summaries/blockers from a
        # previous run; those stale fields are surfaced by the web compact API.
        if getattr(args, "fresh_start", False) or getattr(args, "force_discovery", False):
            for key in [
                "summary",
                "summary_zh",
                "summary_en",
                "latest_blocker",
                "current_blocker",
                "continuation_reason",
            ]:
                self.state.pop(key, None)
            self.state["continuation_required"] = False
        self.cleterminal_timestamps_if_running()
        self.state.setdefault("cycles", [])
        self.state["full_cycle_job"] = self.live_full_cycle_job()

    def live_full_cycle_job(self) -> dict[str, Any]:
        status = str(self.state.get("status") or "running").strip() or "running"
        command = redact_secrets(" ".join(str(part) for part in [sys.executable, *sys.argv]))
        running_stage = self.state.get("current_running_stage") if isinstance(self.state.get("current_running_stage"), dict) else {}
        log_path = ""
        try:
            stdout_target = os.readlink(f"/proc/{os.getpid()}/fd/1")
            if stdout_target.startswith("/"):
                log_path = stdout_target
        except Exception:
            pass
        if not log_path:
            existing_job = self.state.get("full_cycle_job") if isinstance(self.state.get("full_cycle_job"), dict) else {}
            log_path = str(self.state.get("log_path") or existing_job.get("log_path") or existing_job.get("stdout_path") or "")
        persisted_job = read_json(self.paths.state / "full_cycle_job.json", {})
        if not isinstance(persisted_job, dict):
            persisted_job = {}
        if not log_path and str(persisted_job.get("pid") or "") == str(os.getpid()):
            log_path = str(persisted_job.get("log_path") or persisted_job.get("stdout_path") or "")
        job = {
            "project": self.args.project,
            "venue": self.args.venue,
            "status": "running" if status == "running" else status,
            "pid": os.getpid(),
            "cmd": command,
            "command": command,
            "process_alive": status == "running",
            "alive": status == "running",
            "kind": "full_cycle",
            "stage": running_stage.get("stage") or self.state.get("current_stage") or self.state.get("latest_stage") or "full-cycle",
            "started_at": self.state.get("started_at") or now_iso(),
            "updated_at": now_iso(),
            "fresh_start": bool(getattr(self.args, "fresh_start", False)),
            "max_cycles": self.args.max_cycles,
            "iterations_per_cycle": self.args.iterations_per_cycle,
            "trajectory_rounds": self.args.trajectory_rounds,
        }
        if log_path:
            job["log_path"] = log_path
            job["stdout_path"] = log_path
        if str(persisted_job.get("pid") or "") == str(os.getpid()):
            for key in ["web_job_id", "fresh_start", "force_discovery", "use_existing_literature_packet"]:
                if persisted_job.get(key) not in (None, ""):
                    job[key] = persisted_job.get(key)
        child_pid = running_stage.get("pid")
        if child_pid:
            job["child_pid"] = child_pid
            job["child_stage"] = running_stage.get("stage") or ""
        return job

    @staticmethod
    def public_phase_from_stage(stage: Any) -> str:
        raw_stage = str(stage or "full-cycle")
        lowered = raw_stage.lower().replace("_", "-")
        gate_precheck_markers = [
            "paper-evidence-audit-precheck",
            "submission-readiness-precheck",
            "trajectory-evidence-refresh",
            "blocker-action-plan-precheck",
        ]
        if any(marker in lowered for marker in gate_precheck_markers):
            return "experiment"
        if any(token in lowered for token in ["autonomous", "experiment", "trajectory", "training", "repair", "blocker"]):
            return "experiment"
        if any(token in lowered for token in ["paper", "latex", "conference-preview"]):
            return "paper"
        environment_literature_markers = [
            "sync-outputs", "literature-sync", "literature-tool-packet", "build-literature-tool-packet",
            "fresh-research-base-selection", "research-base-selection", "base-selection", "base-candidate",
            "literature-base-candidate", "literature-base-audit", "method-stack-sync",
        ]
        if any(marker in lowered for marker in environment_literature_markers):
            return "environment"
        fresh_find_markers = ["literature-survey", "run-finding", "run-driver", "run-literature-tool"]
        if any(marker in lowered for marker in fresh_find_markers) or lowered in {"find", "literature", "finding"}:
            return "find"
        if any(token in lowered for token in ["reference", "environment", "loader", "smoke"]):
            return "environment"
        if "plan" in lowered:
            return "plan"
        if "ideation" in lowered or "idea" in lowered:
            return "idea"
        if "read" in lowered:
            return "read"
        return "experiment"

    def run_experiment_watchdog(self) -> dict[str, Any]:
        watchdog = SCRIPTS / "experiment_run_watchdog.py"
        if not watchdog.exists():
            return {}
        try:
            proc = subprocess.run(
                [sys.executable, str(watchdog), "--project", self.args.project],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=45,
            )
        except Exception as exc:
            payload = {"status": "error", "error": str(exc), "updated_at": now_iso()}
            self.state["experiment_run_watchdog"] = payload
            return payload
        try:
            payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except Exception:
            payload = {"status": "error", "return_code": proc.returncode, "stdout_tail": proc.stdout[-1200:], "stderr_tail": proc.stderr[-1200:]}
        self.state["experiment_run_watchdog"] = {
            "status": payload.get("status", "unknown") if isinstance(payload, dict) else "unknown",
            "active_run_count": payload.get("active_run_count", 0) if isinstance(payload, dict) else 0,
            "issues": (payload.get("issues", [])[:5] if isinstance(payload, dict) and isinstance(payload.get("issues"), list) else []),
            "updated_at": now_iso(),
        }
        if isinstance(payload, dict) and payload.get("issues"):
            blockers = self.state.get("runtime_blockers") if isinstance(self.state.get("runtime_blockers"), list) else []
            blockers.append({
                "stage": "experiment-run-watchdog",
                "issue": "Experiment artifact contract violation detected; contaminated artifacts must not be imported.",
                "details": payload.get("issues", [])[:5],
                "updated_at": now_iso(),
            })
            self.state["runtime_blockers"] = blockers[-20:]
        return payload

    def sync_live_full_cycle_job_state(self) -> None:
        self.run_experiment_watchdog()
        job = self.live_full_cycle_job()
        self.state["full_cycle_job"] = job
        if str(self.state.get("status") or "") == "running" and job.get("process_alive") is True:
            raw_stage = str(job.get("stage") or "full-cycle")
            phase = self.public_phase_from_stage(raw_stage)
            active_experiments = self.running_experiment_processes()
            if active_experiments:
                phase = "experiment"
                self.state["active_experiment_processes"] = active_experiments[:8]
            public_stage = "find" if phase == "literature" else phase
            summary = f"完整科研自循环正在运行；阶段={public_stage}；PID={job.get('pid') or '-'}。"
            self.state["summary"] = summary
            self.state["summary_zh"] = summary
            self.state["public_phase"] = public_stage
        write_json(self.paths.state / "full_cycle_job.json", job)
        tick_path = self.paths.state / "supervision_tick.json"
        tick = read_json(tick_path, {})
        if isinstance(tick, dict):
            tick["full_cycle_job"] = job
            tick.setdefault("project", self.args.project)
            tick.setdefault("venue", self.args.venue)
            if str(self.state.get("status") or "") == "running":
                tick["status"] = "running"
            tick["generated_at"] = now_iso()
            write_json(tick_path, tick)

    def enforce_selected_base_route_guard(self, stage: str = "") -> dict[str, Any]:
        try:
            report = guard_selected_base_route(self.args.project, source_stage=stage or "run_full_research_cycle")
        except Exception as exc:
            report = {"status": "error", "source_stage": stage, "error": str(exc), "repaired": False}
            blockers = self.state.setdefault("runtime_blockers", [])
            if isinstance(blockers, list):
                blockers.append({
                    "stage": stage or "selected-base-route-guard",
                    "issue": f"selected-base route guard failed: {exc}",
                    "updated_at": now_iso(),
                })
                self.state["runtime_blockers"] = blockers[-20:]
        if isinstance(report, dict) and report.get("repaired"):
            self.state["selected_base_route_guard"] = report
            blockers = self.state.setdefault("runtime_blockers", [])
            if isinstance(blockers, list):
                blockers.append({
                    "stage": stage or "selected-base-route-guard",
                    "issue": "Legacy/control route attempted to overwrite the current selected-base identity; restored from trusted full reference reproduction audit.",
                    "updated_at": now_iso(),
                    "violations": report.get("violations", []),
                })
                self.state["runtime_blockers"] = blockers[-20:]
        return report if isinstance(report, dict) else {}

    def normalize_reference_pass_state(self) -> None:
        self.enforce_selected_base_route_guard("save")
        reference_gate = read_json(self.paths.state / "reference_reproduction_gate.json", {})
        audits = [read_json(self.paths.state / name, {}) for name in fresh_base_state_names(self.paths, "reference_reproduction_audit")]
        reference_passed = bool(
            isinstance(reference_gate, dict)
            and reference_gate.get("status") == "pass"
            and reference_gate.get("decision") == "continue_base"
        ) or any(
            isinstance(audit, dict)
            and artifact_matches_current_repo(self.paths, audit)
            and audit.get("mode") == "full"
            and audit.get("return_code") == 0
            and audit.get("audit_ready")
            and audit.get("paper_level_reproduction_passed")
            for audit in audits
        )
        if not reference_passed:
            return
        stale_prefixes = (
            "fresh_base_",
            "fresh_literature_base_audit",
            "terminal_reference_base_block",
            "reference_reproduction_gate",
            "literature_to_base_route",
            "environment_anchor_selection_required",
            "base_selection_blocked",
        )
        stale_text_markers = (
            "environment-stage claude code anchor selection exists for this run",
            "previous active_repo/reference-reproduction state must not satisfy",
            "active reference base is still blocked",
            "reference base is still blocked",
            "paper-level full reference reproduction is still required",
            "full reference reproduction is still required",
            "paper_evidence_audit",
            "hold-markdown-only",
            "submission_readiness",
            "scientific_progress_gate",
            "no audit-ready promotable",
            "non-promotable candidates",
            "research_assurance_layer",
            "research_evidence_manifest",
            "best reference reproduction",
            "selected_base_reference_full_",
            "base_switch_execution",
            "base_switch_gate status=",
        )
        stale_gate_categories = {
            "experiment_evidence_gate",
            "scientific_progress_gate",
            "submission_readiness",
            "paper_evidence_audit",
            "research_assurance_layer",
            "research_evidence_manifest",
            "base_switch_gate",
            "base_switch_execution",
        }

        def stale_reference_blocker(row: Any) -> bool:
            if not isinstance(row, dict):
                return False
            category = str(row.get("category") or "")
            issue = str(row.get("issue") or row.get("human_summary") or row.get("summary") or "")
            issue_l = issue.lower()
            category_l = category.lower()
            return (
                category.startswith(stale_prefixes)
                or category_l in stale_gate_categories
                or any(marker in issue_l for marker in stale_text_markers)
            )

        current = self.state.get("current_blocker") if isinstance(self.state.get("current_blocker"), dict) else {}
        if stale_reference_blocker(current):
            self.state.pop("current_blocker", None)
        latest = self.state.get("latest_blockers", []) if isinstance(self.state.get("latest_blockers", []), list) else []
        retained = [row for row in latest if not stale_reference_blocker(row)]
        evidence_blocker = {
            "category": "selected_base_experiment_evidence_required",
            "severity": "block",
            "issue": "参考复现已通过；当前缺少当前主线下可审计、可写入论文的 项目目标候选实验。",
            "human_summary": "参考复现已通过；当前主线下一步是由 project agent 继续真实候选实验迭代，而不是写论文或切回旧路线。",
            "next_action": "继续在当前选中基底下设计、运行并审计真实项目目标候选实验；产出 artifact-local audit 和 experiment_registry 后刷新 scientific_progress、paper_evidence、submission_readiness。",
            "evidence": [
                str(self.paths.state / "reference_reproduction_gate.json"),
                str(self.paths.state / "scientific_progress_gate.json"),
                str(self.paths.state / "blocker_action_plan.json"),
            ],
        }
        if not any(isinstance(row, dict) and row.get("category") == evidence_blocker["category"] for row in retained):
            retained.insert(0, evidence_blocker)
        self.state["latest_blockers"] = retained[:8]
        for key, value in (("status", "running"), ("full_status", "reference_reproduction_passed")):
            current_value = str(self.state.get(key) or "")
            if current_value.startswith("blocked_fresh_base_") or current_value in {"blocked_literature_base_audit_required", "blocked_no_viable_base_switch_route"}:
                self.state[key] = value
        self.state["reference_reproduction_passed"] = True
        self.state["reference_gate_status"] = "pass"
        self.state["reference_gate_decision"] = "continue_base"
        self.state["reference_base_switch_required"] = False
        self.state["reference_base_switch_exhausted"] = False
        if str(self.state.get("status") or "") != "blocked_selected_base_viability_gate":
            self.state["continuation_required"] = False
            self.state["continuation_reason"] = ""

    def cleterminal_timestamps_if_running(self) -> None:
        if str(self.state.get("status") or "").lower() == "running":
            self.state.pop("finished_at", None)
            self.state.pop("completed_at", None)

    def terminal_summary(self) -> str:
        status = str(self.state.get("status") or "").strip()
        latest = self.state.get("latest_step") if isinstance(self.state.get("latest_step"), dict) else {}
        phase = str(latest.get("phase") or self.state.get("public_phase") or "experiment")
        if status == "blocked_after_max_cycles":
            blocker = ""
            current = self.state.get("current_blocker") if isinstance(self.state.get("current_blocker"), dict) else {}
            latest_blockers = self.state.get("latest_blockers") if isinstance(self.state.get("latest_blockers"), list) else []
            source = current or (latest_blockers[0] if latest_blockers and isinstance(latest_blockers[0], dict) else {})
            if source:
                blocker = str(source.get("human_summary") or source.get("summary") or source.get("issue") or "").strip()
            if not blocker:
                blocker = "参考复现已通过，但当前主线还缺少可审计、可写入论文的候选实验证据。"
            return f"完整科研自循环已停在{phase}门控；没有正在运行的 full-cycle。{blocker}"
        if status.startswith("blocked_") or status in {"blocked", "stale_full_research_cycle_snapshot"}:
            return f"完整科研自循环已停止；当前状态={status}；没有正在运行的 full-cycle。"
        if status == "completed":
            return "完整科研自循环已完成，论文与证据门控已通过。"
        return str(self.state.get("summary_zh") or self.state.get("summary") or "")

    def refresh_terminal_summary_if_needed(self) -> None:
        status = str(self.state.get("status") or "").strip()
        if status == "running":
            return
        current = str(self.state.get("summary_zh") or self.state.get("summary") or "")
        stale_running = current.startswith("完整科研自循环正在运行") or ("PID=" in current and "正在运行" in current)
        if status in {"blocked_after_max_cycles", "completed", "blocked", "stale_full_research_cycle_snapshot"} or status.startswith("blocked_") or stale_running:
            summary = self.terminal_summary()
            if summary:
                self.state["summary"] = summary
                self.state["summary_zh"] = summary

    def save(self) -> None:
        self.normalize_reference_pass_state()
        self.cleterminal_timestamps_if_running()
        self.sync_live_full_cycle_job_state()
        self.refresh_terminal_summary_if_needed()
        self.state["updated_at"] = now_iso()
        write_json(self.state_path, self.state)
        self.write_report()

    def log(self, message: str) -> None:
        clean = redact_secrets(message)
        print(clean, flush=True)
        append_agent_log(self.args.project, "main", clean)

    def set_current_cycle_record(self, cycle: dict[str, Any]) -> None:
        self.state["current_cycle_record"] = cycle

    def clecurrent_cycle_record(self) -> None:
        self.state.pop("current_cycle_record", None)

    def record_stage_progress(
        self,
        *,
        stage: str,
        pid: int | None = None,
        started_at: str | None = None,
        line_count: int = 0,
        stdout_tail: list[str] | None = None,
        heartbeat: bool = False,
    ) -> None:
        self.state["status"] = "running"
        clean_tail = [redact_secrets(line) for line in (stdout_tail or [])]
        self.state["current_running_stage"] = {
            "stage": stage,
            "pid": pid,
            "started_at": started_at or "",
            "line_count": line_count,
            "last_heartbeat_at": now_iso(),
            "stdout_tail": "\n".join(clean_tail[-20:])[-4000:],
            "heartbeat": heartbeat,
        }
        self.state["latest_step"] = {
            "cycle": self.state.get("current_cycle"),
            "stage": stage,
            "status": "running",
            "pid": pid,
            "line_count": line_count,
        }
        cycle = self.state.get("current_cycle_record")
        if isinstance(cycle, dict):
            cycle["status"] = "running"
            cycle["updated_at"] = now_iso()
            running_steps = cycle.setdefault("running_steps", {})
            if isinstance(running_steps, dict):
                running_steps[stage] = dict(self.state["current_running_stage"])
            self.state["current_cycle_record"] = cycle
        self.save()

    def terminate_process_group(self, proc: subprocess.Popen[str]) -> None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except Exception:
                proc.kill()
            proc.wait()

    def non_error_return_code(self, stage: str, return_code: int) -> bool:
        if return_code == 0:
            return True
        # Several audit/build scripts intentionally return 2 to mean "gate blocked".
        # That is a scientific state, not a pipeline/runtime failure.
        if return_code == 2 and (
            "audit" in stage
            or "readiness" in stage
            or "blocker-action-plan" in stage
            or "base-switch-gate" in stage
            or "trajectory-e2e-verify" in stage
            or "current-find" in stage
        ):
            return True
        return False

    def run(self, cmd: list[str], *, stage: str, required: bool = False, timeout: int | None = None) -> dict[str, Any]:
        self.log("full-cycle: running " + " ".join(str(item) for item in cmd))
        upsert_agent(
            self.args.project,
            "main",
            name="完整科研自循环",
            role="main",
            stage="full-cycle",
            status="running",
            goal=(self.args.topic or self.args.title or self.args.project)[:500],
            command=cmd,
            current_step=f"{stage}: started",
        )
        started = now_iso()
        stdout_tail: list[str] = []
        timed_out = False
        return_code = 1
        line_count = 0
        last_state_write = 0.0
        last_log_heartbeat = 0.0
        try:
            env = os.environ.copy()
            if "--skip-discovery" in {str(part) for part in cmd}:
                # Reuse the validated current Find packet for normal discovery, but do
                # not create a global Find lock. Literature shortfall repair must
                # still be able to launch modules/finding/scripts/run_literature_tool.py unless a
                # caller explicitly sets DISABLE_NEW_FIND=1 or --record-only.
                env.setdefault("USE_EXISTING_LITERATURE_PACKET", "1")
            if stage == "autonomous-research":
                env.setdefault("ARXIV_TIMEOUT_SEC", "12")
                env.setdefault("DISCOVER_RETRIES", "1")
                env.setdefault("SEMANTIC_SCHOLAR_TIMEOUT_SEC", "20")
            if stage == "trajectory-supervisor":
                env["TRAJECTORY_SUPERVISOR_ENTRY"] = "full_cycle"
                env["TRAJECTORY_SUPERVISOR_PARENT_PID"] = str(os.getpid())
            if stage == "literature-survey":
                env.setdefault("YEARS", str(dt.datetime.now(dt.timezone.utc).year))
                env.setdefault("VENUE_IDS", "openreview_iclr_2026,openreview_neurips,dblp_icml,dblp_kdd")
                env.setdefault("WINDOW_DAYS", "180")
                env.setdefault("ARXIV_FULL_SCAN", "1")
                env.setdefault("ARXIV_MAX_QUERIES", "3")
                env.setdefault("ARXIV_PER_QUERY_LIMIT", "100")
                env.setdefault("ARXIV_TIMEOUT_SEC", "45")
            proc = subprocess.Popen(
                cmd,
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
            upsert_agent(self.args.project, "main", pid=proc.pid, status="running", current_step=f"{stage}: subprocess pid {proc.pid}")
            assert proc.stdout is not None
            deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
            self.record_stage_progress(stage=stage, pid=proc.pid, started_at=started, stdout_tail=stdout_tail)
            try:
                while True:
                    now = time.monotonic()
                    if deadline is not None and time.monotonic() > deadline:
                        timed_out = True
                        stdout_tail.append(f"full-cycle: stage {stage} timed out after {timeout}s; terminating and continuing with gates")
                        self.log(stdout_tail[-1])
                        self.terminate_process_group(proc)
                        return_code = 124
                        break
                    ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                    if not ready:
                        if proc.poll() is not None:
                            break
                        if now - last_state_write >= 15:
                            self.record_stage_progress(
                                stage=stage,
                                pid=proc.pid,
                                started_at=started,
                                line_count=line_count,
                                stdout_tail=stdout_tail,
                                heartbeat=True,
                            )
                            last_state_write = now
                        if now - last_log_heartbeat >= 120:
                            # Keep heartbeat state machine-readable without flooding the human job log.
                            self.record_stage_progress(
                                stage=stage,
                                pid=proc.pid,
                                started_at=started,
                                line_count=line_count,
                                stdout_tail=stdout_tail,
                                heartbeat=True,
                            )
                            last_log_heartbeat = now
                        continue
                    line = proc.stdout.readline()
                    if not line:
                        if proc.poll() is not None:
                            break
                        continue
                    text = line.rstrip()
                    if not text:
                        continue
                    line_count += 1
                    text = redact_secrets(text)
                    stdout_tail.append(text)
                    stdout_tail = stdout_tail[-80:]
                    self.log(text)
                    if line_count % 10 == 0 or now - last_state_write >= 10:
                        self.record_stage_progress(
                            stage=stage,
                            pid=proc.pid,
                            started_at=started,
                            line_count=line_count,
                            stdout_tail=stdout_tail,
                        )
                        last_state_write = now
            except KeyboardInterrupt:
                self.terminate_process_group(proc)
                raise
            if return_code != 124:
                return_code = proc.wait()
        except FileNotFoundError as exc:
            stdout_tail.append(str(exc))
            return_code = 127
        result = {
            "stage": stage,
            "command": cmd,
            "started_at": started,
            "finished_at": now_iso(),
            "return_code": return_code,
            "timed_out": timed_out,
            "line_count": line_count,
            "stdout_tail": redact_secrets("\n".join(stdout_tail))[-12000:],
        }
        self.state.pop("current_running_stage", None)
        self.state["latest_step"] = {
            "cycle": self.state.get("current_cycle"),
            "stage": stage,
            "status": "finished",
            "return_code": return_code,
            "timed_out": timed_out,
        }
        if self.non_error_return_code(stage, return_code):
            result["gate_blocked_return_code"] = return_code != 0
        else:
            failures = self.state.setdefault("stage_failures", [])
            if isinstance(failures, list):
                failures.append(
                    {
                        "stage": stage,
                        "return_code": return_code,
                        "timed_out": timed_out,
                        "finished_at": result["finished_at"],
                        "tail": redact_secrets(result["stdout_tail"])[-4000:],
                    }
                )
                self.state["stage_failures"] = failures[-20:]
        if timed_out:
            timeout_blockers = self.state.setdefault("runtime_blockers", [])
            if isinstance(timeout_blockers, list):
                timeout_blockers.append(
                    {
                        "stage": stage,
                        "issue": f"{stage} exceeded {timeout}s and was terminated so the full cycle could continue.",
                        "updated_at": now_iso(),
                    }
                )
                self.state["runtime_blockers"] = timeout_blockers[-20:]
        guard_report = self.enforce_selected_base_route_guard(stage)
        if isinstance(guard_report, dict) and guard_report.get("repaired"):
            result["selected_base_route_guard"] = guard_report
            stdout_tail.append("selected-base route guard restored current route from trusted full reference reproduction audit")
            result["stdout_tail"] = redact_secrets("\n".join(stdout_tail))[-12000:]
        if required and not self.non_error_return_code(stage, return_code):
            self.state["status"] = "error"
            self.state["latest_error"] = result
            self.save()
            raise SystemExit(return_code)
        return result

    def authoritative_gate_context(self, stage: str) -> str:
        """Return compact hard-gate facts that must override stale Claude session memory."""
        target_state = recommendation_target_status(self.paths)
        full_text_gate = current_find_full_text_gate_status(self.paths)
        literature_gate = self.literature_gate_status()
        submission = read_json(self.paths.state / "submission_readiness.json", {})
        blocker_plan = read_json(self.paths.state / "blocker_action_plan.json", {})
        selected_base_viability = read_json(self.paths.state / "selected_base_viability_gate.json", {})
        base_switch_execution = read_json(self.paths.state / "base_switch_execution.json", {})
        current_plan = read_json(self.paths.state / "current_find_research_plan.json", {})
        selected_execution_contract = current_find_execution_contract(self.paths)
        packet = read_json(self.paths.state / "literature_tool_packet.json", {})
        current_run_id = target_state.get("run_id") or current_find_light_run_id(self.paths)
        project_cfg = read_json(self.paths.config, {})
        llm_cfg = project_cfg.get("llm", {}) if isinstance(project_cfg, dict) and isinstance(project_cfg.get("llm"), dict) else {}
        taste_cfg = read_json(CONFIG_PATH, {})
        if not isinstance(taste_cfg, dict):
            taste_cfg = {}
        api_key_env = str(llm_cfg.get("api_key_env") or taste_cfg.get("api_key_env") or os.environ.get("LLM_API_KEY_ENV") or "OPENAI_API_KEY")
        api_key_available = bool(str(llm_cfg.get("api_key") or taste_cfg.get("api_key") or os.environ.get("LLM_API_KEY") or os.environ.get(api_key_env, "")).strip())
        llm_config = {
            "provider": llm_cfg.get("provider"),
            "api_base": llm_cfg.get("api_base"),
            "model": llm_cfg.get("model"),
            "api_key_env": api_key_env,
            "api_key_available": api_key_available,
            "source": str(self.paths.config),
            "note": "Web-saved TASTE config or runtime env is authoritative for LLM access; project.json intentionally does not store raw API keys.",
        }
        plan_status = self.state.get("current_find_research_plan_status", {}) if isinstance(self.state.get("current_find_research_plan_status"), dict) else {}
        packet_summary = packet.get("summary", {}) if isinstance(packet, dict) and isinstance(packet.get("summary"), dict) else {}
        submission_checks = submission.get("checks", []) if isinstance(submission, dict) and isinstance(submission.get("checks"), list) else []
        failed_checks = [row.get("id") or row.get("name") or row.get("check") for row in submission_checks if isinstance(row, dict) and str(row.get("status") or "").lower() not in {"pass", "passed", "ok", "warning"}]
        active_experiment_processes = self.running_experiment_processes()
        env_selection = environment_selection_context(self.paths)
        current_route = fresh_base_route_context(self.paths, self.args.project)
        authoritative = {
            "stage": stage,
            "project": self.args.project,
            "target_venue": self.args.venue,
            "active_experiment_processes": active_experiment_processes[:8],
            "current_route": current_route,
            "environment_selection": env_selection,
            "current_find_run_id": current_run_id,
            "literature_gate": literature_gate,
            "recommendation_target": {
                "actual": target_state.get("actual"),
                "target": target_state.get("target"),
                "shortfall": target_state.get("shortfall"),
                "status": target_state.get("status"),
                "source_count": target_state.get("source_count"),
                "selection": target_state.get("selection"),
            },
            "current_find_downstream": {
                "status": current_plan.get("status") if isinstance(current_plan, dict) else "",
                "source": current_plan.get("source") if isinstance(current_plan, dict) else "",
                "readings": current_plan.get("current_find_reading_count") if isinstance(current_plan, dict) else plan_status.get("readings"),
                "ideas": current_plan.get("current_find_idea_count") if isinstance(current_plan, dict) else plan_status.get("ideas"),
                "plans": current_plan.get("current_find_plan_count") if isinstance(current_plan, dict) else plan_status.get("plans"),
                "selected_plan_id": selected_execution_contract.get("selected_plan_id"),
                "selected_idea_id": selected_execution_contract.get("selected_idea_id"),
                "selected_plan": selected_execution_contract.get("selected_plan") if isinstance(selected_execution_contract.get("selected_plan"), dict) else {},
                "selected_idea": selected_execution_contract.get("selected_idea") if isinstance(selected_execution_contract.get("selected_idea"), dict) else {},
                "selected_by": selected_execution_contract.get("selected_by"),
                "execution_policy": selected_execution_contract.get("execution_policy") if isinstance(selected_execution_contract.get("execution_policy"), dict) else {},
                "candidate_counts": selected_execution_contract.get("candidate_counts") if isinstance(selected_execution_contract.get("candidate_counts"), dict) else {},
                "full_text_gate": full_text_gate,
                "plan_status": plan_status,
            },
            "literature_packet_summary": {
                "status": packet.get("status") if isinstance(packet, dict) else "",
                "summary": packet_summary,
                "current_find_readings": packet_summary.get("current_find_readings"),
                "current_find_ideas": packet_summary.get("current_find_ideas"),
                "current_find_plans": packet_summary.get("current_find_plans"),
            },
            "submission_readiness": {
                "status": submission.get("status") if isinstance(submission, dict) else "",
                "submission_ready": submission.get("submission_ready") if isinstance(submission, dict) else False,
                "failed_checks": [str(item) for item in failed_checks if str(item or "").strip()][:20],
            },
            "blocker_action_plan": {
                "status": blocker_plan.get("status") if isinstance(blocker_plan, dict) else "",
                "summary": blocker_plan.get("summary") if isinstance(blocker_plan, dict) else {},
                "top_actions": (blocker_plan.get("actions", [])[:8] if isinstance(blocker_plan, dict) and isinstance(blocker_plan.get("actions"), list) else []),
            },
            "selected_base_viability_gate": selected_base_viability if isinstance(selected_base_viability, dict) else {},
            "base_switch_execution": base_switch_execution if isinstance(base_switch_execution, dict) else {},
            "llm_config": llm_config,
        }
        hard_lines = [
            "AUTHORITATIVE TASTE HARD-GATE CONTEXT. This block overrides stale Claude session memory and prior summaries.",
            "If these facts contradict earlier session text, the JSON below is correct.",
            "P0: LLM API availability must be judged from web-saved TASTE config or injected runtime env, not from a bare non-interactive shell env. project.json intentionally does not store raw API keys; do not ask to put secrets in bashrc.",
            "P0: Claude may repair a literature shortfall only through modules/finding/scripts/run_literature_tool.py. That wrapper may launch a controlled targeted Find unless DISABLE_NEW_FIND=1/--record-only is explicitly set; do not call raw finding-module commands or run duplicate concurrent Finds.",
            "P0: Before declaring any experiment interrupted, stopped, failed, or restarting it, inspect active_experiment_processes plus ps/proc status and the actual log mtime/tail. A live PID is authoritative over an incomplete epoch log.",
            "P0: If active_experiment_processes contains a matching finetune/main.py/exp_text_init/python-c training run, do not start another run with the same dataset, descriptor, semantic embedding path, objective, or artifact_dir. Wait for the live process to finish, then audit its final artifacts.",
            "P0 EXPERIMENT ARTIFACT CONTRACT: every new experiment must use one fresh unique artifact_dir under projects/<project>/artifacts, one stdout_stderr.log, and exactly one python worker. Never reuse an artifact_dir after a failed/contaminated launch.",
            "P0 EXPERIMENT LAUNCHER: future training launches must use the the launcher with the management Python, and the training argv after `--` must use the project experiment Python executable. Do not use system python, bare python3, conda run, raw nohup/python background jobs, or shell redirection. The launcher writes run_contract.json, run.lock, launcher.pid.json, stdout_stderr.log, python_executable, environment_contract, and expected_outputs, and rejects reused/contaminated artifact dirs.",
            "P0 REFERENCE PROBES: reference-protocol/import/env probes are experiment-environment checks. Repo imports and dependency probes must use the resolved project experiment Python (`EXPERIMENT_PYTHON`/`PROJECT_PYTHON`), never the Web management Python, cfg.python_executable, sys.executable, or bare python. If the project experiment Python lacks dependencies, record a dependency blocker instead of falling back to the management environment.",
            "P0 EXPERIMENT WATCHDOG: before declaring a run stopped or launching a replacement, inspect state/experiment_run_manifest.json and state/experiment_run_watchdog.json. If CONTAMINATED_DO_NOT_IMPORT.txt exists or stdout has NUL bytes, do not import that artifact; relaunch only with a new artifact_dir.",
            "P0: Never change LR/epochs or relaunch a partially complete run just because early metrics are weak or the log has not reached the final epoch. Such changes require a new planned experiment record after the current live run exits.",
            "P0 CURRENT-FIND SELECTED EXECUTION: Read/Idea/Plan may contain several candidate ideas/plans, but downstream environment, experiment, writing, and claim work must consume exactly one selected_plan_id chosen by the main Claude Code/human-supervised contract. Non-selected ideas/plans are backlog only.",
            "P0 SEMANTIC DATA PROVENANCE: do not create text/LLM/semantic evidence by heuristically remapping opaque user/item IDs to external metadata using rating/timestamp joins, popularity order, voting, sample-title plausibility, or similar guesses. Such mappings are evidence only when the source repo/dataset preserved the ID map, or when preprocessing is deterministically rerun from raw data with a saved auditable mapping and regenerated splits/control under the same protocol; otherwise record a truthful blocker or switch to a data route that preserves text-interaction identity.",
            f"P0 CURRENT ROUTE: use environment-stage selected repo only: {current_route.get('title') or 'environment-stage selected anchor'} / {current_route.get('repo_name') or current_route.get('repo_path') or 'environment-stage selected repo'}. Historical active_repo or legacy/control routes must not override this route.",
        ]
        if selected_execution_contract.get("required") and not selected_execution_contract.get("selected_plan_id"):
            hard_lines.append("P0 HARD STOP: current Find has idea/plan candidates but selected_plan_id is empty. Do not run environment, experiment, paper, or claim steps; ask wrapper/project Claude selection to rebuild state/current_find_research_plan.json and taste_plan_bridge.json with a selected plan.")
        if isinstance(selected_base_viability, dict) and selected_base_viability.get("decision") == "base_switch_gate_required":
            hard_lines.extend([
                "P0 SELECTED-BASE VIABILITY: decision=base_switch_gate_required means paper/claim promotion and automatic route switching are blocked. It is NOT authorization to switch route, edit active_repo/evidence_ready_repo_selection, run alternative-route experiments as main route, or promote legacy/control evidence.",
                "P0 CURRENT-ROUTE WORK ALLOWED: continue selected-base scientific-progress repair by designing, implementing, running, and auditing the smallest real project-target candidate experiment on the current selected repo/data/protocol.",
                "P0 FORBIDDEN: writing state/base_switch_execution.json as authorized, modifying active_repo/evidence_ready_repo_selection to any alternative or legacy/control repo, launching alternative-route training/smoke as the main route, or claiming the base switch has executed.",
            ])
        if isinstance(base_switch_execution, dict) and str(base_switch_execution.get("status") or "").startswith("authorized"):
            hard_lines.append("P0 INVALID STATE: state/base_switch_execution.json claims authorization, but selected_base_viability_gate is not an authorization gate. Treat it as invalid unless a dedicated deterministic base-switch approval artifact exists and passes.")
        if active_experiment_processes:
            hard_lines.append(
                "P0 ACTIVE EXPERIMENT: one or more real experiment processes are alive. Treat them as running; monitor logs only, do not relaunch duplicates or promote claims until they exit and artifact-local audits are refreshed."
            )
        if target_state.get("blocking"):
            hard_lines.extend([
                f"P0: Current Find recommended papers are {target_state.get('actual')}/{target_state.get('target')} with shortfall={target_state.get('shortfall')}. These recommendations must come from real abstracts scored by the LLM; do not treat the stricter claim-anchor count as a recommendation shortfall, and do not promote weak/unscored papers.",
                "P0 HARD STOP: While this recommendation shortfall remains, the only allowed actions are Find title+abstract scoring/packet repair actions: build_literature_tool_packet.py, run_literature_tool.py, assess_literature_base_candidates.py, audit_submission_readiness.py, and build_blocker_action_plan.py.",
                "P0 FORBIDDEN until the literature gate clears: repair_paper_orchestra_citations.py, revise_paper_citation_coverage.py, run_paper_pipeline.py, repair_paper_preview_loop.py, repair_paper_figures_loop.py, build_conference_preview_paper.py, figure repair, citation repair, paper writing, paper polishing, base promotion, experiment launch, and claim promotion.",
            ])
        if isinstance(submission, dict) and not bool(submission.get("submission_ready")):
            hard_lines.append("P0: submission_ready is false; paper writing/claim promotion remains blocked until evidence/readiness gates pass.")
            hard_lines.append("P0: In ideation/method-draft text, do not write unverified result language such as 实验表明/results show/bridges the gap/improves/outperforms unless the exact audited run already exists in experiment_registry and the claim ledger cites its artifact-local audit. Use hypothesis/planned evaluation/expected testable effect wording instead.")
        if full_text_gate.get("blocking"):
            hard_lines.extend([
                f"P0 HARD STOP: Read-stage full-text packet gate is blocked: readable packet entries {full_text_gate.get('full_text_reading_count')}/{full_text_gate.get('expected_recommendation_count')}, pending={full_text_gate.get('pending_full_text_reading_count')}.",
                "P0 ALLOWED ACTION ONLY: acquire or prove missing Read-stage full-text/PDF/HTML/page evidence, or let the Read-stage packet use an eligible same-run ranked replacement without rewriting Find outputs. Do not rerun Claude over unchanged missing evidence; do not select bases, launch experiments, write papers, repair citations, or promote claims until this gate passes.",
            ])
        required_idea_count = configured_max_ideas(self.args.project, default=5)
        if isinstance(current_plan, dict) and (safe_int(current_plan.get("current_find_idea_count"), 0) < required_idea_count or safe_int(current_plan.get("current_find_plan_count"), 0) < required_idea_count):
            hard_lines.append("P0: current Find Read/Idea/Plan downstream artifacts are incomplete; repair the research pipeline/output parsing before environment or paper promotion.")
        hard_lines.append(json.dumps(authoritative, ensure_ascii=False, indent=2, sort_keys=True)[:18000])
        return "\n\n".join(hard_lines)

    def claude(self, message: str, *, stage: str, agent_id: str = "main") -> dict[str, Any]:
        prompt_path = self.paths.state / f"full_cycle_prompt_{slug(stage)}.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        authoritative_context = self.authoritative_gate_context(stage)
        # Put hard route/gate facts first so stale Claude session memory or old
        # paper artifacts cannot dominate the turn before the guard context.
        prompt_path.write_text(authoritative_context + "\n\n---\n\n" + message.rstrip() + "\n\n---\n\n" + authoritative_context + "\n", encoding="utf-8")
        cmd = [
            sys.executable,
            str(SCRIPTS / "claude_project_session.py"),
            "--project",
            self.args.project,
            "--stage",
            stage,
            "--message-file",
            str(prompt_path),
            "--timeout-sec",
            str(self.args.claude_timeout_sec),
            "--agent-id",
            agent_id,
        ]
        if self.args.no_resume:
            cmd.append("--no-resume")
        before_guard_report = self.enforce_selected_base_route_guard(f"{stage}-before")
        if isinstance(before_guard_report, dict) and before_guard_report.get("repaired"):
            self.state["status"] = "blocked_selected_base_route_guard"
            self.state["current_goal"] = "selected-base route identity was restored before Claude execution; restart from deterministic current-route context"
            self.save()
            return {
                "stage": stage,
                "status": "blocked_selected_base_route_guard",
                "return_code": 2,
                "guarded_block": "selected_base_route_identity_restored",
                "selected_base_route_guard": before_guard_report,
                "stdout_tail": "selected-base route guard restored current route before Claude execution",
            }
        result = self.run(cmd, stage=stage, required=False, timeout=self.args.claude_timeout_sec + 900 if self.args.claude_timeout_sec > 0 else None)
        guard_report = self.enforce_selected_base_route_guard(stage)
        if isinstance(guard_report, dict) and guard_report.get("repaired"):
            result["selected_base_route_guard"] = guard_report
            result["return_code"] = 2
            result["guarded_block"] = "selected_base_route_identity_restored"
            self.state["status"] = "blocked_selected_base_route_guard"
            self.state["current_goal"] = "selected-base route identity was restored after Claude output; restart from deterministic current-route context"
            self.save()
        return result

    def queued_guidance_pending(self) -> bool:
        queue = read_json(self.paths.state / "guidance_queue.json", [])
        if not isinstance(queue, list):
            return False
        return any(isinstance(row, dict) and str(row.get("status") or "") == "queued" for row in queue)

    def guidance_checkin_prompt(self, cycle_index: int) -> str:
        return f"""
TASTE full research cycle {cycle_index}: consume queued human web guidance at a safe checkpoint and continue the real research loop.

This is not a separate Find and not a paper-writing permission. Read the current project state yourself, especially:
- state/full_research_cycle.json
- state/reference_reproduction_gate.json
- state/scientific_progress_gate.json
- state/experiment_iteration_audit.json
- state/blocker_action_plan.json
- state/guidance_queue.json
- artifacts/fresh_base_experiments/ and artifacts/fresh_base_reference_reproduction/

Required behavior:
1. Treat queued web guidance as user supervision for the current research project, not as evidence.
2. Verify the real process/log/gate state from files and commands before acting.
3. If scientific_progress_gate is blocked because no audit-ready proposed-method run exists, design or implement the smallest LLM-conditioned current-route candidate experiment that can be audited against the selected-base control.
4. Do not write or polish the paper while experiment evidence is blocked.
5. Update research state/reports through the existing scripts and leave explicit next commands/evidence.

Return concise Markdown with: Guidance Consumed, State Verified, Actions Taken, Remaining Blocker, Next Command.
""".strip()

    def active_repo_path(self) -> Path | None:
        env = environment_selection_context(self.paths)
        if not env.get("valid"):
            return None
        selected = env.get("selected", {}) if isinstance(env.get("selected"), dict) else {}
        for key in ["repo_path", "local_path", "path"]:
            text = str(selected.get(key) or "").strip()
            if text and Path(text).exists():
                return Path(text).resolve()
        active = read_json(self.paths.state / "active_repo.json", {})
        if isinstance(active, dict):
            active_run = str(active.get("selected_by") or active.get("fresh_find_run_id") or "")
            active_stage = str(active.get("selection_stage") or active.get("selected_by_stage") or "")
            if active_run == env.get("current_find_run_id") and active_stage == "environment_claude_code":
                for key in ["repo_path", "local_path", "path"]:
                    text = str(active.get(key) or "").strip()
                    if text and Path(text).exists():
                        return Path(text).resolve()
        return None

    def running_experiment_processes(self) -> list[dict[str, Any]]:
        """Detect project-owned real experiment processes, including launcher-contract jobs."""
        rows: list[dict[str, Any]] = []
        watchdog_payload = self.run_experiment_watchdog()
        if isinstance(watchdog_payload, dict) and int(watchdog_payload.get("active_run_count") or 0) > 0:
            process_by_pid = {
                int(row.get("pid")): row
                for row in watchdog_payload.get("processes", [])
                if isinstance(row, dict) and is_int(row.get("pid"))
            }
            seen: set[int] = set()
            for active in watchdog_payload.get("active_runs", []):
                if not isinstance(active, dict):
                    continue
                pids = active.get("process_pids") or active.get("worker_pids") or []
                for raw_pid in pids:
                    if not is_int(raw_pid):
                        continue
                    pid = int(raw_pid)
                    if pid in seen:
                        continue
                    seen.add(pid)
                    proc_row = process_by_pid.get(pid, {})
                    rows.append({
                        "pid": pid,
                        "ppid": proc_row.get("ppid"),
                        "elapsed_sec": proc_row.get("elapsed_sec"),
                        "pcpu": proc_row.get("pcpu"),
                        "pmem": proc_row.get("pmem"),
                        "command": proc_row.get("cmd") or "",
                        "cwd": proc_row.get("cwd") or "",
                        "artifact_dir": active.get("artifact_dir") or "",
                        "contract_status": active.get("contract_status") or active.get("status") or "",
                        "source": "experiment_run_watchdog",
                    })
            if rows:
                return rows

        project_root = str(self.paths.root)
        repo = self.active_repo_path()
        repo_text = str(repo) if repo else ""
        cmd = ["ps", "-eo", "pid,ppid,etimes,pcpu,pmem,cmd"]
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
        if proc.returncode != 0:
            return rows
        own_pid = os.getpid()
        for line in proc.stdout.splitlines()[1:]:
            parts = line.strip().split(None, 5)
            if len(parts) < 6 or not is_int(parts[0]):
                continue
            pid = int(parts[0])
            if pid == own_pid:
                continue
            command = parts[5]
            cwd_text = ""
            try:
                cwd_text = str(Path(f"/proc/{pid}/cwd").resolve())
            except Exception:
                cwd_text = ""
            hay = command.lower()
            if "grep" in hay or "run_full_research_cycle.py" in hay or "claude_project_session.py" in hay:
                continue
            project_owned = (
                project_root in command
                or project_root in cwd_text
                or (repo_text and (repo_text in command or repo_text in cwd_text))
            )
            experiment_like = (
                " main.py" in f" {command}"
                or "python3 -u main.py" in hay
                or "python -u main.py" in hay
                or "finetune.py" in hay
                or "finetune_llm" in hay
                or "exp_text_init" in hay
                or ("python" in hay and "--artifact_dir" in hay and "/artifacts/" in hay)
                or "run_real_repo_smoke.py" in hay
                or "run_active_repo_smoke.py" in hay
                or "run_project.py" in hay
            )
            if project_owned and experiment_like:
                rows.append(
                    {
                        "pid": pid,
                        "ppid": int(parts[1]) if is_int(parts[1]) else None,
                        "elapsed_sec": int(parts[2]) if is_int(parts[2]) else None,
                        "pcpu": parts[3],
                        "pmem": parts[4],
                        "command": command,
                        "cwd": cwd_text,
                    }
                )
        return rows

    def latest_experiment_log_tail(self, limit: int = 6000) -> str:
        logs = sorted(self.paths.root.glob("artifacts/**/stdout_stderr.log"), key=lambda path: path.stat().st_mtime if path.exists() else 0)
        if not logs:
            return ""
        return read_text(logs[-1], limit)[-limit:]

    def artifact_dirs_for_running_experiments(self, running: list[dict[str, Any]]) -> list[Path]:
        """Resolve running experiment processes to TASTE artifact directories.

        Long-running project jobs write metrics gradually.  The full-cycle wait
        loop must import those running artifacts into the registry so the web
        experiment table reflects real progress before the final audit.
        """
        out: list[Path] = []
        seen: set[str] = set()
        candidates: list[Path] = []
        try:
            candidates.extend(path for path in (self.paths.root / "artifacts").glob("**/run_contract.json") if path.is_file())
            candidates.extend(path for path in (self.paths.root / "artifacts").glob("**/launcher.pid.json") if path.is_file())
        except Exception:
            candidates = []
        by_pid: dict[int, Path] = {}
        for sidecar in candidates:
            payload = read_json(sidecar, {})
            if not isinstance(payload, dict):
                continue
            pid = payload.get("pid")
            if not is_int(pid):
                continue
            artifact = payload.get("artifact_dir") or sidecar.parent
            artifact_path = Path(str(artifact)).expanduser()
            if not artifact_path.is_absolute():
                artifact_path = (self.paths.root / artifact_path).resolve()
            by_pid[int(pid)] = artifact_path.resolve()
        for proc in running:
            pid = proc.get("pid") if isinstance(proc, dict) else None
            artifact = by_pid.get(int(pid)) if is_int(pid) else None
            if not artifact and isinstance(proc, dict):
                artifact_text = str(proc.get("artifact_dir") or "").strip()
                if artifact_text:
                    artifact = Path(artifact_text).expanduser().resolve()
            if not artifact and isinstance(proc, dict):
                command = str(proc.get("command") or "")
                match = re.search(r"--artifact[_-]dir(?:=|\s+)(\S+)", command)
                if match:
                    artifact = Path(match.group(1)).expanduser().resolve()
            if not artifact or not artifact.exists() or not artifact.is_dir():
                continue
            key = str(artifact)
            if key in seen:
                continue
            out.append(artifact)
            seen.add(key)
        return out

    def sync_running_experiment_records(self, running: list[dict[str, Any]]) -> dict[str, Any]:
        artifact_dirs = self.artifact_dirs_for_running_experiments(running)
        result: dict[str, Any] = {"updated_at": now_iso(), "artifact_dirs": [str(path) for path in artifact_dirs], "commands": []}
        if not artifact_dirs:
            result["status"] = "no_artifact_contract"
            return result
        commands: list[list[str]] = []
        importer = SCRIPTS / "import_experiment_artifacts.py"
        if importer.exists():
            for artifact in artifact_dirs:
                commands.append([sys.executable, str(importer), "--project", self.args.project, "--artifact-dir", str(artifact), "--allow-incomplete"])
        record_table = SCRIPTS / "build_experiment_record_table.py"
        if record_table.exists():
            commands.append([sys.executable, str(record_table), "--project", self.args.project])
        iteration_audit = SCRIPTS / "audit_experiment_iteration.py"
        if iteration_audit.exists():
            commands.append([sys.executable, str(iteration_audit), "--project", self.args.project])
        command_results: list[dict[str, Any]] = []
        for command in commands:
            try:
                proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=60)
                command_results.append({
                    "cmd": redact_secrets(" ".join(str(part) for part in command)),
                    "return_code": proc.returncode,
                    "stdout_tail": redact_secrets((proc.stdout or "")[-1200:]),
                    "stderr_tail": redact_secrets((proc.stderr or "")[-1200:]),
                })
            except Exception as exc:
                command_results.append({
                    "cmd": redact_secrets(" ".join(str(part) for part in command)),
                    "return_code": 125,
                    "error": str(exc),
                })
        failed = [row for row in command_results if int(row.get("return_code") or 0) not in {0, 1}]
        result["commands"] = command_results
        result["status"] = "error" if failed else "synced"
        return result

    def wait_for_background_experiments(self, stage: str, *, timeout_sec: int | None = None) -> dict[str, Any]:
        timeout = timeout_sec if timeout_sec is not None else self.args.background_experiment_timeout_sec
        started = time.monotonic()
        started_at = now_iso()
        checks: list[dict[str, Any]] = []
        while True:
            running = self.running_experiment_processes()
            sync_result = self.sync_running_experiment_records(running) if running else {"status": "idle", "updated_at": now_iso()}
            checks.append({"checked_at": now_iso(), "running": running[:8], "experiment_record_sync": sync_result})
            if not running:
                result = {
                    "stage": stage,
                    "started_at": started_at,
                    "finished_at": now_iso(),
                    "return_code": 0,
                    "timed_out": False,
                    "line_count": 0,
                    "background_experiments": [],
                    "checks": checks[-20:],
                    "stdout_tail": redact_secrets(self.latest_experiment_log_tail()),
                }
                self.state.pop("background_experiment_wait", None)
                self.state["latest_step"] = {"cycle": self.state.get("current_cycle"), "stage": stage, "status": "finished", "return_code": 0, "timed_out": False}
                self.save()
                return result
            elapsed = time.monotonic() - started
            self.state["status"] = "waiting_for_experiment"
            self.state["current_goal"] = "waiting for background real-data experiment to finish before auditing gates"
            self.state["background_experiment_wait"] = {
                "stage": stage,
                "started_at": started_at,
                "elapsed_sec": int(elapsed),
                "timeout_sec": timeout,
                "running": running[:8],
                "log_tail": redact_secrets(self.latest_experiment_log_tail(4000)),
                "experiment_record_sync": sync_result,
                "updated_at": now_iso(),
            }
            self.state["latest_step"] = {"cycle": self.state.get("current_cycle"), "stage": stage, "status": "waiting", "pid": running[0].get("pid"), "line_count": len(checks)}
            upsert_agent(
                self.args.project,
                "main",
                name="完整科研自循环",
                role="main",
                stage="full-cycle",
                status="running",
                goal=(self.args.topic or self.args.title or self.args.project)[:500],
                current_step=f"{stage}: waiting for {len(running)} background experiment process(es)",
            )
            self.save()
            if timeout and timeout > 0 and elapsed > timeout:
                result = {
                    "stage": stage,
                    "started_at": started_at,
                    "finished_at": now_iso(),
                    "return_code": 124,
                    "timed_out": True,
                    "line_count": len(checks),
                    "background_experiments": running[:8],
                    "checks": checks[-20:],
                    "stdout_tail": self.latest_experiment_log_tail(),
                }
                blockers = self.state.setdefault("runtime_blockers", [])
                if isinstance(blockers, list):
                    blockers.append(
                        {
                            "stage": stage,
                            "issue": f"Background experiment still running after {timeout}s; gates were not re-audited against incomplete output.",
                            "updated_at": now_iso(),
                            "running": running[:8],
                        }
                    )
                    self.state["runtime_blockers"] = blockers[-20:]
                self.save()
                return result
            time.sleep(max(10, min(120, int(self.args.background_experiment_poll_sec))))

    def project_postprocess_commands(self) -> list[list[str]]:
        """Return project-local postprocess commands that can safely ingest completed artifacts."""
        commands: list[list[str]] = []
        project_scripts = self.paths.root / "scripts"
        watchdog = SCRIPTS / "experiment_run_watchdog.py"
        if watchdog.exists():
            commands.append([sys.executable, str(watchdog), "--project", self.args.project])
        runtime_integrity = SCRIPTS / "audit_experiment_runtime_integrity.py"
        if runtime_integrity.exists():
            commands.append([sys.executable, str(runtime_integrity), "--project", self.args.project])
        importer = SCRIPTS / "import_experiment_artifacts.py"
        if importer.exists():
            commands.append([sys.executable, str(importer), "--project", self.args.project, "--scan-completed"])
        record_table = SCRIPTS / "build_experiment_record_table.py"
        if record_table.exists():
            commands.append([sys.executable, str(record_table), "--project", self.args.project])
        return commands

    def postprocess_and_refresh_experiment_gates(self, step_fn: Any, *, suffix: str = "") -> None:
        for index, command in enumerate(self.project_postprocess_commands(), start=1):
            step_fn(self.run(command, stage=f"experiment-postprocess{suffix}-{index}", required=False, timeout=900))
        for stage_name, script in [
            (f"reference-reproduction-gate{suffix}", "audit_reference_reproduction.py"),
            (f"experiment-iteration-audit{suffix}", "audit_experiment_iteration.py"),
            (f"experiment-runtime-integrity{suffix}", "audit_experiment_runtime_integrity.py"),
            (f"paper-evidence-audit{suffix}", "audit_paper_evidence.py"),
            (f"submission-readiness{suffix}", "audit_submission_readiness.py"),
            (f"selected-base-viability{suffix}", "audit_selected_base_viability.py"),
            (f"framework-content-coupling{suffix}", "audit_framework_content_coupling.py"),
            (f"obsolete-baseline-cleanup{suffix}", "audit_obsolete_baseline_cleanup.py"),
            (f"trajectory-refresh{suffix}", "build_research_trajectory_system.py"),
            (f"blocker-action-plan{suffix}", "build_blocker_action_plan.py"),
        ]:
            cmd = [sys.executable, str(SCRIPTS / script), "--project", self.args.project]
            venue_agnostic_scripts = {"audit_experiment_iteration.py", "audit_experiment_runtime_integrity.py", "audit_obsolete_baseline_cleanup.py"}
            if script == "build_research_trajectory_system.py" and self.args.venue:
                cmd.extend(["--venue", self.args.venue])
            elif script not in venue_agnostic_scripts:
                cmd.extend(["--venue", self.args.venue])
            step_fn(self.run(cmd, stage=stage_name, required=False, timeout=1800))
            if script == "audit_obsolete_baseline_cleanup.py":
                self.delegate_obsolete_baseline_cleanup_review_if_needed(step_fn, suffix=suffix)
                self.delegate_obsolete_baseline_cleanup_execution_if_needed(step_fn, suffix=suffix)

    def obsolete_baseline_cleanup_review_prompt(self, plan: dict[str, Any]) -> str:
        candidate_count = len(plan.get("blocked_candidate_paths") or []) if isinstance(plan.get("blocked_candidate_paths"), list) else 0
        preview = (plan.get("blocked_candidate_paths") or [])[:120] if isinstance(plan.get("blocked_candidate_paths"), list) else []
        return f"""
research project-file cleanup review is required for project `{self.args.project}`.

This is a project-context stewardship task, not a framework name-matching delete. Read the cleanup plan and the current project evidence yourself, then decide whether each candidate path is obsolete, shared evidence, current-route evidence, or still useful project context.

Required inputs to inspect:
- state/obsolete_baseline_cleanup_plan.json
- state/evidence_ready_repo_selection.json
- state/active_repo.json
- state/reference_reproduction_gate.json
- state/experiment_registry.json
- state/experiment_record_table.json
- state/base_switch_gate.json
- state/base_switch_execution.json
- reports/obsolete_baseline_cleanup_plan.md

Hard requirements:
1. Do not delete, archive, rename, or rewrite project files in this Claude turn.
2. Protect the current selected route and shared audit/evidence files.
3. If no cleanup should happen, write `state/obsolete_baseline_cleanup_review.json` with `cleanup_authorized=false`, `current_route_reviewed=true`, `protected_current_route=true`, `reviewed_candidate_count={candidate_count}`, `candidate_fingerprint` copied exactly from `state/obsolete_baseline_cleanup_plan.json`, and a rationale.
4. If cleanup is required, write `state/obsolete_baseline_cleanup_authorization.json` with `status=authorized_by_project_claude_review`, `cleanup_authorized=true`, `current_route_reviewed=true`, `protected_current_route=true`, exact `approved_candidate_paths`, exact `protected_paths`, and rationale. Do not execute cleanup in this review turn; The workflow will start a separate project Claude cleanup-execution turn.
5. Do not authorize by method names, dataset names, directory-name heuristics, or old memory. Authorize only exact paths after inspecting project evidence.
6. Do not change base-switch state, paper claims, experiment metrics, or current-route identity.

Cleanup plan summary:
```json
{json.dumps({
  "status": plan.get("status"),
  "decision": plan.get("decision"),
  "candidate_count": plan.get("candidate_count"),
  "candidate_fingerprint": plan.get("candidate_fingerprint"),
  "current_route": plan.get("current_route"),
  "base_switch_gate": plan.get("base_switch_gate"),
  "base_switch_execution": plan.get("base_switch_execution"),
  "candidate_preview": preview,
}, ensure_ascii=False, indent=2)[:16000]}
```

Return concise Markdown with: Files Reviewed, Decision, State File Written, Protected Paths, Remaining Cleanup Blocker.
""".strip()

    def delegate_obsolete_baseline_cleanup_review_if_needed(self, step_fn: Any, *, suffix: str = "") -> None:
        """Ask the project Claude Code session to review cleanup candidates when The workflow needs project context."""
        plan_path = self.paths.state / "obsolete_baseline_cleanup_plan.json"
        plan = read_json(plan_path, {})
        if not isinstance(plan, dict):
            return
        if plan.get("status") != "blocked_pending_project_review" or plan.get("decision") != "project_claude_review_required":
            return
        blocked = plan.get("blocked_candidate_paths")
        if not isinstance(blocked, list) or not blocked:
            return
        fingerprint = str(plan.get("candidate_fingerprint") or "")
        marker_path = self.paths.state / "obsolete_baseline_cleanup_review_request.json"
        marker = read_json(marker_path, {})
        if isinstance(marker, dict) and marker.get("candidate_fingerprint") == fingerprint and marker.get("status") in {"requested", "completed"}:
            review = read_json(self.paths.state / "obsolete_baseline_cleanup_review.json", {})
            auth = read_json(self.paths.state / "obsolete_baseline_cleanup_authorization.json", {})
            reviewed = isinstance(review, dict) and review.get("candidate_fingerprint") == fingerprint
            authorized = isinstance(auth, dict) and auth.get("cleanup_authorized") is True
            if reviewed or authorized:
                return
            if marker.get("status") == "requested":
                return
        request = {
            "status": "requested",
            "requested_at": now_iso(),
            "candidate_fingerprint": fingerprint,
            "candidate_count": len(blocked),
            "policy": "Project Claude Code must review cleanup candidates in project context; The workflow must not delete/archive by name matching.",
            "plan_path": str(plan_path),
        }
        write_json(marker_path, request)
        self.state["obsolete_baseline_cleanup_review_request"] = request
        self.state["current_goal"] = "project Claude Code reviewing obsolete project-file cleanup candidates"
        self.save()
        result = self.claude(
            self.obsolete_baseline_cleanup_review_prompt(plan),
            stage=f"obsolete-baseline-cleanup-review{suffix}",
            agent_id="cleanup-review",
        )
        request["status"] = "completed" if result.get("return_code") == 0 else "blocked"
        request["completed_at"] = now_iso()
        request["return_code"] = result.get("return_code")
        request["result_status"] = result.get("status")
        write_json(marker_path, request)
        self.state["obsolete_baseline_cleanup_review_request"] = request
        self.save()
        step_fn(result)
        step_fn(self.run(
            [sys.executable, str(SCRIPTS / "audit_obsolete_baseline_cleanup.py"), "--project", self.args.project],
            stage=f"obsolete-baseline-cleanup-after-project-review{suffix}",
            required=False,
            timeout=180,
        ))

    def obsolete_baseline_cleanup_execution_prompt(self, plan: dict[str, Any]) -> str:
        candidates = plan.get("candidate_cleanup_paths") or []
        preview = candidates[:120] if isinstance(candidates, list) else []
        return f"""
research project-file cleanup execution is required for project `{self.args.project}`.

This execution is owned by the project Claude Code session because only project-context code can decide how to safely remove or archive concrete research-route files. The framework will not move, delete, archive, or rewrite these project files for you.

Required inputs to inspect before acting:
- state/obsolete_baseline_cleanup_plan.json
- state/obsolete_baseline_cleanup_authorization.json
- state/evidence_ready_repo_selection.json
- state/active_repo.json
- state/reference_reproduction_gate.json
- state/experiment_registry.json
- state/experiment_record_table.json
- reports/obsolete_baseline_cleanup_plan.md

Hard requirements:
1. Execute cleanup only if `state/obsolete_baseline_cleanup_authorization.json` has `cleanup_authorized=true`, `status=authorized_by_project_claude_review`, `current_route_reviewed=true`, and `protected_current_route=true`.
2. Touch only exact paths listed in `candidate_cleanup_paths` and approved in `approved_candidate_paths`; protect every path listed in `protected_paths` plus current-route/shared evidence.
3. Prefer reversible project-local archival over deletion unless the project evidence clearly requires deletion.
4. Do not change base-switch state, paper claims, experiment metrics, or current-route identity.
5. After execution, write `state/obsolete_baseline_cleanup_execution.json` with `status=completed_by_project_claude`, `cleanup_executed=true`, `cleanup_authorized=true`, `current_route_reviewed=true`, `protected_current_route=true`, exact `applied_paths`, exact `remaining_candidate_paths`, `protected_paths`, and rationale. If you decide not to execute after re-inspection, write `state/obsolete_baseline_cleanup_review.json` with the fresh fingerprint and rationale instead.

Cleanup execution summary:
```json
{json.dumps({
  "status": plan.get("status"),
  "decision": plan.get("decision"),
  "candidate_count": plan.get("candidate_count"),
  "candidate_fingerprint": plan.get("candidate_fingerprint"),
  "current_route": plan.get("current_route"),
  "candidate_preview": preview,
  "project_agent_cleanup_authorization": plan.get("project_agent_cleanup_authorization"),
}, ensure_ascii=False, indent=2)[:16000]}
```

Return concise Markdown with: Files Rechecked, Cleanup Actions, State File Written, Protected Paths, Remaining Candidates.
""".strip()

    def delegate_obsolete_baseline_cleanup_execution_if_needed(self, step_fn: Any, *, suffix: str = "") -> None:
        """Ask project Claude Code to execute authorized cleanup; TASTE only audits the receipt."""
        plan_path = self.paths.state / "obsolete_baseline_cleanup_plan.json"
        plan = read_json(plan_path, {})
        if not isinstance(plan, dict):
            return
        if plan.get("status") != "pending_project_claude_cleanup_execution" or plan.get("decision") != "project_claude_cleanup_execution_required":
            return
        candidates = plan.get("candidate_cleanup_paths")
        if not isinstance(candidates, list) or not candidates:
            return
        fingerprint = str(plan.get("candidate_fingerprint") or "")
        marker_path = self.paths.state / "obsolete_baseline_cleanup_execution_request.json"
        marker = read_json(marker_path, {})
        if isinstance(marker, dict) and marker.get("candidate_fingerprint") == fingerprint and marker.get("status") in {"requested", "completed"}:
            execution = read_json(self.paths.state / "obsolete_baseline_cleanup_execution.json", {})
            if isinstance(execution, dict) and execution.get("cleanup_executed") is True:
                return
            if marker.get("status") == "requested":
                return
        request = {
            "status": "requested",
            "requested_at": now_iso(),
            "candidate_fingerprint": fingerprint,
            "candidate_count": len(candidates),
            "policy": "Project Claude Code must execute any authorized project-file cleanup itself; TASTE only audits the receipt.",
            "plan_path": str(plan_path),
        }
        write_json(marker_path, request)
        self.state["obsolete_baseline_cleanup_execution_request"] = request
        self.state["current_goal"] = "project Claude Code executing authorized obsolete project-file cleanup"
        self.save()
        result = self.claude(
            self.obsolete_baseline_cleanup_execution_prompt(plan),
            stage=f"obsolete-baseline-cleanup-execution{suffix}",
            agent_id="cleanup-execution",
        )
        request["status"] = "completed" if result.get("return_code") == 0 else "blocked"
        request["completed_at"] = now_iso()
        request["return_code"] = result.get("return_code")
        request["result_status"] = result.get("status")
        write_json(marker_path, request)
        self.state["obsolete_baseline_cleanup_execution_request"] = request
        self.save()
        step_fn(result)
        step_fn(self.run(
            [sys.executable, str(SCRIPTS / "audit_obsolete_baseline_cleanup.py"), "--project", self.args.project],
            stage=f"obsolete-baseline-cleanup-after-project-execution{suffix}",
            required=False,
            timeout=180,
        ))

    def idea_prompt(self, cycle_index: int) -> str:
        selected_base_viability = read_json(self.paths.state / "selected_base_viability_gate.json", {})
        blocker_plan = read_json(self.paths.state / "blocker_action_plan.json", {})
        selected_execution_contract = current_find_execution_contract(self.paths)
        return f"""
TASTE full research cycle {cycle_index}: refresh the research idea and execution route yourself.

Project: {self.args.project}
Topic: {self.args.topic or '(use project topic)'}
Venue: {self.args.venue}
Title: {self.args.title or '(TASTE may refine the working title in state)'}

Required behavior:
- Before choosing a route, read `planning/reference_workflow_and_claude_code.md`, `planning/literature_tool_packet.md` or `state/literature_tool_packet.json`, and at least one raw artifact in `planning/finding/` so the survey work is not wasted.
- If the current packet is stale, empty, or not specific enough for the current blocker, run `{management_python()} modules/finding/scripts/run_literature_tool.py --project {self.args.project} --query "<targeted query>" --fast-mode --venue {self.args.venue}`; use `--deep-survey` only when broad venue/arXiv coverage is necessary.
- Perform additional network-backed literature/repository search when Claude Code tools allow it; otherwise record the exact network/tool blocker.
- Use TASTE's native research-direction, evolutionary-memory, evidence-assurance, trajectory-optimization, and paper-production modules. Do not address those capabilities by external source-project names.
- Decide whether the current repo remains the best transformable route or whether The workflow should search/switch, based on evidence, not hard-coded topic gates.
- Decide whether the current conda environment should be reused, repaired, or replaced with a new project-specific env; do not delete existing envs.
- Update or respect research_landscape, novelty_map, failed_hypothesis_graph, unexplored_niche_graph, evolutionary memory, evidence manifest, and trajectory optimization queue.
- If you make edits, run local validation commands and leave evidence in research state/reports.
- Do not fabricate metrics, citations, data availability, experiments, or paper claims.
- Do not infer text/semantic item identity for opaque IDs from rating/timestamp joins, popularity order, voting, or sample-title plausibility; require a preserved ID map or a deterministic auditable preprocessing rerun that regenerates splits/control before launching semantic/LLM experiments.
- Literature signals can guide idea/base/code choices, but only local repo/data/experiment artifacts can support scientific claims.
- The main Claude Code session may keep multiple Read/Idea/Plan candidates visible, but it must select one best `selected_plan_id` for downstream execution. Non-selected ideas/plans are backlog only and must not drive environment, experiment, paper, or claim work.
- If `selected_plan_id` is empty while current Find has idea/plan candidates, stop after recording the selection blocker; do not invent or launch an experiment route.
- If `selected_base_viability_gate.decision` is `base_switch_gate_required`, treat candidate/alternative main-route launches, paper/claim promotion, and route execution as blocked until deterministic base-switch evidence passes. Current selected-base evidence repair may use `modules/experimenting/scripts/launch_experiment_run.py --route-scope selected_base_current_route`; bounded candidate gate evidence collection may use `--route-scope base_switch_evidence_collection`. Keep alternatives as proposals until the dedicated gate passes, then execute the switch only through `modules/environment/scripts/execute_authorized_base_switch.py`.

Current-Find selected execution contract:
```json
{json.dumps(selected_execution_contract, ensure_ascii=False, indent=2)[:12000]}
```

Selected-base viability gate:
```json
{json.dumps(selected_base_viability if isinstance(selected_base_viability, dict) else {}, ensure_ascii=False, indent=2)}
```

Current deterministic blocker action plan:
```json
{json.dumps(blocker_plan if isinstance(blocker_plan, dict) else {}, ensure_ascii=False, indent=2)[:12000]}
```

Return concise Markdown with: Idea/Route Decision, Evidence Inspected, Actions Taken, Remaining Blockers, Next TASTE Commands.
""".strip()

    def reference_reproduction_gate_blocked(self) -> tuple[bool, list[str], dict[str, Any]]:
        """A base work must be reproduced to paper-level evidence before TASTE deepens it."""
        gate = read_json(self.paths.state / "reference_reproduction_gate.json", {})
        if not isinstance(gate, dict) or not gate:
            return True, ["reference_reproduction_gate has not been generated yet"], {}
        if gate.get("status") == "pass" and gate.get("decision") == "continue_base":
            return False, [], gate
        blockers = gate.get("blockers", []) if isinstance(gate.get("blockers", []), list) else []
        reasons = [str(item) for item in blockers if str(item).strip()]
        if not reasons:
            reasons.append(f"reference_reproduction_gate.status={gate.get('status')}; decision={gate.get('decision')}")
        return True, reasons, gate

    def reference_base_switch_exhausted(self, gate: dict[str, Any]) -> bool:
        if not isinstance(gate, dict):
            return False
        if gate.get("decision") == "fresh_base_implementation_required":
            return False
        base_switch = gate.get("base_switch", {}) if isinstance(gate.get("base_switch", {}), dict) else {}
        return bool(
            gate.get("status") != "pass"
            and (
                gate.get("decision") == "no_viable_base_switch_route"
                or base_switch.get("exhausted") is True
                or base_switch.get("status") == "exhausted"
            )
        )

    def refresh_fresh_base_implementation_plan(self, *, reason: str = "") -> dict[str, Any]:
        result = self.run(
            [
                sys.executable,
                str(SCRIPTS / "build_fresh_base_implementation_plan.py"),
                "--project",
                self.args.project,
            ],
            stage="fresh-base-implementation-plan",
            required=False,
            timeout=180,
        )
        plan = read_json(self.paths.state / "fresh_base_implementation_plan.json", {})
        if isinstance(plan, dict) and plan:
            selected = plan.get("selected_base", {}) if isinstance(plan.get("selected_base"), dict) else {}
            self.state["fresh_base_implementation_plan"] = {
                "status": plan.get("status", ""),
                "selected_base_title": selected.get("title", ""),
                "repo": plan.get("repo", {}),
                "ready_datasets": plan.get("ready_datasets", []),
                "blocked_datasets": plan.get("blocked_datasets", []),
                "blocker_reasons": plan.get("blocker_reasons", []),
                "reason": reason,
            }
            self.state["fresh_base_implementation_plan_path"] = str(self.paths.state / "fresh_base_implementation_plan.json")
            self.save()
        return result

    def literature_gate_status(self) -> dict[str, Any]:
        """Summarize whether the current literature survey produced enough positive paper anchors."""
        intermediates = read_json(self.paths.state / "taste_literature_intermediates.json", {})
        sync = read_json(self.paths.state / "taste_sync.json", {})
        paper_quality = read_json(self.paths.state / "paper_quality.json", {})
        ideas = read_json(self.paths.state / "idea_candidates.json", {})
        tool_packet = read_json(self.paths.state / "literature_tool_packet.json", {})
        target_state = recommendation_target_status(self.paths)
        counts = intermediates.get("candidate_pool_counts", {}) if isinstance(intermediates, dict) and isinstance(intermediates.get("candidate_pool_counts", {}), dict) else {}
        survey_stats = intermediates.get("survey_stats", {}) if isinstance(intermediates, dict) and isinstance(intermediates.get("survey_stats", {}), dict) else {}
        progress = read_json(self.paths.planning / "finding" / "find_progress.json", {})
        current_run_id = payload_run_id(progress) or current_find_light_run_id(self.paths)
        packet_run_id = str(tool_packet.get("run_id") or tool_packet.get("source_run_id") or "").strip() if isinstance(tool_packet, dict) else ""
        packet_matches_current = bool(current_run_id and packet_run_id == current_run_id)
        paper_summary = paper_quality.get("summary", {}) if isinstance(paper_quality, dict) and isinstance(paper_quality.get("summary", {}), dict) else {}
        idea_summary = ideas.get("summary", {}) if isinstance(ideas, dict) and isinstance(ideas.get("summary", {}), dict) else {}
        packet_summary = tool_packet.get("summary", {}) if isinstance(tool_packet, dict) and isinstance(tool_packet.get("summary", {}), dict) else {}
        packet_layer = tool_packet.get("candidate_layer_summary", {}) if isinstance(tool_packet, dict) and isinstance(tool_packet.get("candidate_layer_summary", {}), dict) else {}
        packet_counts = packet_layer.get("pool_counts", {}) if isinstance(packet_layer.get("pool_counts", {}), dict) else {}
        claim_ready_anchors = 0
        if isinstance(tool_packet, dict):
            strong_papers = tool_packet.get("strong_papers", [])
            if isinstance(strong_papers, list):
                claim_ready_anchors = sum(
                    1 for p in strong_papers
                    if isinstance(p, dict) and p.get("claim_ready_anchor") and p.get("positive_claim_evidence")
                )
        strong_candidates = [safe_int(target_state.get("actual"), 0)]
        if packet_matches_current:
            strong_candidates.extend([
                safe_int(packet_summary.get("strong_paper_anchors"), 0),
                safe_int(packet_counts.get("strong_papers"), 0),
                safe_int(packet_counts.get("strong_recommendations"), 0),
                claim_ready_anchors,
            ])
        if not any(strong_candidates):
            strong_candidates.extend([safe_int(counts.get("strong_recommendations"), 0), safe_int(counts.get("articles"), 0)])
        strong = max(strong_candidates)
        target_counts = target_state.get("counts", {}) if isinstance(target_state.get("counts"), dict) else {}
        candidate_count = max(
            sum(safe_int(counts.get(key), 0) for key in ["screened_ranking", "read_candidates", "arxiv_prefiltered", "evaluated_candidates", "title_candidates", "critique_candidates"]),
            safe_int(packet_summary.get("inspected_candidates"), 0),
            safe_int(target_counts.get("title_candidates"), 0),
            safe_int(target_counts.get("evaluated_candidates"), 0),
            safe_int(target_counts.get("detail_fetched"), 0),
        )
        positive_recent = safe_int(paper_summary.get("recent_high_priority_count"), 0) + safe_int(paper_summary.get("recent_candidate_count"), 0)
        pursue = safe_int(idea_summary.get("pursue_count"), 0)
        status = "not_run"
        blockers: list[str] = []
        has_state = any(isinstance(item, dict) and item for item in [intermediates, sync, tool_packet]) or bool(target_state.get("run_id"))
        if not has_state:
            blockers.append("literature survey state is missing")
        elif target_state.get("blocking"):
            status = "recommendation_shortfall"
            blockers.extend(str(item) for item in target_state.get("blockers", []) if str(item).strip())
            blockers.append("Paper writing, claim promotion, and base selection must wait for TASTE to repair/expand the current Find output; do not pad weak papers into strong recommendations.")
        elif strong > 0 and ((positive_recent > 0 and pursue > 0) or claim_ready_anchors > 0):
            status = "positive_anchors_ready"
        elif candidate_count > 0:
            status = "candidates_but_no_positive_anchor"
            blockers.append(
                "finding found candidates but no paper passed the strong/positive-anchor gate; continue literature expansion or base switching before treating a new idea as supported."
            )
        else:
            status = "no_candidates"
            blockers.append("literature survey produced no visible candidate pool; check source/network/API configuration.")
        return {
            "status": status,
            "strong_recommendations": strong,
            "recommendation_target_count": safe_int(target_state.get("target"), 0),
            "recommendation_shortfall": safe_int(target_state.get("shortfall"), 0),
            "recommendation_source_count": safe_int(target_state.get("source_count"), 0),
            "recommendation_target_status": target_state.get("status", ""),
            "candidate_count": candidate_count,
            "positive_recent_papers": positive_recent,
            "pursue_ideas": pursue,
            "candidate_pool_counts": packet_counts if packet_matches_current and packet_counts else counts,
            "survey_stats": survey_stats,
            "run_id": current_run_id or target_state.get("run_id", ""),
            "claim_ready_anchors": claim_ready_anchors,
            "blockers": blockers,
        }

    def ensure_current_find_research_plan(self, step_fn: Any, *, stage_suffix: str = "") -> dict[str, Any]:
        """Ensure Read/Idea/Plan artifacts are tied to the latest Find run.

        A completed Find without current downstream artifacts is a blocker for
        base choice and Claude Code planning because stale fallback ideas can
        send TASTE back to legacy routes. This helper never launches Find; it only
        rebuilds downstream planning artifacts from the existing find_results.
        """
        taste_dir = self.paths.planning / "finding"
        find_results = current_find_light_payload(self.paths)
        run_id = payload_run_id(find_results) or current_find_light_run_id(self.paths)
        read_results = read_json(taste_dir / "read_results.json", {})
        ideas = read_json(taste_dir / "ideas.json", {})
        plans = read_json(taste_dir / "plans.json", {})
        expected_readings = count_rows(find_results.get("strong_recommendations")) or count_rows(find_results.get("articles"))
        required_idea_count = configured_max_ideas(self.args.project, default=5)
        validation = read_json(self.paths.state / "current_find_claude_reading_validation.json", {})
        validation_ready = current_find_validation_ready(validation, run_id, expected_readings)
        current = bool(
            run_id
            and expected_readings
            and isinstance(read_results, dict) and read_results.get("run_id") == run_id
            and read_results.get("source") == "claude_code_current_find_takeover"
            and count_rows(read_results.get("readings")) == expected_readings
            and validation_ready
            and isinstance(ideas, dict) and ideas.get("run_id") == run_id
            and ideas.get("source") == "claude_code_current_find_takeover"
            and count_rows(ideas.get("ideas")) >= required_idea_count
            and isinstance(plans, dict) and plans.get("run_id") == run_id
            and plans.get("source") == "claude_code_current_find_takeover"
            and count_rows(plans.get("plans")) >= required_idea_count
        )
        summary = {
            "run_id": run_id,
            "current": current,
            "read_run_id": read_results.get("run_id") if isinstance(read_results, dict) else "",
            "idea_run_id": ideas.get("run_id") if isinstance(ideas, dict) else "",
            "plan_run_id": plans.get("run_id") if isinstance(plans, dict) else "",
            "readings": count_rows(read_results.get("readings")) if isinstance(read_results, dict) else 0,
            "ideas": count_rows(ideas.get("ideas")) if isinstance(ideas, dict) else 0,
            "plans": count_rows(plans.get("plans")) if isinstance(plans, dict) else 0,
            "read_source": read_results.get("source") if isinstance(read_results, dict) else "",
            "idea_source": ideas.get("source") if isinstance(ideas, dict) else "",
            "plan_source": plans.get("source") if isinstance(plans, dict) else "",
            "required_source": "claude_code_current_find_takeover",
            "required_counts": {"readings": expected_readings, "ideas": required_idea_count, "plans": required_idea_count},
            "reading_validation_ready": validation_ready,
            "reading_validation_policy": validation.get("policy_version") if isinstance(validation, dict) else "",
            "full_text_reading_count": safe_int(validation.get("full_text_reading_count"), 0) if isinstance(validation, dict) else 0,
            "pending_full_text_reading_count": safe_int(validation.get("pending_full_text_reading_count"), 0) if isinstance(validation, dict) else 0,
            "reading_validation_blockers": validation.get("blockers", []) if isinstance(validation, dict) else ["missing current full-text reading validation"],
        }
        self.state["current_find_research_plan_status"] = summary
        self.save()
        if current:
            return summary
        result = self.run(
            [
                sys.executable,
                str(SCRIPTS / "ensure_current_find_research_plan.py"),
                "--project",
                self.args.project,
            ],
            stage="current-find-read-idea-plan" + stage_suffix,
            required=False,
            timeout=max(900, self.args.claude_timeout_sec + 300 if self.args.claude_timeout_sec > 0 else 0),
        )
        step_fn(result)
        refreshed = read_json(self.paths.state / "current_find_research_plan.json", {})
        summary.update({
            "bridge_return_code": result.get("return_code"),
            "bridge_status": refreshed.get("status") if isinstance(refreshed, dict) else "",
            "bridge_source": refreshed.get("source") if isinstance(refreshed, dict) else "",
            "claude_takeover_status": refreshed.get("claude_takeover", {}).get("status") if isinstance(refreshed, dict) and isinstance(refreshed.get("claude_takeover"), dict) else "",
            "readings": refreshed.get("current_find_reading_count", summary.get("readings")) if isinstance(refreshed, dict) else summary.get("readings"),
            "ideas": refreshed.get("current_find_idea_count", summary.get("ideas")) if isinstance(refreshed, dict) else summary.get("ideas"),
            "plans": refreshed.get("current_find_plan_count", summary.get("plans")) if isinstance(refreshed, dict) else summary.get("plans"),
        })
        plan_gate = current_find_plan_bridge_gate_status(self.paths, summary)
        summary.update({
            "current": not plan_gate.get("blocking"),
            "blocking": bool(plan_gate.get("blocking")),
            "plan_bridge_gate": plan_gate,
            "blockers": plan_gate.get("blockers", []),
        })
        self.state["current_find_research_plan_status"] = summary
        self.save()
        return summary

    def literature_after_survey_summary(self) -> dict[str, Any]:
        """Record whether the freshly refreshed survey is ready to drive route choice."""
        packet = read_json(self.paths.state / "literature_tool_packet.json", {})
        frontend = read_json(self.paths.state / "finding_frontend.json", {})
        find_results = current_find_light_payload(self.paths)
        gate = self.literature_gate_status()
        counts: dict[str, Any] = {}
        if isinstance(frontend, dict) and isinstance(frontend.get("survey_stats", {}), dict):
            counts.update(frontend.get("survey_stats", {}))
        if isinstance(find_results, dict):
            counts.update(
                {
                    "strong_recommendations": count_rows(find_results.get("strong_recommendations")) or count_rows(find_results.get("articles")),
                    "screened_ranking": count_rows(find_results.get("screened_ranking")),
                    "read_candidates": count_rows(find_results.get("read_candidates")),
                    "evaluated_candidates": count_rows(find_results.get("evaluated_candidates")),
                    "title_candidates": count_rows(find_results.get("title_candidates")),
                }
            )
        base_candidates = packet.get("base_work_candidates", []) if isinstance(packet, dict) and isinstance(packet.get("base_work_candidates", []), list) else []
        strong_papers = packet.get("strong_papers", []) if isinstance(packet, dict) and isinstance(packet.get("strong_papers", []), list) else []
        return {
            "status": gate.get("status"),
            "counts": counts,
            "strong_paper_count": len(strong_papers),
            "base_work_candidate_count": len(base_candidates),
            "top_base_candidates": [
                {
                    "title": one_line(item.get("title") or item.get("name") or item.get("id") or "", 160),
                    "venue": item.get("venue") or item.get("source") or "",
                    "year": item.get("year") or "",
                    "reason": one_line(item.get("reason") or item.get("recommendation_note") or item.get("fit_explanation") or "", 220),
                }
                for item in base_candidates[:8]
                if isinstance(item, dict)
            ],
            "blockers": gate.get("blockers", []),
            "must_drive_next_route": True,
            "next_route_rule": "After a fresh survey, The workflow must re-evaluate the active base work against the literature packet before continuing experiments or paper production.",
        }

    def reference_reproduction_repair_prompt(self, cycle_index: int, reasons: list[str], gate: dict[str, Any], action_plan: dict[str, Any]) -> str:
        action_preview = action_plan.get("actions", [])[:8] if isinstance(action_plan.get("actions", []), list) else []
        return f"""
TASTE full research cycle {cycle_index}: reference-work reproduction is the current hard gate.

Do not tune novel ideas, write the paper, repair figures, or polish PDF output until this gate is cleared or the base is switched with evidence.

Current reference reproduction gate:
```json
{json.dumps(gate, ensure_ascii=False, indent=2)}
```

Blocking reasons:
```json
{json.dumps(reasons, ensure_ascii=False, indent=2)}
```

Deterministic blocker action plan:
```json
{json.dumps(action_preview, ensure_ascii=False, indent=2)}
```

Required trajectory:
1. Find or record the active reference paper/table metric target and source for the selected repo/dataset.
2. Reproduce the reference work through TASTE's audit wrapper using the selected repo/env/data and paper-method settings.
3. Inspect runtime, logs, loss/epoch traces, metrics, config diffs, bad-case or missing-bad-case evidence, and audit artifacts.
4. Compare against the paper target with tolerance and compute budget.
5. If the current local run is documented as protocol/data-split incomparable with the paper target, that is still a blocker. Do not edit audit scripts to turn incomparability into pass. Instead reproduce the paper protocol/data split or route to switch base with evidence.
6. If the target cannot be reached or full reproduction is infeasible, route to evidence-backed repo/literature backtracking and update research state for switching base.
7. Persist every decision into state/report files and keep the experiment registry and experiment_records.csv unified.

Return concise Markdown with: Target Evidence, Reproduction Action, Runtime/Compute Decision, State Files Updated, Still Blocked or Cleared.
""".strip()

    def fresh_base_implementation_prompt(self, cycle_index: int, reasons: list[str], gate: dict[str, Any], action_plan: dict[str, Any]) -> str:
        action_preview = action_plan.get("actions", [])[:10] if isinstance(action_plan.get("actions", []), list) else []
        current_plan = read_json(self.paths.state / "current_find_research_plan.json", {})
        experiment_plan = read_json(self.paths.state / "experiment_plan.json", {})
        selected_execution_contract = current_find_execution_contract(self.paths)
        fresh_plan = read_json(self.paths.state / "fresh_base_implementation_plan.json", {})
        literature_packet = read_json(self.paths.state / "literature_tool_packet.json", {})
        ctx = fresh_base_route_context(self.paths, self.args.project)
        return f"""
TASTE full research cycle {cycle_index}: continue the current environment-stage selected anchor implementation route.

Hard scope:
- Work only on the anchor/base selected by the environment-stage Claude Code decision for the current project: {ctx.get("title") or "environment-stage selected anchor"} / `{ctx.get("repo_name") or ctx.get("repo_path") or "environment-stage selected repo"}`.
- Do not start another Find, do not create pair_compare, do not run any legacy/control route as the main route, and do not write or polish the paper.
- Do not start full training unless `fresh_base_implementation_plan.json` shows at least one real dataset with the complete loader contract for the selected base.
- Find/Read/Idea/Plan outputs are planning evidence only; they never select the anchor by themselves. Local repo/data/env/reproduction artifacts are required before claims.
- Execute only the current-Find `selected_plan_id` below. Other ideas/plans remain backlog and must not drive implementation, launches, paper prose, or claim updates.
- If the selected plan is missing while current-Find candidates exist, stop and record the blocker instead of choosing a route ad hoc.

Current Find bridge:
```json
{json.dumps({
    "current_find_research_plan": {
        "run_id": current_plan.get("run_id") if isinstance(current_plan, dict) else "",
        "status": current_plan.get("status") if isinstance(current_plan, dict) else "",
        "readings": current_plan.get("current_find_reading_count") if isinstance(current_plan, dict) else 0,
        "ideas": current_plan.get("current_find_idea_count") if isinstance(current_plan, dict) else 0,
        "plans": current_plan.get("current_find_plan_count") if isinstance(current_plan, dict) else 0,
        "primary_route": current_plan.get("primary_route") if isinstance(current_plan, dict) else "",
        "selected_execution_contract": selected_execution_contract,
        "candidate_backlog_count": len(current_plan.get("ideas", [])) if isinstance(current_plan, dict) and isinstance(current_plan.get("ideas", []), list) else 0,
    },
    "experiment_plan": {
        "run_id": experiment_plan.get("run_id") if isinstance(experiment_plan, dict) else "",
        "status": experiment_plan.get("status") if isinstance(experiment_plan, dict) else "",
        "claude_code_autonomous_loop": experiment_plan.get("claude_code_autonomous_loop", []) if isinstance(experiment_plan, dict) else [],
    },
    "literature_packet": {
        "run_id": literature_packet.get("run_id") if isinstance(literature_packet, dict) else "",
        "status": literature_packet.get("status") if isinstance(literature_packet, dict) else "",
        "summary": literature_packet.get("summary", {}) if isinstance(literature_packet, dict) else {},
    },
}, ensure_ascii=False, indent=2)}
```

Fresh-base implementation plan:
```json
{json.dumps(fresh_plan if isinstance(fresh_plan, dict) else {}, ensure_ascii=False, indent=2)}
```

Reference gate blockers:
```json
{json.dumps(reasons, ensure_ascii=False, indent=2)}
```

Deterministic action plan:
```json
{json.dumps(action_preview, ensure_ascii=False, indent=2)}
```

Required autonomous work:
1. Inspect `state/current_find_research_plan.json`, `state/experiment_plan.json`, `planning/finding/read.md`, `planning/finding/idea.md`, and `planning/finding/plan.md` first.
2. Inspect the environment-stage selected repository from `fresh_base_implementation_plan.json`: README, entrypoints, model/data loaders, dataset paths, imports, metrics, and CLI arguments.
3. Produce machine-readable fresh-base audit artifacts under `state/` and human report(s) under `reports/`: exact dataset contract, expected paths, embedding pickle schema, import/package requirements, minimal smoke commands, and blocked data acquisition steps.
4. If safe and useful, add small wrapper/probe scripts that only inspect imports, argparse, files, schemas, and loader readiness. Do not run long training or fabricate data.
5. Do not run raw `gdown`, `curl`, or `wget` yourself. Data acquisition must go through `{management_python()} modules/environment/scripts/probe_fresh_base_data_acquisition.py --project {self.args.project} --attempt-download --timeout-sec 45`; if it fails or times out, record the exact blocker and stop before training.
6. Re-run `{management_python()} modules/environment/scripts/build_fresh_base_implementation_plan.py --project {self.args.project}`, `{management_python()} modules/experimenting/scripts/audit_reference_reproduction.py --project {self.args.project} --venue {self.args.venue}`, and `{management_python()} modules/planning/scripts/build_blocker_action_plan.py --project {self.args.project} --venue {self.args.venue}` after changes.
7. Leave gates blocked unless at least one real dataset contract for the environment-stage selected anchor is complete and loader/import probes pass.

Return concise Markdown with: Files Inspected, Artifacts Written, Commands Run, Data/Env Contract, Still Blocked or Cleared, Next TASTE Command.
""".strip()

    def reference_base_switch_prompt(self, cycle_index: int, reasons: list[str], gate: dict[str, Any], action_plan: dict[str, Any]) -> str:
        """Prompt Claude to consume the fresh finding packet before continuing a blocked base."""
        action_preview = action_plan.get("actions", [])[:8] if isinstance(action_plan.get("actions", []), list) else []
        packet = read_json(self.paths.state / "literature_tool_packet.json", {})
        base_candidates = packet.get("base_work_candidates", []) if isinstance(packet, dict) and isinstance(packet.get("base_work_candidates", []), list) else []
        strong_papers = packet.get("strong_papers", []) if isinstance(packet, dict) and isinstance(packet.get("strong_papers", []), list) else []
        candidate_summary = packet.get("candidate_layer_summary", {}) if isinstance(packet, dict) and isinstance(packet.get("candidate_layer_summary", {}), dict) else {}
        top_candidates = [
            {
                "title": one_line(item.get("title") or item.get("name") or item.get("id") or "", 180),
                "venue": item.get("venue") or item.get("source") or "",
                "year": item.get("year") or "",
                "score": item.get("score") or item.get("final_score") or item.get("relevance_score") or item.get("total_score"),
                "url": item.get("url") or item.get("pdf_url") or item.get("link") or item.get("openreview_url") or "",
                "reason": one_line(item.get("reason") or item.get("recommendation_note") or item.get("fit_explanation") or "", 260),
            }
            for item in base_candidates[:10]
            if isinstance(item, dict)
        ]
        return f"""
TASTE full research cycle {cycle_index}: switch or backtrack the reference base before any more legacy-route main-route work.

The reference gate explicitly says `decision=switch_base`. Do not launch another main-route legacy repair run, do not tune novel methods on a legacy route as the primary route, and do not write or polish the paper. A currently running legacy-route process may only be treated as historical/background comparison evidence after it finishes and is audited; it must not block the base-switch decision.

Fresh finding literature packet:
```json
{json.dumps({
    "status": packet.get("status") if isinstance(packet, dict) else "",
    "summary": packet.get("summary", {}) if isinstance(packet, dict) else {},
    "candidate_layer_summary": candidate_summary,
    "top_base_work_candidates": top_candidates,
    "strong_paper_count": len(strong_papers),
    "packet_files": [
        str(self.paths.state / "literature_tool_packet.json"),
        str(self.paths.planning / "literature_tool_packet.md"),
        str(self.paths.planning / "finding" / "find_results.json"),
        str(self.paths.planning / "finding" / "read.md"),
    ],
}, ensure_ascii=False, indent=2)}
```

Current blocked reference gate:
```json
{json.dumps(gate, ensure_ascii=False, indent=2)}
```

Blocking reasons:
```json
{json.dumps(reasons, ensure_ascii=False, indent=2)}
```

Deterministic blocker action plan:
```json
{json.dumps(action_preview, ensure_ascii=False, indent=2)}
```

Required trajectory:
1. Read `state/literature_tool_packet.json` or `planning/literature_tool_packet.md` and at least one raw finding artifact under `planning/finding/`.
2. Compare the active legacy route against the top finding base candidates from the current project packet.
3. Select a new base only if it has paper target evidence, runnable or reasonably obtainable code/env, real-data path, and feasible reproduction cost; otherwise record a no-route blocker with exact evidence.
4. If a new base is selected, update research state for the new route and prepare environment/data/reproduction commands through existing modules. Do not create a second TASTE flow.
5. If a legacy route remains only as fallback/control, record why it is weaker than the fresh candidates and stop treating it as the default main route.
6. Keep `experiment_registry.json` and `experiment_records.csv` unified; do not fabricate metrics or weaken gates.

Return concise Markdown with: Literature Candidates Compared, Base-Switch Decision, Repos/Data/Code Checked, Files Updated, Next TASTE Command.
""".strip()

    def experiment_evidence_gate_blocked(self) -> tuple[bool, list[str]]:
        """Paper production is only useful after real experiment evidence is credible."""
        reasons: list[str] = []
        literature_gate = self.literature_gate_status()
        if literature_gate.get("status") in {"not_run", "no_candidates", "candidates_but_no_positive_anchor", "recommendation_shortfall"}:
            reasons.extend(str(item) for item in literature_gate.get("blockers", []) if str(item).strip())
        evidence_audit = read_text(self.paths.reports / "paper_evidence_audit.md").lower()
        submission = read_json(self.paths.state / "submission_readiness.json", {})
        assurance = read_json(self.paths.state / "research_assurance_layer.json", {})
        manifest = read_json(self.paths.state / "research_evidence_manifest.json", {})
        progress_gate = read_json(self.paths.state / "scientific_progress_gate.json", {})
        reference_gate = read_json(self.paths.state / "reference_reproduction_gate.json", {})
        experiments = read_json(self.paths.state / "experiment_registry.json", [])
        exp_rows = experiments.get("experiments", []) if isinstance(experiments, dict) and isinstance(experiments.get("experiments", []), list) else experiments if isinstance(experiments, list) else []
        audit_ready = [row for row in exp_rows if isinstance(row, dict) and row.get("audit_ready")]
        real_ready_names: set[str] = set()
        dataset_registry = read_json(self.paths.state / "dataset_registry.json", [])
        if isinstance(dataset_registry, list):
            real_ready_names.update(
                str(row.get("name") or row.get("dataset"))
                for row in dataset_registry
                if isinstance(row, dict)
                and row.get("claim_ready")
                and row.get("loader_probe_success")
                and not str(row.get("name") or row.get("dataset") or "").startswith("synthetic")
            )
        repo_data_requirements = read_json(self.paths.state / "repo_data_requirements.json", {})
        if isinstance(repo_data_requirements, dict):
            real_ready_names.update(str(name) for name in repo_data_requirements.get("ready_datasets", []) or [] if str(name).strip())
        real_audit_ready = [
            row for row in audit_ready
            if str(row.get("dataset") or "") in real_ready_names and not str(row.get("dataset") or "").startswith("synthetic")
        ]
        failed = submission.get("failed_checks", []) if isinstance(submission.get("failed_checks", []), list) else []
        failed_ids = {
            str(row.get("id") or row.get("name") or "").lower()
            for row in failed
            if isinstance(row, dict)
        }
        evidence_failed_ids = {
            "evidence_gate_allows_template",
            "real_audit_ready_experiment",
            "scientific_progress_gate_pass",
            "reference_reproduction_gate_pass",
            "claim_verdicts_available",
            "bad_cases_available",
            "counterexamples_available",
            "assurance_layer_pass",
            "evidence_manifest_pass",
        }
        if "hold-markdown-only" in evidence_audit:
            reasons.append("paper_evidence_audit recommends hold-markdown-only")
        if failed_ids & evidence_failed_ids:
            reasons.append("submission_readiness has experiment/evidence failed checks: " + ", ".join(sorted(failed_ids & evidence_failed_ids)))
        if isinstance(assurance, dict) and assurance.get("status") == "blocked":
            reasons.append("research_assurance_layer.status=blocked")
        if isinstance(manifest, dict) and manifest.get("status") in {"blocked", "warn"}:
            reasons.append(f"research_evidence_manifest.status={manifest.get('status')}")
        if not real_audit_ready:
            reasons.append("no audit-ready real-data experiment is currently available for paper promotion")
        if isinstance(progress_gate, dict) and progress_gate.get("status") != "pass":
            blockers = progress_gate.get("blockers", [])
            if isinstance(blockers, list) and blockers:
                reasons.append("scientific_progress_gate blocked: " + "; ".join(str(item) for item in blockers[:4]))
            else:
                reasons.append(f"scientific_progress_gate.status={progress_gate.get('status')}")
        if isinstance(reference_gate, dict) and reference_gate.get("status") not in {"", "pass"}:
            blockers = reference_gate.get("blockers", [])
            if isinstance(blockers, list) and blockers:
                reasons.append("reference_reproduction_gate blocked: " + "; ".join(str(item) for item in blockers[:4]))
            else:
                reasons.append(f"reference_reproduction_gate.status={reference_gate.get('status')}")
        return bool(reasons), reasons

    def blocker_prompt(self, cycle_index: int, blockers: list[dict[str, Any]], gate: dict[str, Any]) -> str:
        blocker_action_plan = read_json(self.paths.state / "blocker_action_plan.json", {})
        action_preview = blocker_action_plan.get("actions", [])[:12] if isinstance(blocker_action_plan.get("actions", []), list) else []
        active_experiment_processes = self.running_experiment_processes()
        gate_for_prompt = dict(gate) if isinstance(gate, dict) else {}
        experiment_gates = gate_for_prompt.get("experiment_gates") if isinstance(gate_for_prompt.get("experiment_gates"), dict) else {}
        if experiment_gates.get("paper_blocked_until_experiment_gate_passes"):
            gate_for_prompt["latest_pdf"] = ""
            gate_for_prompt["latest_pdf_info"] = {
                "suppressed_in_claude_prompt": True,
                "reason": "scientific evidence gates are blocked; old paper/PDF artifacts are not current-route evidence",
            }
            paper_status = gate_for_prompt.get("paper_status") if isinstance(gate_for_prompt.get("paper_status"), dict) else {}
            gate_for_prompt["paper_status"] = {
                "promotion_gate": paper_status.get("promotion_gate"),
                "paper_review_verdict": paper_status.get("paper_review_verdict"),
                "suppressed_in_claude_prompt": True,
                "reason": "paper-stage artifacts stay non-authoritative until scientific evidence gates pass",
            }
        return f"""
TASTE full research cycle {cycle_index} did not pass the local gates.

You must continue the autonomous research trajectory instead of stopping after diagnosis.
Use TASTE's native method contracts already synced into the project: research-direction management, evolutionary memory, evidence assurance, trajectory optimization, and paper production. Do not present these as separate external agents.

Current gate snapshot:
```json
{json.dumps(gate_for_prompt, ensure_ascii=False, indent=2)}
```

Current blockers:
```json
{json.dumps(blockers[:30], ensure_ascii=False, indent=2)}
```

Deterministic blocker action plan:
```json
{json.dumps(action_preview, ensure_ascii=False, indent=2)}
```

Active experiment processes that must not be duplicated:
```json
{json.dumps(active_experiment_processes[:8], ensure_ascii=False, indent=2)}
```

Repair requirements:
- First read `planning/reference_workflow_and_claude_code.md`, then follow `state/blocker_action_plan.json`: use each action's route, skill_contract, recommended_commands, and success_checks. If you deviate, record the evidence-backed reason.
- If the top route is `reference_reproduction_repair`, first reproduce the reference work to its paper/table target, record target evidence and runtime feasibility, or switch base with evidence. Do not tune novel ideas or write the paper before this clears.
- If the top route is `experiment_evidence_repair`, do not run paper writing or PDF repair. First debug/run/repair real-data experiments, baseline reproduction, metric/evaluation code, bad-case evidence, claim verdicts, or repo/data switches.
- If `selected_base_viability_gate.decision=base_switch_gate_required`, stop candidate/alternative main-route training and paper/claim promotion until deterministic gate evidence passes. Current selected-base evidence repair may continue via `modules/experimenting/scripts/launch_experiment_run.py --route-scope selected_base_current_route`; bounded candidate gate evidence collection may use `--route-scope base_switch_evidence_collection`. If the dedicated gate passes, execute route identity changes only through `modules/environment/scripts/execute_authorized_base_switch.py`; evidence still remains non-promotable until downstream audits pass.
- Failed, weak, or negative experiment findings may update audits, failed-hypothesis memory, and prune decisions, but they must not become paper contributions or an automatic topic re-scope. If evidence shows the selected route cannot support the user target topic, keep paper/claim gates blocked and write a deterministic route proposal instead of rewriting the research target.
- If the active experiment process list is non-empty, do not declare the run interrupted/stopped from a partial log, and do not start a duplicate or hyperparameter-changing replacement. Wait for the live PID to exit, then audit the final stdout, metrics, artifact-local audit, and gates.
- Observe live training non-invasively only. Do not send signals, attach strace/gdb/py-spy, read blocking `/proc/<pid>/fd/*` pipes, kill, restart, or launch a duplicate while a live PID exists unless artifact-local evidence proves a hard failure.
- For every new training launch, use `{management_python()} modules/experimenting/scripts/launch_experiment_run.py --project {self.args.project} --artifact-name <unique_slug> --cwd <project_or_repo_dir> -- <project-experiment-python> -u <training_script.py> ...` so TASTE owns the PID, lock, artifact contract, project experiment Python, and stdout/stderr log.
- Do not use system `python`, bare `python3`, `conda run`, raw `nohup`, shell backgrounding, or manual stdout redirection for new experiments. If a repo script needs adaptation, write a repo-local wrapper and launch that wrapper through the the launcher with the project experiment Python.
- The launcher must produce `run_contract.json`, `run.lock`, `launcher.pid.json`, `stdout_stderr.log`, `python_executable`, `environment_contract`, `expected_outputs`, and `audit_refresh_required`; import/audit scripts must treat contaminated, wrong-interpreter, or failed artifacts as non-evidence.
- If a log is empty while the process is alive, record that the run is waiting for output and keep monitoring; do not treat the empty log as failure.
- If stopping a run is unavoidable, first write the stop reason, PID, command, artifact path, and evidence to an artifact-local audit or run note.
- Only after experiment evidence gates pass should paper-stage regeneration, TeX/figure/citation repair, venue page-accounting diagnosis, or PDF refresh run.
- Fix root causes in repo/data/env/experiment/paper pipeline when possible, then rerun the relevant TASTE commands.
- If a blocker is scientific evidence, produce or repair real-data experiment evidence; do not weaken gates or invent results.
- If a blocker is paper structure, citation count, CIKM/venue LaTeX format, or figures, use TASTE paper-stage section/evidence/venue/figure files and audits to repair them only after scientific evidence gates are not blocking.
- If the current repo is no longer the best transformable route, make that decision explicit with local/network evidence and update research state for the next cycle.
- Preserve all trajectory memory and failed-hypothesis records.
- Obsolete baseline or route cleanup is a project-context decision, not a framework auto-delete. Use `audit_obsolete_baseline_cleanup.py` only to enumerate candidates and protections. If cleanup is scientifically/project-operationally required, project Claude Code must inspect current route evidence, shared evidence files, and candidate paths, then write `state/obsolete_baseline_cleanup_authorization.json` with `status=authorized_by_project_claude_review`, `cleanup_authorized=true`, `current_route_reviewed=true`, `protected_current_route=true`, `approved_candidate_paths`, `protected_paths`, and rationale. Project Claude Code must then execute cleanup itself in a separate project turn and write `state/obsolete_baseline_cleanup_execution.json` with `status=completed_by_project_claude`, `cleanup_executed=true`, exact `applied_paths`, exact `remaining_candidate_paths`, protected paths, and rationale. framework must only audit that receipt; do not manually delete project files or authorize cleanup by directory/name matching alone.

Return concise Markdown with: Root Cause, Files/State Changed, Commands Run, Evidence, Still Blocked or Cleared.
""".strip()

    def finish_current_find_full_text_gate_block(self, cycle: dict[str, Any], pdf_before: dict[str, Any], full_text_gate: dict[str, Any]) -> dict[str, Any]:
        pdf_after = self.latest_pdf_fingerprint()
        gate = self.gate_snapshot()
        gate["current_find_full_text_gate"] = full_text_gate
        gate["pdf_before"] = pdf_before
        gate["pdf_after"] = pdf_after
        gate["pdf_changed_this_cycle"] = self.pdf_changed(pdf_before, pdf_after)
        gate["paper_pipeline_skipped"] = True
        gate["paper_pipeline_skipped_reason"] = "Read-stage full-text packet gate blocks base selection, experiments, paper writing, citation repair, figure repair, and claim promotion until the reading packet has verified full-text evidence or same-run replacements"
        blockers = [
            {
                "category": "current_find_full_text_reading_gate",
                "severity": "block",
                "issue": (
                    f"Read-stage packet entries are not fully readable: "
                    f"full_text={full_text_gate.get('full_text_reading_count')}/{full_text_gate.get('expected_recommendation_count')}; "
                    f"pending={full_text_gate.get('pending_full_text_reading_count')}."
                ),
                "human_summary": (
                    f"Read-stage 阅读包还没有完成全文证据核验："
                    f"已全文 {full_text_gate.get('full_text_reading_count')}/{full_text_gate.get('expected_recommendation_count')}，"
                    f"待补 {full_text_gate.get('pending_full_text_reading_count')}。系统不会继续基底选择、实验或论文写作。"
                ),
                "evidence": full_text_gate.get("evidence", []),
                "next_action": full_text_gate.get("next_action") or "Acquire or prove missing Read-stage full-text/PDF/HTML/page evidence, or let Read use an eligible same-run ranked replacement without rewriting Find outputs.",
            }
        ]
        blocker_action_plan = read_json(self.paths.state / "blocker_action_plan.json", {})
        gate["blocker_action_plan"] = {
            "status": blocker_action_plan.get("status", "") if isinstance(blocker_action_plan, dict) else "",
            "summary": blocker_action_plan.get("summary", {}) if isinstance(blocker_action_plan, dict) else {},
            "top_actions": (blocker_action_plan.get("actions", [])[:8] if isinstance(blocker_action_plan, dict) and isinstance(blocker_action_plan.get("actions", []), list) else []),
        }
        cycle.update(
            {
                "finished_at": now_iso(),
                "status": "blocked_current_find_full_text_reading",
                "gate": gate,
                "blockers": blockers,
                "pdf_after": pdf_after,
                "pdf_changed": gate["pdf_changed_this_cycle"],
                "paper_pipeline_skipped": True,
            }
        )
        self.state["status"] = "blocked_current_find_full_text_reading"
        self.state["latest_gate"] = gate
        self.state["latest_blockers"] = blockers
        self.state["latest_blocker_action_plan"] = gate["blocker_action_plan"]
        self.state["latest_pdf_info"] = pdf_after
        self.state["paper_iteration_required"] = False
        self.state["paper_pipeline_skipped"] = True
        self.state["paper_pipeline_skipped_reason"] = gate["paper_pipeline_skipped_reason"]
        self.state["continuation_required"] = True
        self.state["continuation_reason"] = "Read-stage full-text packet is incomplete because one or more packet entries lack verified full-text evidence; acquire evidence or use an eligible same-run replacement before any Claude rewrite"
        self.state["current_goal"] = (
            f"complete Read-stage full-text packet gate "
            f"({full_text_gate.get('full_text_reading_count')}/{full_text_gate.get('expected_recommendation_count')}, "
            f"pending {full_text_gate.get('pending_full_text_reading_count')}); experiments and paper blocked"
        )
        self.save()
        return cycle

    def finish_current_find_plan_bridge_gate_block(self, cycle: dict[str, Any], pdf_before: dict[str, Any], plan_gate: dict[str, Any]) -> dict[str, Any]:
        plan_gate = plan_gate if isinstance(plan_gate, dict) else {}
        pdf_after = self.latest_pdf_fingerprint()
        gate = self.gate_snapshot()
        gate["current_find_plan_bridge_gate"] = plan_gate
        gate["pdf_before"] = pdf_before
        gate["pdf_after"] = pdf_after
        gate["pdf_changed_this_cycle"] = self.pdf_changed(pdf_before, pdf_after)
        gate["paper_pipeline_skipped"] = True
        gate["paper_pipeline_skipped_reason"] = "Current Find Read/Idea/Plan bridge is missing, stale, blocked, or lacks a unique selected_plan_id; downstream execution is stopped"
        issue = "; ".join(str(item) for item in plan_gate.get("blockers", [])[:5]) or "Current Find Read/Idea/Plan bridge is not ready for downstream execution."
        blockers = [
            {
                "category": "current_find_plan_bridge_gate",
                "severity": "block",
                "issue": issue,
                "human_summary": "当前 Find 的精读、idea、plan 或唯一执行计划没有和最新 Find run 对齐；系统不会继续环境、实验、论文或 Claude 修复。",
                "run_id": plan_gate.get("run_id", ""),
                "plan_run_id": plan_gate.get("plan_run_id", ""),
                "selected_plan_id": plan_gate.get("selected_plan_id", ""),
                "evidence": plan_gate.get("evidence", []),
                "next_action": plan_gate.get("next_action") or "Repair the current-Find bridge and rerun modules/reading/scripts/ensure_current_find_research_plan.py --project <project>.",
            }
        ]
        blocker_action_plan = read_json(self.paths.state / "blocker_action_plan.json", {})
        gate["blocker_action_plan"] = {
            "status": blocker_action_plan.get("status", "") if isinstance(blocker_action_plan, dict) else "",
            "summary": blocker_action_plan.get("summary", {}) if isinstance(blocker_action_plan, dict) else {},
            "top_actions": (blocker_action_plan.get("actions", [])[:8] if isinstance(blocker_action_plan, dict) and isinstance(blocker_action_plan.get("actions", []), list) else []),
        }
        stop_payload = {
            "project": self.args.project,
            "target_venue": self.args.venue,
            "status": "blocked_current_find_plan_bridge",
            "generated_at": now_iso(),
            "plan_bridge_gate": plan_gate,
            "policy": "Full-cycle cannot enter environment, experiment, paper, claim, or Claude repair stages until current-Find Read/Idea/Plan artifacts are current, unblocked, fully validated, and have exactly one selected_plan_id.",
        }
        write_json(self.paths.state / "current_find_plan_bridge_gate_stop.json", stop_payload)
        cycle.update(
            {
                "finished_at": now_iso(),
                "status": "blocked_current_find_plan_bridge",
                "gate": gate,
                "blockers": blockers,
                "pdf_after": pdf_after,
                "pdf_changed": gate["pdf_changed_this_cycle"],
                "paper_pipeline_skipped": True,
            }
        )
        self.state["status"] = "blocked_current_find_plan_bridge"
        self.state["latest_gate"] = gate
        self.state["latest_blockers"] = blockers
        self.state["latest_blocker_action_plan"] = gate["blocker_action_plan"]
        self.state["latest_pdf_info"] = pdf_after
        self.state["current_find_plan_bridge_gate"] = plan_gate
        self.state["paper_iteration_required"] = False
        self.state["paper_pipeline_skipped"] = True
        self.state["paper_pipeline_skipped_reason"] = gate["paper_pipeline_skipped_reason"]
        self.state["continuation_required"] = True
        self.state["continuation_reason"] = issue
        self.state["current_goal"] = "repair current-Find Read/Idea/Plan bridge before downstream TASTE execution"
        self.save()
        return cycle


    def current_find_selected_plan_gate_blocking(self, contract: dict[str, Any]) -> bool:
        if not isinstance(contract, dict) or not contract.get("required"):
            return False
        selected_plan_id = str(contract.get("selected_plan_id") or "").strip()
        selection_issue = str(contract.get("selection_issue") or "").strip()
        status = str(contract.get("status") or "").strip()
        counts = contract.get("candidate_counts") if isinstance(contract.get("candidate_counts"), dict) else {}
        candidate_count = safe_int(counts.get("ideas"), 0) + safe_int(counts.get("plans"), 0)
        blocking_issues = {
            "missing_selected_plan",
            "ambiguous_selected_plan",
            "selected_plan_id_missing",
            "selected_plan_missing_matching_idea",
        }
        return bool(candidate_count and (not selected_plan_id or selection_issue in blocking_issues or status.startswith("blocked_")))

    def finish_current_find_selected_plan_gate_block(self, cycle: dict[str, Any], pdf_before: dict[str, Any], selected_contract: dict[str, Any]) -> dict[str, Any]:
        selected_contract = selected_contract if isinstance(selected_contract, dict) else {}
        pdf_after = self.latest_pdf_fingerprint()
        gate = self.gate_snapshot()
        gate["current_find_selected_execution_contract"] = selected_contract
        gate["pdf_before"] = pdf_before
        gate["pdf_after"] = pdf_after
        gate["pdf_changed_this_cycle"] = self.pdf_changed(pdf_before, pdf_after)
        gate["paper_pipeline_skipped"] = True
        gate["paper_pipeline_skipped_reason"] = "Current Find Read/Idea/Plan candidates require exactly one selected_plan_id before environment, experiment, paper, or claim execution"
        selection_issue = str(selected_contract.get("selection_issue") or "missing_selected_plan").strip() or "missing_selected_plan"
        status = "blocked_ambiguous_selected_plan" if selection_issue == "ambiguous_selected_plan" else "blocked_missing_selected_plan"
        issue = str(selected_contract.get("reason") or "Current Find produced idea/plan candidates, but no valid exactly-one selected_plan_id contract exists.")
        blockers = [
            {
                "category": "current_find_selected_plan_gate",
                "severity": "block",
                "issue": issue,
                "human_summary": (
                    "当前 Find 的精读、idea 和 plan 候选已生成，但主控 Claude Code 或人类监督还没有选出唯一可执行 selected_plan_id。"
                    "系统不会继续环境、实验、论文或结论提升。"
                ),
                "selection_issue": selection_issue,
                "selected_plan_id": str(selected_contract.get("selected_plan_id") or ""),
                "candidate_counts": selected_contract.get("candidate_counts", {}),
                "evidence": [
                    str(self.paths.planning / "finding" / "ideas.json"),
                    str(self.paths.planning / "finding" / "plans.json"),
                    str(self.paths.state / "current_find_research_plan.json"),
                    str(self.paths.state / "experiment_plan.json"),
                    str(self.paths.state / "taste_plan_bridge.json"),
                ],
                "next_action": "Rerun modules/reading/scripts/ensure_current_find_research_plan.py --project <project> so the main Claude Code compares the five plans from full readings and writes exactly one selected_for_execution/execute_next plan; all other plans must remain backlog.",
            }
        ]
        blocker_action_plan = read_json(self.paths.state / "blocker_action_plan.json", {})
        gate["blocker_action_plan"] = {
            "status": blocker_action_plan.get("status", "") if isinstance(blocker_action_plan, dict) else "",
            "summary": blocker_action_plan.get("summary", {}) if isinstance(blocker_action_plan, dict) else {},
            "top_actions": (blocker_action_plan.get("actions", [])[:8] if isinstance(blocker_action_plan, dict) and isinstance(blocker_action_plan.get("actions", []), list) else []),
        }
        stop_payload = {
            "project": self.args.project,
            "target_venue": self.args.venue,
            "status": status,
            "generated_at": now_iso(),
            "selected_execution_contract": selected_contract,
            "policy": "Full-cycle cannot enter environment, experiment, paper, or claim stages until current-Find Read/Idea/Plan has exactly one selected_plan_id chosen by main Claude Code or human supervision. Candidate ideas/plans are backlog only.",
        }
        write_json(self.paths.state / "current_find_selected_plan_gate_stop.json", stop_payload)
        cycle.update(
            {
                "finished_at": now_iso(),
                "status": status,
                "gate": gate,
                "blockers": blockers,
                "pdf_after": pdf_after,
                "pdf_changed": gate["pdf_changed_this_cycle"],
                "paper_pipeline_skipped": True,
            }
        )
        self.state["status"] = status
        self.state["latest_gate"] = gate
        self.state["latest_blockers"] = blockers
        self.state["latest_blocker_action_plan"] = gate["blocker_action_plan"]
        self.state["latest_pdf_info"] = pdf_after
        self.state["current_find_selected_execution_contract"] = selected_contract
        self.state["paper_iteration_required"] = False
        self.state["paper_pipeline_skipped"] = True
        self.state["paper_pipeline_skipped_reason"] = gate["paper_pipeline_skipped_reason"]
        self.state["continuation_required"] = True
        self.state["continuation_reason"] = issue
        self.state["current_goal"] = "select exactly one current-Find plan before downstream TASTE execution"
        self.save()
        return cycle

    def finish_literature_recommendation_gate_block(self, cycle: dict[str, Any], pdf_before: dict[str, Any], target_state: dict[str, Any]) -> dict[str, Any]:
        literature_gate = self.literature_gate_status()
        pdf_after = self.latest_pdf_fingerprint()
        gate = self.gate_snapshot()
        gate["pdf_before"] = pdf_before
        gate["pdf_after"] = pdf_after
        gate["pdf_changed_this_cycle"] = self.pdf_changed(pdf_before, pdf_after)
        gate["paper_pipeline_skipped"] = True
        gate["paper_pipeline_skipped_reason"] = "Find recommendation count gate shortfall blocks paper, citation, figure, experiment, base, and claim promotion actions"
        blockers = [
            {
                "category": "literature_recommendation_gate",
                "severity": "block",
                "issue": "; ".join(str(item) for item in literature_gate.get("blockers", [])[:4]) or (
                    f"Find recommended papers are below target: {target_state.get('actual')}/{target_state.get('target')}; shortfall={target_state.get('shortfall')}"
                ),
                "evidence": [
                    str(self.paths.planning / "finding" / "find_progress.json"),
                    str(self.paths.state / "literature_tool_packet.json"),
                    str(self.paths.state / "blocker_action_plan.json"),
                ],
                "next_action": "Repair the current Find literature/scoring packet or record targeted follow-up queries; do not run paper/citation/figure/experiment/base-promotion actions.",
            }
        ]
        blocker_action_plan = read_json(self.paths.state / "blocker_action_plan.json", {})
        gate["blocker_action_plan"] = {
            "status": blocker_action_plan.get("status", "") if isinstance(blocker_action_plan, dict) else "",
            "summary": blocker_action_plan.get("summary", {}) if isinstance(blocker_action_plan, dict) else {},
            "top_actions": (blocker_action_plan.get("actions", [])[:8] if isinstance(blocker_action_plan, dict) and isinstance(blocker_action_plan.get("actions", []), list) else []),
        }
        cycle.update(
            {
                "finished_at": now_iso(),
                "status": "blocked_literature_recommendation_gate",
                "gate": gate,
                "blockers": blockers,
                "pdf_after": pdf_after,
                "pdf_changed": gate["pdf_changed_this_cycle"],
                "paper_pipeline_skipped": True,
            }
        )
        self.state["status"] = "blocked_literature_recommendation_gate"
        self.state["latest_gate"] = gate
        self.state["latest_blockers"] = blockers
        self.state["latest_blocker_action_plan"] = gate["blocker_action_plan"]
        self.state["latest_pdf_info"] = pdf_after
        self.state["paper_iteration_required"] = False
        self.state["paper_pipeline_skipped"] = True
        self.state["paper_pipeline_skipped_reason"] = gate["paper_pipeline_skipped_reason"]
        self.state["continuation_required"] = True
        self.state["continuation_reason"] = "current Find recommended papers below target; repair title+abstract scoring packet only"
        self.state["current_goal"] = (
            f"repair current Find literature gate ({target_state.get('actual')}/{target_state.get('target')}, "
            f"shortfall {target_state.get('shortfall')}); paper/citation/claim promotion blocked"
        )
        self.save()
        return cycle

    def base_switch_gate_authorized(self) -> bool:
        gate = read_json(self.paths.state / "base_switch_gate.json", {})
        return bool(
            isinstance(gate, dict)
            and gate.get("status") == "pass"
            and gate.get("decision") == "authorize_base_switch"
            and gate.get("switch_authorized") is True
        )

    def base_switch_execution_authorized(self) -> bool:
        execution = read_json(self.paths.state / "base_switch_execution.json", {})
        return bool(
            self.base_switch_gate_authorized()
            and isinstance(execution, dict)
            and str(execution.get("status") or "").startswith("authorized_by_deterministic_base_switch_gate")
        )

    def selected_base_viability_gate_blocked(self) -> tuple[bool, dict[str, Any]]:
        gate = read_json(self.paths.state / "selected_base_viability_gate.json", {})
        if not isinstance(gate, dict):
            return False, {}
        blocked = gate.get("status") == "blocked" and gate.get("decision") == "base_switch_gate_required"
        return (blocked, gate)

    def finish_selected_base_viability_gate_block(
        self,
        cycle: dict[str, Any],
        pdf_before: dict[str, Any],
        viability_gate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        guard_report = self.enforce_selected_base_route_guard("selected-base-viability-hard-stop")
        viability_gate = viability_gate if isinstance(viability_gate, dict) else read_json(self.paths.state / "selected_base_viability_gate.json", {})
        base_switch_execution = read_json(self.paths.state / "base_switch_execution.json", {})
        base_switch_gate = read_json(self.paths.state / "base_switch_gate.json", {})
        blocker_action_plan = read_json(self.paths.state / "blocker_action_plan.json", {})
        pdf_after = self.latest_pdf_fingerprint()
        gate = self.gate_snapshot()
        gate["pdf_before"] = pdf_before
        gate["pdf_after"] = pdf_after
        gate["pdf_changed_this_cycle"] = self.pdf_changed(pdf_before, pdf_after)
        gate["paper_pipeline_skipped"] = True
        gate["paper_pipeline_skipped_reason"] = (
            "selected-base viability gate requires a dedicated deterministic base-switch approval chain; "
            "open-ended Claude ideation/blocker repair is suppressed"
        )
        gate["selected_base_viability_gate"] = viability_gate if isinstance(viability_gate, dict) else {}
        gate["base_switch_execution"] = base_switch_execution if isinstance(base_switch_execution, dict) else {}
        gate["base_switch_gate"] = base_switch_gate if isinstance(base_switch_gate, dict) else {}
        gate["selected_base_route_guard"] = guard_report if isinstance(guard_report, dict) else {}
        gate["blocker_action_plan"] = {
            "status": blocker_action_plan.get("status", "") if isinstance(blocker_action_plan, dict) else "",
            "summary": blocker_action_plan.get("summary", {}) if isinstance(blocker_action_plan, dict) else {},
            "top_actions": (blocker_action_plan.get("actions", [])[:8] if isinstance(blocker_action_plan, dict) and isinstance(blocker_action_plan.get("actions", []), list) else []),
        }
        issue = str(
            (viability_gate.get("issue") if isinstance(viability_gate, dict) else "")
            or "selected-base full reference reproduction passed, but current selected repo has no audit-ready promotable project-target candidate; deterministic base-switch gate is required before any route switch."
        )
        evidence = viability_gate.get("evidence", []) if isinstance(viability_gate, dict) and isinstance(viability_gate.get("evidence", []), list) else []
        if not evidence:
            evidence = [
                str(self.paths.state / "selected_base_viability_gate.json"),
                str(self.paths.state / "active_repo.json"),
                str(self.paths.state / "evidence_ready_repo_selection.json"),
                str(self.paths.state / "reference_reproduction_gate.json"),
                str(self.paths.state / "scientific_progress_gate.json"),
                str(self.paths.state / "blocker_action_plan.json"),
            ]
        blockers = [
            {
                "category": "selected_base_viability_gate",
                "severity": "block",
                "issue": issue,
                "evidence": evidence,
                "next_action": (
                    "Keep active_repo/evidence_ready_repo_selection on the trusted selected base. "
                    "Do not call open-ended Claude ideation or blocker repair for an alternative main route. "
                    "Run modules/environment/scripts/audit_deterministic_base_switch_gate.py and require state/base_switch_gate.json to pass before any switch."
                ),
                "human_summary": (
                    "selected-base 参考复现已通过，但当前选中仓库没有可提升的 项目目标候选证据；"
                    "这里应当确定性阻塞并等待专门的 base-switch 审批门控，而不是让 Claude 自由切换到候选/旧路线或继续盲跑实验。"
                ),
            }
        ]
        if isinstance(base_switch_gate, dict) and base_switch_gate:
            gate_status = str(base_switch_gate.get("status") or "")
            gate_decision = str(base_switch_gate.get("decision") or "")
            if not (gate_status == "pass" and gate_decision == "authorize_base_switch"):
                blockers.append(
                    {
                        "category": "base_switch_gate",
                        "severity": "block",
                        "issue": f"base_switch_gate status={gate_status}; decision={gate_decision}; switch_authorized={base_switch_gate.get('switch_authorized', False)}.",
                        "evidence": [str(self.paths.state / "base_switch_gate.json"), str(self.paths.reports / "base_switch_gate.md")],
                        "next_action": "Keep the alternative route as a proposal until the deterministic base-switch gate passes every provenance/loader/data/protocol/smoke/full-reproduction check.",
                    }
                )
        else:
            blockers.append(
                {
                    "category": "base_switch_gate",
                    "severity": "block",
                    "issue": "base_switch_gate has not been generated; selected_base_viability_gate is not switch authorization.",
                    "evidence": [str(self.paths.state / "base_switch_gate.json")],
                    "next_action": "Run modules/environment/scripts/audit_deterministic_base_switch_gate.py before any route switch.",
                }
            )
        if isinstance(base_switch_execution, dict) and base_switch_execution:
            status = str(base_switch_execution.get("status") or "")
            if status.startswith("invalid") or status.startswith("authorized"):
                blockers.append(
                    {
                        "category": "base_switch_execution",
                        "severity": "block",
                        "issue": f"base_switch_execution status={status}; selected_base_viability_gate is not switch authorization.",
                        "evidence": [str(self.paths.state / "base_switch_execution.json")],
                        "next_action": "Treat alternative routes as proposals only until a dedicated deterministic base-switch gate passes.",
                    }
                )
        if isinstance(guard_report, dict) and guard_report.get("violations"):
            blockers.append(
                {
                    "category": "selected_base_route_guard",
                    "severity": "block",
                    "issue": "Selected-base route guard found and repaired route identity contamination.",
                    "evidence": [str(self.paths.state / "selected_base_route_guard.json")],
                    "violations": guard_report.get("violations", []),
                }
            )
        cycle.update(
            {
                "finished_at": now_iso(),
                "status": "blocked_selected_base_viability_gate",
                "gate": gate,
                "blockers": blockers,
                "pdf_after": pdf_after,
                "pdf_changed": gate["pdf_changed_this_cycle"],
                "paper_pipeline_skipped": True,
            }
        )
        self.state["status"] = "blocked_selected_base_viability_gate"
        self.state["latest_gate"] = gate
        self.state["latest_blockers"] = blockers
        self.state["latest_blocker_action_plan"] = gate["blocker_action_plan"]
        self.state["latest_pdf_info"] = pdf_after
        self.state["paper_iteration_required"] = False
        self.state["paper_pipeline_skipped"] = True
        self.state["paper_pipeline_skipped_reason"] = gate["paper_pipeline_skipped_reason"]
        self.state["current_goal"] = "selected-base reference reproduction passed; continue current-route project-target experiment repair"
        self.state["summary"] = "reference reproduction passed; scientific progress is blocked until a current-route audit-ready project-target candidate exists."
        self.state["summary_zh"] = "参考复现已通过；当前缺少当前主线下可审计、可推广的 项目目标候选实验。论文/claim 和自动切基底保持阻塞，TASTE 可继续当前主线实验迭代。"
        self.state["continuation_required"] = True
        self.state["continuation_reason"] = "continue current-route scientific-progress repair; no paper/claim promotion and no automatic route switch"
        self.save()
        return cycle

    def run_cycle(self, cycle_index: int) -> dict[str, Any]:
        pdf_before = self.latest_pdf_fingerprint()
        cycle: dict[str, Any] = {
            "cycle": cycle_index,
            "started_at": now_iso(),
            "status": "running",
            "steps": [],
            "pdf_before": pdf_before,
        }
        self.state["current_cycle"] = cycle_index
        self.state["current_goal"] = "refresh idea, run experiments, build paper, audit gates"
        self.set_current_cycle_record(cycle)
        self.save()

        def step(result: dict[str, Any]) -> None:
            cycle["steps"].append(result)
            self.state["latest_step"] = {"cycle": cycle_index, **{k: result.get(k) for k in ["stage", "return_code", "timed_out", "line_count"]}}
            self.set_current_cycle_record(cycle)
            self.save()

        discovery_first = bool(getattr(self.args, "fresh_start", False) or getattr(self.args, "force_discovery", False) or (cycle_index == 1 and getattr(self.args, "first_cycle_discovery", False)))
        if self.args.use_existing_literature_packet:
            discovery_first = False
            self.state["literature_source"] = "existing_validated_packet"
            self.state["current_goal"] = "use validated fresh literature packet and continue to reference/experiment gates"
            self.save()
        if not discovery_first:
            cycle["current_find_research_plan_status"] = self.ensure_current_find_research_plan(step, stage_suffix="-preflight")
            plan_bridge_gate_preflight = current_find_plan_bridge_gate_status(self.paths, cycle["current_find_research_plan_status"])
            if plan_bridge_gate_preflight.get("blocking"):
                step(self.run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", self.args.project, "--venue", self.args.venue], stage="blocker-action-plan-current-find-plan-gate-preflight", required=False, timeout=180))
                return self.finish_current_find_plan_bridge_gate_block(cycle, pdf_before, plan_bridge_gate_preflight)
            full_text_gate_preflight = current_find_full_text_gate_status(self.paths)
            if full_text_gate_preflight.get("blocking"):
                step(self.run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", self.args.project, "--venue", self.args.venue], stage="blocker-action-plan-current-find-read-gate-preflight", required=False, timeout=180))
                return self.finish_current_find_full_text_gate_block(cycle, pdf_before, full_text_gate_preflight)

        step(self.run([sys.executable, str(SCRIPTS / "sync_third_party_research_stack.py"), "--project", self.args.project], stage="method-stack-sync"))
        if discovery_first:
            self.state["summary"] = "Fresh Find/literature survey is running; waiting for current LLM title and abstract scoring evidence."
            self.state["summary_zh"] = "新的 Find/文献调研正在运行；等待本轮标题筛选、摘要抓取和 LLM 摘要评分产物。"
            self.state["current_goal"] = "run fresh Find/literature survey and replace stale literature outputs"
            self.save()
            step(self.run([sys.executable, str(SCRIPTS / "plan_literature_review.py"), "--project", self.args.project], stage="literature-plan"))
            taste_timeout = max(1800, min(self.args.autonomous_timeout_sec, int(os.environ.get("TIMEOUT_SEC", "14400"))))
            if os.environ.get("FULL_CYCLE_REFRESH_LOCAL_DB", "1").lower() in {"1", "true", "yes", "on"}:
                db_timeout = int(os.environ.get("DB_UPDATE_TIMEOUT_SEC", "1800"))
                step(self.run(
                    [
                        sys.executable,
                        str(SCRIPTS / "update_local_database.py"),
                        "--project",
                        self.args.project,
                        "--if-missing",
                        "--timeout-sec",
                        str(db_timeout),
                    ],
                    stage="taste-local-database-update",
                    timeout=db_timeout + 120,
                ))
            taste_cmd = [
                sys.executable,
                str(SCRIPTS / "run_frontend.py"),
                "--project",
                self.args.project,
                "--deep-survey",
                "--timeout-sec",
                str(taste_timeout),
            ]
            if os.environ.get("FULL_CYCLE_SKIP_HF_GITHUB", "0").lower() in {"1", "true", "yes", "on"}:
                taste_cmd.extend(["--skip-huggingface", "--skip-github"])
            if os.environ.get("FULL_CYCLE_SKIP_VENUES", "0").lower() in {"1", "true", "yes", "on"}:
                taste_cmd.append("--skip-venues")
            if os.environ.get("FULL_CYCLE_FAST_DOWNSTREAM", "1").lower() in {"1", "true", "yes", "on"}:
                taste_cmd.extend(["--max-papers", os.environ.get("FULL_CYCLE_MAX_PAPERS", "20"), "--max-ideas", os.environ.get("FULL_CYCLE_MAX_IDEAS", "5")])
            step(self.run(taste_cmd, stage="literature-survey", timeout=taste_timeout + 120))
            taste_step = cycle["steps"][-1] if cycle.get("steps") else {}
            if int(taste_step.get("return_code") or 0) != 0:
                self.state["status"] = "blocked_literature_survey"
                self.state["current_goal"] = "fresh literature survey failed; do not continue with stale literature packet"
                self.state["latest_blocker"] = {
                    "stage": "literature-survey",
                    "return_code": taste_step.get("return_code"),
                    "tail": str(taste_step.get("stdout_tail") or "")[-4000:],
                    "updated_at": now_iso(),
                }
                cycle.update(
                    {
                        "finished_at": now_iso(),
                        "status": "blocked_literature_survey",
                        "blockers": [
                            {
                                "category": "literature_survey",
                                "severity": "block",
                                "issue": "Fresh finding literature survey failed; The workflow must not continue with stale candidate papers or stale literature packets.",
                                "evidence": [
                                    str(self.paths.logs / "finding_frontend.log"),
                                    str(self.paths.planning / "finding" / "find_results.json"),
                                ],
                                "next_action": "Fix the finding runtime/root cause, rerun fresh discovery, then rebuild the literature packet before base selection or experiments.",
                            }
                        ],
                    }
                )
                self.state["latest_blockers"] = cycle["blockers"]
                self.save()
                return cycle
            step(self.run([sys.executable, str(SCRIPTS / "sync_outputs.py"), "--project", self.args.project, "--allow-empty"], stage="literature-sync", timeout=180))
            step(self.run([sys.executable, str(SCRIPTS / "build_literature_tool_packet.py"), "--project", self.args.project, "--venue", self.args.venue], stage="literature-tool-packet", timeout=180))
            cycle["current_find_research_plan_status"] = self.ensure_current_find_research_plan(step)
            plan_bridge_gate_after_read = current_find_plan_bridge_gate_status(self.paths, cycle["current_find_research_plan_status"])
            if plan_bridge_gate_after_read.get("blocking"):
                step(self.run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", self.args.project, "--venue", self.args.venue], stage="blocker-action-plan-current-find-plan-gate", required=False, timeout=180))
                return self.finish_current_find_plan_bridge_gate_block(cycle, pdf_before, plan_bridge_gate_after_read)
            full_text_gate_after_read = current_find_full_text_gate_status(self.paths)
            if full_text_gate_after_read.get("blocking"):
                step(self.run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", self.args.project, "--venue", self.args.venue], stage="blocker-action-plan-current-find-read-gate", required=False, timeout=180))
                return self.finish_current_find_full_text_gate_block(cycle, pdf_before, full_text_gate_after_read)
            target_state_after_literature = recommendation_target_status(self.paths)
            if target_state_after_literature.get("blocking"):
                step(self.run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", self.args.project, "--venue", self.args.venue], stage="blocker-action-plan-literature-gate", required=False, timeout=180))
                return self.finish_literature_recommendation_gate_block(cycle, pdf_before, target_state_after_literature)
            selected_contract_after_read = current_find_execution_contract(self.paths)
            if self.current_find_selected_plan_gate_blocking(selected_contract_after_read):
                step(self.run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", self.args.project, "--venue", self.args.venue], stage="blocker-action-plan-current-find-selected-plan-gate", required=False, timeout=180))
                return self.finish_current_find_selected_plan_gate_block(cycle, pdf_before, selected_contract_after_read)
            step(self.run([sys.executable, str(SCRIPTS / "select_fresh_research_base.py"), "--project", self.args.project], stage="fresh-research-base-selection", timeout=180))
            step(self.run([sys.executable, str(SCRIPTS / "prepare_initialization.py"), "--project", self.args.project], stage="initialization-brief"))
            survey_summary = self.literature_after_survey_summary()
            cycle["literature_after_survey"] = survey_summary
            self.state["literature_after_survey"] = survey_summary
            self.state["current_goal"] = "fresh literature survey complete; re-evaluate base route before experiments"
            self.save()
        else:
            step(self.run([sys.executable, str(SCRIPTS / "sync_outputs.py"), "--project", self.args.project, "--allow-empty"], stage="literature-sync-existing", required=False, timeout=180))
            step(self.run([sys.executable, str(SCRIPTS / "build_literature_tool_packet.py"), "--project", self.args.project, "--venue", self.args.venue], stage="literature-tool-packet-refresh", required=False, timeout=180))
            cycle["current_find_research_plan_status"] = self.ensure_current_find_research_plan(step, stage_suffix="-refresh")
            plan_bridge_gate_after_read = current_find_plan_bridge_gate_status(self.paths, cycle["current_find_research_plan_status"])
            if plan_bridge_gate_after_read.get("blocking"):
                step(self.run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", self.args.project, "--venue", self.args.venue], stage="blocker-action-plan-current-find-plan-gate-refresh", required=False, timeout=180))
                return self.finish_current_find_plan_bridge_gate_block(cycle, pdf_before, plan_bridge_gate_after_read)
            full_text_gate_after_read = current_find_full_text_gate_status(self.paths)
            if full_text_gate_after_read.get("blocking"):
                step(self.run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", self.args.project, "--venue", self.args.venue], stage="blocker-action-plan-current-find-read-gate-refresh", required=False, timeout=180))
                return self.finish_current_find_full_text_gate_block(cycle, pdf_before, full_text_gate_after_read)
            target_state_after_literature = recommendation_target_status(self.paths)
            if target_state_after_literature.get("blocking"):
                step(self.run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", self.args.project, "--venue", self.args.venue], stage="blocker-action-plan-literature-gate-refresh", required=False, timeout=180))
                return self.finish_literature_recommendation_gate_block(cycle, pdf_before, target_state_after_literature)
            selected_contract_after_read = current_find_execution_contract(self.paths)
            if self.current_find_selected_plan_gate_blocking(selected_contract_after_read):
                step(self.run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", self.args.project, "--venue", self.args.venue], stage="blocker-action-plan-current-find-selected-plan-gate-refresh", required=False, timeout=180))
                return self.finish_current_find_selected_plan_gate_block(cycle, pdf_before, selected_contract_after_read)
            step(self.run([sys.executable, str(SCRIPTS / "select_fresh_research_base.py"), "--project", self.args.project], stage="fresh-research-base-selection-refresh", required=False, timeout=180))
            survey_summary = self.literature_after_survey_summary()
            cycle["literature_after_survey"] = survey_summary
            self.state["literature_after_survey"] = survey_summary
            self.save()
        step(self.run([sys.executable, str(SCRIPTS / "assess_literature_base_candidates.py"), "--project", self.args.project], stage="literature-base-candidate-assessment", required=False, timeout=180))
        step(self.run([sys.executable, str(SCRIPTS / "audit_reference_reproduction.py"), "--project", self.args.project, "--venue", self.args.venue], stage="reference-reproduction-gate-initial"))
        step(self.run([sys.executable, str(SCRIPTS / "build_research_trajectory_system.py"), "--project", self.args.project, "--venue", self.args.venue], stage="trajectory-refresh"))
        step(self.run([sys.executable, str(SCRIPTS / "audit_selected_base_viability.py"), "--project", self.args.project, "--venue", self.args.venue], stage="selected-base-viability-initial"))
        selected_base_preblocked, _selected_base_pregate = self.selected_base_viability_gate_blocked()
        if selected_base_preblocked:
            step(self.run([sys.executable, str(SCRIPTS / "audit_deterministic_base_switch_gate.py"), "--project", self.args.project, "--venue", self.args.venue], stage="base-switch-gate-initial", required=False, timeout=180))
            if self.base_switch_gate_authorized() and not self.base_switch_execution_authorized():
                step(self.run([sys.executable, str(SCRIPTS / "execute_authorized_base_switch.py"), "--project", self.args.project, "--venue", self.args.venue], stage="base-switch-execution-initial", required=False, timeout=180))
        step(self.run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", self.args.project, "--venue", self.args.venue], stage="blocker-action-plan-initial"))
        selected_base_blocked, selected_base_gate = self.selected_base_viability_gate_blocked()
        if selected_base_blocked:
            cycle["selected_base_viability_gate"] = selected_base_gate
            self.state["selected_base_viability_notice"] = {
                "status": selected_base_gate.get("status", "") if isinstance(selected_base_gate, dict) else "blocked",
                "decision": selected_base_gate.get("decision", "") if isinstance(selected_base_gate, dict) else "continue_experiment_evidence_repair",
                "policy": "candidate/alternative-route experiments, paper/claim promotion, and automatic route switching are blocked; current selected-base evidence repair requires launcher route_scope=selected_base_current_route and artifact-local audit contracts",
            }
            self.state["current_goal"] = "refresh deterministic base-switch gate and blocker action plan; keep candidate routes proposal-only until gate pass; allow launcher-scoped current-route repair and bounded base-switch evidence collection"
            self.save()

        target_state_after_plan = recommendation_target_status(self.paths)
        if target_state_after_plan.get("blocking"):
            literature_gate = self.literature_gate_status()
            pdf_after = self.latest_pdf_fingerprint()
            gate = self.gate_snapshot()
            gate["pdf_before"] = pdf_before
            gate["pdf_after"] = pdf_after
            gate["pdf_changed_this_cycle"] = self.pdf_changed(pdf_before, pdf_after)
            gate["paper_pipeline_skipped"] = True
            gate["paper_pipeline_skipped_reason"] = "Find recommendation count gate shortfall blocks paper, citation, figure, experiment, base, and claim promotion actions"
            blockers = [
                {
                    "category": "literature_recommendation_gate",
                    "severity": "block",
                    "issue": "; ".join(str(item) for item in literature_gate.get("blockers", [])[:4]) or (
                        f"Find recommended papers are below target: {target_state_after_plan.get('actual')}/{target_state_after_plan.get('target')}; shortfall={target_state_after_plan.get('shortfall')}"
                    ),
                    "evidence": [
                        str(self.paths.planning / "finding" / "find_progress.json"),
                        str(self.paths.state / "literature_tool_packet.json"),
                        str(self.paths.state / "blocker_action_plan.json"),
                    ],
                    "next_action": "Repair the current Find literature/scoring packet or record targeted follow-up queries; do not run paper/citation/figure/experiment/base-promotion actions.",
                }
            ]
            blocker_action_plan = read_json(self.paths.state / "blocker_action_plan.json", {})
            gate["blocker_action_plan"] = {
                "status": blocker_action_plan.get("status", "") if isinstance(blocker_action_plan, dict) else "",
                "summary": blocker_action_plan.get("summary", {}) if isinstance(blocker_action_plan, dict) else {},
                "top_actions": (blocker_action_plan.get("actions", [])[:8] if isinstance(blocker_action_plan, dict) and isinstance(blocker_action_plan.get("actions", []), list) else []),
            }
            cycle.update(
                {
                    "finished_at": now_iso(),
                    "status": "blocked_literature_recommendation_gate",
                    "gate": gate,
                    "blockers": blockers,
                    "pdf_after": pdf_after,
                    "pdf_changed": gate["pdf_changed_this_cycle"],
                    "paper_pipeline_skipped": True,
                }
            )
            self.state["status"] = "blocked_literature_recommendation_gate"
            self.state["latest_gate"] = gate
            self.state["latest_blockers"] = blockers
            self.state["latest_blocker_action_plan"] = gate["blocker_action_plan"]
            self.state["latest_pdf_info"] = pdf_after
            self.state["paper_iteration_required"] = False
            self.state["paper_pipeline_skipped"] = True
            self.state["paper_pipeline_skipped_reason"] = gate["paper_pipeline_skipped_reason"]
            self.state["continuation_required"] = True
            self.state["continuation_reason"] = "current Find recommended papers below target; repair title+abstract scoring packet only"
            self.state["current_goal"] = (
                f"repair current Find literature gate ({target_state_after_plan.get('actual')}/{target_state_after_plan.get('target')}, "
                f"shortfall {target_state_after_plan.get('shortfall')}); paper/citation/claim promotion blocked"
            )
            self.save()
            return cycle

        selected_contract_after_plan = current_find_execution_contract(self.paths)
        if self.current_find_selected_plan_gate_blocking(selected_contract_after_plan):
            step(self.run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", self.args.project, "--venue", self.args.venue], stage="blocker-action-plan-current-find-selected-plan-gate-after-plan", required=False, timeout=180))
            return self.finish_current_find_selected_plan_gate_block(cycle, pdf_before, selected_contract_after_plan)

        reference_blocked, reference_reasons, reference_gate = self.reference_reproduction_gate_blocked()
        if reference_blocked:
            blocker_action_plan = read_json(self.paths.state / "blocker_action_plan.json", {})
            reference_repair_skipped = False
            if reference_gate.get("decision") == "literature_base_audit_required":
                self.state["current_goal"] = "fresh Find base candidates require repo/data/env audit before any legacy route can remain the main route"
                self.state["reference_base_switch_required"] = True
                self.state["reference_base_switch_exhausted"] = False
                self.state["paper_pipeline_skipped"] = True
                self.state["paper_pipeline_skipped_reason"] = "fresh literature base candidates are not yet repo/data/env audited"
                self.state["continuation_required"] = True
                self.state["continuation_reason"] = "repo/data/env audit pending for fresh literature base candidates"
                self.save()
                reference_repair_skipped = True
            elif reference_gate.get("decision") in {"fresh_base_implementation_required", "fresh_base_reference_probe_required", "fresh_base_reference_smoke_required", "fresh_base_reference_reproduction_required"}:
                plan_result = self.refresh_fresh_base_implementation_plan(reason="reference gate requires environment-stage selected anchor implementation route")
                step(plan_result)
                ctx = fresh_base_route_context(self.paths, self.args.project)
                self.state["current_goal"] = f"environment-stage anchor selected; checking data/env contract for {ctx.get('title') or 'environment-stage selected anchor'} before Claude implementation"
                self.state["reference_base_switch_required"] = True
                self.state["reference_base_switch_exhausted"] = False
                self.state["paper_pipeline_skipped"] = True
                self.state["paper_pipeline_skipped_reason"] = "fresh paper base needs code/data/protocol implementation route"
                self.state["continuation_required"] = True
                self.state["continuation_reason"] = "run bounded selected-base evidence probes; do not run any legacy/control route as the main route"
                self.save()
                step(self.run([sys.executable, str(SCRIPTS / "run_safe_unblock.py"), "--project", self.args.project, "--venue", self.args.venue], stage="selected-base-safe-unblock", required=False, timeout=1200))
                fresh_after_probe = read_json(self.paths.state / "fresh_base_implementation_plan.json", {})
                if isinstance(fresh_after_probe, dict) and fresh_after_probe.get("status") != "implementation_ready_for_reference_probe":
                    block_category = fresh_base_block_category(fresh_after_probe)
                elif fresh_base_reference_smoke_passed(self.paths):
                    block_category = "blocked_fresh_base_reference_reproduction_required"
                elif fresh_base_reference_protocol_passed(self.paths):
                    block_category = "blocked_fresh_base_reference_smoke_required"
                else:
                    block_category = "blocked_fresh_base_reference_probe_required"
                self.state["status"] = block_category
                goal, next_action, skipped_reason = fresh_base_status_text(block_category, ctx)
                self.state["current_goal"] = goal
                self.state["continuation_required"] = True
                self.state["continuation_reason"] = next_action
                self.state["paper_pipeline_skipped"] = True
                self.state["paper_pipeline_skipped_reason"] = skipped_reason
                if isinstance(fresh_after_probe, dict):
                    self.state["fresh_base_implementation_plan"] = {
                        "status": fresh_after_probe.get("status", ""),
                        "repo": fresh_after_probe.get("repo", {}),
                        "ready_datasets": fresh_after_probe.get("ready_datasets", []),
                        "blocked_datasets": fresh_after_probe.get("blocked_datasets", []),
                        "blocker_reasons": fresh_after_probe.get("blocker_reasons", []),
                        "data_acquisition": fresh_after_probe.get("fresh_base_data_acquisition", {}),
                    }
                self.save()
                step(self.run([sys.executable, str(SCRIPTS / "audit_reference_reproduction.py"), "--project", self.args.project, "--venue", self.args.venue], stage="reference-reproduction-gate-after-fresh-base-unblock", required=False, timeout=180))
                step(self.run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", self.args.project, "--venue", self.args.venue], stage="blocker-action-plan-after-fresh-base-unblock", required=False, timeout=180))
                reference_repair_skipped = True
            elif self.reference_base_switch_exhausted(reference_gate):
                self.state["current_goal"] = "reference reproduction blocked and base-switch exhausted; paper/claim promotion remains blocked"
                self.state["reference_base_switch_required"] = False
                self.state["reference_base_switch_exhausted"] = True
                self.state["paper_pipeline_skipped"] = True
                self.state["paper_pipeline_skipped_reason"] = "reference reproduction blocked and no evidence-ready alternative base exists"
                self.save()
                reference_repair_skipped = True
            elif reference_gate.get("decision") == "switch_base":
                self.state["current_goal"] = "switch blocked reference base using fresh finding literature packet"
                self.state["reference_base_switch_required"] = True
                self.save()
                step(self.claude(self.reference_base_switch_prompt(cycle_index, reference_reasons, reference_gate, blocker_action_plan), stage="reference-base-switch"))
            else:
                step(self.claude(self.reference_reproduction_repair_prompt(cycle_index, reference_reasons, reference_gate, blocker_action_plan), stage="reference-reproduction-repair"))
            if not reference_repair_skipped:
                wait_result = self.wait_for_background_experiments("wait-reference-reproduction-experiments")
                step(wait_result)
            else:
                wait_result = {
                    "stage": "wait-reference-reproduction-experiments",
                    "status": "skipped",
                    "timed_out": False,
                    "reason": "fresh literature/base implementation gate handled without launching legacy-route/reference repair",
                }
                step(wait_result)
            if wait_result.get("timed_out"):
                pdf_after = self.latest_pdf_fingerprint()
                gate = self.gate_snapshot()
                gate["pdf_before"] = pdf_before
                gate["pdf_after"] = pdf_after
                gate["pdf_changed_this_cycle"] = self.pdf_changed(pdf_before, pdf_after)
                gate["paper_pipeline_skipped"] = True
                gate["paper_pipeline_skipped_reason"] = "background reference reproduction experiment is still running; gates must wait for completed output"
                blockers = self.collect_blockers(gate)
                blockers.insert(
                    0,
                    {
                        "category": "background_experiment_running",
                        "severity": "block",
                        "issue": "A real-data reference reproduction experiment is still running, so TASTE cannot use stale gate results or continue to paper production.",
                        "evidence": [str(self.paths.root / "artifacts")],
                        "next_action": "Wait for the experiment to finish, postprocess metrics/audit artifacts, then rerun reference/scientific evidence gates.",
                    },
                )
                cycle.update(
                    {
                        "finished_at": now_iso(),
                        "status": "waiting_for_background_experiment",
                        "gate": gate,
                        "blockers": blockers,
                        "pdf_after": pdf_after,
                        "pdf_changed": gate["pdf_changed_this_cycle"],
                        "paper_pipeline_skipped": True,
                    }
                )
                self.state["latest_gate"] = gate
                self.state["latest_blockers"] = blockers
                self.state["latest_pdf_info"] = pdf_after
                self.state["paper_iteration_required"] = False
                self.state["continuation_required"] = True
                self.state["continuation_reason"] = "background experiment is still running"
                self.state["current_goal"] = "waiting for background experiment before rerunning gates"
                self.save()
                return cycle
            if not reference_repair_skipped:
                self.postprocess_and_refresh_experiment_gates(step, suffix="-after-reference-wait")
                step(self.run([sys.executable, str(SCRIPTS / "audit_reference_reproduction.py"), "--project", self.args.project, "--venue", self.args.venue], stage="reference-reproduction-gate-after-repair"))
                step(self.run([sys.executable, str(SCRIPTS / "audit_paper_evidence.py"), "--project", self.args.project, "--venue", self.args.venue], stage="paper-evidence-audit-reference-gate"))
                step(self.run([sys.executable, str(SCRIPTS / "audit_submission_readiness.py"), "--project", self.args.project, "--venue", self.args.venue], stage="submission-readiness-reference-gate"))
                step(self.run([sys.executable, str(SCRIPTS / "build_research_trajectory_system.py"), "--project", self.args.project, "--venue", self.args.venue], stage="trajectory-reference-gate-refresh"))
                step(self.run([sys.executable, str(SCRIPTS / "build_blocker_action_plan.py"), "--project", self.args.project, "--venue", self.args.venue], stage="blocker-action-plan-reference-gate"))
            reference_blocked, reference_reasons, reference_gate = self.reference_reproduction_gate_blocked()
            if reference_blocked:
                pdf_after = self.latest_pdf_fingerprint()
                gate = self.gate_snapshot()
                gate["reference_reproduction_gate"] = reference_gate
                gate["pdf_before"] = pdf_before
                gate["pdf_after"] = pdf_after
                gate["pdf_changed_this_cycle"] = self.pdf_changed(pdf_before, pdf_after)
                gate["paper_pipeline_skipped"] = True
                gate["paper_pipeline_skipped_reason"] = "reference reproduction gate blocked before novel experiments or paper production"
                gate["experiment_evidence_blockers"] = reference_reasons
                decision = str(reference_gate.get("decision") or "")
                blockers = self.collect_blockers(gate)
                if decision in {"fresh_base_implementation_required", "fresh_base_reference_probe_required", "fresh_base_reference_smoke_required", "fresh_base_reference_reproduction_required"}:
                    fresh_base = {}
                    base_switch = reference_gate.get("base_switch", {}) if isinstance(reference_gate.get("base_switch"), dict) else {}
                    if isinstance(base_switch.get("fresh_paper_base"), dict):
                        fresh_base = base_switch.get("fresh_paper_base", {})
                    title = str(fresh_base.get("title") or "environment-stage selected anchor")
                    plan = read_json(self.paths.state / "fresh_base_implementation_plan.json", {})
                    plan_status = str(plan.get("status") or "") if isinstance(plan, dict) else ""
                    plan_blockers = plan.get("blocker_reasons", []) if isinstance(plan, dict) and isinstance(plan.get("blocker_reasons"), list) else []
                    ctx = fresh_base_route_context(self.paths, self.args.project)
                    block_category = fresh_base_block_category(
                        plan,
                        reference_probe_required=decision == "fresh_base_reference_probe_required",
                        reference_smoke_required=decision == "fresh_base_reference_smoke_required",
                        reference_reproduction_required=decision == "fresh_base_reference_reproduction_required",
                    )
                    goal, next_action, _ = fresh_base_status_text(block_category, ctx)
                    blockers = [
                        {
                            "category": decision,
                            "severity": "block",
                            "issue": (
                                f"{goal}: {title}. {next_action}"
                                + (f" implementation_plan_status={plan_status}; blockers={'; '.join(str(item) for item in plan_blockers[:4])}." if plan_status or plan_blockers else "")
                            ),
                            "evidence": [
                                str(self.paths.state / "fresh_research_base.json"),
                                str(self.paths.state / "fresh_base_implementation_plan.json"),
                                str(self.paths.state / "literature_tool_packet.json"),
                                str(self.paths.planning / "finding" / "find_results.json"),
                                str(self.paths.state / "reference_reproduction_gate.json"),
                                str(self.paths.state / "blocker_action_plan.json"),
                            ],
                            "next_action": next_action,
                        }
                    ]
                else:
                    blockers.insert(
                        0,
                        {
                            "category": "reference_reproduction_gate",
                            "severity": "block",
                            "issue": "; ".join(reference_reasons[:8]),
                            "evidence": [
                                str(self.paths.state / "reference_reproduction_gate.json"),
                                str(self.paths.reports / "reference_reproduction_gate.md"),
                                str(self.paths.state / "blocker_action_plan.json"),
                            ],
                            "next_action": "Audit fresh Find literature base candidates before repairing any legacy route or choosing a new base.",
                        },
                    )
                literature_summary = cycle.get("literature_after_survey") if isinstance(cycle.get("literature_after_survey"), dict) else self.state.get("literature_after_survey", {})
                if decision != "fresh_base_implementation_required" and isinstance(literature_summary, dict) and literature_summary:
                    blockers.insert(
                        1,
                        {
                            "category": "literature_to_base_route",
                            "severity": "block",
                            "issue": (
                                "Fresh literature survey is available, but the active reference base is still blocked. "
                                "The workflow must use the new literature packet to repair or switch the base route before experiments or paper writing."
                            ),
                            "evidence": [
                                str(self.paths.state / "literature_tool_packet.json"),
                                str(self.paths.planning / "finding" / "find_results.json"),
                                str(self.paths.state / "reference_reproduction_gate.json"),
                            ],
                            "next_action": "Run repo/data/env audit for the top literature/base-work candidates, then rerun reference reproduction gate.",
                            "human_summary": (
                                "调研已经刷新，但当前基底仍卡在复现/可比性门控；所以实验和论文不会继续更新。"
                                "下一步必须基于新调研完成候选基底的代码/数据/环境审计。"
                            ),
                        },
                    )
                blocker_action_plan = read_json(self.paths.state / "blocker_action_plan.json", {})
                gate["blocker_action_plan"] = {
                    "status": blocker_action_plan.get("status", "") if isinstance(blocker_action_plan, dict) else "",
                    "summary": blocker_action_plan.get("summary", {}) if isinstance(blocker_action_plan, dict) else {},
                    "top_actions": (blocker_action_plan.get("actions", [])[:8] if isinstance(blocker_action_plan, dict) and isinstance(blocker_action_plan.get("actions", []), list) else []),
                }
                fresh_base_plan = read_json(self.paths.state / "fresh_base_implementation_plan.json", {})
                if isinstance(fresh_base_plan, dict) and fresh_base_plan:
                    gate["fresh_base_implementation_plan"] = {
                        "status": fresh_base_plan.get("status", ""),
                        "repo": fresh_base_plan.get("repo", {}),
                        "ready_datasets": fresh_base_plan.get("ready_datasets", []),
                        "blocked_datasets": fresh_base_plan.get("blocked_datasets", []),
                        "blocker_reasons": fresh_base_plan.get("blocker_reasons", []),
                    }
                cycle.update(
                    {
                        "finished_at": now_iso(),
                        "status": "blocked_reference_reproduction_gate",
                        "gate": gate,
                        "blockers": blockers,
                        "pdf_after": pdf_after,
                        "pdf_changed": gate["pdf_changed_this_cycle"],
                        "paper_pipeline_skipped": True,
                    }
                )
                self.state["latest_gate"] = gate
                self.state["latest_blockers"] = blockers
                self.state["latest_blocker_action_plan"] = gate["blocker_action_plan"]
                self.state["latest_pdf_info"] = pdf_after
                self.state["paper_iteration_required"] = False
                if reference_gate.get("decision") == "literature_base_audit_required":
                    self.state["status"] = "blocked_literature_base_audit_required"
                    self.state["current_goal"] = "fresh Find base candidates must be audited before choosing a legacy route or a new base"
                    self.state["continuation_required"] = True
                    self.state["continuation_reason"] = "run repo/data/env audit for fresh literature base candidates"
                    self.state["reference_base_switch_exhausted"] = False
                    self.state["paper_pipeline_skipped"] = True
                    self.state["paper_pipeline_skipped_reason"] = "fresh literature base candidates are not yet repo/data/env audited"
                elif reference_gate.get("decision") in {"fresh_base_implementation_required", "fresh_base_reference_probe_required", "fresh_base_reference_smoke_required", "fresh_base_reference_reproduction_required"}:
                    decision = str(reference_gate.get("decision") or "")
                    block_category = fresh_base_block_category(
                        fresh_base_plan,
                        reference_probe_required=decision == "fresh_base_reference_probe_required",
                        reference_smoke_required=decision == "fresh_base_reference_smoke_required",
                        reference_reproduction_required=decision == "fresh_base_reference_reproduction_required",
                    )
                    self.state["status"] = block_category
                    ctx = fresh_base_route_context(self.paths, self.args.project)
                    goal, next_action, skipped_reason = fresh_base_status_text(block_category, ctx)
                    self.state["current_goal"] = goal
                    self.state["continuation_reason"] = next_action
                    self.state["paper_pipeline_skipped_reason"] = skipped_reason
                    self.state["continuation_required"] = True
                    self.state["reference_base_switch_exhausted"] = False
                    if isinstance(fresh_base_plan, dict) and fresh_base_plan:
                        self.state["fresh_base_implementation_plan"] = {
                            "status": fresh_base_plan.get("status", ""),
                            "repo": fresh_base_plan.get("repo", {}),
                            "ready_datasets": fresh_base_plan.get("ready_datasets", []),
                            "blocked_datasets": fresh_base_plan.get("blocked_datasets", []),
                            "blocker_reasons": fresh_base_plan.get("blocker_reasons", []),
                            "data_acquisition": fresh_base_plan.get("fresh_base_data_acquisition", {}),
                        }
                    self.state["paper_pipeline_skipped"] = True
                elif self.reference_base_switch_exhausted(reference_gate):
                    self.state["status"] = "blocked_no_viable_reference_base"
                    self.state["current_goal"] = "reference reproduction blocked and base-switch exhausted; await new base/data/protocol/compute evidence"
                    self.state["continuation_required"] = False
                    self.state["continuation_reason"] = "no autonomous route remains without new external evidence"
                    self.state["reference_base_switch_exhausted"] = True
                    self.state["paper_pipeline_skipped"] = True
                    self.state["paper_pipeline_skipped_reason"] = "reference reproduction blocked and no evidence-ready alternative base exists"
                else:
                    self.state["current_goal"] = "repair paper-level reference reproduction or switch base before novel experiments"
                self.save()
                return cycle

        idea_result = self.claude(self.idea_prompt(cycle_index), stage="full-cycle-ideation")
        step(idea_result)
        if str(idea_result.get("status") or "") == "blocked_selected_base_route_guard" or idea_result.get("guarded_block") == "selected_base_route_identity_restored":
            pdf_after = self.latest_pdf_fingerprint()
            gate = self.gate_snapshot()
            gate["pdf_before"] = pdf_before
            gate["pdf_after"] = pdf_after
            gate["pdf_changed_this_cycle"] = self.pdf_changed(pdf_before, pdf_after)
            gate["paper_pipeline_skipped"] = True
            gate["paper_pipeline_skipped_reason"] = "selected-base route guard blocked a legacy/control route overwrite before experiments"
            guard_report = idea_result.get("selected_base_route_guard", {}) if isinstance(idea_result.get("selected_base_route_guard"), dict) else {}
            blockers = [
                {
                    "category": "selected_base_route_guard",
                    "severity": "block",
                    "issue": "Claude attempted to overwrite the current selected-base identity after wrapper-managed full reference reproduction had passed; TASTE restored the trusted selected-base route and stopped this turn.",
                    "evidence": [
                        str(self.paths.state / "selected_base_route_guard.json"),
                        str(self.paths.state / "fresh_base_reference_reproduction_audit.json"),
                        str(self.paths.state / "evidence_ready_repo_selection.json"),
                        str(self.paths.state / "active_repo.json"),
                    ],
                    "next_action": "Restart the full cycle from deterministic selected-base context; route-switch proposals must be recorded as non-authoritative rationale until TASTE base-switch gates approve them.",
                    "guard_status": guard_report.get("status"),
                    "violations": guard_report.get("violations", []),
                }
            ]
            cycle.update(
                {
                    "finished_at": now_iso(),
                    "status": "blocked_selected_base_route_guard",
                    "gate": gate,
                    "blockers": blockers,
                    "pdf_after": pdf_after,
                    "pdf_changed": gate["pdf_changed_this_cycle"],
                    "paper_pipeline_skipped": True,
                }
            )
            self.state["latest_gate"] = gate
            self.state["latest_blockers"] = blockers
            self.state["latest_pdf_info"] = pdf_after
            self.state["status"] = "blocked_selected_base_route_guard"
            self.state["current_goal"] = "selected-base route guard restored trusted route; restart full cycle from current selected-base context"
            self.state["continuation_required"] = True
            self.state["continuation_reason"] = "selected-base route overwrite blocked"
            self.state["paper_pipeline_skipped"] = True
            self.state["paper_pipeline_skipped_reason"] = gate["paper_pipeline_skipped_reason"]
            self.save()
            return cycle

        ideation_block_status = claude_ideation_block_status(idea_result)
        if ideation_block_status:
            pdf_after = self.latest_pdf_fingerprint()
            gate = self.gate_snapshot()
            gate["pdf_before"] = pdf_before
            gate["pdf_after"] = pdf_after
            gate["pdf_changed_this_cycle"] = self.pdf_changed(pdf_before, pdf_after)
            gate["paper_pipeline_skipped"] = True
            gate["paper_pipeline_skipped_reason"] = "full-cycle ideation did not complete cleanly; autonomous experiment execution is blocked"
            tool_guard = idea_result.get("tool_policy_guard") if isinstance(idea_result.get("tool_policy_guard"), dict) else {}
            if ideation_block_status == "blocked_tool_policy":
                issue = "Full-cycle ideation was blocked by the Claude tool policy guard; TASTE must stop before autonomous research instead of continuing from an old selected plan."
                state_status = "blocked_tool_policy"
                current_goal = "full-cycle ideation was blocked by tool policy; repair the prompt/tool path before autonomous research"
                continuation_reason = str(tool_guard.get("reason") or "Claude tool policy blocked the ideation turn")
            else:
                issue = f"Full-cycle ideation ended with status={ideation_block_status}; TASTE cannot safely enter autonomous research without a completed ideation decision."
                state_status = "blocked_full_cycle_ideation"
                current_goal = "full-cycle ideation did not complete; repair ideation before autonomous research"
                continuation_reason = issue
            blockers = [
                {
                    "category": "full_cycle_ideation",
                    "severity": "block",
                    "issue": issue,
                    "evidence": [
                        str(self.paths.state / "full_cycle_prompt_full-cycle-ideation.md"),
                        str(self.paths.state / "claude_project_session_last_result.json"),
                        str(self.paths.state / "selected_base_route_guard.json"),
                    ],
                    "next_action": "Repair the ideation/tool-policy path and rerun full-cycle from the web UI; do not launch autonomous research until ideation completes or records an explicit scientific blocker.",
                    "ideation_status": ideation_block_status,
                    "tool_policy_guard": tool_guard,
                }
            ]
            cycle.update(
                {
                    "finished_at": now_iso(),
                    "status": state_status,
                    "gate": gate,
                    "blockers": blockers,
                    "pdf_after": pdf_after,
                    "pdf_changed": gate["pdf_changed_this_cycle"],
                    "paper_pipeline_skipped": True,
                }
            )
            self.state["latest_gate"] = gate
            self.state["latest_blockers"] = blockers
            self.state["latest_pdf_info"] = pdf_after
            self.state["status"] = state_status
            self.state["current_goal"] = current_goal
            self.state["continuation_required"] = True
            self.state["continuation_reason"] = continuation_reason
            self.state["paper_pipeline_skipped"] = True
            self.state["paper_pipeline_skipped_reason"] = gate["paper_pipeline_skipped_reason"]
            self.save()
            return cycle

        idea_tail = str(idea_result.get("stdout_tail") or idea_result.get("stdout") or "")
        idea_tail_lower = idea_tail.lower()
        semantic_provenance_blocked = any(
            marker in idea_tail_lower
            for marker in [
                "execution-blocked",
                "cannot be executed on this data",
                "data provenance blocker",
                "data provenance constraint",
                "p0 semantic data provenance",
            ]
        ) and any(
            marker in idea_tail_lower
            for marker in [
                "opaque integer",
                "no item text",
                "lacks item text",
                "zero item text",
                "without item text",
                "heuristically mapping",
            ]
        )
        if semantic_provenance_blocked:
            pdf_after = self.latest_pdf_fingerprint()
            gate = self.gate_snapshot()
            gate["pdf_before"] = pdf_before
            gate["pdf_after"] = pdf_after
            gate["pdf_changed_this_cycle"] = self.pdf_changed(pdf_before, pdf_after)
            gate["paper_pipeline_skipped"] = True
            gate["paper_pipeline_skipped_reason"] = "ideation recorded a semantic data provenance blocker before autonomous experiment execution"
            blockers = [
                {
                    "category": "semantic_data_provenance_blocker",
                    "severity": "block",
                    "issue": "Project ideation concluded the current selected route cannot execute the LLM/semantic experiment because the selected data exposes only opaque IDs without a preserved text/metadata mapping.",
                    "evidence": [
                        str(self.paths.state / "full_cycle_prompt_full-cycle-ideation.md"),
                        str(self.paths.state / "claude_project_session_last_result.json"),
                        str(self.paths.root / "data" / "amazon-beauty" / "README_UNTRUSTED_MAPPING.txt"),
                    ],
                    "next_action": "Record the provenance blocker or switch through deterministic base-switch gates; do not launch autonomous research from the stale selected plan until a preserved ID map or auditable preprocessing rerun exists.",
                }
            ]
            cycle.update(
                {
                    "finished_at": now_iso(),
                    "status": "blocked_semantic_data_provenance",
                    "gate": gate,
                    "blockers": blockers,
                    "pdf_after": pdf_after,
                    "pdf_changed": gate["pdf_changed_this_cycle"],
                    "paper_pipeline_skipped": True,
                }
            )
            self.state["latest_gate"] = gate
            self.state["latest_blockers"] = blockers
            self.state["latest_pdf_info"] = pdf_after
            self.state["status"] = "blocked_semantic_data_provenance"
            self.state["current_goal"] = "semantic data provenance blocker recorded; stop before autonomous research and repair route evidence"
            self.state["continuation_required"] = True
            self.state["continuation_reason"] = "current selected route lacks auditable text/metadata identity for LLM semantic experiments"
            self.state["paper_pipeline_skipped"] = True
            self.state["paper_pipeline_skipped_reason"] = gate["paper_pipeline_skipped_reason"]
            self.save()
            return cycle

        autonomous = [
            sys.executable,
            str(SCRIPTS / "run_autonomous_research.py"),
            "--project",
            self.args.project,
            "--iterations",
            str(self.args.iterations_per_cycle),
            "--venue",
            self.args.venue,
            "--execute-plan",
            "--prepare-env",
            "--real-bootstrap-env",
        ]
        if self.args.force_discovery and not self.args.use_existing_literature_packet:
            autonomous.append("--deep-literature-survey")
        else:
            literature_gate = self.literature_gate_status()
            if literature_gate.get("status") == "positive_anchors_ready":
                autonomous.append("--skip-discovery")
            else:
                # Preserve the validated current Find packet. Targeted literature
                # repair may launch a controlled targeted Find through the wrapper;
                # downstream experiment/paper/base promotion stays blocked until the literature gate clears.
                autonomous.append("--skip-discovery")
                self.state["literature_gate"] = literature_gate
                self.state["literature_repair_policy"] = "targeted_find_allowed"
                if literature_gate.get("status") == "recommendation_shortfall":
                    self.state["current_goal"] = (
                        f"repair current Find recommendation shortfall "
                        f"({literature_gate.get('strong_recommendations', 0)}/{literature_gate.get('recommendation_target_count', 0)}); "
                        "run controlled targeted literature repair when needed; do not promote paper claims"
                    )
                else:
                    self.state["current_goal"] = "continue from current literature packet; targeted Find is allowed only when the literature gate needs repair"
                self.save()
        if self.args.topic:
            autonomous.extend(["--topic", self.args.topic])
        if self.args.title:
            autonomous.extend(["--title", self.args.title])
        if self.args.max_launches:
            autonomous.extend(["--max-launches", str(self.args.max_launches)])
        if self.args.auto_install_latex:
            autonomous.append("--auto-install-latex")
        if self.args.skip_fetch:
            autonomous.append("--skip-fetch")
        autonomous.append("--skip-paper")
        step(self.run(autonomous, stage="autonomous-research", timeout=self.args.autonomous_timeout_sec))
        wait_result = self.wait_for_background_experiments("wait-autonomous-experiments")
        step(wait_result)
        if wait_result.get("timed_out"):
            pdf_after = self.latest_pdf_fingerprint()
            gate = self.gate_snapshot()
            gate["pdf_before"] = pdf_before
            gate["pdf_after"] = pdf_after
            gate["pdf_changed_this_cycle"] = self.pdf_changed(pdf_before, pdf_after)
            gate["paper_pipeline_skipped"] = True
            gate["paper_pipeline_skipped_reason"] = "background autonomous experiment is still running; paper production is blocked until experiment evidence is complete"
            blockers = self.collect_blockers(gate)
            blockers.insert(
                0,
                {
                    "category": "background_experiment_running",
                    "severity": "block",
                    "issue": "A real-data experiment is still running, so TASTE cannot audit scientific progress or regenerate the paper from incomplete output.",
                    "evidence": [str(self.paths.root / "artifacts")],
                    "next_action": "Wait for completion, postprocess metrics/audit artifacts, then rerun experiment evidence gates.",
                },
            )
            cycle.update(
                {
                    "finished_at": now_iso(),
                    "status": "waiting_for_background_experiment",
                    "gate": gate,
                    "blockers": blockers,
                    "pdf_after": pdf_after,
                    "pdf_changed": gate["pdf_changed_this_cycle"],
                    "paper_pipeline_skipped": True,
                }
            )
            self.state["latest_gate"] = gate
            self.state["latest_blockers"] = blockers
            self.state["latest_pdf_info"] = pdf_after
            self.state["paper_iteration_required"] = False
            self.state["continuation_required"] = True
            self.state["continuation_reason"] = "background experiment is still running"
            self.state["current_goal"] = "waiting for background experiment before rerunning gates"
            self.save()
            return cycle
        self.postprocess_and_refresh_experiment_gates(step, suffix="-after-autonomous-wait")

        supervisor = [
            sys.executable,
            str(SCRIPTS / "run_research_trajectory_supervisor.py"),
            "--project",
            self.args.project,
            "--rounds",
            str(self.args.trajectory_rounds),
            "--timeout-sec",
            str(self.args.claude_timeout_sec),
            "--venue",
            self.args.venue,
        ]
        step(self.run(supervisor, stage="trajectory-supervisor", timeout=self.args.trajectory_timeout_sec))

        for stage_name, script in [
            ("paper-evidence-audit-precheck", "audit_paper_evidence.py"),
            ("submission-readiness-precheck", "audit_submission_readiness.py"),
            ("selected-base-viability-precheck", "audit_selected_base_viability.py"),
            ("base-switch-gate-precheck", "audit_deterministic_base_switch_gate.py"),
            ("trajectory-evidence-refresh", "build_research_trajectory_system.py"),
            ("blocker-action-plan-precheck", "build_blocker_action_plan.py"),
        ]:
            cmd = [sys.executable, str(SCRIPTS / script), "--project", self.args.project]
            if script != "build_research_trajectory_system.py" or self.args.venue:
                cmd.extend(["--venue", self.args.venue])
            step(self.run(cmd, stage=stage_name))

        if self.base_switch_gate_authorized() and not self.base_switch_execution_authorized():
            step(self.run([sys.executable, str(SCRIPTS / "execute_authorized_base_switch.py"), "--project", self.args.project, "--venue", self.args.venue], stage="base-switch-execution-precheck", required=False, timeout=180))
        selected_base_blocked, selected_base_gate = self.selected_base_viability_gate_blocked()
        if selected_base_blocked:
            cycle["selected_base_viability_gate"] = selected_base_gate
            self.state["selected_base_viability_notice"] = {
                "status": selected_base_gate.get("status", "") if isinstance(selected_base_gate, dict) else "blocked",
                "decision": selected_base_gate.get("decision", "") if isinstance(selected_base_gate, dict) else "continue_experiment_evidence_repair",
                "policy": "candidate/alternative-route experiments, paper/claim promotion, and automatic route switching are blocked; current selected-base evidence repair requires launcher route_scope=selected_base_current_route and artifact-local audit contracts",
            }
            self.state["current_goal"] = "refresh deterministic base-switch gate and blocker action plan; keep candidate routes proposal-only until gate pass; allow launcher-scoped current-route repair and bounded base-switch evidence collection"
            self.save()

        evidence_blocked, evidence_reasons = self.experiment_evidence_gate_blocked()
        if evidence_blocked and self.queued_guidance_pending():
            step(self.claude(self.guidance_checkin_prompt(cycle_index), stage="full-cycle-guidance-checkin"))
            self.postprocess_and_refresh_experiment_gates(step, suffix="-after-guidance-checkin")
            evidence_blocked, evidence_reasons = self.experiment_evidence_gate_blocked()
            self.state["current_goal"] = (
                "queued human guidance consumed; experiment evidence still blocked"
                if evidence_blocked else
                "queued human guidance consumed; experiment evidence gate cleared, continuing toward paper gates"
            )
            self.save()
        if evidence_blocked:
            pdf_after = self.latest_pdf_fingerprint()
            gate = self.gate_snapshot()
            gate["pdf_before"] = pdf_before
            gate["pdf_after"] = pdf_after
            gate["pdf_changed_this_cycle"] = self.pdf_changed(pdf_before, pdf_after)
            gate["paper_pipeline_skipped"] = True
            gate["paper_pipeline_skipped_reason"] = "experiment evidence gate blocked before paper production"
            gate["experiment_evidence_blockers"] = evidence_reasons
            blockers = self.collect_blockers(gate)
            blockers.insert(
                0,
                {
                    "category": "experiment_evidence_gate",
                    "severity": "block",
                    "issue": "; ".join(evidence_reasons[:8]),
                    "evidence": [
                        str(self.paths.reports / "paper_evidence_audit.md"),
                        str(self.paths.state / "submission_readiness.json"),
                        str(self.paths.state / "experiment_registry.json"),
                        str(self.paths.state / "scientific_progress_gate.json"),
                    ],
                    "next_action": "Run experiment_evidence_repair before paper production.",
                },
            )
            blocker_action_plan = read_json(self.paths.state / "blocker_action_plan.json", {})
            gate["blocker_action_plan"] = {
                "status": blocker_action_plan.get("status", "") if isinstance(blocker_action_plan, dict) else "",
                "summary": blocker_action_plan.get("summary", {}) if isinstance(blocker_action_plan, dict) else {},
                "top_actions": (blocker_action_plan.get("actions", [])[:8] if isinstance(blocker_action_plan, dict) and isinstance(blocker_action_plan.get("actions", []), list) else []),
            }
            cycle.update(
                {
                    "finished_at": now_iso(),
                    "status": "blocked",
                    "gate": gate,
                    "blockers": blockers,
                    "pdf_after": pdf_after,
                    "pdf_changed": gate["pdf_changed_this_cycle"],
                    "paper_pipeline_skipped": True,
                }
            )
            self.state["latest_gate"] = gate
            self.state["latest_blockers"] = blockers
            self.state["latest_blocker_action_plan"] = gate["blocker_action_plan"]
            self.state["latest_pdf_info"] = pdf_after
            self.state["paper_iteration_required"] = False
            self.state["current_goal"] = "repair experiment evidence before paper production"
            self.save()
            return cycle

        paper = [
            sys.executable,
            str(SCRIPTS / "run_paper_pipeline.py"),
            "--project",
            self.args.project,
            "--venue",
            self.args.venue,
        ]
        if self.args.title:
            paper.extend(["--title", self.args.title])
        previous_gate = self.state.get("latest_gate", {}) if isinstance(self.state.get("latest_gate", {}), dict) else {}
        if (
            self.state.get("paper_iteration_required")
            or self.state.get("pdf_changed_this_cycle") is False
            or (previous_gate and not previous_gate.get("submission_ready"))
        ):
            paper.append("--refresh-current-paper")
        if self.args.skip_fetch:
            paper.append("--skip-fetch")
        else:
            paper.append("--refresh-current-venue")
        if self.args.auto_install_latex:
            paper.append("--auto-install-latex")
        self.state["paper_stage_policy"] = {
            "venue_contract": "resolve latest official target-venue requirements and validate the official LaTeX template before writing",
            "preview_repair": "when body pages are within the official limit, repair figure/table footprint, citation coverage, bibliography density, and venue compliance from current artifacts",
            "research_boundary": "paper generation is an writing task and must not change scientific claims or intervene in experiments",
            "updated_at": now_iso(),
        }
        self.save()
        step(self.run(paper, stage="paper-pipeline", timeout=self.args.paper_timeout_sec))

        figure_loop = [
            sys.executable,
            str(SCRIPTS / "repair_paper_figures_loop.py"),
            "--project",
            self.args.project,
            "--venue",
            self.args.venue,
            "--title",
            self.args.title or "",
            "--max-rounds",
            str(self.args.figure_repair_rounds),
            "--timeout-sec",
            str(self.args.claude_timeout_sec),
        ]
        step(self.run(figure_loop, stage="paper-figure-repair"))

        preview_loop = [
            sys.executable,
            str(SCRIPTS / "repair_paper_preview_loop.py"),
            "--project",
            self.args.project,
            "--venue",
            self.args.venue,
            "--title",
            self.args.title or "",
            "--max-rounds",
            str(self.args.paper_repair_rounds),
            "--timeout-sec",
            str(self.args.claude_timeout_sec),
        ]
        # Preview repair always re-checks the current venue contract. The
        # loop prompt decides from audits whether the real blocker is figure
        # footprint, references, template compliance, or prose length.
        if self.state.get("paper_iteration_required") or self.state.get("pdf_changed_this_cycle") is False:
            preview_loop.append("--refresh-current-paper")
        step(self.run(preview_loop, stage="paper"))

        for stage_name, script in [
            ("paper-evidence-audit", "audit_paper_evidence.py"),
            ("conference-preview", "build_conference_preview_paper.py"),
            ("paper-normality-audit", "audit_paper_normality.py"),
            ("paper-figure-audit", "audit_paper_figures.py"),
            ("submission-readiness", "audit_submission_readiness.py"),
            ("blocker-action-plan", "build_blocker_action_plan.py"),
            ("research-manifest", "research_manifest.py"),
            ("trajectory-final-refresh", "build_research_trajectory_system.py"),
            ("trajectory-e2e-verify", "verify_research_trajectory_end_to_end.py"),
        ]:
            cmd = [sys.executable, str(SCRIPTS / script), "--project", self.args.project]
            if script not in {"verify_research_trajectory_end_to_end.py"}:
                cmd.extend(["--venue", self.args.venue])
            if script == "build_conference_preview_paper.py":
                cmd.extend(["--title", self.args.title or ""])
            step(self.run(cmd, stage=stage_name))

        pdf_after = self.latest_pdf_fingerprint()
        gate = self.gate_snapshot()
        pdf_changed = self.pdf_changed(pdf_before, pdf_after)
        gate["pdf_before"] = pdf_before
        gate["pdf_after"] = pdf_after
        gate["pdf_changed_this_cycle"] = pdf_changed
        blockers = self.collect_blockers(gate)
        blocker_action_plan = read_json(self.paths.state / "blocker_action_plan.json", {})
        gate["blocker_action_plan"] = {
            "status": blocker_action_plan.get("status", "") if isinstance(blocker_action_plan, dict) else "",
            "summary": blocker_action_plan.get("summary", {}) if isinstance(blocker_action_plan, dict) else {},
            "top_actions": (blocker_action_plan.get("actions", [])[:8] if isinstance(blocker_action_plan, dict) and isinstance(blocker_action_plan.get("actions", []), list) else []),
        }
        if not gate.get("complete") and pdf_before.get("exists") and pdf_after.get("exists") and not pdf_changed:
            blockers.append(
                {
                    "category": "paper_iteration_stalled",
                    "severity": "block",
                    "issue": "Paper PDF did not change during this full-cycle run while paper/readiness gates are still blocked.",
                    "evidence": [pdf_after.get("path", ""), pdf_after.get("sha256", "")],
                    "next_action": "Regenerate or repair the current venue-formatted paper preview from current audit blockers before treating the cycle as progressed; diagnose figures, bibliography, and venue rules before changing prose.",
                }
            )
        cycle.update(
            {
                "finished_at": now_iso(),
                "status": "passed" if gate.get("complete") else "blocked",
                "gate": gate,
                "blockers": blockers,
                "pdf_after": pdf_after,
                "pdf_changed": pdf_changed,
            }
        )
        self.state["latest_gate"] = gate
        self.state["latest_blockers"] = blockers
        self.state["latest_blocker_action_plan"] = gate["blocker_action_plan"]
        self.state["latest_pdf_info"] = pdf_after
        self.state["pdf_changed_this_cycle"] = pdf_changed
        self.state["paper_iteration_required"] = bool(not gate.get("complete") and (not pdf_changed or not gate.get("accepted_preview") or not gate.get("submission_ready")))
        self.state["current_goal"] = "paper complete" if gate.get("complete") else "repair blockers and continue"
        self.save()
        return cycle

    def path_exists(self, value: Any) -> bool:
        text = str(value or "").strip()
        return bool(text and Path(text).exists())

    def latest_pdf(self) -> str:
        state = get_active_paper_state(self.args.project, venue=self.args.venue)
        candidates = [
            state.get("conference_preview_pdf"),
            state.get("pdf_path"),
            state.get("blocked_preview_pdf"),
            state.get("latest_preview_pdf"),
            state.get("paper_orchestra_final_pdf"),
        ]
        for item in candidates:
            text = str(item or "").strip()
            if text and Path(text).exists():
                return text
        current_regeneration_incomplete = bool(
            (state.get("paper_current_regeneration_requested") or state.get("paper_orchestra_force_refresh"))
            and str(state.get("paper_orchestra_bridge_status") or "") != "generated"
            and not state.get("paper_orchestra_pdf_generated")
        )
        if current_regeneration_incomplete:
            return ""
        output_root = self.paths.root / "paper" / "output"
        pdfs = sorted(output_root.glob("**/*.pdf"), key=lambda path: path.stat().st_mtime) if output_root.exists() else []
        return str(pdfs[-1]) if pdfs else ""

    def latest_pdf_fingerprint(self) -> dict[str, Any]:
        path_text = self.latest_pdf()
        if not path_text:
            return {"path": "", "exists": False, "sha256": "", "size": 0, "mtime": 0.0, "mtime_iso": ""}
        path = Path(path_text)
        if not path.exists():
            return {"path": path_text, "exists": False, "sha256": "", "size": 0, "mtime": 0.0, "mtime_iso": ""}
        stat = path.stat()
        return {
            "path": str(path),
            "exists": True,
            "sha256": sha256_file(path),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "mtime_iso": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat(),
        }

    @staticmethod
    def pdf_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
        return bool(
            after.get("exists")
            and (
                before.get("path") != after.get("path")
                or before.get("sha256") != after.get("sha256")
                or before.get("size") != after.get("size")
            )
        )

    def gate_snapshot(self) -> dict[str, Any]:
        paper_state = get_active_paper_state(self.args.project, venue=self.args.venue)
        submission = read_json(self.paths.state / "submission_readiness.json", {})
        normality = read_json(self.paths.state / "paper_normality_audit.json", {})
        figures = read_json(self.paths.state / "paper_figure_quality_audit.json", {})
        orchestra = read_json(self.paths.state / "paper_orchestra_state.json", {})
        trajectory = read_json(self.paths.state / "research_trajectory_system.json", {})
        manifest = read_json(self.paths.state / "research_evidence_manifest.json", {})
        claim_ledger = read_json(self.paths.state / "claim_ledger.json", {})
        reference_gate = read_json(self.paths.state / "reference_reproduction_gate.json", {})
        progress_gate = read_json(self.paths.state / "scientific_progress_gate.json", {})
        iteration_audit = read_json(self.paths.state / "experiment_iteration_audit.json", {})
        latest_pdf = self.latest_pdf()
        latest_pdf_info = self.latest_pdf_fingerprint()
        reference_ready = bool(
            isinstance(reference_gate, dict)
            and reference_gate.get("status") == "pass"
            and reference_gate.get("decision") == "continue_base"
        )
        progress_ready = bool(isinstance(progress_gate, dict) and progress_gate.get("status") == "pass")
        accepted_preview = bool(
            paper_state.get("conference_preview_ready")
            and (paper_state.get("normal_preview_ready") or paper_state.get("paper_normality_ready"))
            and (paper_state.get("venue_template_format_ready") or paper_state.get("paper_venue_format_status") == "pass")
            and (paper_state.get("paper_figure_quality_ready") or paper_state.get("paper_figure_quality_status") == "pass")
            and latest_pdf
        )
        submission_ready = bool(submission.get("submission_ready") and submission.get("status") == "submission_ready")
        literature_gate = self.literature_gate_status()
        literature_ready = literature_gate.get("status") == "positive_anchors_ready"
        gate = {
            "complete": bool(accepted_preview and submission_ready and reference_ready and progress_ready and literature_ready),
            "accepted_preview": accepted_preview,
            "submission_ready": submission_ready,
            "literature_ready": literature_ready,
            "literature_gate": literature_gate,
            "latest_pdf": latest_pdf,
            "latest_pdf_info": latest_pdf_info,
            "experiment_gates": {
                "reference_reproduction_status": reference_gate.get("status", "") if isinstance(reference_gate, dict) else "",
                "reference_reproduction_decision": reference_gate.get("decision", "") if isinstance(reference_gate, dict) else "",
                "reference_reproduction_blockers": (reference_gate.get("blockers", [])[:8] if isinstance(reference_gate, dict) and isinstance(reference_gate.get("blockers", []), list) else []),
                "scientific_progress_status": progress_gate.get("status", "") if isinstance(progress_gate, dict) else "",
                "scientific_progress_blockers": (progress_gate.get("blockers", [])[:8] if isinstance(progress_gate, dict) and isinstance(progress_gate.get("blockers", []), list) else []),
                "best_candidate": progress_gate.get("best_candidate", {}) if isinstance(progress_gate, dict) else {},
                "best_control": progress_gate.get("best_control", {}) if isinstance(progress_gate, dict) else {},
                "iteration_audit_status": iteration_audit.get("status", "") if isinstance(iteration_audit, dict) else "",
                "iteration_audit_blockers": (iteration_audit.get("blockers", [])[:8] if isinstance(iteration_audit, dict) and isinstance(iteration_audit.get("blockers", []), list) else []),
                "paper_blocked_until_experiment_gate_passes": bool(not reference_ready or not progress_ready),
            },
            "paper_status": {
                "conference_preview_ready": bool(paper_state.get("conference_preview_ready")),
                "paper_normality_status": paper_state.get("paper_normality_status") or normality.get("status"),
                "paper_normality_pages": paper_state.get("paper_normality_pages") or (normality.get("metrics", {}) if isinstance(normality.get("metrics", {}), dict) else {}).get("pages"),
                "paper_normality_body_pages": paper_state.get("paper_normality_body_pages") or (normality.get("metrics", {}) if isinstance(normality.get("metrics", {}), dict) else {}).get("body_pages"),
                "paper_normality_estimated_reference_pages": paper_state.get("paper_normality_estimated_reference_pages") or (normality.get("metrics", {}) if isinstance(normality.get("metrics", {}), dict) else {}).get("estimated_reference_pages"),
                "paper_normality_citation_count": paper_state.get("paper_normality_citation_count") or (normality.get("metrics", {}) if isinstance(normality.get("metrics", {}), dict) else {}).get("citation_count"),
                "venue_submission_policy_status": paper_state.get("venue_submission_policy_status") or (normality.get("metrics", {}) if isinstance(normality.get("metrics", {}), dict) else {}).get("venue_submission_policy", {}).get("status", ""),
                "venue_submission_policy": paper_state.get("venue_submission_policy") or (normality.get("metrics", {}) if isinstance(normality.get("metrics", {}), dict) else {}).get("venue_submission_policy", {}),
                "paper_venue_format_status": paper_state.get("paper_venue_format_status"),
                "paper_figure_quality_status": paper_state.get("paper_figure_quality_status") or figures.get("status"),
                "paper_figure_blocker_count": paper_state.get("paper_figure_blocker_count") or figures.get("blocked_count"),
                "promotion_gate": paper_state.get("promotion_gate"),
                "paper_review_verdict": paper_state.get("paper_review_verdict"),
            },
            "submission_readiness": {
                "status": submission.get("status", ""),
                "failed_checks": len(submission.get("failed_checks", [])) if isinstance(submission.get("failed_checks", []), list) else 0,
                "blockers": submission.get("blockers", [])[:20] if isinstance(submission.get("blockers", []), list) else [],
                "warnings": submission.get("warnings", [])[:20] if isinstance(submission.get("warnings", []), list) else [],
                "metrics": submission.get("metrics", {}) if isinstance(submission.get("metrics", {}), dict) else {},
            },
            "paper_orchestra": {
                "status": orchestra.get("status", ""),
                "promotion_gate_recommendation": orchestra.get("promotion_gate_recommendation", ""),
            },
            "evidence_manifest": {
                "status": manifest.get("status", ""),
                "weak_or_unsupported_claims": manifest.get("weak_or_unsupported_claims", [])[:20] if isinstance(manifest.get("weak_or_unsupported_claims", []), list) else [],
            },
            "claim_ledger": {
                "claim_count": len(claim_ledger.get("claims", [])) if isinstance(claim_ledger.get("claims", []), list) else 0,
            },
            "trajectory": {
                "phase": (trajectory.get("summary", {}) if isinstance(trajectory.get("summary", {}), dict) else {}).get("phase", trajectory.get("phase", "")),
                "assurance_status": (trajectory.get("summary", {}) if isinstance(trajectory.get("summary", {}), dict) else {}).get("assurance_status", ""),
                "optimization_queue_size": (trajectory.get("summary", {}) if isinstance(trajectory.get("summary", {}), dict) else {}).get("optimization_queue_size", ""),
                "capability_status": (trajectory.get("summary", {}) if isinstance(trajectory.get("summary", {}), dict) else {}).get("capability_status", ""),
            },
        }
        return gate

    def collect_blockers(self, gate: dict[str, Any]) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        paper = gate.get("paper_status", {}) if isinstance(gate.get("paper_status", {}), dict) else {}
        experiment_gates = gate.get("experiment_gates", {}) if isinstance(gate.get("experiment_gates", {}), dict) else {}
        literature_gate = gate.get("literature_gate", {}) if isinstance(gate.get("literature_gate", {}), dict) else {}
        if literature_gate.get("status") in {"not_run", "no_candidates", "candidates_but_no_positive_anchor", "recommendation_shortfall"}:
            blockers.append(
                {
                    "category": "literature_gate",
                    "severity": "block",
                    "issue": "; ".join(str(item) for item in literature_gate.get("blockers", [])[:4]) or f"status={literature_gate.get('status')}",
                    "evidence": [
                        str(self.paths.planning / "finding" / "find_progress.json"),
                        str(self.paths.state / "literature_tool_packet.json"),
                        str(self.paths.state / "taste_literature_intermediates.json"),
                        str(self.paths.state / "paper_quality.json"),
                        str(self.paths.state / "idea_candidates.json"),
                    ],
                }
            )
        reference_status = experiment_gates.get("reference_reproduction_status")
        reference_decision = experiment_gates.get("reference_reproduction_decision")
        if reference_status and not (reference_status == "pass" and reference_decision == "continue_base"):
            blockers.append(
                {
                    "category": "reference_reproduction_gate",
                    "severity": "block",
                    "issue": "; ".join(str(item) for item in experiment_gates.get("reference_reproduction_blockers", [])[:4]) or f"status={reference_status}; decision={reference_decision}",
                    "evidence": [str(self.paths.state / "reference_reproduction_gate.json")],
                }
            )
        progress_status = experiment_gates.get("scientific_progress_status")
        if progress_status and progress_status != "pass":
            blockers.append(
                {
                    "category": "scientific_progress_gate",
                    "severity": "block",
                    "issue": "; ".join(str(item) for item in experiment_gates.get("scientific_progress_blockers", [])[:4]) or f"status={progress_status}",
                    "evidence": [str(self.paths.state / "scientific_progress_gate.json"), str(self.paths.state / "experiment_registry.json")],
                }
            )
        if not gate.get("latest_pdf"):
            blockers.append({"category": "paper_pdf", "severity": "block", "issue": "No PDF artifact exists yet."})
        if not gate.get("accepted_preview"):
            for key in ["conference_preview_ready", "paper_normality_status", "paper_venue_format_status", "paper_figure_quality_status"]:
                value = paper.get(key)
                ok = value is True or value == "pass"
                if not ok:
                    blockers.append({"category": "accepted_preview", "severity": "block", "issue": f"{key}={value}"})
        if not gate.get("submission_ready"):
            submission = read_json(self.paths.state / "submission_readiness.json", {})
            failed = submission.get("failed_checks", []) if isinstance(submission.get("failed_checks", []), list) else []
            for row in failed[:20]:
                if not isinstance(row, dict):
                    continue
                blockers.append(
                    {
                        "category": "submission_readiness",
                        "severity": row.get("severity") or row.get("status") or "block",
                        "issue": f"{row.get('name') or row.get('id')}: {row.get('detail')}",
                        "evidence": row.get("evidence", []),
                    }
                )
            if not failed:
                blockers.append({"category": "submission_readiness", "severity": "block", "issue": f"status={gate.get('submission_readiness', {}).get('status', '')}"})
        return blockers

    def write_report(self) -> None:
        lines = [
            "# Full Research Cycle\n\n",
            f"- project: {self.state.get('project', '')}\n",
            f"- status: {self.state.get('status', '')}\n",
            f"- current_cycle: {self.state.get('current_cycle', 0)} / {self.state.get('max_cycles', 0)}\n",
            f"- topic: {self.state.get('topic', '')}\n",
            f"- venue: {self.state.get('venue', '')}\n",
            f"- title: {self.state.get('title', '')}\n",
            f"- updated_at: {self.state.get('updated_at', '')}\n",
            "\n## Latest Gate\n\n",
        ]
        gate = self.state.get("latest_gate", {}) if isinstance(self.state.get("latest_gate", {}), dict) else {}
        for key in ["complete", "accepted_preview", "submission_ready", "literature_ready", "latest_pdf"]:
            lines.append(f"- {key}: {gate.get(key, '')}\n")
        literature_gate = gate.get("literature_gate", {}) if isinstance(gate.get("literature_gate", {}), dict) else {}
        if literature_gate:
            lines.append(f"- literature_status: {literature_gate.get('status', '')}\n")
            lines.append(f"- strong_recommendations: {literature_gate.get('strong_recommendations', 0)} / {literature_gate.get('recommendation_target_count', 0)}\n")
            lines.append(f"- recommendation_shortfall: {literature_gate.get('recommendation_shortfall', 0)}\n")
        pdf_info = gate.get("latest_pdf_info", {}) if isinstance(gate.get("latest_pdf_info", {}), dict) else {}
        if pdf_info:
            lines.append(f"- latest_pdf_sha256: {pdf_info.get('sha256', '')}\n")
            lines.append(f"- latest_pdf_mtime: {pdf_info.get('mtime_iso', '')}\n")
        if "pdf_changed_this_cycle" in gate:
            lines.append(f"- pdf_changed_this_cycle: {gate.get('pdf_changed_this_cycle')}\n")
        if self.state.get("continuation_required"):
            lines.append(f"- continuation_required: {self.state.get('continuation_required')}\n")
            lines.append(f"- continuation_reason: {one_line(self.state.get('continuation_reason', ''))}\n")
        lines.append("\n## Latest Blockers\n\n")
        blockers = self.state.get("latest_blockers", []) if isinstance(self.state.get("latest_blockers", []), list) else []
        if blockers:
            for row in blockers[:30]:
                if isinstance(row, dict):
                    lines.append(f"- [{row.get('severity', '')}] {row.get('category', '')}: {one_line(row.get('issue', ''))}\n")
        else:
            lines.append("- none\n")
        action_plan = self.state.get("latest_blocker_action_plan", {}) if isinstance(self.state.get("latest_blocker_action_plan", {}), dict) else {}
        action_summary = action_plan.get("summary", {}) if isinstance(action_plan.get("summary", {}), dict) else {}
        lines.append("\n## Blocker Action Plan\n\n")
        lines.append(f"- status: {action_plan.get('status', '')}\n")
        lines.append(f"- actions: {action_summary.get('action_count', 0)}\n")
        lines.append(f"- autonomous_actions: {action_summary.get('autonomous_action_count', 0)}\n")
        lines.append(f"- manual_actions: {action_summary.get('manual_action_count', 0)}\n")
        for row in action_plan.get("top_actions", [])[:8] if isinstance(action_plan.get("top_actions", []), list) else []:
            if isinstance(row, dict):
                lines.append(f"- [{row.get('priority', '')}] {row.get('route', '')}: {one_line(row.get('issue', ''))}\n")
        running_cycle = self.state.get("current_cycle_record", {}) if isinstance(self.state.get("current_cycle_record", {}), dict) else {}
        if running_cycle:
            lines.append("\n## Current Running Cycle\n\n")
            lines.append(f"- cycle {running_cycle.get('cycle')}: {running_cycle.get('status')} steps={len(running_cycle.get('steps', []))} started_at={running_cycle.get('started_at', '')} updated_at={running_cycle.get('updated_at', '')}\n")
        lines.append("\n## Cycles\n\n")
        for cycle in self.state.get("cycles", [])[-10:] if isinstance(self.state.get("cycles", []), list) else []:
            if not isinstance(cycle, dict):
                continue
            lines.append(f"- cycle {cycle.get('cycle')}: {cycle.get('status')} steps={len(cycle.get('steps', []))} blockers={len(cycle.get('blockers', [])) if isinstance(cycle.get('blockers', []), list) else 0}\n")
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text("".join(lines), encoding="utf-8")

    def run_all(self) -> int:
        cfg = load_project_config(self.args.project)
        title = self.args.title or str(get_active_paper_state(self.args.project, venue=self.args.venue).get("title") or cfg.get("topic") or self.args.project)
        self.args.title = title
        run_started_at = now_iso()
        self.state.update(
            {
                "status": "running",
                "started_at": run_started_at,
                "run_started_at": run_started_at,
                "stage_failures": [],
                "runtime_blockers": [],
                "latest_gate": {},
                "latest_blockers": [],
                "latest_blocker_action_plan": {},
                "continuation_required": False,
                "continuation_reason": "",
                "paper_iteration_required": False,
            }
        )
        self.state.pop("current_running_stage", None)
        self.state.pop("current_cycle_record", None)
        self.state.pop("latest_blocker", None)
        self.state.pop("latest_error", None)
        upsert_agent(
            self.args.project,
            "main",
            name="完整科研自循环",
            role="main",
            stage="full-cycle",
            status="running",
            goal=(self.args.topic or title or self.args.project)[:500],
            current_step="starting full research cycle",
        )
        self.save()
        cycles: list[dict[str, Any]] = []
        for cycle_index in range(1, max(1, self.args.max_cycles) + 1):
            cycle = self.run_cycle(cycle_index)
            if not isinstance(cycle, dict):
                cycle = {
                    "cycle": cycle_index,
                    "started_at": run_started_at,
                    "finished_at": now_iso(),
                    "status": "runtime_error",
                    "steps": [],
                    "blockers": [
                        {
                            "category": "runtime",
                            "severity": "block",
                            "issue": "Full-cycle stage returned no cycle state; this is a controller bug, not a scientific result.",
                            "evidence": [str(self.state_path)],
                            "next_action": "Inspect the latest stage failure and repair the controller before restarting the full cycle.",
                        }
                    ],
                }
            cycles.append(cycle)
            self.state["cycles"] = [*self.state.get("cycles", []), cycle][-20:]
            self.clecurrent_cycle_record()
            self.save()
            cycle_status = str(cycle.get("status") or "")
            if cycle_status == "passed":
                self.state["status"] = "completed"
                self.state["completed_at"] = now_iso()
                self.state["current_goal"] = "final paper passed local TASTE gates"
                self.save()
                mark_agent(self.args.project, "main", "done", current_step="full research cycle completed")
                return 0
            if cycle_status == "blocked_literature_recommendation_gate":
                self.state["status"] = "blocked_literature_recommendation_gate"
                self.state["finished_at"] = now_iso()
                self.state["current_goal"] = "current Find recommended papers are below target; repair title+abstract scoring packet before any downstream stage"
                self.state["continuation_required"] = True
                self.state["continuation_reason"] = "Find recommendation gate shortfall"
                self.state["paper_pipeline_skipped"] = True
                self.state["paper_pipeline_skipped_reason"] = "literature strong-recommendation gate shortfall"
                self.save()
                mark_agent(self.args.project, "main", "blocked", current_step="Find recommendation gate shortfall; downstream repair suppressed")
                return 2
            if cycle_status == "blocked_selected_base_route_guard":
                self.state["status"] = "blocked_selected_base_route_guard"
                self.state["finished_at"] = now_iso()
                self.state["current_goal"] = "selected-base route overwrite was blocked; restart full-cycle from trusted selected-base context"
                self.state["continuation_required"] = True
                self.state["continuation_reason"] = "selected-base route guard blocked legacy/control overwrite"
                self.state["paper_pipeline_skipped"] = True
                self.state["paper_pipeline_skipped_reason"] = "route guard stopped the cycle before experiments or paper production"
                self.save()
                mark_agent(self.args.project, "main", "blocked", current_step="selected-base route guard blocked legacy/control overwrite")
                return 2
            if cycle_status == "blocked_selected_base_viability_gate":
                cycle_status = "blocked_selected_base_viability_gate"
                self.state["status"] = "blocked_selected_base_viability_gate"
                self.state["finished_at"] = now_iso()
                self.state["selected_base_viability_notice"] = {
                    "status": "blocked",
                    "decision": "base_switch_gate_required",
                    "policy": "candidate/alternative-route experiments, paper/claim promotion, and automatic route switching are blocked; current selected-base evidence repair requires launcher route_scope=selected_base_current_route and artifact-local audit contracts",
                }
                self.state["current_goal"] = "refresh deterministic base-switch gate and blocker action plan; keep candidate routes proposal-only until gate pass; allow launcher-scoped current-route repair and bounded base-switch evidence collection"
                self.state["continuation_required"] = True
                self.state["continuation_reason"] = "selected-base viability requires deterministic base-switch gate; current and candidate route experiments remain paused"
                self.state["paper_pipeline_skipped"] = True
                self.state["paper_pipeline_skipped_reason"] = "selected-base viability gate blocks new experiments, paper/claim promotion, and automatic route switching"
                self.save()
                mark_agent(self.args.project, "main", "blocked", current_step="selected-base viability requires deterministic base-switch gate; candidate-route experiments paused, current-route repair must use launcher route-scope")
                return 2
            if cycle_status == "blocked_literature_survey":
                self.state["status"] = "blocked_literature_survey"
                self.state["finished_at"] = now_iso()
                self.state["current_goal"] = "fresh literature survey failed; repair finding and rebuild the literature packet before any Claude repair, experiment, paper writing, or claim promotion"
                self.state["continuation_required"] = True
                self.state["continuation_reason"] = "fresh finding survey did not complete and fallback artifacts are not scientific evidence"
                self.state["paper_pipeline_skipped"] = True
                self.state["paper_pipeline_skipped_reason"] = "fresh literature evidence is incomplete"
                self.save()
                mark_agent(self.args.project, "main", "blocked", current_step="fresh finding survey failed; no blocker repair or experiments launched")
                return 2
            if cycle_status == "blocked_reference_reproduction_gate":
                reference_gate = read_json(self.paths.state / "reference_reproduction_gate.json", {})
                if isinstance(reference_gate, dict) and reference_gate.get("decision") == "literature_base_audit_required":
                    self.state["status"] = "blocked_literature_base_audit_required"
                    self.state["finished_at"] = now_iso()
                    self.state["current_goal"] = "fresh Find base candidates must be audited before choosing a legacy route or a new base"
                    self.state["continuation_required"] = True
                    self.state["continuation_reason"] = "repo/data/env audit pending for fresh literature base candidates"
                    self.state["paper_pipeline_skipped"] = True
                    self.state["paper_pipeline_skipped_reason"] = "fresh literature base candidates are not yet repo/data/env audited"
                    self.state["reference_base_switch_exhausted"] = False
                    self.save()
                    mark_agent(self.args.project, "main", "blocked", current_step="fresh literature base audit required before legacy-route continuation")
                    return 2
                if isinstance(reference_gate, dict) and reference_gate.get("decision") in {"fresh_base_implementation_required", "fresh_base_reference_probe_required", "fresh_base_reference_smoke_required", "fresh_base_reference_reproduction_required"}:
                    self.refresh_fresh_base_implementation_plan(reason="final full-cycle block on fresh-base implementation")
                    plan = read_json(self.paths.state / "fresh_base_implementation_plan.json", {})
                    decision = str(reference_gate.get("decision") or "")
                    block_category = fresh_base_block_category(
                        plan,
                        reference_probe_required=decision == "fresh_base_reference_probe_required",
                        reference_smoke_required=decision == "fresh_base_reference_smoke_required",
                        reference_reproduction_required=decision == "fresh_base_reference_reproduction_required",
                    )
                    self.state["status"] = block_category
                    self.state["finished_at"] = now_iso()
                    ctx = fresh_base_route_context(self.paths, self.args.project)
                    goal, next_action, skipped_reason = fresh_base_status_text(block_category, ctx)
                    self.state["current_goal"] = goal
                    self.state["continuation_reason"] = next_action
                    self.state["paper_pipeline_skipped_reason"] = skipped_reason
                    self.state["continuation_required"] = True
                    self.state["paper_pipeline_skipped"] = True
                    self.state["reference_base_switch_exhausted"] = False
                    if isinstance(plan, dict) and plan:
                        self.state["fresh_base_implementation_plan"] = {
                            "status": plan.get("status", ""),
                            "repo": plan.get("repo", {}),
                            "ready_datasets": plan.get("ready_datasets", []),
                            "blocked_datasets": plan.get("blocked_datasets", []),
                            "blocker_reasons": plan.get("blocker_reasons", []),
                            "data_acquisition": plan.get("fresh_base_data_acquisition", {}),
                        }
                    fresh_base = {}
                    base_switch = reference_gate.get("base_switch", {}) if isinstance(reference_gate, dict) and isinstance(reference_gate.get("base_switch"), dict) else {}
                    if isinstance(base_switch.get("fresh_paper_base"), dict):
                        fresh_base = base_switch.get("fresh_paper_base", {})
                    title = str(fresh_base.get("title") or ctx.get("title") or "environment-stage selected anchor")
                    plan_blockers = plan.get("blocker_reasons", []) if isinstance(plan, dict) and isinstance(plan.get("blocker_reasons"), list) else []
                    self.state["latest_blockers"] = [
                        {
                            "category": "fresh_base_reference_reproduction_required" if block_category == "blocked_fresh_base_reference_reproduction_required" else "fresh_base_reference_smoke_required" if block_category == "blocked_fresh_base_reference_smoke_required" else "fresh_base_reference_probe_required" if block_category == "blocked_fresh_base_reference_probe_required" else "fresh_base_data_required" if block_category == "blocked_fresh_base_data_required" else "fresh_base_implementation_required",
                            "severity": "block",
                            "issue": (
                                f"{goal}: {title}. {next_action}"
                                + (f" blockers={'; '.join(str(item) for item in plan_blockers[:4])}." if plan_blockers else "")
                            ),
                            "evidence": [
                                str(self.paths.state / "fresh_research_base.json"),
                                str(self.paths.state / "fresh_base_implementation_plan.json"),
                                str(self.paths.state / "literature_tool_packet.json"),
                                str(self.paths.planning / "finding" / "find_results.json"),
                                str(self.paths.state / "reference_reproduction_gate.json"),
                            ],
                            "next_action": next_action,
                        }
                    ]
                    self.save()
                    mark_agent(self.args.project, "main", "blocked", current_step=("fresh base reference reproduction audit required" if block_category == "blocked_fresh_base_reference_reproduction_required" else "fresh base bounded reference smoke required" if block_category == "blocked_fresh_base_reference_smoke_required" else "fresh base reference protocol probe required" if block_category == "blocked_fresh_base_reference_probe_required" else "fresh base data contract required" if block_category == "blocked_fresh_base_data_required" else "fresh paper base selected; implementation route required"))
                    return 2
                if self.reference_base_switch_exhausted(reference_gate):
                    self.state["status"] = "blocked_no_viable_reference_base"
                    self.state["finished_at"] = now_iso()
                    self.state["current_goal"] = "reference reproduction blocked and base-switch exhausted; no autonomous paper/claim route remains"
                    self.state["continuation_required"] = False
                    self.state["continuation_reason"] = "await new evidence-ready base, compatible paper protocol/data, or stronger compute"
                    self.state["paper_pipeline_skipped"] = True
                    self.state["paper_pipeline_skipped_reason"] = "reference reproduction blocked and no evidence-ready alternative base exists"
                    self.state["reference_base_switch_exhausted"] = True
                    self.save()
                    mark_agent(self.args.project, "main", "blocked", current_step="reference reproduction blocked; base-switch exhausted; paper production suppressed")
                    return 2
            if cycle_index < self.args.max_cycles:
                reference_gate = read_json(self.paths.state / "reference_reproduction_gate.json", {})
                hard_decisions = {
                    "fresh_base_implementation_required",
                    "fresh_base_reference_probe_required",
                    "fresh_base_reference_smoke_required",
                    "fresh_base_reference_reproduction_required",
                }
                if isinstance(reference_gate, dict) and reference_gate.get("decision") in hard_decisions:
                    self.state["status"] = self.state.get("status") or "blocked_reference_reproduction_gate"
                    self.state["current_goal"] = "fresh-base hard gate is handled by deterministic TASTE probes/wrappers, not open-ended Claude repair"
                    self.state["continuation_required"] = True
                    self.state["continuation_reason"] = "run safe-unblock/full-cycle again to continue deterministic fresh-base gate progression"
                    self.save()
                    mark_agent(self.args.project, "main", "blocked", current_step="fresh-base hard gate awaiting deterministic wrapper continuation")
                    return 2
                self.state["status"] = "repairing"
                self.state["current_goal"] = "feed blockers back to Claude Code and continue"
                self.save()
                self.claude(self.blocker_prompt(cycle_index, cycle.get("blockers", []), cycle.get("gate", {})), stage="full-cycle-blocker-repair")

        if cycles and str(cycles[-1].get("status") or "") == "blocked_literature_survey":
            self.state["status"] = "blocked_literature_survey"
            self.state["finished_at"] = now_iso()
            self.state["current_goal"] = "fresh literature survey failed; final blocker repair is suppressed until TASTE completes and literature_tool_packet is rebuilt"
            self.state["continuation_required"] = True
            self.state["continuation_reason"] = "fresh finding survey did not complete"
            self.save()
            mark_agent(self.args.project, "main", "blocked", current_step="fresh finding survey failed; final blocker repair suppressed")
            return 2

        self.state["status"] = "blocked_after_max_cycles"
        self.state["finished_at"] = now_iso()
        latest_cycle = cycles[-1] if cycles else {}
        latest_blockers = latest_cycle.get("blockers", []) if isinstance(latest_cycle.get("blockers", []), list) else []
        latest_gate = latest_cycle.get("gate", {}) if isinstance(latest_cycle.get("gate", {}), dict) else {}
        reference_gate = read_json(self.paths.state / "reference_reproduction_gate.json", {})
        hard_decisions = {
            "fresh_base_implementation_required",
            "fresh_base_reference_probe_required",
            "fresh_base_reference_smoke_required",
            "fresh_base_reference_reproduction_required",
        }
        if isinstance(reference_gate, dict) and reference_gate.get("decision") in hard_decisions:
            self.state["current_goal"] = "fresh-base hard gate remains active; final open-ended Claude repair is suppressed"
            self.state["continuation_required"] = True
            self.state["continuation_reason"] = "continue deterministic fresh-base safe-unblock probes/wrappers"
            self.state["next_repair_prompt_stage"] = "deterministic-fresh-base-safe-unblock"
            self.save()
            mark_agent(self.args.project, "main", "blocked", current_step="full research cycle stopped at fresh-base hard gate; deterministic wrapper continuation required")
            return 2
        self.state["current_goal"] = "configured cycle limit reached; blocker-repair prompt queued and next full-cycle run must continue from these blockers"
        self.state["continuation_required"] = True
        self.state["continuation_reason"] = "local paper/readiness gates did not pass before the configured cycle limit"
        self.state["next_repair_prompt_stage"] = "full-cycle-blocker-repair-final"
        self.save()
        self.claude(self.blocker_prompt(self.args.max_cycles, latest_blockers, latest_gate), stage="full-cycle-blocker-repair-final")
        self.state["status"] = "blocked_after_max_cycles"
        self.state["current_goal"] = "blocked after configured cycles; continuation prompt recorded for the next autonomous run"
        self.save()
        mark_agent(self.args.project, "main", "blocked", current_step="full research cycle stopped at configured cycle limit; continuation is required")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a full TASTE autonomous research cycle until local paper/readiness gates pass or a configured cycle limit is reached.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--topic", default="")
    parser.add_argument("--venue", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--max-cycles", type=int, default=3)
    parser.add_argument("--iterations-per-cycle", type=int, default=1)
    parser.add_argument("--trajectory-rounds", type=int, default=1)
    parser.add_argument("--max-launches", type=int, default=1)
    parser.add_argument("--paper-repair-rounds", type=int, default=2)
    parser.add_argument("--figure-repair-rounds", type=int, default=1)
    parser.add_argument("--claude-timeout-sec", type=int, default=14400)
    parser.add_argument("--autonomous-timeout-sec", type=int, default=14400)
    parser.add_argument("--trajectory-timeout-sec", type=int, default=14400)
    parser.add_argument("--paper-timeout-sec", type=int, default=7200)
    parser.add_argument("--background-experiment-timeout-sec", type=int, default=int(os.environ.get("BACKGROUND_EXPERIMENT_TIMEOUT_SEC", "0")), help="Wait this long for project-owned background experiments; 0 means wait without a deadline.")
    parser.add_argument("--background-experiment-poll-sec", type=int, default=int(os.environ.get("BACKGROUND_EXPERIMENT_POLL_SEC", "60")))
    parser.add_argument("--coding-backend", default="", help="Deprecated compatibility option; downstream execution uses Claude Code.")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--auto-install-latex", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--fresh-start", action="store_true", help="Run the first cycle as a from-scratch research pass with fresh finding discovery.")
    parser.add_argument("--force-discovery", action="store_true", help="Do not skip discovery in autonomous cycles.")
    parser.add_argument("--first-cycle-discovery", action="store_true", default=True, help="Refresh literature discovery on the first full-cycle iteration.")
    parser.add_argument("--use-existing-literature-packet", action="store_true", help="Skip fresh finding discovery and continue from a separately validated literature_tool_packet/find_results pair.")
    args = parser.parse_args()
    # Full-cycle is the safe orchestrator for fresh-base blockers; individual
    # experiment/paper/claim entrypoints remain protected by pipeline_guard.
    if not args.venue:
        args.venue = project_target_venue(args.project, "ICLR")
    if not args.topic:
        cfg = load_project_config(args.project)
        args.topic = str(cfg.get("topic") or cfg.get("user_prompt") or "")
    if not args.title:
        args.title = args.topic or args.project
    if args.use_existing_literature_packet and not args.force_discovery:
        os.environ.setdefault("USE_EXISTING_LITERATURE_PACKET", "1")
    return FullCycle(args).run_all()


if __name__ == "__main__":
    raise SystemExit(main())
