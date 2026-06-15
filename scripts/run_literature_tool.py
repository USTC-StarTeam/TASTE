#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths

UTC = timezone.utc


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def current_find_progress(paths) -> tuple[str, dict[str, Any], dict[str, Any]]:
    current_progress = load_json(paths.planning / "finding" / "find_progress.json", {})
    current_results = load_json(paths.planning / "finding" / "find_results.json", {})
    current_run_id = ""
    if isinstance(current_progress, dict):
        current_run_id = str(current_progress.get("run_id") or "")
    if not current_run_id and isinstance(current_results, dict):
        current_run_id = str(current_results.get("run_id") or "")
    return current_run_id, current_progress if isinstance(current_progress, dict) else {}, current_results if isinstance(current_results, dict) else {}


def write_running_state(paths, args, queries: list[str], cmd: list[str], env: dict[str, str]) -> None:
    current_run_id, current_progress, current_results = current_find_progress(paths)
    progress_counts = current_progress.get("counts", {}) if isinstance(current_progress.get("counts"), dict) else {}
    log_path = paths.logs / "literature_tool.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("受控 Find 补检索已启动；实时 TASTE 渠道、标题筛选、摘要抓取和 LLM 打分日志会追加在这里。\n", encoding="utf-8")
    full_path = paths.state / "full_research_cycle.json"
    full = load_json(full_path, {})
    if not isinstance(full, dict):
        full = {}
    summary = "受控 Find 补检索正在运行；旧推荐统计只作为历史参考，本轮完成前禁止环境、实验、论文和 claim promotion。"
    for key in ["latest_error", "current_blocker", "latest_blocker", "continuation_reason"]:
        full.pop(key, None)
    full.update({
        "project": args.project,
        "venue": args.venue,
        "status": "running",
        "summary": summary,
        "summary_zh": summary,
        "current_goal": "run controlled targeted Find repair with mandatory abstract retrieval and LLM scoring",
        "updated_at": datetime.now(UTC).isoformat(),
        "controlled_literature_tool": {
            "status": "running",
            "pid": os.getpid(),
            "queries": queries,
            "command": cmd,
            "log_path": str(log_path),
            "started_at": datetime.now(UTC).isoformat(),
        },
    })
    if current_run_id:
        full["previous_find_run_id"] = current_run_id
        full.pop("current_find_run_id", None)
    save_json(full_path, full)
    state = {
        "project": args.project,
        "venue": args.venue,
        "queries": queries,
        "mode": "deep_survey" if args.deep_survey else "fast_mode" if args.fast_mode else "standard",
        "commands": {"taste": cmd},
        "return_codes": {},
        "status": "running",
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
        "current_find_run_id": current_run_id,
        "current_strong_recommendations": int(current_progress.get("strong_recommendation_count") or len(current_results.get("strong_recommendations", [])) or 0),
        "current_recommendation_target_count": int(current_progress.get("recommendation_target_count") or 0),
        "current_recommendation_shortfall": int(current_progress.get("recommendation_shortfall") or 0),
        "current_counts": progress_counts,
        "packet_path": str(paths.planning / "literature_tool_packet.md"),
        "packet_json": str(paths.state / "literature_tool_packet.json"),
        "log_path": str(log_path),
        "failure_summary": "",
        "guardrail": "Controlled TASTE literature survey is running. Previous failures are history until this invocation finishes; weak papers still cannot be promoted to satisfy gates.",
        "record_only_requested": False,
        "new_find_allowed": True,
        "llm_model": env.get("LLM_MODEL", ""),
        "llm_api_base": env.get("LLM_API_BASE", ""),
    }
    state["current_find_plan_sync"] = sync_current_find_plan_queries(paths, state)
    save_json(paths.state / "literature_tool_last_run.json", state)
    save_json(paths.state / "taste_targeted_queries.json", state)




