#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from literature_policy import now_utc, repo_sort_key, score_repo_candidate
from project_paths import ROOT, build_paths, load_project_config

GITHUB_API = "https://api.github.com/search/repositories"


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


def flush_progress(paths, payload: dict[str, Any]) -> None:
    save_json(paths.state / "literature_base_audit.json", payload)
    print(
        "[literature-base-audit] {status}: {candidate_count}/{total} candidates, remaining={remaining}, repos={repos}, gate={gate}".format(
            status=payload.get("status", "running"),
            candidate_count=payload.get("candidate_count", 0),
            total=payload.get("total_audit_required_count", 0),
            remaining=payload.get("remaining_candidate_count", 0),
            repos=payload.get("repo_candidates_discovered_count", 0),
            gate=payload.get("selection_gate", ""),
        ),
        flush=True,
    )


def one_line(value: Any, limit: int = 260) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def safe_slug(value: str, limit: int = 80) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip()).strip("_").lower()
    return (text or "candidate")[:limit]




def candidate_search_queries(row: dict[str, Any], per_candidate: int) -> list[str]:
    title = str(row.get("title") or row.get("name") or "").strip()
    reason = str(row.get("reason") or row.get("summary") or "").strip()
    queries: list[str] = []
    if title:
        safe_title = " ".join(title.replace(chr(34), " ").split())
        quoted = f'"{safe_title}"'
        queries.extend([quoted, quoted + " code", quoted + " github"])
    compact = re.sub(r"[^A-Za-z0-9 ]+", " ", title).strip()
    words = [w for w in compact.split() if len(w) > 2]
    if words:
        queries.append(" ".join(words[:8]) + " github")
    reason_words = [w for w in re.sub(r"[^A-Za-z0-9 ]+", " ", reason).split() if len(w) > 3]
    if reason_words:
        queries.append(" ".join((words + reason_words)[:10]) + " github")
    out: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            out.append(query)
    return out[: max(1, per_candidate)]

