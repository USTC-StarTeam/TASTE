#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths

FRAMEWORK_ROOTS = [ROOT / "framework" / "scripts", ROOT / "framework", ROOT / "web" / "backend", ROOT / "web" / "frontend", ROOT / "modules" / "finding", ROOT / "modules" / "reading", ROOT / "modules" / "ideation", ROOT / "modules" / "planning", ROOT / "modules" / "environment", ROOT / "modules" / "experimenting", ROOT / "modules" / "writing"]
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".sh", ".md"}
IGNORED_DIR_NAMES = {"__pycache__", ".pytest_cache", "node_modules", "dist", "build", ".git", "data", "local_database", "runs", "state", "logs", "cache", ".cache"}
RUNTIME_STATE_DIR_NAMES = {"runs", "state", "logs", "cache", ".cache", "local_database", "quarantine"}
DEBRIS_NAME_MARKERS = (".bak", ".tmp.", ".broken_", ".orig", ".rej")
GENERATED_FRAMEWORK_CACHE_NAMES = {
    "article.md",
    "read.md",
    "idea.md",
    "plan.md",
    "strict_recommendation_audit.md",
    "critique_candidates.md",
    "read_candidates.md",
    "screened_ranking.md",
    "source_status.md",
    "biorxiv.md",
    "github.md",
    "hf.md",
    "nature.md",
    "science.md",
    "arxiv_prefiltered.json",
    "arxiv_raw.json",
    "category_scan_report.json",
    "find_results.json",
    "read_results.json",
    "ideas.json",
    "plans.json",
    "strict_recommendation_audit.json",
    "title_filter_report.json",
    "venue_health_report.json",
}
GENERATED_FRAMEWORK_STAGE_DIRS = {"auto_find", "auto_read", "auto_idea", "auto_plan"}
GENERIC_ALLOWLIST = {
    "ar", "taste", "paperorchestra", "paper", "orchestra", "llm",
    "recommendation", "baseline", "control", "candidate", "experiment",
    "reference", "reproduction", "synthetic", "toy", "train", "test", "data", "main",
    "find", "read", "idea", "plan", "github", "http", "https", "home", "workspace",
    "projects", "repos", "selected", "candidates", "state", "artifacts", "logs", "python",
    "dataset", "method", "model", "run", "audit", "gate", "fresh", "base", "route",
    "full", "bounded", "smoke", "probe", "loader", "contract", "venue", "iclr", "neurips",
    "kdd", "icml", "current", "legacy", "public", "status", "summary", "action", "script",
    "json", "from", "path", "name", "source", "search", "filter", "score", "update", "policy",
    "repo", "local", "artifact", "config", "project", "topic", "title", "paper", "paperwork",
    "progress", "text", "official", "evidence", "commit", "short", "split", "cond", "claude",
    "code", "native", "planning", "packet", "tool", "results", "with", "literature", "ideas",
    "plans", "category", "eval", "recommendations", "article", "blocker", "semantic", "sync",
    "initialization", "scientific", "report", "quality", "metric", "metrics", "note", "notes",
    "description", "generated", "output", "stdout", "stderr", "command", "launcher", "runtime",
    "reference_reproduction", "base_reference", "fresh_base_reference_reproduction",
    "reproduction_audit", "selected_base_reference", "audit.json", "audit_json", "metrics.json",
    "fresh_base_reference_reproduction_audit", "fresh_base_reference_reproduction_audit.json",
    "fresh_base_reference_full_reproduction_audit", "fresh_base_reference_full_reproduction_audit.json",
    "referencereproduction", "basereference", "finetune_llm", "finetune_llm_seminit",
    "exp_text_init", "exp_text_init_standard_train", "llm_candidate", "llm_fusion", "llm_emb",
    "text_embeddings", "descriptions", "base_experiments", "fresh_base_experiments",
    "embedding_initialization", "test_eval", "all_test_evaluations", "semantic_proxy",
    "cluster_non_promotable_candidate_rows", "repo_real_reproduction_smoke",
    "reproduction_smoke", "repo_real_metric_audit_probe", "repo_real_hparam_sanity",
    "short_reproduction", "data_paired", "evidence_ready_repo_and_data_paired",
    "selected_base_official_reference", "official_reference", "audit_probe",
    "proxy_semantic_embedding_candidate", "embedding_candidate", "current_route_candidate",
    "route_candidate", "run_contract_present", "contract_present", "command_recorded",
    "dataset_recorded", "method_recorded", "planned_epochs_recorded",
    "completion_evidence_recorded", "log_present_and_hashed", "checkpoint_sha256",
    "artifact_contract", "artifact_contract_assessment",
    "default", "core", "models", "model", "prompt", "prompts", "tools", "tool",
    "analysis", "requirements", "components", "figure", "figures", "adapters",
    "generation", "datasets", "checks", "links", "forks", "init", "__init__", "__init__.py",
    "workspace_root", "workspace", "conda", "miniforge", "cuda", "gpu", "home",
    "ready", "decision", "paired", "passed", "authorized", "blocked", "required",
    "system", "systems", "language", "large", "condition", "conditional", "generative", "discrete",
    "research", "researcher", "researchers", "profile", "profiles", "agent", "agents",
    "workflow", "workflows", "autonomous", "automation", "benchmark", "benchmarks",
    "benchmarking", "bench", "web", "api", "available", "skill", "skills",
    "environment", "environments", "detection", "detect", "assurance",
}