def write_completed_full_cycle_state(paths, args, state: dict[str, Any]) -> None:
    full_path = paths.state / "full_research_cycle.json"
    full = load_json(full_path, {})
    if not isinstance(full, dict):
        full = {}
    strong = int(state.get("current_strong_recommendations") or 0)
    target = int(state.get("current_recommendation_target_count") or 0)
    shortfall = int(state.get("current_recommendation_shortfall") or 0)
    run_id = str(state.get("current_find_run_id") or "")
    llm_blocked = _looks_like_llm_quota_blocker(state.get("failure_summary"))
    if llm_blocked:
        status = "blocked_literature_llm_quota_exhausted"
        summary = str(state.get("failure_summary") or "LLM API 额度/配置不可用，Find 必需的摘要评分或补评分无法继续。")
        goal = "restore usable LLM API via web config/probe, then rerun complete Find/full-cycle; experiments/paper/claim remain blocked"
    elif shortfall:
        status = "blocked_literature_recommendation_gate"
        summary = f"受控 Find 补检索已完成；当前 Find 推荐文章 {strong}/{target}，短缺 {shortfall}。文献门控阻塞，禁止环境基底、实验、论文和 claim promotion。"
        goal = f"repair current Find literature gate ({strong}/{target}, shortfall {shortfall}); paper/citation/claim promotion blocked"
    else:
        status = "literature_tool_completed"
        summary = f"受控 Find 补检索已完成；当前 Find 推荐文章 {strong}/{target}，文献推荐门控已满足，等待 TASTE 继续环境基底选择门控。"
        goal = "continue from current literature packet to environment/base selection gates"
    controlled = full.get("controlled_literature_tool") if isinstance(full.get("controlled_literature_tool"), dict) else {}
    controlled.update({
        "status": state.get("status"),
        "finished_at": datetime.now(UTC).isoformat(),
        "current_find_run_id": run_id,
        "current_strong_recommendations": strong,
        "current_recommendation_target_count": target,
        "current_recommendation_shortfall": shortfall,
        "return_codes": state.get("return_codes", {}),
        "log_path": state.get("log_path", ""),
    })
    for key in ["literature_gate", "latest_gate", "latest_blockers", "latest_blocker_action_plan", "current_blocker", "latest_blocker"]:
        full.pop(key, None)
    full.update({
        "project": args.project,
        "venue": args.venue,
        "status": status,
        "summary": summary,
        "summary_zh": summary,
        "current_goal": goal,
        "current_find_run_id": run_id,
        "recommendation_shortfall": shortfall,
        "current_strong_recommendations": strong,
        "current_recommendation_target_count": target,
        "current_recommendation_shortfall": shortfall,
        "literature_gate": {
            "status": "blocked_llm_quota_exhausted" if llm_blocked else "recommendation_shortfall" if shortfall else "passed",
            "strong_recommendations": strong,
            "recommendation_target_count": target,
            "recommendation_shortfall": shortfall,
            "run_id": run_id,
            "llm_blocked": llm_blocked,
            "blocked_reason": state.get("failure_summary", "") if llm_blocked else "",
        },
        "latest_blockers": ([str(state.get("failure_summary") or "LLM API quota/configuration is unavailable"), f"Find strong recommendations are below target: {strong}/{target}; shortfall={shortfall}"] if llm_blocked else [f"Find strong recommendations are below target: {strong}/{target}; shortfall={shortfall}"] if shortfall else []),
        "updated_at": datetime.now(UTC).isoformat(),
        "controlled_literature_tool": controlled,
    })
    save_json(full_path, full)

def read_focus_queries(path: str) -> list[str]:
    if not path:
        return []
    file_path = Path(path)
    if not file_path.exists():
        return []
    text = file_path.read_text(encoding="utf-8", errors="ignore")
    values: list[str] = []
    if file_path.suffix.lower() == ".json":
        try:
            payload = json.loads(text)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            for key in ["queries", "followup_queries", "suggested_followup_queries", "targets"]:
                for item in payload.get(key, []) if isinstance(payload.get(key, []), list) else []:
                    if isinstance(item, str) and item.strip():
                        values.append(item.strip())
                    elif isinstance(item, dict):
                        title = str(item.get("title") or item.get("query") or item.get("name") or "").strip()
                        if title:
                            values.append(title)
        elif isinstance(payload, list):
            for item in payload:
                if isinstance(item, str) and item.strip():
                    values.append(item.strip())
                elif isinstance(item, dict):
                    title = str(item.get("title") or item.get("query") or item.get("name") or "").strip()
                    if title:
                        values.append(title)
    else:
        for line in text.splitlines():
            stripped = line.strip(" -\t")
            if stripped:
                values.append(stripped)
    return values