def github_search(project: str, query: str, limit: int, timeout: int = 25) -> dict[str, Any]:
    params = urllib.parse.urlencode({"q": query, "per_page": max(1, min(limit, 10)), "sort": "stars", "order": "desc"})
    url = f"{GITHUB_API}?{params}"
    headers = {
        "User-Agent": "TASTE-Literature-Base-Audit/0.1",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = __import__("os").environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8", "ignore"))
        items = []
        for item in raw.get("items", []) or []:
            items.append({
                "name": item.get("full_name") or item.get("name") or "",
                "url": item.get("html_url") or "",
                "summary": item.get("description") or "",
                "stars": item.get("stargazers_count", 0),
                "forks": item.get("forks_count", 0),
                "language": item.get("language") or "",
                "topics": item.get("topics", []) or [],
                "last_pushed_at": item.get("pushed_at") or "",
                "updated_at": item.get("updated_at") or "",
                "created_at": item.get("created_at") or "",
                "recent_activity": True,
                "has_license": bool(item.get("license")),
                "source": "fresh_literature_github_search",
                "query": query,
                "project": project,
            })
        return {"status": "ok", "query": query, "items": items}
    except (HTTPError, URLError, TimeoutError) as exc:
        return {"status": "unavailable", "query": query, "error": str(exc), "items": []}




def current_find_run_id(paths, explicit: str = "") -> str:
    explicit = str(explicit or "").strip()
    if explicit:
        return explicit
    for path in [
        paths.planning / "finding" / "find_progress.json",
        paths.planning / "finding" / "find_results.json",
        paths.state / "current_find_research_plan.json",
        paths.state / "fresh_research_base.json",
    ]:
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            continue
        for key in ["run_id", "current_find_run_id", "find_run_id", "fresh_find_run_id", "source_run_id"]:
            value = str(payload.get(key) or "").strip()
            if value:
                return value
    return ""


def _candidate_row_from_base(row: dict[str, Any], index: int, run_id: str) -> dict[str, Any]:
    return {
        "rank": row.get("rank", index),
        "title": row.get("title") or row.get("name") or "",
        "venue": row.get("venue") or row.get("source") or "",
        "year": row.get("year", ""),
        "score": row.get("score") or row.get("recommendation_score") or row.get("final_score") or 0,
        "fresh_base_score": row.get("fresh_base_score", 0),
        "reason": row.get("fit_explanation_zh") or row.get("fit_explanation") or row.get("reason") or row.get("abstract_zh") or row.get("abstract_en") or row.get("abstract") or "",
        "code_links": row.get("code_links") if isinstance(row.get("code_links"), list) else [],
        "signals": row.get("signals") if isinstance(row.get("signals"), dict) else {},
        "fresh_find_run_id": run_id,
    }


def candidates_from_fresh_base_state(paths, run_id: str) -> list[dict[str, Any]]:
    fresh = load_json(paths.state / "fresh_research_base.json", {})
    if not isinstance(fresh, dict):
        return []
    fresh_run_id = str(fresh.get("fresh_find_run_id") or "").strip()
    if run_id and fresh_run_id != run_id:
        return []
    rows = fresh.get("top_candidates") if isinstance(fresh.get("top_candidates"), list) else []
    return [_candidate_row_from_base(row, index, run_id or fresh_run_id) for index, row in enumerate(rows, start=1) if isinstance(row, dict)]


def candidates_from_current_find(paths, run_id: str) -> list[dict[str, Any]]:
    if not run_id:
        return []
    find = load_json(paths.planning / "finding" / "find_results.json", {})
    packet = load_json(paths.state / "literature_tool_packet.json", {})
    if not isinstance(find, dict):
        return []
    find_run = str(find.get("run_id") or find.get("current_find_run_id") or "").strip()
    if find_run and find_run != run_id:
        return []
    try:
        from select_fresh_research_base import build_candidate_pool
        cfg = load_project_config(paths.name)
        rows = build_candidate_pool(find, packet if isinstance(packet, dict) else {}, cfg)
    except Exception:
        rows = []
    if not rows:
        for key in ["strong_recommendations", "articles", "recommended", "recommendations"]:
            pool = find.get(key)
            if isinstance(pool, list) and pool:
                rows = [row for row in pool if isinstance(row, dict)]
                break
    return [_candidate_row_from_base(row, index, run_id) for index, row in enumerate(rows, start=1) if isinstance(row, dict)]


def candidates_from_current_fresh_base(paths, explicit_run_id: str = "") -> tuple[str, list[dict[str, Any]]]:
    run_id = current_find_run_id(paths, explicit_run_id)
    rows = candidates_from_fresh_base_state(paths, run_id)
    if rows:
        return run_id, rows
    return run_id, candidates_from_current_find(paths, run_id)


def repo_rows_from_code_links(project: str, row: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    title = str(row.get("title") or "")
    for url in row.get("code_links") or []:
        text = str(url or "").strip()
        if not text:
            continue
        if "github.com/" in text.lower():
            parts = text.split("github.com/", 1)[1].strip("/").split("/")
            name = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
        else:
            name = safe_slug(title or text, 60)
        out.append({
            "name": name,
            "url": text,
            "summary": f"Code link supplied by current Find base candidate: {one_line(title, 120)}",
            "stars": 0,
            "forks": 0,
            "language": "",
            "topics": [],
            "recent_activity": True,
            "has_license": False,
            "source": "fresh_literature_github_search",
            "query": "code_links_from_fresh_research_base",
            "project": project,
            "literature_base_title": title,
            "literature_base_rank": row.get("rank", ""),
            "fresh_find_run_id": run_id,
            "notes": f"current fresh_research_base code link for: {one_line(title, 120)}",
        })
    return out

def repo_registry_rows(paths) -> list[dict[str, Any]]:
    rows = load_json(paths.state / "repo_candidates.json", [])
    return rows if isinstance(rows, list) else []


def merge_repo_rows(paths, rows: list[dict[str, Any]]) -> None:
    existing = repo_registry_rows(paths)
    by_url: dict[str, dict[str, Any]] = {}
    for row in existing:
        if isinstance(row, dict):
            key = str(row.get("url") or row.get("name") or "").lower()
            if key:
                by_url[key] = dict(row)
    cfg = load_project_config(paths.name)
    reference_time = now_utc()
    for row in rows:
        key = str(row.get("url") or row.get("name") or "").lower()
        if not key:
            continue
        merged = dict(by_url.get(key, {}))
        merged.update({k: v for k, v in row.items() if v not in (None, "", [], {})})
        merged.setdefault("notes", "fresh Find literature base audit repo candidate; must be cloned/probed before selection")
        merged.update(score_repo_candidate(merged, cfg, reference_time=reference_time))
        merged["score"] = merged.get("repo_reuse_score", merged.get("score", 0))
        by_url[key] = merged
    out = sorted(by_url.values(), key=repo_sort_key)
    save_json(paths.state / "repo_candidates.json", out)
    lines = ["# Repo Candidates\n\n", "| Score | Name | URL | Fit | Activity | Install | Entrypoint | Notes |\n", "| --- | --- | --- | --- | --- | --- | --- | --- |\n"]
    for row in out:
        lines.append(f"| {row.get('score', 0)} | {row.get('name', '')} | {row.get('url', '')} | {row.get('task_fit', False)} | {row.get('activity_bucket', row.get('recent_activity', False))} | {row.get('has_install', False)} | {row.get('has_entrypoint', False)} | {row.get('notes', '')} |\n")
    (paths.reports / "repo_candidates.md").write_text("".join(lines), encoding="utf-8")


def run_cmd(cmd: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
        return {"command": cmd, "return_code": proc.returncode, "stdout_tail": (proc.stdout or "")[-2000:], "stderr_tail": (proc.stderr or "")[-2000:]}
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "ignore")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "ignore")
        return {"command": cmd, "return_code": 124, "stdout_tail": stdout[-2000:], "stderr_tail": (stderr + f"\nTIMEOUT after {timeout}s")[-2000:]}


def build(project: str, limit: int, repo_search_per_candidate: int, repo_limit: int, probe_timeout: int, fresh_find_run_id: str = "") -> dict[str, Any]:
    paths = build_paths(project)
    assessment = load_json(paths.state / "literature_base_candidate_assessment.json", {})
    current_run_id, current_candidates = candidates_from_current_fresh_base(paths, fresh_find_run_id)
    assessment_run_id = str(assessment.get("fresh_find_run_id") or "").strip() if isinstance(assessment, dict) else ""
    # Environment/base audit must follow the current Find Top-N base candidates.
    # The broader assessment file may contain boundary/background papers for
    # critique, but those are not the ordered recommendation pool that should
    # drive repository selection.
    if current_run_id and current_candidates:
        assessment = {
            "fresh_find_run_id": current_run_id,
            "audit_required_candidates": current_candidates,
            "source": "current_find_authoritative_candidate_pool",
            "status": "current_find_base_candidates",
        }
    elif current_run_id and assessment_run_id != current_run_id:
        assessment = {
            "fresh_find_run_id": current_run_id,
            "audit_required_candidates": current_candidates,
            "source": "current_find_authoritative_candidate_pool",
            "status": "current_find_base_candidates",
        }
    all_candidates = assessment.get("audit_required_candidates", []) if isinstance(assessment, dict) else []
    all_candidates = [row for row in all_candidates if isinstance(row, dict)]
    candidates = all_candidates[: max(1, limit)]
    audit_complete = len(candidates) >= len(all_candidates)
    discovered: list[dict[str, Any]] = []
    audited: list[dict[str, Any]] = []
    progress_payload = {
        "project": project,
        "generated_at": now_iso(),
        "fresh_find_run_id": assessment.get("fresh_find_run_id", "") if isinstance(assessment, dict) else "",
        "candidate_count": 0,
        "total_audit_required_count": len(all_candidates),
        "audit_complete": False,
        "remaining_candidate_count": len(all_candidates),
        "repo_candidates_discovered_count": 0,
        "audited_literature_candidates": [],
        "selector": {},
        "selection_gate": "repo_search_running",
        "selected": {},
        "status": "running_fresh_literature_repo_search",
        "policy": "Current Find fresh base candidates must be resolved through repo/data/env evidence before any legacy active_repo can remain or be replaced as the main route.",
    }
    flush_progress(paths, progress_payload)
    for index, row in enumerate(candidates, start=1):
        record = {
            "rank": row.get("rank", index),
            "title": row.get("title", ""),
            "venue": row.get("venue", ""),
            "year": row.get("year", ""),
            "fresh_find_run_id": assessment.get("fresh_find_run_id", "") if isinstance(assessment, dict) else "",
            "queries": [],
            "repo_candidates": [],
            "decision": "repo_search_pending",
        }
        direct_rows = repo_rows_from_code_links(project, row, record["fresh_find_run_id"])
        for item in direct_rows:
            discovered.append(item)
            record["repo_candidates"].append({"name": item.get("name"), "url": item.get("url"), "stars": item.get("stars"), "query": "code_links_from_fresh_research_base"})
        for query in candidate_search_queries(row, repo_search_per_candidate):
            print(f"[literature-base-audit] candidate {index}/{len(candidates)} query: {query}", flush=True)
            result = github_search(project, query, repo_limit)
            record["queries"].append({"query": query, "status": result.get("status"), "error": result.get("error", ""), "count": len(result.get("items", []))})
            for item in result.get("items", []) or []:
                item = dict(item)
                item["literature_base_title"] = row.get("title", "")
                item["literature_base_rank"] = row.get("rank", index)
                item["fresh_find_run_id"] = record["fresh_find_run_id"]
                item["notes"] = f"fresh Find base candidate audit for: {one_line(row.get('title'), 120)}"
                discovered.append(item)
                record["repo_candidates"].append({"name": item.get("name"), "url": item.get("url"), "stars": item.get("stars"), "query": query})
        record["decision"] = "repo_candidates_discovered_needs_clone_probe" if record["repo_candidates"] else "no_repo_candidate_found_yet"
        audited.append(record)
        progress_payload.update({
            "generated_at": now_iso(),
            "candidate_count": len(audited),
            "remaining_candidate_count": max(0, len(all_candidates) - len(audited)),
            "repo_candidates_discovered_count": len(discovered),
            "audited_literature_candidates": audited,
            "selection_gate": "repo_search_running" if len(audited) < len(candidates) else "repo_search_complete_selector_pending",
            "status": "running_fresh_literature_repo_search" if len(audited) < len(candidates) else "running_selector_pending",
        })
        flush_progress(paths, progress_payload)
    if discovered:
        print(f"[literature-base-audit] merging {len(discovered)} repo candidates into repo registry", flush=True)
        merge_repo_rows(paths, discovered)
    progress_payload.update({
        "generated_at": now_iso(),
        "candidate_count": len(candidates),
        "remaining_candidate_count": max(0, len(all_candidates) - len(candidates)),
        "repo_candidates_discovered_count": len(discovered),
        "audited_literature_candidates": audited,
        "selection_gate": "selector_running",
        "status": "running_evidence_ready_repo_selector",
    })
    flush_progress(paths, progress_payload)
    print("[literature-base-audit] running evidence-ready repo selector", flush=True)
    select = run_cmd([
        sys.executable,
        "framework/scripts/run_module.py",
        "environment",
        "--action",
        "select_evidence_ready",
        "--project",
        project,
        "--limit",
        str(max(8, min(24, len(discovered) or 8))),
        "--timeout-sec",
        str(probe_timeout),
        "--allow-veto-fallback",
        "--use-claude-review",
        "--selection-stage",
        "literature_repo_data_audit",
        "--candidate-source",
        "fresh_literature_github_search",
        "--fresh-find-run-id",
        progress_payload.get("fresh_find_run_id", ""),
        "--exclude-active-repo",
    ], ROOT, timeout=max(180, probe_timeout * max(1, min(6, len(discovered) or 1)) + 240))
    selection = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    selected = selection.get("selected", {}) if isinstance(selection, dict) and isinstance(selection.get("selected"), dict) else {}
    completed = bool(selected)
    payload = {
        "project": project,
        "generated_at": now_iso(),
        "fresh_find_run_id": assessment.get("fresh_find_run_id", "") if isinstance(assessment, dict) else "",
        "candidate_count": len(candidates),
        "total_audit_required_count": len(all_candidates),
        "audit_complete": audit_complete,
        "remaining_candidate_count": max(0, len(all_candidates) - len(candidates)),
        "repo_candidates_discovered_count": len(discovered),
        "audited_literature_candidates": audited,
        "selector": select,
        "selection_gate": selection.get("selection_gate", "") if isinstance(selection, dict) else "",
        "selected": selected,
        "status": (
            "completed_selected_evidence_ready_base" if completed else
            "blocked_partial_fresh_literature_base_audit" if not audit_complete else
            "blocked_no_evidence_ready_base_from_fresh_literature_yet"
        ),
        "policy": "Current Find fresh base candidates must be resolved through repo/data/env evidence before any legacy active_repo can remain or be replaced as the main route.",
    }
    save_json(paths.state / "literature_base_audit.json", payload)
    if isinstance(assessment, dict) and assessment:
        assessment = dict(assessment)
        assessment["last_audit_generated_at"] = payload["generated_at"]
        assessment["last_audit_status"] = payload["status"]
        assessment["last_audit_repo_candidates_discovered_count"] = payload["repo_candidates_discovered_count"]
        assessment["last_audit_selection_gate"] = payload.get("selection_gate", "")
        assessment["last_audit_selected"] = selected
        assessment["last_audit_candidate_count"] = payload["candidate_count"]
        assessment["last_audit_total_required_count"] = payload["total_audit_required_count"]
        assessment["last_audit_complete"] = payload["audit_complete"]
        assessment["last_audit_remaining_candidate_count"] = payload["remaining_candidate_count"]
        if completed:
            assessment["status"] = "fresh_literature_base_audit_completed_selected_base"
            assessment["stale_existing_base_decision"] = False
            assessment["stale_reason"] = "Fresh literature base audit selected an evidence-ready base candidate; rerun reference reproduction gate for the selected route."
        elif not audit_complete:
            assessment["status"] = "blocked_pending_literature_base_audit"
            assessment["stale_existing_base_decision"] = True
            assessment["stale_reason"] = (
                f"Fresh literature base audit has only checked {payload['candidate_count']} / {payload['total_audit_required_count']} candidates. "
                "Remaining candidates must be audited before TASTE may declare no viable base switch or keep a historical route as the main route."
            )
        else:
            assessment["status"] = "fresh_literature_base_audit_completed_no_evidence_ready_base"
            assessment["stale_existing_base_decision"] = False
            assessment["stale_reason"] = "Fresh literature base candidates were searched/audited, but no evidence-ready repo/data/env route was selected yet."
        save_json(paths.state / "literature_base_candidate_assessment.json", assessment)
    if (not completed) and audit_complete:
        blocker = {
            "status": "blocked",
            "blocker_type": "fresh_literature_base_audit_no_evidence_ready_route",
            "recommended_route": "continue_search_or_request_new_evidence_before_historical_route",
            "fresh_find_run_id": payload.get("fresh_find_run_id", ""),
            "total_candidates_evaluated": payload.get("candidate_count", 0),
            "repo_candidates_discovered_count": payload.get("repo_candidates_discovered_count", 0),
            "execution_ready_count": 0,
            "reason": "Fresh Find candidates were consumed by repo/data/env audit, but no evidence-ready base route was selected. A historical route may not be treated as validated unless a separate gate explicitly accepts it after this audit.",
            "updated_at": payload["generated_at"],
        }
        save_json(paths.state / "repo_selection_blocker.json", blocker)
    lines = ["# Literature Base Audit\n\n"]
    for key in ["status", "fresh_find_run_id", "candidate_count", "total_audit_required_count", "audit_complete", "remaining_candidate_count", "repo_candidates_discovered_count", "selection_gate"]:
        lines.append(f"- {key}: {payload.get(key)}\n")
    if selected:
        lines.append(f"- selected_repo: {selected.get('name')} | {selected.get('repo_path')} | dataset={selected.get('claim_ready_dataset')}\n")
    lines.append("\n## Literature Candidates\n")
    for row in audited:
        lines.append(f"- rank={row.get('rank')} title={row.get('title')} decision={row.get('decision')} repos={len(row.get('repo_candidates', []))}\n")
    lines.append("\n## Selector\n")
    lines.append(f"- return_code: {select.get('return_code')}\n")
    if select.get("stderr_tail"):
        lines.append("```text\n" + str(select.get("stderr_tail"))[-1600:] + "\n```\n")
    (paths.reports / "literature_base_audit.md").write_text("".join(lines), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve fresh Find base-work candidates through repo/data/env audit before TASTE continues.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--repo-search-per-candidate", type=int, default=3)
    parser.add_argument("--repo-limit", type=int, default=5)
    parser.add_argument("--probe-timeout-sec", type=int, default=120)
    parser.add_argument("--fresh-find-run-id", default="", help="Current Find run id that must drive literature base audit.")
    args = parser.parse_args()
    payload = build(args.project, args.limit, args.repo_search_per_candidate, args.repo_limit, args.probe_timeout_sec, args.fresh_find_run_id)
    print(build_paths(args.project).reports / "literature_base_audit.md")
    return 0 if payload.get("status") == "completed_selected_evidence_ready_base" else 2


if __name__ == "__main__":
    raise SystemExit(main())