def identifier_contains(haystack: str, token: str) -> bool:
    if not token:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(token.lower())}(?![a-z0-9])", haystack.lower()) is not None


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def compact(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def normalized_forms(value: Any, *, split_parts: bool = False, min_len: int = 4) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return set()
    lower = text.lower()
    pieces = [piece for piece in re.split(r"[^a-z0-9]+", lower) if piece]
    forms: set[str] = set()
    sep = re.sub(r"[^a-z0-9]+", "_", lower).strip("_")
    joined = re.sub(r"[^a-z0-9]+", "", lower)
    for form in [lower, sep, joined]:
        if len(form) >= min_len:
            forms.add(form)
    if len(pieces) >= 2:
        for form in ["_".join(pieces[-2:]), "".join(pieces[-2:])]:
            if len(form) >= min_len:
                forms.add(form)
    if split_parts:
        for piece in pieces:
            if len(piece) >= min_len:
                forms.add(piece)
    return {form for form in forms if len(form) >= min_len and form not in GENERIC_ALLOWLIST and not form.isdigit()}


def add_identifier(tokens: set[str], value: Any, *, split_parts: bool = False, min_len: int = 4) -> None:
    tokens.update(normalized_forms(value, split_parts=split_parts, min_len=min_len))


def add_short_identifier(short_tokens: set[str], value: Any) -> None:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    if 2 <= len(text) <= 3 and text not in GENERIC_ALLOWLIST and not text.isdigit():
        short_tokens.add(text)


def add_repo_identifier(tokens: set[str], value: Any) -> None:
    if not isinstance(value, str):
        return
    text = str(value or "").strip()
    if not text:
        return
    cleaned = text.rstrip("/").split("github.com/", 1)[-1]
    cleaned = cleaned.removesuffix(".git")
    add_identifier(tokens, cleaned, split_parts=False)
    parts = [part for part in re.split(r"[/\\]+", cleaned) if part]
    if parts:
        add_identifier(tokens, parts[-1], split_parts=True)


def add_repo_path_identifier(tokens: set[str], value: Any) -> None:
    if not isinstance(value, str):
        return
    text = str(value or "").strip()
    if not text:
        return
    name = Path(text).name
    add_identifier(tokens, name, split_parts=False)
    pieces = [piece for piece in re.split(r"[^a-z0-9]+", name.lower()) if piece]
    if pieces:
        add_identifier(tokens, pieces[-1], split_parts=False)


def add_phrase(phrases: set[str], value: Any) -> None:
    if not isinstance(value, str):
        return
    text = " ".join(str(value or "").lower().split())
    if len(text) >= 24:
        phrases.add(text)


def collect_project_content(project: str) -> tuple[set[str], set[str], set[str], list[str]]:
    paths = build_paths(project)
    tokens: set[str] = set()
    short_tokens: set[str] = set()
    phrases: set[str] = set()
    sources: list[str] = []

    add_identifier(tokens, project, split_parts=True)
    if tokens:
        sources.append("project_id")

    config_payload = load_json(paths.config, {})
    if isinstance(config_payload, dict) and config_payload:
        sources.append(str(paths.config.relative_to(ROOT)))
        # Project prose often contains generic words such as system, language,
        # large, or condition. Use it for phrase matching only; single-token
        # hard failures are reserved for project ids, repo names, dataset ids,
        # and explicit state identities.
        for key in ["topic", "title", "user_prompt", "research_interest"]:
            add_phrase(phrases, config_payload.get(key, ""))
        for row in config_payload.get("queries", []) if isinstance(config_payload.get("queries"), list) else []:
            add_phrase(phrases, row)
        literature = config_payload.get("literature", {}) if isinstance(config_payload.get("literature"), dict) else {}
        for axis in literature.get("topic_axes", []) if isinstance(literature.get("topic_axes"), list) else []:
            if not isinstance(axis, dict):
                continue
            add_phrase(phrases, axis.get("name", ""))
            for marker in axis.get("required_any", []) if isinstance(axis.get("required_any"), list) else []:
                add_phrase(phrases, marker)

    state_names = [
        "evidence_ready_repo_selection.json",
        "active_repo.json",
        "fresh_research_base.json",
        "fresh_base_implementation_plan.json",
        "reference_reproduction_gate.json",
        "dataset_registry.json",
        "repo_data_requirements.json",
        "base_switch_gate.json",
        "base_switch_execution.json",
        "obsolete_baseline_cleanup_plan.json",
        "obsolete_baseline_cleanup_authorization.json",
        "selected_base_viability_gate.json",
    ]
    repo_keys = {"repo", "repo_name", "repo_url", "url"}
    repo_name_keys = {"name"}
    dataset_keys = {"dataset", "dataset_name", "claim_ready_dataset", "benchmark"}
    phrase_keys = {"title", "paper_title", "base_title", "literature_base_title", "selected_base_title"}
    path_keys = {"repo_path", "local_path", "selected_repo_path", "active_repo_path"}
    identity_parents = {"", "selected", "selected_base", "active_repo", "repo", "fresh_paper_base", "current_route"}

    def visit(value: Any, *, depth: int = 0, parent_key: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key or "")
                identity_context = depth <= 3 and parent_key in identity_parents
                if identity_context and key_text in repo_keys:
                    add_repo_identifier(tokens, item)
                    sources.append(f"state_repo_key:{key_text}")
                elif identity_context and key_text in repo_name_keys and isinstance(item, str) and ("/" in item or "github" in item.lower()):
                    add_repo_identifier(tokens, item)
                    sources.append(f"state_repo_name_key:{key_text}")
                elif identity_context and key_text in dataset_keys and isinstance(item, str):
                    add_identifier(tokens, item, split_parts=False)
                    add_short_identifier(short_tokens, item)
                    sources.append(f"state_dataset_key:{key_text}")
                elif identity_context and key_text in phrase_keys:
                    add_phrase(phrases, item)
                    sources.append(f"state_phrase_key:{key_text}")
                elif identity_context and key_text in path_keys:
                    add_repo_path_identifier(tokens, item)
                    sources.append(f"state_repo_path_key:{key_text}")
                visit(item, depth=depth + 1, parent_key=key_text)
        elif isinstance(value, list):
            for item in value[:120]:
                visit(item, depth=depth + 1, parent_key=parent_key)

    for name in state_names:
        state_path = paths.state / name
        payload = load_json(state_path, {})
        if payload:
            sources.append(str(state_path.relative_to(ROOT)))
            visit(payload)

    for repo_dir in [paths.repos_selected, paths.repos_candidates]:
        if repo_dir.exists():
            for child in repo_dir.iterdir():
                if child.is_dir():
                    add_repo_path_identifier(tokens, child.name)
                    sources.append(str(child.relative_to(ROOT)))

    tokens = {token for token in tokens if len(token) >= 4 and token not in GENERIC_ALLOWLIST and not token.isdigit()}
    short_tokens = {token for token in short_tokens if 2 <= len(token) <= 3 and token not in GENERIC_ALLOWLIST and not token.isdigit()}
    return tokens, short_tokens, phrases, sorted(set(sources))[:120]

def framework_debris_reason(path: Path) -> str:
    name = path.name.lower()
    if any(marker in name for marker in DEBRIS_NAME_MARKERS):
        return "generated_backup_or_temp_file"
    if path.parent.name in GENERATED_FRAMEWORK_STAGE_DIRS and name in GENERATED_FRAMEWORK_CACHE_NAMES:
        return "generated_framework_cache_file"
    if name == ".config.json":
        return "legacy_runtime_config_inside_framework_root"
    if name.startswith(".config.json.bak"):
        return "generated_config_backup"
    return ""


def framework_file_is_text_or_debris(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or bool(framework_debris_reason(path))


def iter_framework_files() -> list[Path]:
    out: list[Path] = []
    for base in FRAMEWORK_ROOTS:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if any(part in IGNORED_DIR_NAMES for part in path.parts):
                continue
            if path.is_file() and framework_file_is_text_or_debris(path):
                out.append(path)
    return sorted(out)


def framework_runtime_state_findings() -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for base in FRAMEWORK_ROOTS:
        if not base.exists():
            continue
        for name in sorted(RUNTIME_STATE_DIR_NAMES):
            path = base / name
            if not path.exists() or not path.is_dir():
                continue
            try:
                rel = str(path.relative_to(ROOT))
            except Exception:
                rel = str(path)
            findings.append({
                "file": rel,
                "line": 0,
                "tokens": [],
                "context": "runtime_state_or_cache_directory_inside_framework_root",
                "kind": "framework_runtime_state_or_cache_dir",
                "severity": "warn",
            })
    return findings


def scan_file(path: Path, tokens: set[str], short_tokens: set[str], phrases: set[str]) -> list[dict[str, Any]]:
    rel = str(path.relative_to(ROOT))
    findings: list[dict[str, Any]] = []
    debris_reason = framework_debris_reason(path)
    if debris_reason:
        findings.append({
            "file": rel,
            "line": 0,
            "tokens": [],
            "context": debris_reason,
            "kind": "framework_generated_backup_or_cache",
            "severity": "block",
        })
        return findings
    name_l = path.name.lower()
    name_hits = sorted(token for token in tokens if identifier_contains(name_l, token))
    if name_hits:
        findings.append({
            "file": rel,
            "line": 0,
            "tokens": name_hits[:12],
            "context": path.name,
            "kind": "framework_filename_content_token",
            "severity": "block",
        })
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return findings
    for line_no, line in enumerate(text.splitlines(), start=1):
        hay = line.lower()
        hits = sorted(token for token in tokens if identifier_contains(hay, token))
        short_hits = sorted(token for token in short_tokens if token and re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", hay))
        phrase_hits = sorted(phrase for phrase in phrases if phrase and phrase in hay)
        if not hits and not short_hits and not phrase_hits:
            continue
        findings.append({
            "file": rel,
            "line": line_no,
            "tokens": (hits + short_hits + phrase_hits)[:12],
            "context": compact(line),
            "kind": "framework_code_content_token",
            "severity": "block",
        })
    return findings


def write_report(paths, payload: dict[str, Any]) -> Path:
    out = paths.reports / "framework_content_coupling_audit.md"
    lines = [
        "# TASTE Framework Content Coupling Audit\n\n",
        f"- status: {payload.get('status')}\n",
        f"- finding_count: {payload.get('finding_count')}\n",
        f"- framework_debris_count: {payload.get('framework_debris_count', 0)}\n",
        f"- framework_runtime_state_dir_count: {payload.get('framework_runtime_state_dir_count', 0)}\n",
        "- policy: framework/control code must not hard-code project-specific paper, repo, method, dataset, or route names. Project-local files may contain project science. Generated backups/caches do not belong in framework roots.\n\n",
    ]
    for row in payload.get("findings", [])[:100]:
        loc = f"{row.get('file')}:{row.get('line')}" if row.get("line") else str(row.get("file"))
        lines.append(f"- {loc}: tokens={', '.join(row.get('tokens') or [])}; {row.get('context', '')}\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(lines), encoding="utf-8")
    return out


def build(project: str) -> dict[str, Any]:
    paths = build_paths(project)
    tokens, short_tokens, phrases, token_sources = collect_project_content(project)
    findings: list[dict[str, Any]] = []
    findings.extend(framework_runtime_state_findings())
    for path in iter_framework_files():
        findings.extend(scan_file(path, tokens, short_tokens, phrases))
    findings.sort(key=lambda row: (str(row.get("file")), int(row.get("line") or 0), str(row.get("context") or "")))
    block_findings = [row for row in findings if row.get("severity") == "block"]
    warn_findings = [row for row in findings if row.get("severity") != "block"]
    status = "blocked" if block_findings else "warn" if warn_findings else "pass"
    payload = {
        "project": project,
        "generated_at": now_iso(),
        "status": status,
        "policy": "framework/control code must remain research-content agnostic; project-local science belongs under projects/<project>.",
        "scope": [str(path.relative_to(ROOT)) for path in FRAMEWORK_ROOTS],
        "project_content_token_count": len(tokens),
        "project_content_short_token_count": len(short_tokens),
        "project_content_phrase_count": len(phrases),
        "project_content_token_sources": token_sources,
        "framework_debris_count": sum(1 for row in findings if row.get("kind") == "framework_generated_backup_or_cache"),
        "framework_runtime_state_dir_count": sum(1 for row in findings if row.get("kind") == "framework_runtime_state_or_cache_dir"),
        "finding_count": len(findings),
        "blocking_finding_count": len(block_findings),
        "warning_finding_count": len(warn_findings),
        "findings": findings[:500],
        "next_action": "Keep framework code content-agnostic. Remaining warnings are runtime/cache directories under the framework root; keep those runtime stores out of framework/auto_research when doing the next state-boundary cleanup.",
    }
    save_json(paths.state / "framework_content_coupling_audit.json", payload)
    report = write_report(paths, payload)
    payload["report_path"] = str(report)
    save_json(paths.state / "framework_content_coupling_audit.json", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit whether framework code is coupled to project-specific research content.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="", help="Accepted for orchestrator compatibility; this audit is venue-agnostic.")
    args = parser.parse_args()
    payload = build(args.project)
    print(build_paths(args.project).state / "framework_content_coupling_audit.json")
    return 0 if payload.get("status") in {"pass", "warn"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