def dedupe(values: list[str], limit: int = 24) -> list[str]:
    out: list[str] = []
    seen = set()
    for value in values:
        text = " ".join(str(value or "").split())
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _redact(text: str, env: dict[str, str]) -> str:
    redacted = str(text or "")
    for key in ["OPENAI_API_KEY", "LLM_API_KEY", str(env.get("LLM_API_KEY_ENV") or "")]:
        value = env.get(key, "") if key else ""
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return redacted


def run(
    cmd: list[str],
    *,
    env: dict[str, str],
    timeout: int | None = None,
    live_log_path: Path | None = None,
    append_log: bool = True,
) -> subprocess.CompletedProcess[str]:
    started = datetime.now(UTC).isoformat()
    if live_log_path:
        live_log_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append_log and live_log_path.exists() else "w"
        with live_log_path.open(mode, encoding="utf-8") as handle:
            handle.write("\
[literature_tool] started_at={} timeout_sec={} cmd={}\
".format(started, timeout or 0, " ".join(str(x) for x in cmd)))
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, start_new_session=True)
    lines: list[str] = []
    assert proc.stdout is not None
    deadline = None if not timeout or timeout <= 0 else datetime.now(UTC).timestamp() + timeout
    try:
        while True:
            if deadline is not None and datetime.now(UTC).timestamp() > deadline:
                raise subprocess.TimeoutExpired(cmd, timeout, output="".join(lines), stderr="")
            line = proc.stdout.readline()
            if line:
                clean = _redact(line, env)
                lines.append(clean)
                print(clean, end="", flush=True)
                if live_log_path:
                    with live_log_path.open("a", encoding="utf-8") as handle:
                        handle.write(clean)
                continue
            return_code = proc.poll()
            if return_code is not None:
                remainder = proc.stdout.read() or ""
                if remainder:
                    clean = _redact(remainder, env)
                    lines.append(clean)
                    print(clean, end="", flush=True)
                    if live_log_path:
                        with live_log_path.open("a", encoding="utf-8") as handle:
                            handle.write(clean)
                if live_log_path:
                    with live_log_path.open("a", encoding="utf-8") as handle:
                        handle.write("\
[literature_tool] finished_at={} return_code={}\
".format(datetime.now(UTC).isoformat(), return_code))
                return subprocess.CompletedProcess(cmd, return_code, "".join(lines), "")
    except subprocess.TimeoutExpired as exc:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        exc.output = "".join(lines)
        exc.stderr = ""
        if live_log_path:
            with live_log_path.open("a", encoding="utf-8") as handle:
                handle.write("\
[literature_tool] timeout_at={} timeout_sec={}\
".format(datetime.now(UTC).isoformat(), timeout or 0))
        raise


def _looks_like_llm_quota_blocker(value: Any) -> bool:
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


def _failure_summary_from_logs(logs: list[dict[str, Any]]) -> str:
    joined = "\n".join(str(row.get("stdout_tail") or "") + "\n" + str(row.get("stderr_tail") or "") for row in logs if isinstance(row, dict))
    compact = " ".join(joined.split())
    if "Invalid API Key" in compact or "LLM HTTP 401" in compact:
        return "LLM configuration error: chat_completions returned 401 Invalid API Key. Update and validate the saved web LLM API key before rerunning controlled Find."
    if any(token in compact for token in ["LLM HTTP 429", "quota_exceeded", "quota exceeded", "token plan limit exhausted", "too many requests"]):
        return "LLM quota/rate-limit error: the saved LLM API reached a 429/quota limit during mandatory Find scoring. Save and validate a usable API key/base/model before rerunning controlled Find."
    if "NameError" in compact:
        idx = compact.find("NameError")
        return compact[idx: idx + 500]
    for marker in ["FatalLLMConfigurationError", "TimeoutError", "timed out", "Traceback"]:
        idx = compact.find(marker)
        if idx >= 0:
            return compact[idx: idx + 700]
    return ""


def sync_current_find_plan_queries(paths, state: dict[str, Any]) -> str:
    plan_path = paths.state / "current_find_research_plan.json"
    plan = load_json(plan_path, {})
    if not isinstance(plan, dict):
        return "missing_current_find_research_plan"
    current_run = str(state.get("current_find_run_id") or "")
    plan_run = str(plan.get("run_id") or plan.get("find_run_id") or "")
    if current_run and plan_run and current_run != plan_run:
        progress = load_json(paths.planning / "finding" / "find_progress.json", {})
        results = load_json(paths.planning / "finding" / "find_results.json", {})
        canonical_run = str((progress if isinstance(progress, dict) else {}).get("run_id") or (results if isinstance(results, dict) else {}).get("run_id") or "")
        if canonical_run != current_run:
            return "skipped_run_id_mismatch"
        progress = progress if isinstance(progress, dict) else {}
        results = results if isinstance(results, dict) else {}
        strong = int(progress.get("strong_recommendation_count") or len(results.get("strong_recommendations") or results.get("articles") or []) or 0)
        target = int(progress.get("recommendation_target_count") or state.get("current_recommendation_target_count") or 20)
        shortfall = max(0, int(progress.get("recommendation_shortfall") if progress.get("recommendation_shortfall") is not None else target - strong))
        evaluated = int(((progress.get("counts") or {}) if isinstance(progress.get("counts"), dict) else {}).get("evaluated_candidates") or state.get("current_counts", {}).get("evaluated_candidates", 0) if isinstance(state.get("current_counts"), dict) else 0)
        read_payload = load_json(paths.planning / "finding" / "read_results.json", {})
        idea_payload = load_json(paths.planning / "finding" / "ideas.json", {})
        plan_payload = load_json(paths.planning / "finding" / "plans.json", {})
        llm_blocked = _looks_like_llm_quota_blocker(state.get("failure_summary")) or _looks_like_llm_quota_blocker(state.get("targeted_search_tool_status"))
        plan.update({
            "run_id": current_run,
            "status": "blocked_literature_llm_quota_exhausted" if llm_blocked else "blocked_literature_recommendation_gate" if shortfall else "current_find_ready",
            "literature_gate": {
                "status": "blocked_llm_quota_exhausted" if llm_blocked else "shortfall" if shortfall else "passed",
                "blocked": bool(shortfall or llm_blocked),
                "run_id": current_run,
                "strong_recommendations": strong,
                "recommendation_target_count": target,
                "recommendation_shortfall": shortfall,
                "evaluated_candidates": evaluated,
                "source": "planning/finding/find_progress.json + state/current_find_research_plan.json:targeted_search_tool_status" if llm_blocked else "planning/finding/find_progress.json",
                "llm_blocked": llm_blocked,
                "blocked_reason": state.get("failure_summary", "") if llm_blocked else "",
            },
            "current_find_reading_count": len(read_payload.get("readings") or []) if isinstance(read_payload, dict) else 0,
            "current_find_idea_count": len(idea_payload.get("ideas") or []) if isinstance(idea_payload, dict) else 0,
            "current_find_plan_count": len(plan_payload.get("plans") or []) if isinstance(plan_payload, dict) else 0,
            "base_selection_status": "blocked_by_literature_gate" if shortfall else plan.get("base_selection_status"),
            "next_required_stage": "literature_repair" if shortfall else plan.get("next_required_stage"),
            "blockers": [f"current Find strong recommendations are below target: {strong}/{target}; shortfall={shortfall}"] if shortfall else [],
        })
    if current_run and (not plan_run or plan_run == current_run):
        progress = load_json(paths.planning / "finding" / "find_progress.json", {})
        results = load_json(paths.planning / "finding" / "find_results.json", {})
        progress = progress if isinstance(progress, dict) else {}
        results = results if isinstance(results, dict) else {}
        strong = int(progress.get("strong_recommendation_count") or len(results.get("strong_recommendations") or results.get("articles") or []) or 0)
        target = int(progress.get("recommendation_target_count") or state.get("current_recommendation_target_count") or 20)
        shortfall = max(0, int(progress.get("recommendation_shortfall") if progress.get("recommendation_shortfall") is not None else target - strong))
        evaluated = int(((progress.get("counts") or {}) if isinstance(progress.get("counts"), dict) else {}).get("evaluated_candidates") or state.get("current_counts", {}).get("evaluated_candidates", 0) if isinstance(state.get("current_counts"), dict) else 0)
        llm_blocked = _looks_like_llm_quota_blocker(state.get("failure_summary")) or _looks_like_llm_quota_blocker(state.get("targeted_search_tool_status"))
        plan.update({
            "run_id": current_run,
            "status": "blocked_literature_llm_quota_exhausted" if llm_blocked else "blocked_literature_recommendation_gate" if shortfall else "current_find_ready",
            "literature_gate": {
                "status": "blocked_llm_quota_exhausted" if llm_blocked else "shortfall" if shortfall else "passed",
                "blocked": bool(shortfall or llm_blocked),
                "run_id": current_run,
                "strong_recommendations": strong,
                "recommendation_target_count": target,
                "recommendation_shortfall": shortfall,
                "evaluated_candidates": evaluated,
                "source": "planning/finding/find_progress.json + state/current_find_research_plan.json:targeted_search_tool_status" if llm_blocked else "planning/finding/find_progress.json",
                "llm_blocked": llm_blocked,
                "blocked_reason": state.get("failure_summary", "") if llm_blocked else "",
            },
            "base_selection_status": "blocked_by_literature_gate" if shortfall else plan.get("base_selection_status"),
            "next_required_stage": "literature_repair" if shortfall else plan.get("next_required_stage"),
            "blockers": [f"current Find strong recommendations are below target: {strong}/{target}; shortfall={shortfall}"] if shortfall else [],
        })

    queries = dedupe(
        [q for q in plan.get("targeted_search_queries", []) if isinstance(q, str)]
        + [q for q in state.get("queries", []) if isinstance(q, str)],
        limit=24,
    )
    plan.update({
        "targeted_search_queries": queries,
        "targeted_search_query_count": len(queries),
        "targeted_search_tool_status": {
            "status": state.get("status"),
            "venue": state.get("venue"),
            "packet_return_code": state.get("packet_return_code"),
            "return_codes": state.get("return_codes"),
            "failure_summary": state.get("failure_summary"),
            "guardrail": state.get("guardrail"),
            "record_only_requested": state.get("record_only_requested"),
            "new_find_allowed": state.get("new_find_allowed"),
        },
        "literature_repair_policy": "targeted_find_allowed",
        "last_literature_tool_mode": "record_only" if state.get("record_only_requested") else "controlled_targeted_find",
        "allowed_actions": [
            "python3 scripts/build_literature_tool_packet.py --project {project}".format(project=state.get("project") or "<project>"),
            "python3 scripts/run_literature_tool.py --project {project} --query \"<targeted literature gap query>\" --fast-mode".format(project=state.get("project") or "<project>"),
            "python3 scripts/audit_submission_readiness.py --project {project}".format(project=state.get("project") or "<project>"),
            "python3 scripts/build_blocker_action_plan.py --project {project}".format(project=state.get("project") or "<project>"),
        ],
        "updated_at": datetime.now(UTC).isoformat(),
    })
    save_json(plan_path, plan)
    return "updated"


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude-callable TASTE literature tool wrapper. It uses the integrated TASTE pipeline and refreshes TASTE literature packets.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    parser.add_argument("--query", action="append", default=[], help="Targeted literature query chosen by project agent.")
    parser.add_argument("--focus-file", default="", help="Optional JSON/Markdown/TXT file containing targeted query strings or paper titles.")
    parser.add_argument("--fast-mode", action="store_true", help="Use a narrow, quick targeted survey.")
    parser.add_argument("--deep-survey", action="store_true", help="Use full local venue/arXiv survey budgets.")
    parser.add_argument("--max-papers", type=int, default=20)
    parser.add_argument("--max-ideas", type=int, default=6)
    parser.add_argument("--timeout-sec", type=int, default=int(os.environ.get("TIMEOUT_SEC", "3600")))
    parser.add_argument("--skip-arxiv", action="store_true")
    parser.add_argument("--skip-huggingface", action="store_true")
    parser.add_argument("--skip-github", action="store_true")
    parser.add_argument("--skip-venues", action="store_true")
    parser.add_argument(
        "--allow-new-find",
        action="store_true",
        help="Backward-compatible no-op: new targeted Find is allowed by default unless DISABLE_NEW_FIND=1 or --record-only is supplied.",
    )
    parser.add_argument(
        "--record-only",
        action="store_true",
        help="Record targeted literature queries and rebuild the packet without launching a new Find.",
    )
    args = parser.parse_args()

    paths = build_paths(args.project)
    previous_packet = load_json(paths.state / "literature_tool_packet.json", {})
    previous_queries = previous_packet.get("suggested_followup_queries", []) if isinstance(previous_packet, dict) else []
    queries = dedupe(list(args.query or []) + read_focus_queries(args.focus_file) + [q for q in previous_queries if isinstance(q, str)][:8])
    env = os.environ.copy()
    project_cfg = load_json(paths.config, {})
    llm_cfg = project_cfg.get("llm", {}) if isinstance(project_cfg, dict) and isinstance(project_cfg.get("llm"), dict) else {}
    api_key_env = env.get("LLM_API_KEY_ENV") or llm_cfg.get("api_key_env") or "OPENAI_API_KEY"
    api_key = (env.get(api_key_env, "") if api_key_env else "") or env.get("LLM_API_KEY", "") or llm_cfg.get("api_key", "")
    if api_key_env:
        env["LLM_API_KEY_ENV"] = str(api_key_env)
    if api_key:
        env["LLM_API_KEY"] = str(api_key)
        if api_key_env:
            env[str(api_key_env)] = str(api_key)
    for env_key, cfg_key in [("LLM_API_BASE", "api_base"), ("LLM_MODEL", "model"), ("LLM_PROVIDER", "provider"), ("LLM_API_MODE", "api_mode")]:
        if not env.get(env_key) and llm_cfg.get(cfg_key):
            env[env_key] = str(llm_cfg.get(cfg_key))
    if queries:
        env["EXTRA_QUERIES"] = json.dumps(queries, ensure_ascii=False)
    if args.fast_mode and not args.deep_survey:
        # Historical callers pass --fast-mode for "targeted repair", but the TASTE
        # literature gate still requires complete Read/Idea/Plan artifacts and
        # enough detail/abstract coverage. Keep the run targeted via queries,
        # not by truncating papers, ideas, venues, or detail scoring.
        env.setdefault("METADATA_TIMEOUT_SEC", "12")
        env.setdefault("TITLE_FILTER_TIMEOUT_SEC", "60")
        env.setdefault("ABSTRACT_SCORING_TIMEOUT_SEC", "120")
        env.setdefault("ABSTRACT_SCORING_WALL_TIMEOUT_SEC", "180")
        env.setdefault("VENUE_DETAIL_FETCH_COUNT", "240")
        env.setdefault("DETAIL_FETCH_COUNT", "240")
        env.setdefault("LLM_CONCURRENCY", "4")
        env.setdefault("ABSTRACT_SCORING_MAX_WORKERS", "6")
        env.setdefault("ABSTRACT_SCORING_BATCH_SIZE", "10")
    record_only = bool(args.record_only) or env.get("DISABLE_NEW_FIND", "0").lower() in {"1", "true", "yes", "on"}
    if record_only:
        env["DISABLE_NEW_FIND"] = "1"
    if record_only:
        current_progress = load_json(paths.planning / "finding" / "find_progress.json", {})
        current_results = load_json(paths.planning / "finding" / "find_results.json", {})
        current_run_id = ""
        if isinstance(current_progress, dict):
            current_run_id = str(current_progress.get("run_id") or "")
        if not current_run_id and isinstance(current_results, dict):
            current_run_id = str(current_results.get("run_id") or "")
        progress_counts = current_progress.get("counts", {}) if isinstance(current_progress, dict) and isinstance(current_progress.get("counts"), dict) else {}
        current_strong = 0
        current_target = 0
        current_shortfall = 0
        if isinstance(current_progress, dict):
            try:
                current_strong = int(current_progress.get("strong_recommendation_count") or 0)
            except Exception:
                current_strong = 0
            try:
                current_target = int(current_progress.get("recommendation_target_count") or 0)
            except Exception:
                current_target = 0
            try:
                current_shortfall = int(current_progress.get("recommendation_shortfall") or 0)
            except Exception:
                current_shortfall = 0
        if not current_strong and isinstance(current_results, dict):
            current_strong = len(current_results.get("strong_recommendations", []))
        if current_target and not current_shortfall:
            current_shortfall = max(0, current_target - current_strong)
        state = {
            "project": args.project,
            "venue": args.venue,
            "queries": queries,
            "status": "recorded_queries_no_find_requested",
            "current_find_run_id": current_run_id,
            "current_strong_recommendations": current_strong,
            "current_recommendation_target_count": current_target,
            "current_recommendation_shortfall": current_shortfall,
            "current_evaluated_candidates": int(progress_counts.get("evaluated_candidates") or progress_counts.get("detail_fetched") or (len(current_results.get("evaluated_candidates", [])) if isinstance(current_results, dict) else 0) or 0),
            "current_counts": progress_counts,
            "guardrail": "Record-only mode was explicitly requested for this invocation. Normal TASTE literature repair may launch a controlled targeted Find when this flag is absent; weak papers still cannot be promoted to satisfy the gate.",
            "record_only_requested": True,
            "new_find_allowed": False,
        }
        save_json(paths.state / "taste_targeted_queries.json", state)
        packet_cmd = [sys.executable, str(ROOT / "scripts" / "build_literature_tool_packet.py"), "--project", args.project]
        if args.venue:
            packet_cmd.extend(["--venue", args.venue])
        packet = run(packet_cmd, env=env, timeout=180, live_log_path=paths.logs / "literature_tool.log")
        state["packet_return_code"] = packet.returncode
        state["packet_stdout_tail"] = packet.stdout[-3000:]
        state["packet_stderr_tail"] = packet.stderr[-3000:]
        state["current_find_plan_sync"] = sync_current_find_plan_queries(paths, state)
        save_json(paths.state / "taste_targeted_queries.json", state)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if packet.returncode == 0 else packet.returncode
    if args.deep_survey:
        env["DEEP_SURVEY"] = "1"
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "run_frontend.py"),
        "--project",
        args.project,
        "--max-papers",
        str(args.max_papers),
        "--max-ideas",
        str(args.max_ideas),
        "--timeout-sec",
        str(args.timeout_sec),
    ]
    if args.fast_mode and not args.deep_survey:
        # Do not forward --fast-mode to run_frontend: that mode caps
        # max_papers/max_ideas and skips venue discovery, which breaks the TASTE
        # full-cycle contract. The wrapper's fast flag now only means targeted
        # repair queries with bounded but gate-complete budgets.
        pass
    if args.deep_survey:
        cmd.append("--deep-survey")
    if args.skip_arxiv:
        cmd.append("--skip-arxiv")
    if args.skip_huggingface:
        cmd.append("--skip-huggingface")
    if args.skip_github:
        cmd.append("--skip-github")
    if args.skip_venues:
        cmd.append("--skip-venues")
    write_running_state(paths, args, queries, cmd, env)
    logs: list[dict[str, Any]] = []
    proc = run(cmd, env=env, timeout=args.timeout_sec + 180 if args.timeout_sec > 0 else None, live_log_path=paths.logs / "literature_tool.log")
    logs.append({"stage": "taste", "command": cmd, "return_code": proc.returncode, "stdout_tail": proc.stdout[-6000:], "stderr_tail": proc.stderr[-6000:]})
    sync_cmd = [sys.executable, str(ROOT / "scripts" / "sync_outputs.py"), "--project", args.project, "--allow-empty"]
    packet_cmd = [sys.executable, str(ROOT / "scripts" / "build_literature_tool_packet.py"), "--project", args.project]
    if args.venue:
        packet_cmd.extend(["--venue", args.venue])
    if proc.returncode in {0, 124}:
        sync = run(sync_cmd, env=env, timeout=300, live_log_path=paths.logs / "literature_tool.log")
        logs.append({"stage": "sync", "command": sync_cmd, "return_code": sync.returncode, "stdout_tail": sync.stdout[-3000:], "stderr_tail": sync.stderr[-3000:]})
        packet = run(packet_cmd, env=env, timeout=180, live_log_path=paths.logs / "literature_tool.log")
        logs.append({"stage": "packet", "command": packet_cmd, "return_code": packet.returncode, "stdout_tail": packet.stdout[-3000:], "stderr_tail": packet.stderr[-3000:]})
    else:
        sync = subprocess.CompletedProcess(sync_cmd, 0, "skipped because TASTE failed before producing a valid current Find output", "")
        packet = subprocess.CompletedProcess(packet_cmd, 0, "skipped because TASTE failed before producing a valid current Find output", "")
        logs.append({"stage": "sync", "command": sync_cmd, "return_code": "skipped", "stdout_tail": sync.stdout, "stderr_tail": ""})
        logs.append({"stage": "packet", "command": packet_cmd, "return_code": "skipped", "stdout_tail": packet.stdout, "stderr_tail": ""})
    if proc.returncode == 0 and sync.returncode == 0 and packet.returncode == 0:
        tool_status = "completed"
    elif proc.returncode == 124:
        tool_status = "taste_timeout_partial"
    elif proc.returncode != 0:
        tool_status = "failed_taste_preserved_previous_find"
    else:
        tool_status = "completed_with_warnings"
    current_progress = load_json(paths.planning / "finding" / "find_progress.json", {})
    progress_counts = current_progress.get("counts", {}) if isinstance(current_progress, dict) and isinstance(current_progress.get("counts"), dict) else {}
    state = {
        "project": args.project,
        "venue": args.venue,
        "queries": queries,
        "mode": "deep_survey" if args.deep_survey else "fast_mode" if args.fast_mode else "standard",
        "commands": {
            "taste": cmd,
            "sync": sync_cmd,
            "packet": packet_cmd,
        },
        "return_codes": {
            "taste": proc.returncode,
            "sync": sync.returncode,
            "packet": packet.returncode,
        },
        "status": tool_status,
        "current_find_run_id": str(current_progress.get("run_id") or "") if isinstance(current_progress, dict) else "",
        "current_strong_recommendations": int(current_progress.get("strong_recommendation_count") or 0) if isinstance(current_progress, dict) else 0,
        "current_recommendation_target_count": int(current_progress.get("recommendation_target_count") or 0) if isinstance(current_progress, dict) else 0,
        "current_recommendation_shortfall": int(current_progress.get("recommendation_shortfall") or 0) if isinstance(current_progress, dict) else 0,
        "current_counts": progress_counts,
        "packet_path": str(paths.planning / "literature_tool_packet.md"),
        "packet_json": str(paths.state / "literature_tool_packet.json"),
        "log_path": str(paths.logs / "literature_tool.log"),
        "failure_summary": _failure_summary_from_logs(logs) if tool_status != "completed" else "",
        "guardrail": "This wrapper ran a controlled TASTE literature survey for targeted literature repair. Outputs are literature signals only until TASTE verifies repo/data/experiment evidence; weak papers must not be promoted to satisfy gates.",
        "record_only_requested": False,
        "new_find_allowed": True,
    }
    state["current_find_plan_sync"] = sync_current_find_plan_queries(paths, state)
    save_json(paths.state / "literature_tool_last_run.json", state)
    save_json(paths.state / "taste_targeted_queries.json", state)
    write_completed_full_cycle_state(paths, args, state)
    paths.logs.mkdir(parents=True, exist_ok=True)
    with (paths.logs / "literature_tool.log").open("a", encoding="utf-8") as handle:
        handle.write("\n[literature_tool] final_state\n")
        handle.write(json.dumps({"state": state, "stages": logs}, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    if proc.returncode not in {0, 124}:
        return proc.returncode
    if sync.returncode != 0:
        return sync.returncode
    return packet.returncode


if __name__ == "__main__":
    raise SystemExit(main())
