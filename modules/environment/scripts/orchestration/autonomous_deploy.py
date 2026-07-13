#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import json
import re
import shutil
import shlex
import subprocess
import sys
import time
from string import Template
from pathlib import Path
from typing import Any

MODULE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = MODULE_ROOT.parents[1]
RUNTIME_ROOT = MODULE_ROOT / ".runtime"
RUNS_ROOT = RUNTIME_ROOT / "runs"
PROMPTS_ROOT = MODULE_ROOT / "prompts"
SKILL_ROOT = MODULE_ROOT / "skills" / "environment-deployment"
PUBLIC_ENTRYPOINT_ENV = "ENVIRONMENT_PUBLIC_ENTRYPOINT_ACTIVE"
DECISION_POLICY_VERSION = "environment.deployment_decision.v79"
APPROVAL_GATE_REQUIRED_CHECKS = (
    "repository_source",
    "repository_documentation",
    "conda_environment",
    "machine_fit",
    "dataset_evidence",
    "required_commands",
    "paper_claims_verified",
    "success_criteria_schema",
    "success_criteria_paper_binding",
    "metric_evidence",
    "paper_context",
    "reproduce_full",
    "paper_config_alignment",
    "workspace_write_audit",
)
ENVIRONMENT_HANDOFF_REQUIRED_CHECKS = (
    "repository_source",
    "conda_environment",
    "machine_fit",
    "dataset_runtime",
    "required_commands",
    "runtime_smoke",
    "paper_config_alignment",
    "workspace_write_audit",
)
ENVIRONMENT_HANDOFF_ALLOWED_PENDING_CHECKS = (
    "repository_documentation",
    "dataset_evidence",
    "paper_claims_verified",
    "success_criteria_schema",
    "success_criteria_paper_binding",
    "metric_evidence",
    "paper_context",
    "reproduce_full",
)
MODULES_ROOT = REPO_ROOT / "modules"
for item in list(sys.path):
    try:
        resolved = Path(item).expanduser().resolve(strict=False)
        if resolved != MODULE_ROOT and resolved != MODULE_ROOT / "scripts":
            try:
                resolved.relative_to(MODULES_ROOT)
                sys.path.remove(item)
            except ValueError:
                pass
    except Exception:
        pass
for candidate in [MODULE_ROOT, MODULE_ROOT / "scripts"]:
    text = str(candidate)
    while text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)

from scripts.common.claude_runner import run_claude_json
from scripts.common.io_utils import ensure_within, read_json, slugify, utc_now, write_json
from scripts.common.plan_schema import load_experiment_plan, normalize_plan
from scripts.common.shell import EXTERNAL_RUNTIME_ENV_KEYS, command_is_dangerous, command_text, command_tokens, isolated_runtime_env, run_logged, runtime_env
from scripts.environment.runtime_probe import detect_machine_profile, find_conda_executable
from scripts.repository.repo_manager import clone_or_reuse, collect_repo_evidence
from scripts.reproduction.decision import compare_metric_values, metric_criteria_passed, normalize_verdict, success_criteria_issues
from scripts.reproduction.paper_evidence import collect_paper_evidence


def require_public_entrypoint() -> None:
    if os.environ.get(PUBLIC_ENTRYPOINT_ENV) == "1":
        return
    raise SystemExit("Use modules/environment/main.py to call Environment functionality.")


def resolve_run_dir(value: str) -> Path:
    if not str(value or "").strip():
        raise SystemExit("Environment internal error: missing --run-dir from modules/environment/main.py")
    path = Path(value).expanduser().resolve()
    try:
        ensure_within(path, RUNS_ROOT)
    except ValueError as exc:
        raise SystemExit(f"--run-dir 必须位于 {RUNS_ROOT} 内：{exc}") from exc
    if not path.exists() or not path.is_dir():
        raise SystemExit(f"--run-dir 必须由 modules/environment/main.py 预先创建：{path}")
    return path


def is_github_repo_url(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("https://github.com/") or text.startswith("http://github.com/") or text.startswith("git@github.com:") or text.startswith("ssh://git@github.com/")


def canonical_repo_url(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("git@github.com:"):
        text = "https://github.com/" + text[len("git@github.com:"):]
    elif text.startswith("ssh://git@github.com/"):
        text = "https://github.com/" + text[len("ssh://git@github.com/"):]
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return text.rstrip("/").lower()


def validate_repo_candidate_review(original_candidates: list[str], reviewed: dict[str, Any]) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    status = str(reviewed.get("status") or "").strip().lower()
    if status != "ready":
        issues.append(f"仓库候选审阅 status 不是 ready：{status or 'missing'}")
        return [], issues
    ordered = reviewed.get("ordered_repo_urls")
    if not isinstance(ordered, list) or not ordered:
        issues.append("仓库候选审阅缺少非空 ordered_repo_urls")
        return [], issues
    allowed = {canonical_repo_url(item): str(item).strip() for item in original_candidates if str(item).strip()}
    clean: list[str] = []
    for item in ordered:
        url = str(item or "").strip()
        key = canonical_repo_url(url)
        if not url:
            continue
        if key not in allowed:
            issues.append(f"仓库候选审阅编造了原候选之外的 URL：{url}")
            continue
        if not is_github_repo_url(url):
            issues.append(f"仓库候选审阅返回非 GitHub URL：{url}")
            continue
        if allowed[key] not in clean:
            clean.append(allowed[key])
    if not clean:
        issues.append("仓库候选审阅没有留下可克隆的 GitHub 原候选")
    return clean, issues


def repo_candidates_after_review(original_candidates: list[str], review_result: dict[str, Any]) -> tuple[list[str], list[str]]:
    reviewed_json = review_result.get("json") if isinstance(review_result.get("json"), dict) else {}
    if not reviewed_json:
        return [], ["repo candidate review did not produce valid JSON"]
    if str(reviewed_json.get("status") or "").strip().lower() == "reject":
        return [], [str(reviewed_json.get("reject_reason") or "Claude Code 判定 plan 中的 GitHub 仓库候选不可信")]
    clean_ordered, review_issues = validate_repo_candidate_review([str(item) for item in original_candidates], reviewed_json if isinstance(reviewed_json, dict) else {})
    return clean_ordered, review_issues


def validate_discovered_repo(discovered: dict[str, Any]) -> tuple[str, list[str]]:
    issues: list[str] = []
    status = str(discovered.get("status") or "").strip().lower()
    repo_url = str(discovered.get("repo_url") or "").strip()
    if status != "ready":
        issues.append(f"仓库发现 status 不是 ready：{status or 'missing'}")
    if not repo_url:
        issues.append("仓库发现缺少 repo_url")
    elif not is_github_repo_url(repo_url):
        issues.append(f"仓库发现返回非 GitHub URL：{repo_url}")
    evidence = discovered.get("evidence")
    if not isinstance(evidence, list) or not [item for item in evidence if str(item).strip()]:
        issues.append("仓库发现缺少可信 evidence")
    if "confidence" in discovered:
        try:
            confidence = float(discovered.get("confidence"))
            if confidence < 0.5:
                issues.append(f"仓库发现 confidence 过低：{confidence}")
        except Exception:
            issues.append("仓库发现 confidence 不是数字")
    return repo_url, issues


def discovered_repo_candidates(discovered: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(url: Any, source: str, evidence: Any = None) -> None:
        text = str(url or "").strip().rstrip("/.,);]}>\"'")
        if not is_github_repo_url(text):
            return
        key = canonical_repo_url(text)
        if key in seen:
            return
        seen.add(key)
        row: dict[str, Any] = {"url": text, "source": source}
        if evidence:
            row["evidence"] = evidence
        candidates.append(row)

    add(discovered.get("repo_url"), "repo_url", discovered.get("evidence"))
    return candidates


def repo_specs_by_url(normalized_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    specs = normalized_plan.get("repo_candidate_specs") if isinstance(normalized_plan.get("repo_candidate_specs"), list) else []
    out: dict[str, dict[str, Any]] = {}
    for item in specs:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if url:
            out.setdefault(canonical_repo_url(url), dict(item))
    return out


def repo_spec_for_url(url: str, specs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return dict(specs.get(canonical_repo_url(url)) or {"url": url})


def successful_clone_repo_rows(clone_attempts: list[dict[str, Any]], primary_repo_path: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    primary_resolved = ""
    if primary_repo_path:
        try:
            primary_resolved = str(Path(primary_repo_path).expanduser().resolve())
        except Exception:
            primary_resolved = str(primary_repo_path)
    for attempt in clone_attempts:
        if not isinstance(attempt, dict) or attempt.get("exists") is not True:
            continue
        receipt = attempt.get("clone_receipt") if isinstance(attempt.get("clone_receipt"), dict) else {}
        if int(receipt.get("return_code") or 0) != 0:
            continue
        repo_path = str(attempt.get("repo_path") or "").strip()
        if not repo_path:
            continue
        try:
            resolved = str(Path(repo_path).expanduser().resolve())
            repo_dir = Path(repo_path).name
        except Exception:
            resolved = repo_path
            repo_dir = Path(repo_path).name
        if resolved in seen:
            continue
        seen.add(resolved)
        rows.append({
            "repo_url": attempt.get("repo_url", ""),
            "repo_path": repo_path,
            "repo_dir": repo_dir,
            "head_commit": attempt.get("head_commit", ""),
            "role": "primary" if primary_resolved and resolved == primary_resolved else "auxiliary",
        })
    return rows


def collect_auxiliary_repo_evidence(available_rows: list[dict[str, Any]], primary_repo_path: Path) -> list[dict[str, Any]]:
    primary_resolved = ""
    try:
        primary_resolved = str(primary_repo_path.expanduser().resolve())
    except Exception:
        primary_resolved = str(primary_repo_path)
    out: list[dict[str, Any]] = []
    for row in available_rows:
        repo_path_text = str(row.get("repo_path") or "").strip()
        if not repo_path_text:
            continue
        try:
            repo_path = Path(repo_path_text).expanduser().resolve()
        except Exception:
            continue
        if str(repo_path) == primary_resolved or not repo_path.exists():
            continue
        evidence = collect_repo_evidence(repo_path)
        out.append({
            "repo_url": row.get("repo_url", ""),
            "repo_path": str(repo_path),
            "repo_dir": row.get("repo_dir", repo_path.name),
            "head_commit": row.get("head_commit", ""),
            "evidence_summary": evidence.get("evidence_summary", {}),
            "readmes": (evidence.get("readmes") if isinstance(evidence.get("readmes"), list) else [])[:2],
            "config_files": (evidence.get("config_files") if isinstance(evidence.get("config_files"), list) else [])[:10],
            "python_entrypoints": (evidence.get("python_entrypoints") if isinstance(evidence.get("python_entrypoints"), list) else [])[:12],
        })
    return out


def clone_ref_from_spec(spec: dict[str, Any]) -> tuple[str, str]:
    commit = str(spec.get("commit") or "").strip()
    branch_or_tag = str(spec.get("branch") or spec.get("tag") or "").strip()
    if not branch_or_tag and not commit:
        branch_or_tag = str(spec.get("revision") or "").strip()
    return branch_or_tag, commit


def github_only_rejection(run_id: str, run_dir: Path, normalized: dict[str, Any], repo_url: str, machine: dict[str, Any]) -> dict[str, Any]:
    verdict = {
        "decision": "reject",
        "allow_next_module": False,
        "reject_reason": f"仓库来源不是 GitHub，environment 不把非 GitHub 源作为论文复现基底：{repo_url}",
        "failure_taxonomy": [{"category": "repository_unreliable", "evidence": [f"非 GitHub 仓库 URL：{repo_url}"], "repairable": False}],
    }
    return final_decision_payload(run_id, run_dir, normalized, {"repo_url": repo_url, "exists": False}, machine, [], verdict)



def _git_status_line_path_and_status(line: str) -> tuple[str, str]:
    if len(line) < 4:
        return "", ""
    status = line[:2]
    raw = line[3:].strip()
    if " -> " in raw:
        raw = raw.split(" -> ", 1)[-1].strip()
    return raw.strip('"'), status


def _workspace_path_state(path: str, status: str) -> dict[str, Any]:
    target = REPO_ROOT / path
    state: dict[str, Any] = {"status": status}
    try:
        stat = target.lstat()
    except FileNotFoundError:
        state["kind"] = "missing"
        return state
    except Exception as exc:
        state["kind"] = "unknown"
        state["stat_error"] = f"{type(exc).__name__}: {exc}"
        return state
    state.update({
        "mode": stat.st_mode,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    })
    if target.is_symlink():
        state["kind"] = "symlink"
        try:
            state["target"] = os.readlink(target)
        except Exception as exc:
            state["target_error"] = f"{type(exc).__name__}: {exc}"
    elif target.is_file():
        state["kind"] = "file"
    elif target.is_dir():
        state["kind"] = "directory"
    else:
        state["kind"] = "other"
    return state


def _external_runtime_write_prefixes() -> tuple[str, ...]:
    prefixes: set[str] = set()
    for value in str(os.environ.get("ENVIRONMENT_WORKSPACE_AUDIT_IGNORE_PATHS") or "").split(os.pathsep):
        text = value.strip()
        if not text:
            continue
        candidate = Path(text).expanduser()
        resolved = (candidate if candidate.is_absolute() else REPO_ROOT / candidate).resolve()
        try:
            relative = resolved.relative_to(REPO_ROOT.resolve()).as_posix().strip("/")
        except ValueError:
            continue
        if relative and relative != "modules/environment" and not relative.startswith("modules/environment/"):
            prefixes.add(relative)
    return tuple(sorted(prefixes))


def _is_external_runtime_write(path: str) -> bool:
    rel = str(path or "").strip().lstrip("./")
    if not rel:
        return False
    return any(rel == prefix or rel.startswith(prefix + "/") for prefix in _external_runtime_write_prefixes())


def _filter_external_runtime_writes(paths: list[str] | set[str]) -> list[str]:
    return sorted(path for path in paths if not _is_external_runtime_write(path))


def _git_status_outside_environment() -> dict[str, dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=30,
        )
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    state: dict[str, dict[str, Any]] = {}
    for line in proc.stdout.splitlines():
        raw, status = _git_status_line_path_and_status(line)
        if not raw or raw == "modules/environment" or raw.startswith("modules/environment/") or _is_external_runtime_write(raw):
            continue
        state[raw] = _workspace_path_state(raw, status)
    return state


def _workspace_audit_marker(run_dir: Path) -> Path:
    return ensure_within(Path(run_dir) / ".workspace_write_audit_marker", MODULE_ROOT)


def _workspace_write_audit_baseline(run_dir: Path) -> dict[str, Any]:
    marker = _workspace_audit_marker(run_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(utc_now() + "\n", encoding="utf-8")
    now = time.time()
    os.utime(marker, (now, now))
    return {
        "schema_version": "environment.workspace_audit_baseline.v2",
        "created_at": utc_now(),
        "marker_path": str(marker),
        "git_state": _git_status_outside_environment(),
    }


def _baseline_git_state(baseline_outside_state: dict[str, Any] | set[str], current: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if isinstance(baseline_outside_state, set):
        return {path: current.get(path, {}) for path in baseline_outside_state}
    if isinstance(baseline_outside_state, dict) and isinstance(baseline_outside_state.get("git_state"), dict):
        return dict(baseline_outside_state.get("git_state") or {})
    return dict(baseline_outside_state or {})


def _relative_repo_path(path: Path) -> str:
    try:
        return path.expanduser().resolve().relative_to(REPO_ROOT.expanduser().resolve()).as_posix()
    except Exception:
        return ""


def _recent_workspace_paths_outside_environment(marker_path: str, limit: int = 200) -> list[str]:
    marker = Path(str(marker_path or "")).expanduser()
    if not marker.exists():
        return []
    prune_paths = [REPO_ROOT / ".git", MODULE_ROOT]
    find_expr: list[str] = ["find", str(REPO_ROOT), "("]
    for index, item in enumerate(prune_paths):
        if index:
            find_expr.append("-o")
        find_expr.extend(["-path", str(item)])
    find_expr.extend([")", "-prune", "-o", "-newer", str(marker), "-print"])
    try:
        proc = subprocess.run(find_expr, cwd=REPO_ROOT, text=True, capture_output=True, timeout=45)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for raw in proc.stdout.splitlines():
        rel = _relative_repo_path(Path(raw))
        if not rel or rel == "." or rel.startswith(".git/") or rel == "modules/environment" or rel.startswith("modules/environment/") or _is_external_runtime_write(rel):
            continue
        if rel not in seen:
            seen.add(rel)
            paths.append(rel)
        if len(paths) >= limit:
            break
    return sorted(paths)


def _workspace_write_audit(baseline_outside_state: dict[str, Any] | set[str]) -> dict[str, Any]:
    current = _git_status_outside_environment()
    baseline = _baseline_git_state(baseline_outside_state, current)
    marker_path = str(baseline_outside_state.get("marker_path") or "") if isinstance(baseline_outside_state, dict) else ""
    recent_paths = _recent_workspace_paths_outside_environment(marker_path)
    baseline_paths = set(baseline)
    current_paths = set(current)
    new_paths = _filter_external_runtime_writes(current_paths - baseline_paths)
    changed_paths = _filter_external_runtime_writes(path for path in baseline_paths & current_paths if baseline.get(path) != current.get(path))
    resolved_paths = _filter_external_runtime_writes(baseline_paths - current_paths)
    changed_details = [
        {"path": path, "before": baseline.get(path), "after": current.get(path)}
        for path in changed_paths[:40]
    ]
    failed = bool(new_paths or changed_paths or resolved_paths or recent_paths)
    return {
        "status": "passed" if not failed else "failed",
        "policy": "environment 运行期间不能新增、修改或清理 modules/environment 之外的 TASTE git 路径，也不能在 environment 之外写入或修改被 git 忽略的中间产物。",
        "baseline_schema_version": str(baseline_outside_state.get("schema_version") or "legacy") if isinstance(baseline_outside_state, dict) else "legacy_set",
        "filesystem_marker_path": marker_path,
        "filesystem_marker_exists": bool(marker_path and Path(marker_path).exists()),
        "baseline_outside_environment_count": len(baseline_paths),
        "current_outside_environment_count": len(current_paths),
        "new_outside_environment_paths": new_paths[:120],
        "new_outside_environment_count": len(new_paths),
        "changed_outside_environment_paths": changed_paths[:120],
        "changed_outside_environment_count": len(changed_paths),
        "changed_outside_environment_details": changed_details,
        "resolved_outside_environment_paths": resolved_paths[:120],
        "resolved_outside_environment_count": len(resolved_paths),
        "recent_filesystem_paths_outside_environment": recent_paths[:120],
        "recent_filesystem_paths_outside_environment_count": len(recent_paths),
    }


def _recompute_approval_gate(gate: dict[str, Any]) -> dict[str, Any]:
    checks = gate.get("checks") if isinstance(gate.get("checks"), list) else []
    missing = [str(row.get("reason") or row.get("name") or "未命名门槛") for row in checks if isinstance(row, dict) and not row.get("passed")]
    gate["schema_version"] = str(gate.get("schema_version") or "environment.approval_gate.v1")
    gate["policy_version"] = DECISION_POLICY_VERSION
    gate["passed"] = not missing
    gate["missing"] = missing
    gate["checks"] = checks
    return gate


def _upsert_approval_gate_check(gate: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(gate, dict):
        gate = {}
    checks = gate.get("checks") if isinstance(gate.get("checks"), list) else []
    name = str(check.get("name") or "").strip()
    replaced = False
    for index, row in enumerate(checks):
        if isinstance(row, dict) and str(row.get("name") or "") == name:
            checks[index] = check
            replaced = True
            break
    if not replaced:
        checks.append(check)
    gate["checks"] = checks
    return _recompute_approval_gate(gate)


def _attach_workspace_audit_to_approval_gate(decision: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    audit_passed = audit.get("status") == "passed"
    check = {
        "name": "workspace_write_audit",
        "passed": audit_passed,
        "reason": "工作区审计通过" if audit_passed else "工作区审计发现 environment 之外新增、修改、清理 git 路径或写入忽略产物",
        "evidence": audit,
    }
    verdict = decision.get("verdict") if isinstance(decision.get("verdict"), dict) else {}
    existing_gate = verdict.get("approval_gate") if isinstance(verdict.get("approval_gate"), dict) else decision.get("approval_gate")
    should_attach = isinstance(existing_gate, dict) and bool(existing_gate)
    should_attach = should_attach or decision.get("allow_next_module") is True or not audit_passed
    if not should_attach:
        return decision
    gate = _upsert_approval_gate_check(existing_gate if isinstance(existing_gate, dict) else {}, check)
    decision["approval_gate"] = gate
    if isinstance(verdict, dict):
        verdict["approval_gate"] = gate
        decision["verdict"] = verdict
    if not gate.get("passed"):
        decision.setdefault("approval_contract", {})["approved"] = False
    return decision


def _apply_workspace_write_audit(decision: dict[str, Any], baseline_outside_state: dict[str, dict[str, Any]] | set[str]) -> dict[str, Any]:
    audit = _workspace_write_audit(baseline_outside_state)
    decision["workspace_write_audit"] = audit
    decision = _attach_workspace_audit_to_approval_gate(decision, audit)
    if audit.get("status") == "failed":
        verdict = decision.setdefault("verdict", {})
        if isinstance(verdict, dict):
            verdict["decision"] = "continue_repair"
            verdict["allow_next_module"] = False
            verdict.setdefault("repair_plan", []).append("后端工作区审计发现 environment 之外新增、修改、清理 git 变更或写入忽略产物；必须先检查并清理越界写入。")
            taxonomy = verdict.setdefault("failure_taxonomy", [])
            if isinstance(taxonomy, list):
                taxonomy.append({"category": "repository_code", "evidence": [*audit.get("new_outside_environment_paths", []), *audit.get("changed_outside_environment_paths", []), *audit.get("resolved_outside_environment_paths", []), *audit.get("recent_filesystem_paths_outside_environment", [])], "repairable": True})
        decision["decision"] = "continue_repair"
        decision["allow_next_module"] = False
        decision["exit_code"] = 30
        decision.setdefault("approval_contract", {})["approved"] = False
    return decision



def attach_runtime_isolation_summary(decision: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    runtime_root = run_dir / ".runtime"
    env_name = str((decision.get("verdict") or {}).get("env_name") or "") if isinstance(decision.get("verdict"), dict) else ""
    decision["runtime_isolation"] = {
        "runtime_root": str(runtime_root),
        "conda_envs_dir": str(run_dir / "conda_envs"),
        "cache_root": str(runtime_root / "cache"),
        "tmp_dir": str(runtime_root / "tmp"),
        "command_home": str(runtime_root / "home"),
        "command_home_isolated": True,
        "claude_home_isolated": False,
        "cache_env_keys_redirected": [
            "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "TMPDIR", "TEMP", "TMP",
            "PIP_CACHE_DIR", "HF_HOME", "HF_DATASETS_CACHE", "TRANSFORMERS_CACHE", "TORCH_HOME",
            "MPLCONFIGDIR", "WANDB_DIR", "WANDB_CACHE_DIR", "WANDB_CONFIG_DIR", "PYTHONPYCACHEPREFIX", "CONDA_PKGS_DIRS",
        ],
        "base_env_scrubbed_keys": sorted([*EXTERNAL_RUNTIME_ENV_KEYS, "CONDA_PREFIX_*"]),
        "path_scrubbed_prefix_sources": ["CONDA_PREFIX", "CONDA_PREFIX_*", "VIRTUAL_ENV"],
        "command_env_protected_keys": sorted(PROTECTED_COMMAND_ENV_KEYS),
        "command_env_path_list_keys_checked": sorted(PATH_LIST_COMMAND_ENV_KEYS),
        "command_env_exact_path_keys_checked": sorted(EXACT_PATH_COMMAND_ENV_KEYS),
        "python_entrypoints_wrapped_in_conda_prefix": sorted(PYTHON_ENV_ENTRYPOINTS),
        "policy": "实验命令使用隔离 HOME 和 run 内缓存目录；基础环境会清除继承自调用进程的 Conda/Python/动态库变量，并从 PATH 剔除继承的 conda/venv bin；conda -n 和 Python 生态入口会重写到 run-local conda_envs/<env>；命令 cwd、路径值参数、本地脚本/文件参数、Python -c 内联代码路径字面量和命令级 env 路径均必须留在本次 run 目录内；Python -X/-W 等解释器选项之后的 -c 也会被识别；已知路径参数的裸相对值、bash/python 脚本文件参数、下载/解压/构建/依赖工具的贴值短路径选项，以及 PIP_TARGET/PIP_PREFIX/PIP_INDEX_URL/PIP_FIND_LINKS/PIP_CONSTRAINT/UV_INDEX_URL/PYTHONUSERBASE/LD_PRELOAD/CONDA_ENVS_PATH 等命令级路径环境变量也会按命令 cwd 解析；`file://` 本地源会剥离成真实路径检查，`PIP_FIND_LINKS`/`UV_FIND_LINKS`/约束文件等多值 env 会按 shell 风格空白分词逐项检查，远程 http/https/git/HF/S3/GS URL 保持可用。Claude Code 自身保留 HOME 以便认证，但缓存仍指向 run 目录。",
    }
    if env_name:
        decision["runtime_isolation"]["declared_env_name"] = env_name
    return decision

def finalize_and_write_decision(decision: dict[str, Any], run_dir: Path, paper_evidence: dict[str, Any], baseline_outside_state: dict[str, dict[str, Any]] | set[str]) -> int:
    decision = attach_paper_summary(decision, run_dir, paper_evidence)
    decision = attach_runtime_isolation_summary(decision, run_dir)
    decision = _ensure_approval_gate_for_early_decision(decision, paper_evidence)
    decision = _apply_workspace_write_audit(decision, baseline_outside_state)
    decision = _refresh_environment_handoff_workspace_audit(decision)
    write_json(run_dir / "environment_deployment_decision.json", decision)
    emit_progress("complete", f"Environment finished with decision={decision.get('decision') or 'unknown'}.")
    print(json.dumps(decision, ensure_ascii=False, indent=2))
    return int(decision["exit_code"])


def emit_progress(phase: str, message: str, *, round_index: int = 0) -> None:
    payload: dict[str, Any] = {"event": "environment_progress", "phase": phase, "message": message}
    if round_index > 0:
        payload["round"] = round_index
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _prompt_template(name: str) -> Template:
    path = PROMPTS_ROOT / name
    return Template(path.read_text(encoding="utf-8"))


def _prompt_json(value: Any, limit: int, *, source_path: Path | None = None) -> str:
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    if len(rendered) <= limit:
        return rendered
    if source_path is None:
        return rendered
    source_path = source_path.expanduser().resolve()
    if not source_path.exists():
        write_json(source_path, value)
    return json.dumps(
        {
            "_prompt_truncated": True,
            "must_read_full_json": str(source_path),
            "original_json_chars": len(rendered),
        },
        ensure_ascii=False,
        indent=2,
    )


def _render_prompt(name: str, **values: Any) -> str:
    rendered_values = {key: str(value) for key, value in values.items()}
    rendered_values.setdefault("environment_skill_path", str(SKILL_ROOT / "SKILL.md"))
    return _prompt_template(name).safe_substitute(rendered_values).strip() + "\n"


def _claude_add_dirs(*paths: Path) -> list[Path]:
    out: list[Path] = []
    for path in [*paths, SKILL_ROOT]:
        if path.exists() and path not in out:
            out.append(path)
    return out


def prompt_repo_candidate_review(normalized_plan: dict[str, Any], repo_candidates: list[str], output_path: Path) -> str:
    normalized_path = output_path.parent / "input_plan.normalized.json"
    return _render_prompt(
        "repo_candidate_review.md",
        output_path=output_path,
        repo_candidates_json=_prompt_json(repo_candidates, 20000, source_path=normalized_path),
        normalized_plan_json=_prompt_json(normalized_plan, 30000, source_path=normalized_path),
    )


def prompt_repo_discovery(normalized_plan: dict[str, Any], output_path: Path) -> str:
    return _render_prompt(
        "repo_discovery.md",
        output_path=output_path,
        normalized_plan_json=_prompt_json(normalized_plan, 30000, source_path=output_path.parent / "input_plan.normalized.json"),
    )


def prompt_environment_plan(normalized_plan: dict[str, Any], machine: dict[str, Any], repo_evidence: dict[str, Any], paper_evidence: dict[str, Any], previous_rounds: list[dict[str, Any]], output_path: Path, round_index: int, fixed_env_name: str = "") -> str:
    run_dir = output_path.parent.parent
    return _render_prompt(
        "environment_plan.md",
        output_path=output_path,
        round_index=round_index,
        normalized_plan_json=_prompt_json(normalized_plan, 40000, source_path=run_dir / "input_plan.normalized.json"),
        machine_json=_prompt_json(machine, 20000, source_path=run_dir / "machine_profile.json"),
        repo_evidence_json=_prompt_json(repo_evidence, 50000, source_path=run_dir / "repo_evidence.json"),
        paper_evidence_json=_prompt_json(paper_evidence, 50000, source_path=run_dir / "paper_evidence.json"),
        previous_rounds_json=_prompt_json(previous_rounds, 40000, source_path=output_path.parent / "previous_rounds.prompt.json"),
        fixed_env_name_instruction=(
            f"Set env_name exactly to `{fixed_env_name}` in this and every later round."
            if fixed_env_name
            else "Choose one concise env_name now; the backend will fix that name for every later round."
        ),
    )


def prompt_audit_judgement(normalized_plan: dict[str, Any], paper_evidence: dict[str, Any], env_plan: dict[str, Any], receipts: list[dict[str, Any]], metric_evidence: list[dict[str, Any]], output_path: Path) -> str:
    run_dir = output_path.parent.parent
    env_plan_paths = sorted(output_path.parent.glob("claude_environment_plan_round_*.json"))
    env_plan_path = env_plan_paths[-1] if env_plan_paths else output_path.parent / "environment_plan.audit_input.json"
    return _render_prompt(
        "audit_judgement.md",
        output_path=output_path,
        normalized_plan_json=_prompt_json(normalized_plan, 25000, source_path=run_dir / "input_plan.normalized.json"),
        paper_evidence_json=_prompt_json(paper_evidence, 30000, source_path=run_dir / "paper_evidence.json"),
        env_plan_json=_prompt_json(env_plan, 30000, source_path=env_plan_path),
        receipts_json=_prompt_json(receipts, 50000, source_path=output_path.parent / "command_receipts.json"),
        metric_evidence_json=_prompt_json(metric_evidence, 12000, source_path=output_path.parent / "metric_evidence.prompt.json"),
    )

def env_prefix_for(run_dir: Path, env_name: str) -> Path:
    name = slugify(env_name or "environment_env", "environment_env")
    return run_dir / "conda_envs" / name


def _replace_or_add_conda_prefix(tokens: list[str], env_prefix: Path) -> list[str]:
    if not tokens or Path(tokens[0]).name not in {"conda", "mamba", "micromamba"}:
        return tokens
    prefix = str(env_prefix)
    out: list[str] = []
    found = False
    skip_next = False
    for index, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if token in {"-n", "--name"} and index + 1 < len(tokens):
            if not found:
                out.extend(["-p", prefix])
                found = True
            skip_next = True
            continue
        if token.startswith("--name="):
            if not found:
                out.append(f"--prefix={prefix}")
                found = True
            continue
        if token in {"-p", "--prefix"} and index + 1 < len(tokens):
            out.extend(["-p", prefix])
            found = True
            skip_next = True
            continue
        if token.startswith("--prefix="):
            out.append(f"--prefix={prefix}")
            found = True
            continue
        out.append(token)
    if found:
        return out
    if len(out) >= 3 and out[1] == "env" and out[2] in {"create", "update"}:
        return [*out[:3], "-p", prefix, *out[3:]]
    if len(out) >= 2 and out[1] in {"create", "install", "update", "run"}:
        return [*out[:2], "-p", prefix, *out[2:]]
    return out

def _raw_path_starts_with_run_repos(raw: str) -> bool:
    normalized = str(raw or "").strip().strip('"').strip("'").replace("\\", "/")
    return normalized == "repos" or normalized.startswith("repos/")


def _path_base_for_value(raw: str, cwd: Path, run_dir: Path) -> Path:
    if _raw_path_starts_with_run_repos(raw):
        return run_dir.expanduser().resolve()
    return cwd.expanduser().resolve()


def _resolve_command_path_value(path_value: str, cwd: Path, run_dir: Path) -> Path:
    value = _strip_path_token(path_value)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = _path_base_for_value(value, cwd, run_dir) / candidate
    return ensure_within(candidate, run_dir.expanduser().resolve())


def _normalize_run_repo_path_token(token: str, cwd: Path, run_dir: Path) -> tuple[str, bool]:
    value = _strip_path_token(token)
    if not value or _is_url_like_token(value) or not _path_is_under_run_repos(value):
        return str(token), False
    try:
        resolved = _resolve_command_path_value(value, cwd, run_dir)
    except Exception:
        return str(token), False
    if not resolved.exists():
        return str(token), False
    token_text = str(token)
    quote = ""
    stripped = token_text.strip()
    if (stripped.startswith('"') and stripped.endswith('"')) or (stripped.startswith("'") and stripped.endswith("'")):
        quote = stripped[0]
    if "=" in stripped:
        key, _old_value = stripped.split("=", 1)
        if key.startswith("--") or key.isupper():
            replacement = f"{key}={resolved}"
            return (f"{quote}{replacement}{quote}" if quote else replacement), True
    for option in sorted(ATTACHED_SHORT_PATH_VALUE_OPTIONS, key=len, reverse=True):
        if stripped.startswith(option) and len(stripped) > len(option):
            replacement = f"{option}{resolved}"
            return (f"{quote}{replacement}{quote}" if quote else replacement), True
    return str(resolved), True


def normalize_run_repo_command_paths(command: list[str], cwd: Path, run_dir: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not command:
        return command, []
    updated = [str(token) for token in command]
    migrations: list[dict[str, str]] = []
    inner_command_index = _conda_run_inner_command_index(updated)
    skip_next_path_value = False
    for index, token in enumerate(updated):
        if index == 0:
            continue
        candidate_token = token
        path_value_expected = False
        command_head = _path_option_head_for_index(updated, index, inner_command_index)
        if skip_next_path_value:
            skip_next_path_value = False
            path_value_expected = True
        elif token in _path_value_options_for_head(command_head):
            if index + 1 < len(updated):
                skip_next_path_value = True
            continue
        else:
            assignment_value = _path_option_assignment_value(token, command_head)
            attached_value = _attached_short_path_option_value(token, command_head) if assignment_value is None else None
            if assignment_value is not None or attached_value is not None:
                path_value_expected = True
            elif not _path_is_under_run_repos(_strip_path_token(token)):
                continue
        if not path_value_expected and not _path_is_under_run_repos(_strip_path_token(candidate_token)):
            continue
        before = updated[index]
        after, changed = _normalize_run_repo_path_token(before, cwd, run_dir)
        if changed and after != before:
            updated[index] = after
            migrations.append({"migration": "normalized run repo path argument to absolute run-local path", "before": before, "after": after})
    return updated, migrations


def resolve_command_cwd(row: dict[str, Any], repo_path: Path, run_dir: Path) -> Path:
    raw = str(row.get("cwd") or "repo").strip()
    if raw in {"", "repo", "."}:
        return repo_path
    if raw == "run":
        return run_dir
    candidate = Path(raw)
    if not candidate.is_absolute():
        base = run_dir if _raw_path_starts_with_run_repos(raw) else repo_path
        candidate = base / candidate
    return ensure_within(candidate, run_dir)


URL_LIKE_PREFIXES = ("http://", "https://", "git+http://", "git+https://", "ssh://", "s3://", "gs://", "hf://")
PATH_VALUE_OPTIONS = {
    "--output", "--output-dir", "--output_path", "--output-path", "--out", "--out-dir", "--out_path", "--out-path",
    "--save", "--save-dir", "--save_path", "--save-path", "--model-dir", "--model_dir",
    "--data", "--data-dir", "--data_dir", "--data-root", "--data_root", "--dataset", "--dataset-dir", "--dataset_dir", "--dataset-root", "--dataset_root",
    "--cache", "--cache-dir", "--cache_dir", "--log-dir", "--log_dir", "--logs", "--work-dir", "--work_dir",
    "--checkpoint", "--checkpoint-dir", "--checkpoint_dir", "--ckpt", "--ckpt-dir", "--ckpt_dir", "--weights", "--pretrained", "--config",
    "-r", "--requirement", "--requirements", "-c", "--constraint", "--constraints", "-e", "--editable",
    "-f", "--find-links", "--index-url", "--extra-index-url", "--file", "--env-file",
}
ATTACHED_SHORT_PATH_VALUE_OPTIONS = {"-r", "-c", "-e", "-f"}
HEAD_PATH_VALUE_OPTIONS = {
    "curl": {"-o", "--output"},
    "wget": {"-O", "-P", "--output-document", "--directory-prefix"},
    "tar": {"-C", "--directory"},
    "bsdtar": {"-C", "--directory"},
    "gtar": {"-C", "--directory"},
    "unzip": {"-d"},
    "git": {"-C"},
    "make": {"-C"},
    "gmake": {"-C"},
    "ninja": {"-C"},
    "cmake": {"-S", "-B", "-H", "--install-prefix"},
    "pip": {"-t", "-b", "--target", "--src", "--build", "--root", "--prefix"},
    "pip3": {"-t", "-b", "--target", "--src", "--build", "--root", "--prefix"},
    "rsync": {"-T", "--temp-dir", "--backup-dir", "--partial-dir", "--compare-dest", "--copy-dest", "--link-dest", "--files-from", "--exclude-from", "--include-from"},
    "7z": {"-o", "-w"},
    "7za": {"-o", "-w"},
    "7zr": {"-o", "-w"},
    "conda": {"--cwd"},
    "mamba": {"--cwd"},
    "micromamba": {"--cwd"},
}
HEAD_ATTACHED_SHORT_PATH_VALUE_OPTIONS = {
    "curl": {"-o"},
    "wget": {"-O", "-P"},
    "tar": {"-C"},
    "bsdtar": {"-C"},
    "gtar": {"-C"},
    "unzip": {"-d"},
    "git": {"-C"},
    "make": {"-C"},
    "gmake": {"-C"},
    "ninja": {"-C"},
    "cmake": {"-S", "-B", "-H"},
    "pip": {"-t", "-b"},
    "pip3": {"-t", "-b"},
    "rsync": {"-T"},
    "7z": {"-o", "-w"},
    "7za": {"-o", "-w"},
    "7zr": {"-o", "-w"},
}
LOCAL_FILE_ARGUMENT_SUFFIXES = {
    ".py", ".sh", ".bash", ".zsh", ".fish", ".pl", ".r", ".jl", ".lua",
    ".yml", ".yaml", ".json", ".jsonl", ".toml", ".ini", ".cfg", ".conf", ".txt",
    ".csv", ".tsv", ".pkl", ".pickle", ".pt", ".pth", ".ckpt", ".onnx", ".npz", ".npy",
    ".whl", ".zip", ".tar", ".gz", ".tgz",
}
COMMAND_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROTECTED_COMMAND_ENV_KEYS = {
    "HOME", "PATH", "NVM_BIN", "NVM_DIR", "ENVIRONMENT_DEV_RUN_DIR",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "TMPDIR", "TEMP", "TMP",
    "PIP_CACHE_DIR", "HF_HOME", "HF_DATASETS_CACHE", "TRANSFORMERS_CACHE", "TORCH_HOME",
    "MPLCONFIGDIR", "WANDB_DIR", "WANDB_CACHE_DIR", "WANDB_CONFIG_DIR",
    "PYTHONPYCACHEPREFIX", "CONDA_PKGS_DIRS",
    "PYTHONHOME", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "VIRTUAL_ENV",
}
PATH_LIST_COMMAND_ENV_KEYS = {
    "PYTHONPATH", "LD_LIBRARY_PATH", "LD_PRELOAD", "LIBRARY_PATH", "CPATH", "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH", "PKG_CONFIG_PATH", "CMAKE_PREFIX_PATH", "CONDA_ENVS_PATH",
}
EXACT_PATH_COMMAND_ENV_KEYS = {
    "PIP_TARGET", "PIP_PREFIX", "PIP_SRC", "PIP_BUILD", "PIP_CONFIG_FILE", "PIP_FIND_LINKS",
    "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_CONSTRAINT", "PIP_REQUIREMENT",
    "PIP_CERT", "PIP_CLIENT_CERT", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_FIND_LINKS",
    "UV_CONSTRAINT", "UV_REQUIREMENT", "UV_OVERRIDE", "UV_PROJECT_ENVIRONMENT", "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR", "PYTHONUSERBASE", "PYTHONSTARTUP", "TORCH_EXTENSIONS_DIR",
    "TRITON_CACHE_DIR", "NUMBA_CACHE_DIR", "JUPYTER_CONFIG_DIR", "IPYTHONDIR", "KERAS_HOME",
    "NLTK_DATA", "SPACY_DATA", "MAMBA_ROOT_PREFIX",
}
MULTI_VALUE_COMMAND_ENV_KEYS = {
    "PIP_FIND_LINKS", "PIP_EXTRA_INDEX_URL", "PIP_CONSTRAINT", "PIP_REQUIREMENT",
    "UV_FIND_LINKS", "UV_EXTRA_INDEX_URL", "UV_CONSTRAINT", "UV_REQUIREMENT", "UV_OVERRIDE",
}
RUN_SCOPED_ENV_KEY_MARKERS = (
    "OUTPUT", "OUT_DIR", "SAVE", "LOG", "CACHE", "DATA", "DATASET", "CHECKPOINT",
    "CKPT", "TMP", "TEMP", "WANDB", "HF_", "TORCH", "MLFLOW", "RESULT", "ARTIFACT",
)
INLINE_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
PYTHON_ENV_ENTRYPOINTS = {"python", "python3", "pip", "pip3", "pytest", "torchrun", "accelerate", "deepspeed"}
RUN_ENV_ENTRYPOINTS = {*PYTHON_ENV_ENTRYPOINTS, "hf"}
CONDA_PREFIX_OPTIONS = {"-p", "--prefix"}
SYSTEM_EXECUTABLE_PATH_HEADS = {
    "bash", "sh", "zsh", "git", "curl", "wget", "tar", "unzip", "rsync",
    "chmod", "mkdir", "cp", "mv", "ln", "sed", "awk", "grep", "find", "make",
}


TERMINAL_REJECT_CATEGORIES = {
    "repository_unreliable",
    "data_unreliable",
    "paper_unreliable",
    "machine_compute_unavailable",
}
REJECT_AUDITABLE_ROW_KEYS = {
    "source", "sources", "evidence_source", "url", "urls", "log_path", "log_paths",
    "command", "commands", "phase", "phases", "return_code", "return_codes", "status_code",
    "status_codes", "path", "paths", "paper_url", "repo_url", "dataset_url",
}
REJECT_AUDITABLE_CODE_KEYS = {"return_code", "return_codes", "status_code", "status_codes"}
REJECT_AUDITABLE_TEXT_RE = re.compile(
    r"(https?://|git@github\.com:|github\.com/|arxiv|doi\b|file://|/|"
    r"\.(?:log|json|txt|md|csv|tsv|pdf|yaml|yml)\b|"
    r"return[_ -]?code|exit[_ -]?code|status[_ -]?code|404|403|401|410|timeout|timed out|"
    r"connection refused|not found|does not exist|unavailable|permission denied|checksum|sha256|"
    r"cuda out of memory|out of memory|oom|vram|显存|cuda|gpu|4090|5090|a100|h100|"
    r"requires?.*\d|requirement.*\d|需要.*\d|至少.*\d|>=|<=|>|<)",
    re.IGNORECASE,
)
APPROVAL_METRIC_PHASES = {"reproduce_full", "eval", "evaluate", "evaluation", "test", "benchmark"}


def _evidence_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _value_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if str(value or "").strip():
        return [str(value).strip()]
    return []


def _reject_row_has_auditable_field(row: dict[str, Any]) -> bool:
    for key in REJECT_AUDITABLE_ROW_KEYS:
        values = _value_list(row.get(key))
        if not values:
            continue
        if key in REJECT_AUDITABLE_CODE_KEYS:
            return True
        if any(REJECT_AUDITABLE_TEXT_RE.search(value) for value in values):
            return True
    return False


def _reject_evidence_is_auditable(row: dict[str, Any], evidence: list[str]) -> bool:
    if any(REJECT_AUDITABLE_TEXT_RE.search(item) for item in evidence):
        return True
    return _reject_row_has_auditable_field(row)


def _repairable_flag_is_true(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized in {"true", "yes", "y", "1", "on", "repairable", "可修复", "可修", "是"}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return False


def reject_evidence_issues(verdict: dict[str, Any]) -> list[str]:
    taxonomy = verdict.get("failure_taxonomy") if isinstance(verdict.get("failure_taxonomy"), list) else []
    if not taxonomy:
        return ["reject 缺少 failure_taxonomy；不能证明仓库/数据/论文不可靠或算力不可满足"]
    supported = False
    repairable_terminal_rows: list[str] = []
    weak_terminal_rows: list[str] = []
    seen_categories: list[str] = []
    for index, row in enumerate(taxonomy):
        if not isinstance(row, dict):
            continue
        category = str(row.get("category") or "").strip()
        if category:
            seen_categories.append(category)
        evidence = _evidence_items(row.get("evidence"))
        if category in TERMINAL_REJECT_CATEGORIES and evidence:
            if _repairable_flag_is_true(row.get("repairable")):
                repairable_terminal_rows.append(f"failure_taxonomy[{index}] {category} 标记为 repairable=true")
                continue
            if not _reject_evidence_is_auditable(row, evidence):
                weak_terminal_rows.append(f"failure_taxonomy[{index}] {category} 的 evidence 缺少 URL/日志路径/命令/状态码/错误码/路径/硬件需求对比等可审计信号")
                continue
            supported = True
    if supported:
        return []
    issues = [
        "reject 没有不可修终态证据；只有 repository_unreliable/data_unreliable/paper_unreliable/machine_compute_unavailable 且带可审计 evidence、非 repairable=true 时才允许拒绝；普通 machine_compute 必须继续修复"
    ]
    if seen_categories:
        issues.append("当前类别：" + ", ".join(seen_categories[:12]))
    issues.extend(repairable_terminal_rows[:5])
    issues.extend(weak_terminal_rows[:5])
    return issues


def enforce_reject_evidence(verdict: dict[str, Any]) -> dict[str, Any]:
    if str(verdict.get("decision") or "").strip().lower() != "reject":
        return verdict
    issues = reject_evidence_issues(verdict)
    if not issues:
        return verdict
    fixed = dict(verdict)
    fixed["decision"] = "continue_repair"
    fixed["allow_next_module"] = False
    fixed["reject_downgraded_by_backend"] = True
    fixed["reject_evidence_issues"] = issues
    repair_plan = fixed.get("repair_plan")
    if not isinstance(repair_plan, list):
        repair_plan = _evidence_items(repair_plan)
    repair_plan.append("后端降级：拒绝缺少仓库/数据/论文不可靠或算力不可满足的不可修证据，应继续修复并补充证据。")
    fixed["repair_plan"] = repair_plan
    taxonomy = fixed.get("failure_taxonomy")
    taxonomy = list(taxonomy) if isinstance(taxonomy, list) else []
    taxonomy.append({"category": "unknown", "evidence": issues, "repairable": True})
    fixed["failure_taxonomy"] = taxonomy
    return fixed


def _is_url_like_token(token: str) -> bool:
    lowered = token.lower()
    return lowered.startswith(URL_LIKE_PREFIXES) or lowered.startswith("git@github.com:") or "://" in lowered


def _strip_path_token(token: str) -> str:
    value = str(token or "").strip().strip('"').strip("'")
    if "=" in value:
        key, maybe_path = value.split("=", 1)
        if key.startswith("--") or key.isupper():
            value = maybe_path
    for option in ATTACHED_SHORT_PATH_VALUE_OPTIONS:
        if value.startswith(option) and len(value) > len(option):
            attached = value[len(option):]
            if attached.startswith(("file://", "/", "~/", "./", "../")):
                value = attached
                break
    if value.startswith("file://"):
        value = value[len("file://"):]
    return value


def _path_value_options_for_head(head: str) -> set[str]:
    return set(PATH_VALUE_OPTIONS) | set(HEAD_PATH_VALUE_OPTIONS.get(str(head or ""), set()))


def _attached_short_path_value_options_for_head(head: str) -> set[str]:
    return set(ATTACHED_SHORT_PATH_VALUE_OPTIONS) | set(HEAD_ATTACHED_SHORT_PATH_VALUE_OPTIONS.get(str(head or ""), set()))


def _path_option_assignment_value(token: str, head: str = "") -> str | None:
    value = str(token or "").strip().strip('"').strip("'")
    if "=" not in value:
        return None
    key, maybe_path = value.split("=", 1)
    return maybe_path if key in _path_value_options_for_head(head) else None


def _attached_short_path_option_value(token: str, head: str = "") -> str | None:
    value = str(token or "").strip().strip('"').strip("'")
    if value.startswith("--"):
        return None
    for option in sorted(_attached_short_path_value_options_for_head(head), key=len, reverse=True):
        if value.startswith(option) and len(value) > len(option):
            attached = value[len(option):]
            if attached.startswith("="):
                attached = attached[1:]
            return attached or None
    return None


CONDA_RUN_OPTIONS_WITH_VALUE = {"-n", "--name", "-p", "--prefix", "--cwd"}
CONDA_RUN_ASSIGNMENT_OPTIONS_WITH_VALUE = ("--name=", "--prefix=", "--cwd=")
PYTHON_MODULE_PATH_OPTION_HEADS = {"pip", "pip3"}


def _conda_run_inner_command_index(tokens: list[str]) -> int | None:
    if len(tokens) < 3 or Path(str(tokens[0])).name not in {"conda", "mamba", "micromamba"}:
        return None
    if str(tokens[1]) != "run":
        return None
    index = 2
    while index < len(tokens):
        token = str(tokens[index] or "")
        if token == "--":
            return index + 1 if index + 1 < len(tokens) else None
        if token in CONDA_RUN_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(token.startswith(prefix) for prefix in CONDA_RUN_ASSIGNMENT_OPTIONS_WITH_VALUE):
            index += 1
            continue
        if token.startswith("-") and token != "-":
            index += 1
            continue
        return index
    return None


def _python_module_inner_command_index(tokens: list[str], python_index: int) -> int | None:
    if python_index >= len(tokens) or not Path(str(tokens[python_index] or "")).name.startswith("python"):
        return None
    index = python_index + 1
    while index < len(tokens):
        current = str(tokens[index] or "")
        if current == "--":
            return None
        if current in {"-m", "--module"}:
            if index + 1 >= len(tokens):
                return None
            module_name = Path(str(tokens[index + 1] or "")).name
            return index + 1 if module_name in PYTHON_MODULE_PATH_OPTION_HEADS else None
        if current in PYTHON_INLINE_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if current.startswith("-X") and len(current) > 2:
            index += 1
            continue
        if current.startswith("-W") and len(current) > 2:
            index += 1
            continue
        if current.startswith("-") and current != "-":
            index += 1
            continue
        return None
    return None


def _path_option_head_for_index(tokens: list[str], index: int, inner_command_index: int | None = None) -> str:
    command_start = 0
    if inner_command_index is not None and inner_command_index < len(tokens) and index >= inner_command_index:
        command_start = inner_command_index
    module_index = _python_module_inner_command_index(tokens, command_start)
    if module_index is not None and index > module_index:
        return Path(str(tokens[module_index])).name
    if inner_command_index is not None and index >= inner_command_index and inner_command_index < len(tokens):
        return Path(str(tokens[inner_command_index])).name
    return Path(str(tokens[0])).name if tokens else ""


def _looks_like_explicit_path(token: str) -> bool:
    value = _strip_path_token(token)
    if not value or _is_url_like_token(value):
        return False
    normalized = value.replace("\\", "/")
    return value.startswith(("/", "~/", "./", "../")) or normalized.startswith("../") or "/../" in normalized or normalized.endswith("/..")


def _looks_like_local_file_argument(token: str) -> bool:
    raw = str(token or "").strip().strip('"').strip("'")
    if not raw or raw in {"-", "--"} or raw.startswith("-") or any(char.isspace() for char in raw):
        return False
    value = _strip_path_token(raw)
    if not value or _is_url_like_token(value):
        return False
    normalized = value.replace("\\", "/")
    return _looks_like_explicit_path(value) or "/" in normalized or Path(value).suffix.lower() in LOCAL_FILE_ARGUMENT_SUFFIXES


def _looks_like_inline_code_path_literal(value: str) -> bool:
    text = _strip_path_token(str(value or ""))
    if not text or _is_url_like_token(text):
        return False
    normalized = text.replace("\\", "/")
    return _looks_like_explicit_path(text) or normalized.startswith("../") or "/../" in normalized or normalized.endswith("/..")


def _joined_string_literal_prefix(node: ast.JoinedStr) -> str:
    parts: list[str] = []
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            parts.append(part.value)
            continue
        break
    return "".join(parts)


def _python_inline_string_literals(code: str) -> list[str]:
    try:
        tree = ast.parse(str(code or ""))
    except SyntaxError:
        return []
    parent_by_id = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if isinstance(parent_by_id.get(id(node)), ast.JoinedStr):
                continue
            literals.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            prefix = _joined_string_literal_prefix(node)
            if prefix:
                literals.append(prefix)
    return literals


def _path_is_under_run_repos(path_value: str) -> bool:
    normalized = str(path_value or "").strip().strip('"').strip("'").replace("\\", "/")
    return normalized == "repos" or normalized.startswith("repos/") or "/repos/" in normalized


def _missing_run_repo_path_issue(path_value: str, cwd: Path, run_dir: Path, *, allow_create: bool = False) -> str | None:
    value = _strip_path_token(path_value)
    if allow_create or not value or _is_url_like_token(value) or not _path_is_under_run_repos(value):
        return None
    try:
        candidate = _resolve_command_path_value(value, cwd, run_dir)
    except ValueError:
        return None
    try:
        exists = candidate.exists()
    except Exception:
        exists = False
    if exists:
        return None
    try:
        display = candidate.relative_to(run_dir.expanduser().resolve()).as_posix()
    except Exception:
        display = value
    return f"引用了不存在的本次 run 仓库路径：{display}；环境计划只能使用已克隆仓库目录，不得编造 repos/<name>"


def _python_inline_code_repo_path_issues(code: str, cwd: Path, run_dir: Path) -> list[str]:
    issues: list[str] = []
    seen: set[str] = set()
    for literal in _python_inline_string_literals(str(code or "")):
        value = _strip_path_token(literal)
        if value in seen:
            continue
        seen.add(value)
        issue = _missing_run_repo_path_issue(value, cwd, run_dir)
        if issue:
            issues.append(f"Python 内联代码{issue}")
    return issues


def _python_inline_code_boundary_issues(code: str, cwd: Path, run_dir: Path) -> list[str]:
    issues: list[str] = []
    seen: set[str] = set()
    resolved_cwd = cwd.expanduser().resolve()
    resolved_run = run_dir.expanduser().resolve()
    for literal in _python_inline_string_literals(str(code or "")):
        if not _looks_like_inline_code_path_literal(literal):
            continue
        value = _strip_path_token(literal)
        if value in seen:
            continue
        seen.add(value)
        try:
            candidate = _resolve_command_path_value(value, resolved_cwd, resolved_run)
        except ValueError:
            issues.append(f"Python 内联代码路径越界：{value} 不在本次 run 目录内；请把辅助脚本、数据、缓存和输出写到 run/repo 内")
    return issues


PYTHON_INLINE_OPTIONS_WITH_VALUE = {"-X", "-W", "--check-hash-based-pycs"}


def _python_inline_code_by_option_index(tokens: list[str]) -> dict[int, str | None]:
    inline: dict[int, str | None] = {}
    for start, token in enumerate(tokens):
        if not Path(str(token or "")).name.startswith("python"):
            continue
        index = start + 1
        while index < len(tokens):
            current = str(tokens[index] or "")
            if current == "--":
                break
            if current == "-c":
                inline[index] = str(tokens[index + 1]) if index + 1 < len(tokens) else None
                break
            if current.startswith("-c") and len(current) > 2:
                inline[index] = current[2:]
                break
            if current in {"-m", "--module"}:
                break
            if current in PYTHON_INLINE_OPTIONS_WITH_VALUE:
                index += 2
                continue
            if current.startswith("-X") and len(current) > 2:
                index += 1
                continue
            if current.startswith("-W") and len(current) > 2:
                index += 1
                continue
            if current.startswith("-") and current != "-":
                index += 1
                continue
            break
    return inline


def _is_backend_conda_executable(candidate: Path) -> bool:
    try:
        found = find_conda_executable()
        if not found:
            return False
        return candidate.expanduser().resolve() == Path(found).expanduser().resolve()
    except Exception:
        return False


def _command_executable_boundary_issues(tokens: list[str], cwd: Path, run_dir: Path) -> list[str]:
    if not tokens:
        return []
    value = _strip_path_token(str(tokens[0]))
    if not _looks_like_explicit_path(value):
        return []
    head = Path(value).name
    candidate = Path(value).expanduser()
    if candidate.is_absolute() and (head in SYSTEM_EXECUTABLE_PATH_HEADS or (head in {"conda", "mamba", "micromamba"} and _is_backend_conda_executable(candidate))):
        return []
    if not candidate.is_absolute():
        candidate = cwd.expanduser().resolve() / candidate
    try:
        ensure_within(candidate, run_dir.expanduser().resolve())
    except ValueError:
        return [f"命令可执行文件路径越界：{value} 不在本次 run 目录内；外部工具请用命令名，脚本必须放在 run/repo 内"]
    return []


def command_boundary_issues(tokens: list[str], cwd: Path, run_dir: Path) -> list[str]:
    issues: list[str] = []
    resolved_run = run_dir.expanduser().resolve()
    resolved_cwd = cwd.expanduser().resolve()
    try:
        ensure_within(resolved_cwd, resolved_run)
    except ValueError as exc:
        issues.append(str(exc))
    issues.extend(_command_executable_boundary_issues(tokens, resolved_cwd, resolved_run))
    inner_command_index = _conda_run_inner_command_index(tokens)
    python_inline_by_index = _python_inline_code_by_option_index(tokens)
    skip_next_path_value = False
    skip_python_code = False
    for index, token in enumerate(tokens):
        if index == 0:
            continue
        if skip_python_code:
            skip_python_code = False
            continue
        if index in python_inline_by_index:
            code = python_inline_by_index[index]
            if code is None:
                issues.append("Python 内联 -c 缺少代码片段")
            else:
                issues.extend(_python_inline_code_boundary_issues(code, resolved_cwd, resolved_run))
                issues.extend(_python_inline_code_repo_path_issues(code, resolved_cwd, resolved_run))
            skip_python_code = token == "-c"
            continue
        path_value_expected = False
        if skip_next_path_value:
            skip_next_path_value = False
            candidate_token = token
            path_value_expected = True
        else:
            command_head = _path_option_head_for_index(tokens, index, inner_command_index)
            if token in _path_value_options_for_head(command_head):
                if index + 1 >= len(tokens):
                    issues.append(f"路径参数 {token} 缺少取值")
                else:
                    skip_next_path_value = True
                continue
            assignment_value = _path_option_assignment_value(token, command_head)
            attached_value = _attached_short_path_option_value(token, command_head) if assignment_value is None else None
            if assignment_value is not None:
                candidate_token = assignment_value
                path_value_expected = True
            elif attached_value is not None:
                candidate_token = attached_value
                path_value_expected = True
            else:
                candidate_token = token
        value = _strip_path_token(candidate_token)
        local_file_argument = (not path_value_expected) and _looks_like_local_file_argument(candidate_token)
        if path_value_expected:
            if not value or _is_url_like_token(value):
                continue
        elif not (_looks_like_explicit_path(value) or local_file_argument):
            issue = _missing_run_repo_path_issue(value, resolved_cwd, resolved_run)
            if issue:
                issues.append(issue)
            continue
        command_head_for_value = _path_option_head_for_index(tokens, index, inner_command_index)
        allow_missing_repo_create = command_head_for_value == "git" and len(tokens) >= 2 and str(tokens[1]) == "clone" and index == len(tokens) - 1
        repo_issue = _missing_run_repo_path_issue(value, resolved_cwd, resolved_run, allow_create=allow_missing_repo_create)
        if repo_issue:
            issues.append(repo_issue)
        try:
            _resolve_command_path_value(value, resolved_cwd, resolved_run)
        except ValueError:
            label = "命令文件/脚本参数路径越界" if local_file_argument else "命令参数路径越界"
            issues.append(f"{label}：{value} 不在本次 run 目录内")
    return issues


def _is_inline_env_assignment(token: str) -> bool:
    return bool(INLINE_ENV_ASSIGNMENT_RE.match(str(token or "")))


def split_inline_env_tokens(tokens: list[str]) -> tuple[dict[str, str], list[str], list[str]]:
    if not tokens:
        return {}, [], []
    env_values: dict[str, str] = {}
    issues: list[str] = []
    index = 0
    uses_env_wrapper = Path(tokens[0]).name == "env"
    if uses_env_wrapper:
        index = 1
        if index >= len(tokens):
            return {}, tokens, ["env 包装器后缺少实际命令"]
        if str(tokens[index]).startswith("-"):
            return {}, tokens, ["暂不支持 env 命令选项；请把环境变量写入 commands[].env 或使用 env KEY=VALUE command 形式"]
    while index < len(tokens) and _is_inline_env_assignment(tokens[index]):
        key, value = str(tokens[index]).split("=", 1)
        env_values[key] = value
        index += 1
    if index == 0:
        return {}, tokens, []
    if uses_env_wrapper and not env_values:
        return {}, tokens, ["env 包装器必须使用 env KEY=VALUE command 形式；否则会绕过 run 内 Conda 环境重写"]
    if index >= len(tokens):
        return env_values, [], ["命令只包含环境变量赋值，缺少实际可执行命令"]
    return env_values, tokens[index:], issues


def command_env_overrides(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key).strip(): str(item) for key, item in value.items() if str(key).strip() and item is not None}


def normalized_command_and_env(row: dict[str, Any]) -> tuple[list[str], dict[str, str], list[str], dict[str, str]]:
    tokens = command_tokens(row.get("command"))
    inline_env, normalized_tokens, inline_issues = split_inline_env_tokens(tokens)
    explicit_env = command_env_overrides(row.get("env"))
    merged_env = {**inline_env, **explicit_env}
    return normalized_tokens, merged_env, inline_issues, inline_env


def _env_key_is_run_scoped_path(key: str) -> bool:
    upper = str(key or "").upper()
    return any(marker in upper for marker in RUN_SCOPED_ENV_KEY_MARKERS)


def _split_env_multi_path_value(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = re.split(r"\s+", text)
    return [part for part in parts if str(part).strip()]


def _env_path_values(key: str, value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    upper = str(key or "").upper()
    if upper in PATH_LIST_COMMAND_ENV_KEYS:
        parts = text.split(os.pathsep)
    elif upper in MULTI_VALUE_COMMAND_ENV_KEYS:
        parts = _split_env_multi_path_value(text)
    else:
        parts = [text]
    values: list[str] = []
    for part in parts:
        stripped = _strip_path_token(part)
        if not stripped or _is_url_like_token(stripped):
            continue
        values.append(stripped)
    return values


def _env_path_candidate(path_value: str, base_dir: Path) -> Path:
    candidate = Path(_strip_path_token(path_value)).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate


def command_env_boundary_issues(value: Any, run_dir: Path, cwd: Path | None = None) -> list[str]:
    if value is None or value == "":
        return []
    if not isinstance(value, dict):
        return ["env 必须是 object"]
    issues: list[str] = []
    resolved_run = run_dir.expanduser().resolve()
    resolved_cwd = (cwd or run_dir).expanduser().resolve()
    try:
        ensure_within(resolved_cwd, resolved_run)
    except ValueError as exc:
        issues.append(f"env cwd 越界：{exc}")
        resolved_cwd = resolved_run
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip()
        if not COMMAND_ENV_KEY_PATTERN.match(key):
            issues.append(f"env 变量名非法：{key or 'missing'}")
            continue
        if key in PROTECTED_COMMAND_ENV_KEYS:
            issues.append(f"env 禁止覆盖隔离关键变量：{key}")
            continue
        if raw_value is None:
            continue
        upper_key = key.upper()
        if upper_key not in PATH_LIST_COMMAND_ENV_KEYS and upper_key not in EXACT_PATH_COMMAND_ENV_KEYS and not _env_key_is_run_scoped_path(key):
            continue
        for path_value in _env_path_values(key, str(raw_value)):
            candidate = _env_path_candidate(path_value, resolved_cwd)
            try:
                ensure_within(candidate, resolved_run)
            except ValueError:
                issues.append(f"env 路径变量 {key}={path_value} 越界；相对路径按命令 cwd 解析，代码、库、数据、缓存、输出类路径必须在本次 run 目录内")
    return issues


def _metric_names_from_criteria(criteria: list[Any]) -> set[str]:
    names: set[str] = set()
    for item in criteria:
        if not isinstance(item, dict):
            continue
        for key in ["name", "metric"]:
            value = str(item.get(key) or "").strip().lower()
            if value:
                names.add(value)
    return names


def _metric_criteria_by_name(criteria: list[Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in criteria:
        if not isinstance(item, dict):
            continue
        for key in ["name", "metric"]:
            value = str(item.get(key) or "").strip().lower()
            if value and value not in out:
                out[value] = item
    return out


def _criterion_target_value(item: dict[str, Any]) -> tuple[bool, Any, str]:
    for key in ["value", "target", "paper_value"]:
        if key in item:
            return True, item.get(key), key
    return False, None, ""


def _first_present_metric_value(item: dict[str, Any], keys: list[str]) -> tuple[bool, Any, str]:
    for key in keys:
        if key not in item:
            continue
        value = item.get(key)
        if value is not None and value != "":
            return True, value, key
    return False, None, ""


def _receipt_output_text(receipt: dict[str, Any]) -> str:
    return "\n".join(str(receipt.get(key) or "") for key in ["stdout_head", "stdout_tail", "stderr_tail"])


def _receipt_binding_context(receipts: list[dict[str, Any]]) -> tuple[str, set[str]]:
    log_parts: list[str] = []
    refs: set[str] = set()
    for receipt in receipts:
        output_text = _receipt_output_text(receipt).strip()
        if output_text:
            log_parts.append(output_text)
        for key in ["command", "phase", "status", "log_path"]:
            value = str(receipt.get(key) or "").strip()
            if value:
                refs.add(value.lower())
                if key == "log_path":
                    refs.add(Path(value).name.lower())
    return "\n".join(log_parts).lower(), refs


def _receipt_allows_metric_evidence(receipt: dict[str, Any]) -> bool:
    phase = str(receipt.get("phase") or "").strip().lower()
    return bool(_receipt_succeeded(receipt) and receipt.get("required") is not False and phase in APPROVAL_METRIC_PHASES)


def _metric_binding_receipts(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in receipts if _receipt_allows_metric_evidence(row)]


def _strings_from_value(value: Any) -> list[str]:
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_strings_from_value(item))
        return out
    if isinstance(value, dict):
        return [json.dumps(value, ensure_ascii=False, sort_keys=True)]
    if value not in {None, ""}:
        return [str(value).strip()]
    return []


def _metric_evidence_strings(item: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ["evidence", "log_excerpt", "source", "receipt", "log_path", "metric", "criterion"]:
        out.extend(_strings_from_value(item.get(key)))
    return [value for value in out if value]


def _metric_log_excerpt_strings(item: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ["evidence", "log_excerpt", "receipt"]:
        out.extend(_strings_from_value(item.get(key)))
    return [value for value in out if value]


def _observed_tokens(item: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for key in ["observed", "value", "result", "actual"]:
        value = item.get(key)
        if value in {None, ""}:
            continue
        text = str(value).strip().lower()
        tokens.add(text)
        try:
            number_text = text[:-1].strip() if text.endswith("%") else text
            number = float(number_text)
            candidates = {number, number * 100 if abs(number) <= 1 else number / 100}
            for candidate in candidates:
                tokens.add(f"{candidate:g}")
                tokens.add(f"{candidate:.3f}".rstrip("0").rstrip("."))
                tokens.add(f"{candidate:.4f}".rstrip("0").rstrip("."))
        except Exception:
            pass
    return {token for token in tokens if token}


def _excerpt_mentions_metric_result(excerpt: str, metric_name: str, observed_tokens: set[str]) -> bool:
    lowered = str(excerpt or "").lower()
    mentions_metric = bool(metric_name and metric_name in lowered)
    mentions_observed = any(token and token in lowered for token in observed_tokens)
    if not observed_tokens:
        return mentions_metric
    return mentions_observed and (not metric_name or mentions_metric)


def verdict_metric_evidence_supports_claims(verdict: dict[str, Any], receipts: list[dict[str, Any]], criteria: list[Any]) -> tuple[bool, list[str]]:
    evidence = verdict.get("metric_evidence")
    if not isinstance(evidence, list):
        return False, ["Claude 裁决缺少 metric_evidence 数组"]
    if not receipts:
        return False, ["缺少命令回执，无法把 Claude metric_evidence 绑定到执行日志"]
    metric_receipts = _metric_binding_receipts(receipts)
    if not metric_receipts:
        allowed = ", ".join(sorted(APPROVAL_METRIC_PHASES))
        return False, [f"缺少成功且必需的批准指标阶段回执，Claude metric_evidence 只能绑定到这些阶段：{allowed}"]
    metric_names = _metric_names_from_criteria(criteria)
    criteria_by_name = _metric_criteria_by_name(criteria)
    receipt_log_text, receipt_refs = _receipt_binding_context(metric_receipts)
    issues: list[str] = []
    covered_metrics: set[str] = set()
    valid_metric_count = 0
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            issues.append(f"metric_evidence[{index}] 不是 object")
            continue
        if item.get("passed") is not True:
            issues.append(f"metric_evidence[{index}] passed 不是 true")
            continue
        metric_name = str(item.get("metric") or item.get("name") or "").strip().lower()
        if metric_names and metric_name and metric_name not in metric_names:
            issues.append(f"metric_evidence[{index}] metric={metric_name} 不在 success_criteria 中")
            continue
        if metric_names and not metric_name:
            issues.append(f"metric_evidence[{index}] 缺少 metric/name，无法对应 success_criteria")
            continue
        observed_found, observed_value, observed_key = _first_present_metric_value(item, ["observed", "value", "result", "actual"])
        criterion = criteria_by_name.get(metric_name, {}) if metric_name else {}
        target_found, target_value, target_key = _criterion_target_value(criterion) if criterion else (False, None, "")
        if not target_found:
            target_found, target_value, target_key = _first_present_metric_value(item, ["target", "paper_value", "expected", "criterion"])
        if not (observed_found and target_found):
            issues.append(f"metric_evidence[{index}] 缺少 observed/value 和 target/paper_value")
            continue
        operator = str((criterion or {}).get("operator") or (criterion or {}).get("op") or item.get("operator") or item.get("op") or ">=").strip()
        compare_passed, compare_evidence = compare_metric_values(observed_value, target_value, operator)
        if not compare_passed:
            issues.append(
                f"metric_evidence[{index}] 后端比较未通过：metric={metric_name or 'missing'} "
                f"observed({observed_key})={observed_value} {operator} target({target_key})={target_value} 不成立或不可解析；"
                f"compare={compare_evidence}"
            )
            continue
        observed_tokens = _observed_tokens({"observed": observed_value})
        excerpt_strings = _metric_log_excerpt_strings(item)
        has_log_excerpt_binding = False
        for value in excerpt_strings:
            lowered = value.lower()
            if len(lowered) < 4:
                continue
            if lowered in receipt_log_text and _excerpt_mentions_metric_result(lowered, metric_name, observed_tokens):
                has_log_excerpt_binding = True
                break
        if not has_log_excerpt_binding:
            issues.append(f"metric_evidence[{index}] 缺少可在成功批准指标阶段 stdout/stderr 中核验的指标日志摘录；摘录必须包含指标名和 observed 值，真实 log_path/phase/command 只能作为来源，不能单独支撑批准")
            continue
        reference_strings = _metric_evidence_strings(item)
        has_reference_binding = has_log_excerpt_binding
        for value in reference_strings:
            lowered = value.lower()
            if lowered in receipt_refs:
                has_reference_binding = True
                break
            if any(ref and ref in lowered for ref in receipt_refs if len(ref) >= 8):
                has_reference_binding = True
                break
        if not has_reference_binding:
            issues.append(f"metric_evidence[{index}] 没有可绑定到本轮 command_receipts/log_path/phase/command 的来源引用")
            continue
        valid_metric_count += 1
        if metric_name:
            covered_metrics.add(metric_name)
        elif not metric_names:
            return True, []
    if metric_names:
        missing = sorted(metric_names - covered_metrics)
        if missing:
            issues.append(f"metric_evidence 未覆盖全部 success_criteria 指标；缺少={missing}，已覆盖={sorted(covered_metrics)}")
            return False, issues
        return True, []
    if valid_metric_count:
        return True, []
    return False, issues or ["没有任何 metric_evidence 通过执行日志绑定校验"]


def _criterion_is_environment_gate(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    scope = str(row.get("approval_scope") or "").strip().lower()
    if scope == "environment_gate":
        return True
    return row.get("paper_metric") is False


DATASET_PHASE_EXACT = {"dataset", "data", "download", "prepare_data", "preprocess"}
DATASET_PHASE_PHRASES = {
    "download_data", "data_download", "download_dataset", "dataset_download",
    "prepare_data", "data_prepare", "prepare_dataset", "dataset_prepare",
    "preprocess_data", "data_preprocess", "preprocess_dataset", "dataset_preprocess",
    "fetch_data", "data_fetch", "fetch_dataset", "dataset_fetch",
    "get_data", "data_get", "get_dataset", "dataset_get",
    "build_data", "data_build", "build_dataset", "dataset_build",
    "create_data", "data_create", "create_dataset", "dataset_create",
    "convert_data", "data_convert", "convert_dataset", "dataset_convert",
    "extract_data", "data_extract", "extract_dataset", "dataset_extract",
    "unpack_data", "data_unpack", "unpack_dataset", "dataset_unpack",
}
DATASET_ACTION_TOKENS = {"download", "prepare", "preprocess", "fetch", "get", "build", "create", "convert", "extract", "unpack", "下载", "准备", "预处理", "获取", "构建", "创建", "转换", "提取", "解压"}
DATASET_OBJECT_TOKENS = {"data", "dataset", "datasets", "数据", "数据集"}


def _compact_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": receipt.get("phase"),
        "status": receipt.get("status"),
        "return_code": receipt.get("return_code"),
        "required": receipt.get("required"),
        "command": receipt.get("command"),
        "log_path": receipt.get("log_path"),
        "stdout_char_count": receipt.get("stdout_char_count"),
        "stdout_truncated": receipt.get("stdout_truncated"),
    }


def _approval_check(name: str, passed: bool, reason: str, evidence: Any = None) -> dict[str, Any]:
    row = {"name": name, "passed": bool(passed), "reason": reason}
    if evidence is not None and evidence != "" and evidence != []:
        row["evidence"] = evidence
    return row


def _audit_checks_by_name(verdict: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = verdict.get("audit_checks") if isinstance(verdict.get("audit_checks"), list) else []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if name:
            out[name] = row
    return out


def _audit_check(verdict: dict[str, Any], name: str, missing_reason: str) -> dict[str, Any]:
    checks = _audit_checks_by_name(verdict)
    row = checks.get(name)
    if isinstance(row, dict):
        evidence = row.get("evidence") if "evidence" in row else row
        reason = str(row.get("reason") or ("Claude audit passed" if row.get("passed") is True else missing_reason))
        return _approval_check(name, row.get("passed") is True, reason, evidence)
    return _approval_check(name, False, missing_reason, {"source": "claude_audit_judgement.audit_checks", "missing": name})


def _receipt_succeeded(receipt: dict[str, Any]) -> bool:
    if not isinstance(receipt, dict) or "return_code" not in receipt:
        return False
    try:
        return int(receipt.get("return_code")) == 0
    except Exception:
        return False


def _required_successful_receipts(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in receipts if _receipt_succeeded(row) and row.get("required") is not False]


def _optional_successful_receipts(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in receipts if _receipt_succeeded(row) and row.get("required") is False]


def _requested_commit_satisfied(repo_info: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    requested_commit = str(repo_info.get("requested_commit") or "").strip().lower()
    head_commit = str(repo_info.get("head_commit") or "").strip().lower()
    checkout_receipt = repo_info.get("checkout_receipt") if isinstance(repo_info.get("checkout_receipt"), dict) else {}
    checkout_return_code = checkout_receipt.get("return_code")
    if not requested_commit:
        return True, {
            "requested_commit": "",
            "checkout_required": False,
            "checkout_return_code": checkout_return_code,
            "head_matches_requested_commit": True,
        }
    checkout_ok = _receipt_succeeded(checkout_receipt)
    head_matches = bool(head_commit and head_commit.startswith(requested_commit))
    return bool(checkout_ok and head_matches), {
        "requested_commit": requested_commit,
        "checkout_required": True,
        "checkout_return_code": checkout_return_code,
        "head_matches_requested_commit": head_matches,
    }


def _repo_source_gate(repo_info: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    repo_url = str(repo_info.get("repo_url") or "").strip()
    exists = repo_info.get("exists") is True
    head_commit = str(repo_info.get("head_commit") or "").strip()
    clone_receipt = repo_info.get("clone_receipt") if isinstance(repo_info.get("clone_receipt"), dict) else {}
    clone_ok = _receipt_succeeded(clone_receipt)
    github_ok = is_github_repo_url(repo_url)
    commit_ok, commit_evidence = _requested_commit_satisfied(repo_info)
    return bool(github_ok and exists and clone_ok and head_commit and commit_ok), {
        "repo_url": repo_url,
        "is_github": github_ok,
        "exists": exists,
        "clone_return_code": clone_receipt.get("return_code"),
        "clone_succeeded": clone_ok,
        "head_commit": head_commit,
        "requested_branch_or_tag": repo_info.get("requested_branch_or_tag", ""),
        **commit_evidence,
    }


CONDA_SETUP_PHASE_EXACT = {"conda", "env", "environment", "setup", "install", "dependencies", "deps", "requirements", "pip"}
CONDA_VERIFY_PHASE_EXACT = {"verify", "verification", "import", "imports", "import_check", "smoke", "reproduce_smoke", "test", "reproduce_full", "eval", "evaluate", "evaluation", "benchmark"}
CONDA_SETUP_TOKENS = {"conda", "env", "environment", "setup", "install", "dependency", "dependencies", "deps", "requirements", "pip", "安装", "环境", "依赖"}
CONDA_VERIFY_TOKENS = {"verify", "verification", "import", "imports", "check", "smoke", "test", "run", "reproduce", "train", "eval", "evaluate", "benchmark", "验证", "导入", "运行", "训练", "复现"}
CONDA_SETUP_ACTION_COMMANDS = {"create", "install", "update"}
CONDA_ENV_SETUP_ACTION_COMMANDS = {"create", "update"}
CONDA_RUN_SETUP_SCRIPT_MARKERS = {"install", "setup", "requirement", "requirements", "dependency", "dependencies", "deps"}
CONDA_RUN_VERIFY_SCRIPT_MARKERS = {"verify", "check", "smoke", "test", "eval", "evaluate", "benchmark", "train", "reproduce", "run"}
CONDA_VERIFY_INNER_HEADS = {"python", "python3", "pytest", "torchrun", "accelerate", "deepspeed"}


def _phase_matches(phase: str, exact_names: set[str], tokens: set[str]) -> bool:
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", str(phase or "").strip().lower()).strip("_")
    if not normalized:
        return False
    if normalized in exact_names:
        return True
    phase_tokens = {token for token in normalized.split("_") if token}
    return bool(phase_tokens & tokens)


def _receipt_command_tokens(receipt: dict[str, Any]) -> list[str]:
    tokens_value = receipt.get("tokens")
    if isinstance(tokens_value, list):
        return [str(item) for item in tokens_value if str(item).strip()]
    if str(receipt.get("command") or "").strip():
        try:
            return command_tokens(str(receipt.get("command") or ""))
        except Exception:
            return []
    return []


def _receipt_declared_conda_prefix(receipt: dict[str, Any], expected_prefix: Path) -> bool:
    return command_uses_conda_prefix(_receipt_command_tokens(receipt), expected_prefix)


def _conda_run_inner_tokens(tokens: list[str]) -> list[str]:
    index = _conda_run_inner_command_index(tokens)
    return tokens[index:] if index is not None else []


def _script_token_has_marker(token: str, markers: set[str]) -> bool:
    name = Path(str(token or "")).name.lower()
    if not name.endswith((".sh", ".bash", ".py")):
        return False
    return any(marker in name for marker in markers)


def _inner_command_runs_marked_script(inner: list[str], markers: set[str]) -> bool:
    if not inner:
        return False
    head = Path(str(inner[0] or "")).name.lower()
    if head in {"bash", "sh", "zsh"}:
        candidates = [
            str(item or "")
            for item in inner[1:]
            if str(item or "").strip() and not str(item or "").startswith("-")
        ]
    else:
        candidates = [str(inner[0] or "")]
    return any(_script_token_has_marker(candidate, markers) for candidate in candidates)


def _python_inner_installs_dependencies(inner: list[str]) -> bool:
    if not inner or not Path(str(inner[0] or "")).name.startswith("python"):
        return False
    index = 1
    while index < len(inner):
        current = str(inner[index] or "")
        if current == "--":
            return False
        if current in {"-m", "--module"}:
            if index + 2 >= len(inner):
                return False
            module_name = Path(str(inner[index + 1] or "")).name
            return bool(module_name in {"pip", "pip3"} and str(inner[index + 2] or "") == "install")
        if current in PYTHON_INLINE_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if current.startswith("-X") and len(current) > 2:
            index += 1
            continue
        if current.startswith("-W") and len(current) > 2:
            index += 1
            continue
        if current.startswith("-") and current != "-":
            index += 1
            continue
        if Path(current).name.lower() != "setup.py":
            return False
        return any(str(item).lower() == "install" for item in inner[index + 1:])
    return False


def _inner_command_installs_dependencies(inner: list[str]) -> bool:
    if not inner:
        return False
    head = Path(str(inner[0] or "")).name.lower()
    if head in {"pip", "pip3"}:
        return bool(len(inner) > 1 and str(inner[1] or "") == "install")
    if head == "uv":
        return bool(
            len(inner) > 2
            and str(inner[1] or "") == "pip"
            and str(inner[2] or "") == "install"
        )
    return bool(
        _python_inner_installs_dependencies(inner)
        or _inner_command_runs_marked_script(inner, CONDA_RUN_SETUP_SCRIPT_MARKERS)
    )


def _direct_entrypoint_tokens_have_verify_action(tokens: list[str]) -> bool:
    if not tokens or _inner_command_installs_dependencies(tokens):
        return False
    head = Path(str(tokens[0] or "")).name.lower()
    if head in CONDA_VERIFY_INNER_HEADS or head in PYTHON_ENV_ENTRYPOINTS:
        return True
    return False


def _conda_prefix_tokens_have_setup_action(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if _inner_command_installs_dependencies(tokens):
        return True
    if Path(str(tokens[0] or "")).name not in {"conda", "mamba", "micromamba"}:
        return False
    if len(tokens) >= 3 and str(tokens[1] or "") == "env":
        return str(tokens[2] or "") in CONDA_ENV_SETUP_ACTION_COMMANDS
    if len(tokens) >= 2 and str(tokens[1] or "") in CONDA_SETUP_ACTION_COMMANDS:
        return True
    return _inner_command_installs_dependencies(_conda_run_inner_tokens(tokens))


def _conda_prefix_tokens_have_verify_action(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if _direct_entrypoint_tokens_have_verify_action(tokens):
        return True
    if Path(str(tokens[0] or "")).name not in {"conda", "mamba", "micromamba"}:
        return False
    inner = _conda_run_inner_tokens(tokens)
    if not inner or _inner_command_installs_dependencies(inner):
        return False
    head = Path(str(inner[0] or "")).name.lower()
    if head in CONDA_VERIFY_INNER_HEADS or head in PYTHON_ENV_ENTRYPOINTS:
        return True
    return _inner_command_runs_marked_script(inner, CONDA_RUN_VERIFY_SCRIPT_MARKERS)


def _receipt_has_conda_setup_action(receipt: dict[str, Any]) -> bool:
    return _conda_prefix_tokens_have_setup_action(_receipt_command_tokens(receipt))


def _receipt_has_conda_verify_action(receipt: dict[str, Any]) -> bool:
    return _conda_prefix_tokens_have_verify_action(_receipt_command_tokens(receipt))


def _receipt_matches_conda_setup(receipt: dict[str, Any]) -> bool:
    setup_phase = _phase_matches(str(receipt.get("phase") or ""), CONDA_SETUP_PHASE_EXACT, CONDA_SETUP_TOKENS)
    return bool(setup_phase and _receipt_has_conda_setup_action(receipt))


def _receipt_matches_conda_verify(receipt: dict[str, Any]) -> bool:
    verify_phase = _phase_matches(str(receipt.get("phase") or ""), CONDA_VERIFY_PHASE_EXACT, CONDA_VERIFY_TOKENS)
    return bool(verify_phase and _receipt_has_conda_verify_action(receipt))


def _receipt_has_conda_phase_without_matching_action(receipt: dict[str, Any]) -> bool:
    setup_phase = _phase_matches(str(receipt.get("phase") or ""), CONDA_SETUP_PHASE_EXACT, CONDA_SETUP_TOKENS)
    verify_phase = _phase_matches(str(receipt.get("phase") or ""), CONDA_VERIFY_PHASE_EXACT, CONDA_VERIFY_TOKENS)
    setup_mismatch = setup_phase and not _receipt_has_conda_setup_action(receipt)
    verify_mismatch = verify_phase and not _receipt_has_conda_verify_action(receipt)
    return bool(setup_mismatch or verify_mismatch)


def _receipt_uses_conda_prefix(receipt: dict[str, Any], expected_prefix: Path) -> bool:
    prefix = str(receipt.get("conda_env_prefix") or "").strip()
    if not prefix:
        return False
    try:
        if Path(prefix).expanduser().resolve() != expected_prefix.expanduser().resolve():
            return False
    except Exception:
        return False
    return _receipt_declared_conda_prefix(receipt, expected_prefix)


def _conda_environment_gate(env_plan: dict[str, Any], receipts: list[dict[str, Any]], run_dir: Path) -> tuple[bool, dict[str, Any]]:
    env_name = str(env_plan.get("env_name") or "").strip()
    expected_prefix = env_prefix_for(run_dir, env_name) if env_name else run_dir / "conda_envs" / ""
    required_successful_receipts = _required_successful_receipts(receipts)
    optional_successful_receipts = _optional_successful_receipts(receipts)
    successful_prefix_receipts = [row for row in required_successful_receipts if _receipt_uses_conda_prefix(row, expected_prefix)]
    optional_prefix_receipts = [row for row in optional_successful_receipts if _receipt_uses_conda_prefix(row, expected_prefix)]
    ignored_prefix_metadata_receipts = [
        row for row in required_successful_receipts
        if str(row.get("conda_env_prefix") or "").strip() and not _receipt_uses_conda_prefix(row, expected_prefix)
    ]
    setup_receipts = [row for row in successful_prefix_receipts if _receipt_matches_conda_setup(row)]
    verify_receipts = [row for row in successful_prefix_receipts if _receipt_matches_conda_verify(row)]
    optional_setup_receipts = [row for row in optional_prefix_receipts if _receipt_matches_conda_setup(row)]
    optional_verify_receipts = [row for row in optional_prefix_receipts if _receipt_matches_conda_verify(row)]
    phase_action_mismatch_receipts = [row for row in successful_prefix_receipts if _receipt_has_conda_phase_without_matching_action(row)]
    prefix_exists = bool(env_name and expected_prefix.exists() and expected_prefix.is_dir())
    evidence = {
        "env_name": env_name,
        "expected_prefix": str(expected_prefix),
        "prefix_exists": prefix_exists,
        "successful_prefix_receipt_count": len(successful_prefix_receipts),
        "ignored_optional_prefix_receipt_count": len(optional_prefix_receipts),
        "ignored_prefix_metadata_receipt_count": len(ignored_prefix_metadata_receipts),
        "setup_phase_count": len(setup_receipts),
        "verify_phase_count": len(verify_receipts),
        "ignored_optional_setup_phase_count": len(optional_setup_receipts),
        "ignored_optional_verify_phase_count": len(optional_verify_receipts),
        "phase_action_mismatch_count": len(phase_action_mismatch_receipts),
        "setup_phases": [str(row.get("phase") or "") for row in setup_receipts[:8]],
        "verify_phases": [str(row.get("phase") or "") for row in verify_receipts[:8]],
        "ignored_optional_setup_phases": [str(row.get("phase") or "") for row in optional_setup_receipts[:8]],
        "ignored_optional_verify_phases": [str(row.get("phase") or "") for row in optional_verify_receipts[:8]],
        "phase_action_mismatch_phases": [str(row.get("phase") or "") for row in phase_action_mismatch_receipts[:8]],
        "ignored_prefix_metadata_phases": [str(row.get("phase") or "") for row in ignored_prefix_metadata_receipts[:8]],
        "sample_prefix_commands": [str(row.get("command") or "") for row in successful_prefix_receipts[:3]],
        "sample_ignored_optional_prefix_commands": [str(row.get("command") or "") for row in optional_prefix_receipts[:3]],
        "sample_phase_action_mismatch_commands": [str(row.get("command") or "") for row in phase_action_mismatch_receipts[:3]],
        "sample_ignored_prefix_metadata_commands": [str(row.get("command") or "") for row in ignored_prefix_metadata_receipts[:3]],
        "accepted_setup_phase_examples": sorted(CONDA_SETUP_PHASE_EXACT)[:12],
        "accepted_verify_phase_examples": sorted(CONDA_VERIFY_PHASE_EXACT)[:12],
        "accepted_setup_action_examples": ["conda create/install/update -p <prefix>", "conda env create/update -p <prefix>", "conda run -p <prefix> python -m pip install ..."],
        "accepted_verify_action_examples": ["conda run -p <prefix> python -c 'import ...'", "conda run -p <prefix> pytest ...", "conda run -p <prefix> torchrun/train/eval/reproduce script"],
    }
    return bool(env_name and prefix_exists and setup_receipts and verify_receipts), evidence


def _successful_dataset_receipts(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for receipt in receipts:
        phase = str(receipt.get("phase") or "").strip()
        if not phase or int(receipt.get("return_code") or 0) != 0:
            continue
        if receipt.get("required") is False:
            continue
        if _phase_indicates_dataset(phase):
            rows.append(receipt)
    return rows


def _phase_tokens(phase: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9\u4e00-\u9fff]+", str(phase or "").lower()) if token}


def _phase_indicates_dataset(phase: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", str(phase or "").strip().lower()).strip("_")
    if not normalized:
        return False
    if normalized in DATASET_PHASE_EXACT or normalized in DATASET_PHASE_PHRASES:
        return True
    tokens = _phase_tokens(normalized)
    has_object = bool(tokens & DATASET_OBJECT_TOKENS)
    has_action = bool(tokens & DATASET_ACTION_TOKENS)
    return bool(has_object and has_action)


def _ensure_approval_gate_for_early_decision(decision: dict[str, Any], paper_evidence: dict[str, Any]) -> dict[str, Any]:
    if _approval_gate_from_decision(decision):
        return decision
    verdict = decision.get("verdict") if isinstance(decision.get("verdict"), dict) else {}
    repo_info = decision.get("repo") if isinstance(decision.get("repo"), dict) else {}
    repo_ok, repo_gate_evidence = _repo_source_gate(repo_info)
    early_reason = str(verdict.get("reject_reason") or verdict.get("reason") or "尚未进入完整复现流程").strip()
    early_evidence = {
        "decision": decision.get("decision"),
        "reason": early_reason,
        "round_count": len(decision.get("rounds") if isinstance(decision.get("rounds"), list) else []),
        "stage": "pre_reproduction_or_early_exit",
    }
    checks = [
        _approval_check(
            "repository_source",
            repo_ok,
            "GitHub 仓库已克隆、版本已固定并记录 head_commit" if repo_ok else "早停路径尚未完成可信 GitHub 仓库克隆和版本固定",
            repo_gate_evidence,
        ),
        _approval_check(
            "repository_documentation",
            False,
            "早停路径尚未采集仓库 README/文档/配置/入口证据",
            early_evidence,
        ),
        _approval_check(
            "conda_environment",
            False,
            "早停路径尚未生成并验证 run-local Conda 环境",
            early_evidence,
        ),
        _approval_check(
            "machine_fit",
            False,
            "早停路径尚未完成论文硬件要求与本机资源适配评估",
            early_evidence,
        ),
        _approval_check(
            "dataset_evidence",
            False,
            "早停路径尚未完成论文数据集对齐和数据准备验证",
            early_evidence,
        ),
        _approval_check(
            "required_commands",
            False,
            "早停路径尚未执行必需部署/复现命令",
            early_evidence,
        ),
        _approval_check(
            "paper_claims_verified",
            False,
            "早停路径尚未验证论文声明效果",
            {"paper_claims_verified": verdict.get("paper_claims_verified"), "reproduction_success": verdict.get("reproduction_success")},
        ),
        _approval_check(
            "success_criteria_schema",
            False,
            "早停路径尚未生成可校验的 success_criteria 指标结构",
            early_evidence,
        ),
        _approval_check(
            "success_criteria_paper_binding",
            False,
            "早停路径尚未证明 success_criteria 逐项绑定到论文目标指标",
            early_evidence,
        ),
        _approval_check(
            "metric_evidence",
            False,
            "早停路径尚未产生可核验指标证据",
            early_evidence,
        ),
        _approval_check(
            "paper_context",
            False,
            "早停路径尚未由 Claude audit 完成论文上下文裁决",
            early_evidence,
        ),
        _approval_check(
            "reproduce_full",
            False,
            "早停路径尚未产生 reproduce_full 成功回执",
            early_evidence,
        ),
        _approval_check(
            "paper_config_alignment",
            False,
            "早停路径尚未生成论文配置对齐表",
            early_evidence,
        ),
        _approval_check(
            "workspace_write_audit",
            False,
            "等待最终工作区审计写入",
            early_evidence,
        ),
    ]
    gate = _recompute_approval_gate({
        "schema_version": "environment.approval_gate.v1",
        "policy_version": DECISION_POLICY_VERSION,
        "stage": "pre_reproduction_or_early_exit",
        "early_exit_reason": early_reason,
        "checks": checks,
    })
    decision["approval_gate"] = gate
    if isinstance(verdict, dict):
        verdict["approval_gate"] = gate
        decision["verdict"] = verdict
    decision.setdefault("approval_contract", {})["approved"] = False
    if decision.get("allow_next_module") is True:
        decision["decision"] = "continue_repair"
        decision["allow_next_module"] = False
        decision["exit_code"] = 30
        if isinstance(verdict, dict):
            verdict["decision"] = "continue_repair"
            verdict["allow_next_module"] = False
            verdict.setdefault("repair_plan", []).append("后端降级：最终裁决缺少完整 approval_gate，已补齐早停门槛并禁止进入下一模块。")
            decision["verdict"] = verdict
    return decision


def _runtime_smoke_gate(receipts: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
    successful = _required_successful_receipts(receipts)
    smoke_phases = []
    verify_phases = []
    model_phases = []
    for row in successful:
        phase = str(row.get("phase") or "").strip().lower()
        if not phase:
            continue
        if "smoke" in phase or phase in {"reproduce_smoke", "loader_probe", "load_dataset", "loader"}:
            smoke_phases.append(row)
        if _receipt_matches_conda_verify(row):
            verify_phases.append(row)
        if "model" in phase or "loader" in phase:
            model_phases.append(row)
    return bool(smoke_phases and verify_phases), {
        "successful_smoke_phases": [str(row.get("phase") or "") for row in smoke_phases[:8]],
        "successful_verify_phases": [str(row.get("phase") or "") for row in verify_phases[:8]],
        "successful_model_or_loader_phases": [str(row.get("phase") or "") for row in model_phases[:8]],
        "sample_smoke_receipts": [_compact_receipt(row) for row in smoke_phases[:3]],
    }


def _latest_round_with_receipts(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    for row in reversed(rounds):
        if isinstance(row, dict) and str(row.get("receipts_path") or "").strip():
            return row
    return {}


def _latest_env_plan(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    round_record = _latest_round_with_receipts(rounds)
    path = Path(str(round_record.get("env_plan_path") or "")) if round_record else Path("")
    payload = read_json(path, {}) if str(path) else {}
    return payload if isinstance(payload, dict) else {}


def _latest_receipts(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    round_record = _latest_round_with_receipts(rounds)
    path = Path(str(round_record.get("receipts_path") or "")) if round_record else Path("")
    payload = read_json(path, []) if str(path) else []
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _read_round_receipts(round_record: dict[str, Any], run_dir: Path) -> list[dict[str, Any]]:
    path_text = str(round_record.get("receipts_path") or "").strip()
    if not path_text:
        return []
    try:
        receipts_path = ensure_within(Path(path_text).expanduser().resolve(), run_dir.expanduser().resolve())
    except Exception:
        return []
    payload = read_json(receipts_path, [])
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _path_exists_within_run_dir(path_text: str, run_dir: Path) -> bool:
    raw = str(path_text or "").strip()
    if not raw:
        return False
    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = run_dir / candidate
        candidate = ensure_within(candidate.resolve(), run_dir.expanduser().resolve())
    except Exception:
        return False
    return candidate.exists()


def _dataset_receipt_artifact_exists(receipt: dict[str, Any], run_dir: Path) -> bool:
    candidates: list[str] = []
    for key in ["artifact_path", "output_path", "data_path", "dataset_path", "local_dir", "repo_path"]:
        value = receipt.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value)
    tokens = receipt.get("tokens") if isinstance(receipt.get("tokens"), list) else []
    tokens = [str(token) for token in tokens if str(token).strip()]
    if not tokens:
        try:
            tokens = shlex.split(str(receipt.get("command") or ""))
        except Exception:
            tokens = []
    if tokens and tokens[0] == "git" and "clone" in tokens and len(tokens) >= 3:
        candidates.append(tokens[-1])
    for option in ["--local-dir", "--output-dir", "--data-dir", "--dataset-dir"]:
        if option in tokens:
            index = tokens.index(option)
            if index + 1 < len(tokens):
                candidates.append(tokens[index + 1])
    run_text = str(run_dir)
    for token in tokens:
        if token.startswith(("data/", "repos/")) or run_text in token:
            candidates.append(token)
    return any(_path_exists_within_run_dir(candidate, run_dir) for candidate in candidates)


def _historical_dataset_receipts(rounds: list[dict[str, Any]], run_dir: Path) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for round_record in rounds:
        if not isinstance(round_record, dict):
            continue
        for receipt in _successful_dataset_receipts(_read_round_receipts(round_record, run_dir)):
            if _dataset_receipt_artifact_exists(receipt, run_dir):
                enriched = dict(receipt)
                enriched["handoff_reused_from_round"] = round_record.get("round")
                receipts.append(enriched)
    return receipts


def _handoff_receipts(rounds: list[dict[str, Any]], latest_receipts: list[dict[str, Any]], run_dir: Path) -> list[dict[str, Any]]:
    receipts = [row for row in latest_receipts if isinstance(row, dict)]
    seen = {
        (str(row.get("phase") or ""), str(row.get("log_path") or ""), str(row.get("command") or ""))
        for row in receipts
    }
    for receipt in _historical_dataset_receipts(rounds, run_dir):
        key = (str(receipt.get("phase") or ""), str(receipt.get("log_path") or ""), str(receipt.get("command") or ""))
        if key in seen:
            continue
        receipts.append(receipt)
        seen.add(key)
    return receipts


def _required_environment_commands_gate(receipts: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
    required = [row for row in receipts if isinstance(row, dict) and row.get("required") is not False]
    reproduce_full_rows = [row for row in required if str(row.get("phase") or "").strip().lower() == "reproduce_full"]
    failures = [
        row for row in required
        if str(row.get("phase") or "").strip().lower() != "reproduce_full" and not _receipt_succeeded(row)
    ]
    return bool(required and not failures), {
        "required_receipt_count": len(required),
        "ignored_reproduce_full_count": len(reproduce_full_rows),
        "non_full_required_failures": [_compact_receipt(row) for row in failures[:5]],
        "successful_non_full_required_phases": [
            str(row.get("phase") or "")
            for row in required
            if _receipt_succeeded(row) and str(row.get("phase") or "").strip().lower() != "reproduce_full"
        ][:12],
    }


def _handoff_workspace_audit_gate(workspace_audit: dict[str, Any] | None, approval_checks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if isinstance(workspace_audit, dict) and workspace_audit:
        passed = workspace_audit.get("status") == "passed"
        return _approval_check(
            "workspace_write_audit",
            passed,
            "工作区写入审计已通过" if passed else "工作区写入审计未通过或尚未生成",
            workspace_audit,
        )
    source = approval_checks.get("workspace_write_audit")
    if isinstance(source, dict):
        return {**source, "name": "workspace_write_audit"}
    return _approval_check("workspace_write_audit", False, "工作区写入审计未通过或尚未生成")


def build_environment_handoff_gate(
    approval_gate: dict[str, Any],
    receipts: list[dict[str, Any]],
    repo_info: dict[str, Any],
    env_plan: dict[str, Any] | None = None,
    run_dir: Path | None = None,
    workspace_audit: dict[str, Any] | None = None,
    normalized_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    approval_checks = {
        str(row.get("name") or ""): row
        for row in approval_gate.get("checks", [])
        if isinstance(row, dict)
    }
    env_plan = env_plan if isinstance(env_plan, dict) else {}
    runtime_ok, runtime_evidence = _runtime_smoke_gate(receipts)
    checks: list[dict[str, Any]] = []
    for name in ENVIRONMENT_HANDOFF_REQUIRED_CHECKS:
        if name == "repository_source":
            repo_ok, repo_evidence = _repo_source_gate(repo_info)
            checks.append(_approval_check(
                "repository_source",
                repo_ok,
                "GitHub 仓库已克隆、版本已固定并记录 head_commit" if repo_ok else "缺少可信 GitHub 仓库克隆证据，或指定 commit 未成功 checkout/匹配 HEAD",
                repo_evidence,
            ))
            continue
        if name == "conda_environment":
            if env_plan and run_dir is not None:
                conda_ok, conda_evidence = _conda_environment_gate(env_plan, receipts, run_dir)
                checks.append(_approval_check(
                    "conda_environment",
                    conda_ok,
                    "Conda 环境 prefix、依赖安装和导入/运行验证通过" if conda_ok else "缺少可审计的 Conda 环境部署或导入/运行验证证据",
                    conda_evidence,
                ))
            else:
                source = approval_checks.get(name)
                checks.append({**source, "name": name} if isinstance(source, dict) else _approval_check(name, False, f"缺少 approval gate 检查项：{name}"))
            continue
        if name == "machine_fit":
            source = approval_checks.get(name)
            checks.append({**source, "name": name} if isinstance(source, dict) else _approval_check(name, False, f"缺少 approval gate 检查项：{name}"))
            continue
        if name == "dataset_runtime":
            source = approval_checks.get("dataset_evidence")
            checks.append({**source, "name": "dataset_runtime"} if isinstance(source, dict) else _approval_check("dataset_runtime", False, "Claude audit 缺少 dataset_evidence 检查"))
            continue
        if name == "required_commands":
            commands_ok, commands_evidence = _required_environment_commands_gate(receipts)
            checks.append(_approval_check(
                "required_commands",
                commands_ok,
                "environment 交接所需命令已成功；论文级 reproduce_full 可由下游继续验证" if commands_ok else "存在必需 environment 命令失败或缺少命令回执",
                commands_evidence,
            ))
            continue
        if name == "runtime_smoke":
            checks.append(_approval_check(
                "runtime_smoke",
                runtime_ok,
                "loader/model smoke 已在 run-local Conda 环境中通过" if runtime_ok else "缺少 run-local loader/model smoke 通过证据",
                runtime_evidence,
            ))
            continue
        if name == "paper_config_alignment":
            source = approval_checks.get(name)
            checks.append({**source, "name": name} if isinstance(source, dict) else _approval_check(name, False, f"缺少 approval gate 检查项：{name}"))
            continue
        if name == "workspace_write_audit":
            checks.append(_handoff_workspace_audit_gate(workspace_audit, approval_checks))
            continue
    missing = [row["reason"] for row in checks if not row.get("passed")]
    repo_path = str(repo_info.get("repo_path") or "").strip()
    if not repo_path or not Path(repo_path).exists():
        missing.append("repo_path 不存在，不能交给实验阶段")
        checks.append(_approval_check("repo_path_exists", False, "repo_path 不存在，不能交给实验阶段", {"repo_path": repo_path}))
    return {
        "schema_version": "environment.handoff_gate.v1",
        "policy_version": DECISION_POLICY_VERSION,
        "passed": not missing,
        "missing": missing,
        "required_checks": list(ENVIRONMENT_HANDOFF_REQUIRED_CHECKS),
        "pending_downstream_checks": list(ENVIRONMENT_HANDOFF_ALLOWED_PENDING_CHECKS),
        "checks": checks,
    }


def _pending_downstream_metrics(env_plan: dict[str, Any], metric_evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence_by_metric = {
        str(row.get("metric") or row.get("name") or "").strip().lower(): row
        for row in metric_evidence
        if isinstance(row, dict)
    }
    pending: list[dict[str, Any]] = []
    for item in env_plan.get("success_criteria") if isinstance(env_plan.get("success_criteria"), list) else []:
        if not isinstance(item, dict):
            continue
        if _criterion_is_environment_gate(item):
            continue
        metric = str(item.get("metric") or item.get("name") or "").strip()
        if not metric:
            continue
        evidence = evidence_by_metric.get(metric.lower(), {})
        if evidence.get("passed") is True:
            continue
        pending.append({
            "metric": metric,
            "operator": item.get("operator") or item.get("op"),
            "target": item.get("value") if "value" in item else item.get("target"),
            "source": item.get("source") or item.get("evidence_source") or "",
            "status": "pending_experimenting_evaluation",
            "reason": "论文级指标必须由 experimenting/evaluation 阶段基于真实实验日志验证；environment 只交付可运行环境、数据和参考入口。",
        })
    return pending


def build_environment_handoff(
    run_id: str,
    run_dir: Path,
    normalized_plan: dict[str, Any],
    repo_info: dict[str, Any],
    env_plan: dict[str, Any],
    receipts: list[dict[str, Any]],
    approval_gate: dict[str, Any],
    metric_evidence: list[dict[str, Any]],
    workspace_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    env_name = str(env_plan.get("env_name") or "").strip()
    handoff_gate = build_environment_handoff_gate(
        approval_gate, receipts, repo_info,
        env_plan=env_plan, run_dir=run_dir, workspace_audit=workspace_audit, normalized_plan=normalized_plan,
    )
    conda_prefix = str(env_prefix_for(run_dir, env_name)) if env_name else ""
    full_commands = [row for row in env_plan.get("commands", []) if isinstance(row, dict) and str(row.get("phase") or "").strip().lower() == "reproduce_full"] if isinstance(env_plan.get("commands"), list) else []
    smoke_receipts = [row for row in _required_successful_receipts(receipts) if "smoke" in str(row.get("phase") or "").lower() or "loader" in str(row.get("phase") or "").lower()]
    dataset_receipts = _successful_dataset_receipts(receipts)
    ready = bool(handoff_gate.get("passed"))
    return {
        "schema_version": "environment.handoff.v1",
        "policy_version": DECISION_POLICY_VERSION,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "ready_for_experimenting": ready,
        "status": "ready_for_experimenting" if ready else "blocked_environment_handoff",
        "repo": {
            "repo_url": repo_info.get("repo_url", ""),
            "repo_path": repo_info.get("repo_path", ""),
            "head_commit": repo_info.get("head_commit", ""),
        },
        "conda": {
            "env_name": env_name,
            "prefix": conda_prefix,
            "python": str(Path(conda_prefix) / "bin" / "python") if conda_prefix else "",
        },
        "paper": {
            "title": normalized_plan.get("title", ""),
            "paper_url": normalized_plan.get("paper_url", ""),
            "selected_plan_id": normalized_plan.get("selected_plan_id", ""),
            "selected_idea_id": normalized_plan.get("selected_idea_id", ""),
        },
        "data": {
            "run_data_dir": str(run_dir / "data"),
            "successful_dataset_receipts": [_compact_receipt(row) for row in dataset_receipts[:8]],
        },
        "runtime_smoke": {
            "successful_smoke_receipts": [_compact_receipt(row) for row in smoke_receipts[:8]],
        },
        "reference_command_templates": full_commands[:3],
        "pending_downstream_metrics": _pending_downstream_metrics(env_plan, metric_evidence),
        "handoff_gate": handoff_gate,
        "note": (
            "Environment 已验证真实仓库、run-local Conda、数据准备和 loader/model smoke；论文级指标由 Experimenting 基于本 handoff 继续验证。"
            if ready
            else "Environment handoff 尚未通过；必须按 handoff_gate.missing 继续修复，Experimenting 不得使用本次 handoff。"
        ),
    }


def _set_environment_handoff_readiness(decision: dict[str, Any]) -> dict[str, Any]:
    handoff = decision.get("environment_handoff") if isinstance(decision.get("environment_handoff"), dict) else {}
    gate = handoff.get("handoff_gate") if isinstance(handoff.get("handoff_gate"), dict) else {}
    if not gate:
        return decision
    checks = gate.get("checks") if isinstance(gate.get("checks"), list) else []
    missing = [str(row.get("reason") or "") for row in checks if isinstance(row, dict) and not row.get("passed")]
    missing = [item for item in missing if item]
    gate["missing"] = missing
    gate["passed"] = not missing
    handoff["ready_for_experimenting"] = bool(gate.get("passed"))
    handoff["status"] = "ready_for_experimenting" if gate.get("passed") else "blocked_environment_handoff"
    decision["ready_for_experimenting"] = bool(gate.get("passed"))
    contract = decision.setdefault("environment_handoff_contract", {})
    if isinstance(contract, dict):
        contract["ready_for_experimenting"] = bool(gate.get("passed"))
    if gate.get("passed") and decision.get("decision") == "continue_repair":
        decision["decision"] = "environment_ready"
        decision["exit_code"] = 0
        verdict = decision.setdefault("verdict", {})
        if isinstance(verdict, dict):
            verdict["decision"] = "environment_ready"
            verdict["allow_next_module"] = False
            verdict["ready_for_experimenting"] = True
            verdict.setdefault("repair_plan", []).append("论文级指标移交 experimenting/evaluation 阶段验证；environment 阶段已生成可运行 handoff。")
    return decision


def _refresh_environment_handoff_workspace_audit(decision: dict[str, Any]) -> dict[str, Any]:
    handoff = decision.get("environment_handoff") if isinstance(decision.get("environment_handoff"), dict) else {}
    gate = handoff.get("handoff_gate") if isinstance(handoff.get("handoff_gate"), dict) else {}
    checks = gate.get("checks") if isinstance(gate.get("checks"), list) else []
    if not checks:
        return decision
    audit = decision.get("workspace_write_audit") if isinstance(decision.get("workspace_write_audit"), dict) else {}
    audit_passed = audit.get("status") == "passed"
    refreshed = _approval_check(
        "workspace_write_audit",
        audit_passed,
        "工作区写入审计已通过" if audit_passed else "工作区写入审计未通过或尚未生成",
        audit,
    )
    replaced = False
    new_checks: list[dict[str, Any]] = []
    for row in checks:
        if isinstance(row, dict) and str(row.get("name") or "") == "workspace_write_audit":
            new_checks.append(refreshed)
            replaced = True
        else:
            new_checks.append(row)
    if not replaced:
        new_checks.append(refreshed)
    gate["checks"] = new_checks
    return _set_environment_handoff_readiness(decision)


def attach_environment_handoff(
    decision: dict[str, Any],
    run_dir: Path,
    normalized_plan: dict[str, Any],
    repo_info: dict[str, Any],
) -> dict[str, Any]:
    rounds = decision.get("rounds") if isinstance(decision.get("rounds"), list) else []
    env_plan = _latest_env_plan(rounds)
    latest_receipts = _latest_receipts(rounds)
    receipts = _handoff_receipts(rounds, latest_receipts, run_dir)
    latest_round = _latest_round_with_receipts(rounds)
    metric_evidence = latest_round.get("metric_evidence") if isinstance(latest_round.get("metric_evidence"), list) else []
    approval_gate = decision.get("approval_gate") if isinstance(decision.get("approval_gate"), dict) else {}
    handoff = build_environment_handoff(
        run_id=str(decision.get("run_id") or ""), run_dir=run_dir,
        normalized_plan=normalized_plan, repo_info=repo_info, env_plan=env_plan,
        receipts=receipts, approval_gate=approval_gate, metric_evidence=metric_evidence,
        workspace_audit=decision.get("workspace_write_audit") if isinstance(decision.get("workspace_write_audit"), dict) else None,
    )
    decision["environment_handoff"] = handoff
    decision["ready_for_experimenting"] = bool(handoff.get("ready_for_experimenting"))
    contract = decision.setdefault("environment_handoff_contract", {})
    contract.update({
        "ready_for_experimenting": bool(handoff.get("ready_for_experimenting")),
        "meaning": "ready_for_experimenting=true 表示 environment 已交付真实 repo/data/run-local Conda/loader smoke，可进入 experimenting；不表示论文指标已达标。",
        "pending_downstream_metrics": handoff.get("pending_downstream_metrics", []),
    })
    return _set_environment_handoff_readiness(decision)


def build_approval_gate(
    verdict: dict[str, Any],
    receipts: list[dict[str, Any]],
    env_plan: dict[str, Any],
    repo_info: dict[str, Any],
    run_dir: Path,
    criteria_passed: bool,
    metric_evidence: list[dict[str, Any]],
    verdict_metric_ok: bool,
    verdict_metric_issues: list[str],
) -> dict[str, Any]:
    required_failures = [row for row in receipts if row.get("return_code") != 0 and row.get("required") is not False]
    required_ok = bool(receipts and not required_failures)
    claims_ok = bool(verdict.get("paper_claims_verified") is True and verdict.get("reproduction_success") is True)
    criteria_schema_issues = success_criteria_issues(env_plan.get("success_criteria"))
    criteria_schema_ok = not criteria_schema_issues
    metric_ok = bool(criteria_passed or verdict_metric_ok)
    full_receipts = [
        row for row in _required_successful_receipts(receipts)
        if str(row.get("phase") or "").strip().lower() == "reproduce_full"
    ]
    optional_full_receipts = [
        row for row in _optional_successful_receipts(receipts)
        if str(row.get("phase") or "").strip().lower() == "reproduce_full"
    ]
    full_ok = bool(full_receipts)
    repo_ok, repo_gate_evidence = _repo_source_gate(repo_info)
    conda_ok, conda_gate_evidence = _conda_environment_gate(env_plan, receipts, run_dir)
    checks = [
        _approval_check(
            "repository_source",
            repo_ok,
            "GitHub 仓库已克隆、版本已固定并记录 head_commit" if repo_ok else "缺少可信 GitHub 仓库克隆证据，或指定 commit 未成功 checkout/匹配 HEAD",
            repo_gate_evidence,
        ),
        _audit_check(verdict, "repository_documentation", "Claude audit 缺少 repository_documentation 检查"),
        _approval_check(
            "conda_environment",
            conda_ok,
            "Conda 环境 prefix、依赖安装和导入/运行验证通过" if conda_ok else "缺少可审计的 Conda 环境部署或导入/运行验证证据",
            conda_gate_evidence,
        ),
        _audit_check(verdict, "machine_fit", "Claude audit 缺少 machine_fit 检查"),
        _audit_check(verdict, "dataset_evidence", "Claude audit 缺少 dataset_evidence 检查"),
        _approval_check(
            "required_commands",
            required_ok,
            "必需命令全部成功" if required_ok else "存在必需命令失败或缺少命令回执",
            [_compact_receipt(row) for row in required_failures[:5]] if required_failures else {"receipt_count": len(receipts)},
        ),
        _approval_check(
            "paper_claims_verified",
            claims_ok,
            "Claude 声明论文效果已验证" if claims_ok else "缺少 paper_claims_verified=true 或 reproduction_success=true",
            {"paper_claims_verified": verdict.get("paper_claims_verified"), "reproduction_success": verdict.get("reproduction_success")},
        ),
        _approval_check(
            "success_criteria_schema",
            criteria_schema_ok,
            "success_criteria 指标名、比较符、可比较目标值和来源完整" if criteria_schema_ok else "success_criteria 缺少指标名、比较符、可解析目标值或论文/README 来源",
            {"issues": criteria_schema_issues[:10], "criterion_count": len(env_plan.get("success_criteria") if isinstance(env_plan.get("success_criteria"), list) else [])},
        ),
        _audit_check(verdict, "success_criteria_paper_binding", "Claude audit 缺少 success_criteria_paper_binding 检查"),
        _approval_check(
            "metric_evidence",
            metric_ok,
            "指标证据通过" if metric_ok else "缺少可核验且来自必需批准指标阶段的指标/成功标准证据",
            {
                "local_criteria_passed": criteria_passed,
                "local_metric_evidence": metric_evidence[:5],
                "claude_metric_evidence_passed": verdict_metric_ok,
                "claude_metric_evidence_issues": verdict_metric_issues[:10],
            },
        ),
        _audit_check(verdict, "paper_context", "Claude audit 缺少 paper_context 检查"),
        _approval_check(
            "reproduce_full",
            full_ok,
            "reproduce_full 必需阶段成功" if full_ok else "缺少成功且必需的 reproduce_full 回执",
            {
                "successful_required_reproduce_full_receipts": [_compact_receipt(row) for row in full_receipts[:3]],
                "ignored_optional_reproduce_full_receipt_count": len(optional_full_receipts),
                "ignored_optional_reproduce_full_receipts": [_compact_receipt(row) for row in optional_full_receipts[:3]],
            },
        ),
        _audit_check(verdict, "paper_config_alignment", "Claude audit 缺少 paper_config_alignment 检查"),
    ]
    missing = [row["reason"] for row in checks if not row.get("passed")]
    return {
        "schema_version": "environment.approval_gate.v1",
        "policy_version": DECISION_POLICY_VERSION,
        "passed": not missing,
        "missing": missing,
        "checks": checks,
    }


def command_declares_conda_prefix(tokens: list[str], expected_prefix: Path) -> bool:
    expected = expected_prefix.expanduser().resolve()
    for index, token in enumerate(tokens):
        value = str(token)
        candidate = ""
        if value in CONDA_PREFIX_OPTIONS and index + 1 < len(tokens):
            candidate = str(tokens[index + 1])
        elif value.startswith("--prefix="):
            candidate = value.split("=", 1)[1]
        if not candidate:
            continue
        try:
            if Path(candidate).expanduser().resolve() == expected:
                return True
        except Exception:
            continue
    return False


def _command_uses_direct_env_executable(tokens: list[str], env_prefix: Path) -> bool:
    if not tokens:
        return False
    head = Path(str(tokens[0] or "")).name
    if head not in RUN_ENV_ENTRYPOINTS:
        return False
    candidate = Path(str(tokens[0] or "")).expanduser()
    if not candidate.is_absolute():
        return False
    try:
        candidate = candidate.resolve()
        bin_dir = env_prefix.expanduser().resolve() / "bin"
        candidate.relative_to(bin_dir)
        return True
    except Exception:
        return False


def command_uses_conda_prefix(tokens: list[str], env_prefix: Path) -> bool:
    if not tokens:
        return False
    head = Path(tokens[0]).name
    if head in {"conda", "mamba", "micromamba"}:
        return command_declares_conda_prefix(tokens, env_prefix)
    return _command_uses_direct_env_executable(tokens, env_prefix)


def _direct_env_entrypoint_command(tokens: list[str], env_prefix: Path) -> list[str]:
    if not tokens:
        return []
    head = Path(str(tokens[0] or "")).name
    if head in {"pip", "pip3"}:
        return [str(env_prefix / "bin" / "python"), "-m", "pip", *tokens[1:]]
    if head in RUN_ENV_ENTRYPOINTS:
        executable = "python" if head in {"python", "python3"} else head
        return [str(env_prefix / "bin" / executable), *tokens[1:]]
    return []


def conda_env_name_from_command(command: Any) -> str:
    try:
        tokens = command_tokens(command)
    except Exception:
        return ""
    if not tokens or Path(str(tokens[0] or "")).name not in {"conda", "mamba", "micromamba"}:
        return ""
    skip_next = False
    for index, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        value = str(token or "")
        if value in {"-n", "--name"} and index + 1 < len(tokens):
            return str(tokens[index + 1] or "").strip()
        if value.startswith("--name="):
            return value.split("=", 1)[1].strip()
        if value in {"-p", "--prefix"}:
            skip_next = True
            continue
    return ""


def conda_command_sets_active_env(command: Any) -> bool:
    try:
        tokens = command_tokens(command)
    except Exception:
        return False
    if not tokens or Path(str(tokens[0] or "")).name not in {"conda", "mamba", "micromamba"}:
        return False
    if len(tokens) >= 3 and str(tokens[1] or "") == "env":
        return str(tokens[2] or "") in {"create", "update"}
    if len(tokens) >= 2:
        return str(tokens[1] or "") in {"create", "install", "update"}
    return False


def conda_command_creates_env(command: Any) -> bool:
    try:
        tokens = command_tokens(command)
    except Exception:
        return False
    if not tokens or Path(str(tokens[0] or "")).name not in {"conda", "mamba", "micromamba"}:
        return False
    if len(tokens) >= 3 and str(tokens[1] or "") == "env":
        return str(tokens[2] or "") == "create"
    return len(tokens) >= 2 and str(tokens[1] or "") == "create"


def conda_prefix_has_python(env_prefix: Path) -> bool:
    return (env_prefix / "bin" / "python").exists()


def existing_conda_prefix_receipt(command: list[str], log_path: Path, env_prefix: Path, row: dict[str, Any], uses_conda_prefix: bool, script_migrations: list[dict[str, str]]) -> dict[str, Any]:
    now = utc_now()
    text = command_text(command)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        f"$ {text}\n"
        "[复用已存在 run-local Conda prefix] 已检测到目标 prefix/bin/python，跳过 conda create，避免覆盖已安装依赖。\n"
        f"conda_env_prefix={env_prefix}\n",
        encoding="utf-8",
    )
    return {
        "phase": row.get("phase"),
        "command": text,
        "status": "passed",
        "return_code": 0,
        "required": row.get("required"),
        "log_path": str(log_path),
        "conda_env_prefix": str(env_prefix),
        "uses_conda_prefix": uses_conda_prefix,
        "script_migrations": script_migrations,
        "env_keys": sorted((row.get("env") if isinstance(row.get("env"), dict) else {}).keys()),
        "inline_env_keys": row.get("inline_env_keys", []),
        "existing_prefix_reused": True,
        "started_at": now,
        "finished_at": now,
    }


def rewrite_command(command: Any, conda_exe: str, env_name: str, env_prefix: Path) -> list[str]:
    tokens = command_tokens(command)
    if not tokens:
        return tokens
    head = Path(tokens[0]).name
    if head in {"conda", "mamba", "micromamba"}:
        if conda_exe:
            tokens[0] = conda_exe
        tokens = _replace_or_add_conda_prefix(tokens, env_prefix)
        inner_index = _conda_run_inner_command_index(tokens)
        if inner_index is not None and env_name:
            direct = _direct_env_entrypoint_command(tokens[inner_index:], env_prefix)
            if direct:
                return direct
        return tokens
    if env_name:
        direct = _direct_env_entrypoint_command(tokens, env_prefix)
        if direct:
            return direct
    return tokens


def _command_shell_script_tokens(tokens: list[str]) -> list[str]:
    inner = _conda_run_inner_tokens(tokens)
    effective = inner if inner else tokens
    if not effective or Path(str(effective[0] or "")).name not in {"bash", "sh", "zsh"}:
        return []
    candidates: list[str] = []
    saw_separator = False
    for raw in effective[1:]:
        token = str(raw or "")
        if not token:
            continue
        if token == "--" and not saw_separator:
            saw_separator = True
            continue
        if not saw_separator and token.startswith("-"):
            continue
        candidates.append(token)
        break
    return candidates


def _resolve_run_script_path(token: str, cwd: Path, run_dir: Path) -> Path | None:
    script_path = Path(str(token or "")).expanduser()
    if not script_path.is_absolute():
        script_path = cwd.expanduser().resolve() / script_path
    try:
        return ensure_within(script_path, run_dir.expanduser().resolve())
    except ValueError:
        return None


def missing_shell_script_issue(tokens: list[str], cwd: Path, run_dir: Path) -> str:
    for token in _command_shell_script_tokens(tokens):
        script_path = _resolve_run_script_path(token, cwd, run_dir)
        if script_path is None:
            continue
        if script_path.is_file():
            continue
        return f"shell 脚本不存在：{script_path}；环境计划不能引用未实际生成的辅助脚本，请直接使用 JSON 命令或仓库中已存在的脚本"
    return ""

def command_required(row: dict[str, Any]) -> bool:
    return row.get("required") is not False


def validate_environment_plan(plan: dict[str, Any], require_full_reproduction: bool, repo_path: Path | None = None, run_dir: Path | None = None) -> list[str]:
    issues: list[str] = []
    status = str(plan.get("status") or "").strip()
    if status != "ready_to_execute":
        issues.append(f"环境计划 status 必须是 ready_to_execute：{status or 'missing'}")
    env_name = str(plan.get("env_name") or "").strip()
    if not env_name:
        issues.append("环境计划缺少 env_name")
    elif not re.fullmatch(r"[A-Za-z0-9_.-]+", env_name):
        issues.append("环境计划 env_name 只能包含字母、数字、点、下划线和连字符")
    commands = plan.get("commands")
    if not isinstance(commands, list) or not commands:
        issues.append("环境计划缺少 commands 数组")
        return issues
    phases: list[str] = []
    reproduce_full_required = False
    conda_setup_phase_present = False
    conda_verify_phase_present = False
    dataset_phase_present = False
    optional_conda_setup_phases: list[str] = []
    optional_conda_verify_phases: list[str] = []
    optional_dataset_phases: list[str] = []
    for index, row in enumerate(commands):
        if not isinstance(row, dict):
            issues.append(f"commands[{index}] 不是 object")
            continue
        raw_phase = row.get("phase")
        if not isinstance(raw_phase, str) or not raw_phase.strip():
            issues.append(f"commands[{index}] phase 必须是非空字符串")
            phase = ""
        else:
            phase = raw_phase.strip()
        if "required" in row and not isinstance(row.get("required"), bool):
            issues.append(f"commands[{index}] required 必须是 JSON boolean true/false，不能使用字符串或数字")
        if "cwd" in row and row.get("cwd") not in (None, "") and not isinstance(row.get("cwd"), str):
            issues.append(f"commands[{index}] cwd 必须是字符串")
        if phase:
            phases.append(phase)
            is_required = command_required(row)
            if _phase_matches(phase, CONDA_SETUP_PHASE_EXACT, CONDA_SETUP_TOKENS):
                if is_required:
                    conda_setup_phase_present = True
                else:
                    optional_conda_setup_phases.append(phase)
            if _phase_matches(phase, CONDA_VERIFY_PHASE_EXACT, CONDA_VERIFY_TOKENS):
                if is_required:
                    conda_verify_phase_present = True
                else:
                    optional_conda_verify_phases.append(phase)
            if _phase_indicates_dataset(phase):
                if is_required:
                    dataset_phase_present = True
                else:
                    optional_dataset_phases.append(phase)
            if phase.lower() == "reproduce_full" and is_required:
                reproduce_full_required = True
        try:
            tokens, merged_env, inline_env_issues, _inline_env = normalized_command_and_env(row)
        except Exception as exc:
            issues.append(f"commands[{index}] command 无法解析：{type(exc).__name__}: {exc}")
            continue
        issues.extend(f"commands[{index}] {item}" for item in inline_env_issues)
        if not tokens:
            issues.append(f"commands[{index}] command 为空")
        guard = command_is_dangerous(tokens)
        if guard:
            issues.append(f"commands[{index}] 被命令守卫拒绝：{guard}")
        if repo_path is not None and run_dir is not None:
            try:
                cwd = resolve_command_cwd(row, repo_path, run_dir)
                boundary = command_boundary_issues(tokens, cwd, run_dir)
                env_boundary = command_env_boundary_issues(merged_env, run_dir, cwd=cwd)
                issues.extend(f"commands[{index}] {item}" for item in [*boundary, *env_boundary])
            except Exception as exc:
                issues.append(f"commands[{index}] cwd/path 越界或无法解析：{type(exc).__name__}: {exc}")
        try:
            timeout = int(row.get("timeout_sec") or 3600)
            if timeout <= 0:
                issues.append(f"commands[{index}] timeout_sec 必须大于 0")
        except Exception:
            issues.append(f"commands[{index}] timeout_sec 不是整数")
    criteria = plan.get("success_criteria")
    criteria_schema_issues = success_criteria_issues(criteria)
    issues.extend(criteria_schema_issues)
    if not conda_setup_phase_present:
        if optional_conda_setup_phases:
            issues.append("环境计划只有 required=false 的 Conda 环境创建/安装/依赖阶段；支撑批准的 Conda setup 必须是必需命令：" + ", ".join(optional_conda_setup_phases[:5]))
        else:
            issues.append("环境计划缺少 Conda 环境创建/安装/依赖阶段；批准需要独立环境部署证据")
    if not conda_verify_phase_present:
        if optional_conda_verify_phases:
            issues.append("环境计划只有 required=false 的 verify/import/smoke/reproduce_full 等导入或运行验证阶段；支撑批准的 Conda verify 必须是必需命令：" + ", ".join(optional_conda_verify_phases[:5]))
        else:
            issues.append("环境计划缺少 verify/import/smoke/reproduce_full 等导入或运行验证阶段；批准需要证明环境可在本机运行")
    phases_lower = {phase.lower() for phase in phases}
    if require_full_reproduction and not dataset_phase_present:
        if optional_dataset_phases:
            issues.append("默认完整复现模式只有 required=false 的数据集准备/下载/预处理阶段；支撑批准的数据集阶段必须是必需命令：" + ", ".join(optional_dataset_phases[:5]))
        else:
            issues.append("默认完整复现模式要求 commands 包含必需的数据集准备/下载/预处理阶段；批准需要真实数据集准备证据")
    if require_full_reproduction and "reproduce_full" not in phases_lower:
        issues.append("默认完整复现模式要求 commands 包含 phase=reproduce_full")
    if require_full_reproduction and "reproduce_full" in phases_lower and not reproduce_full_required:
        issues.append("默认完整复现模式要求 phase=reproduce_full 的命令必须是 required=true")
    return issues


def validation_receipt(issues: list[str], round_dir: Path) -> dict[str, Any]:
    return {
        "phase": "plan_validation",
        "status": "failed",
        "return_code": 2,
        "required": True,
        "command": "environment internal plan validation",
        "stderr_tail": "\n".join(issues),
        "log_path": str(round_dir / "logs" / "00_plan_validation.log"),
    }

FULL_REPRODUCTION_DEPENDENT_PHASE_TOKENS = (
    "verify_output",
    "verify_outputs",
    "check_output",
    "check_outputs",
    "output",
    "outputs",
    "result",
    "results",
    "metric",
    "metrics",
    "eval",
    "evaluate",
    "evaluation",
    "benchmark",
    "checkpoint",
    "loss_curve",
)


def _row_depends_on_reproduce_full(row: dict[str, Any]) -> bool:
    depends_on = row.get("depends_on") or row.get("after") or row.get("requires")
    if isinstance(depends_on, str):
        values = [depends_on]
    elif isinstance(depends_on, list):
        values = [str(item) for item in depends_on]
    else:
        values = []
    return any(str(item).strip().lower() == "reproduce_full" for item in values)


def _phase_is_full_reproduction_dependent(phase: str) -> bool:
    normalized = str(phase or "").strip().lower().replace("-", "_")
    return any(token in normalized for token in FULL_REPRODUCTION_DEPENDENT_PHASE_TOKENS)


def command_rows(plan: dict[str, Any], include_full: bool, default_timeout: int = 3600) -> list[dict[str, Any]]:
    rows = plan.get("commands") if isinstance(plan.get("commands"), list) else []
    normalized: list[dict[str, Any]] = []
    skipped_full_reproduction = False
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            row = {"phase": "unspecified", "command": row}
        phase = str(row.get("phase") or "unspecified").strip()
        phase_lower = phase.lower()
        if not include_full:
            if phase_lower == "reproduce_full":
                skipped_full_reproduction = True
                continue
            if _row_depends_on_reproduce_full(row) or (skipped_full_reproduction and _phase_is_full_reproduction_dependent(phase)):
                continue
        command, env_values, inline_env_issues, inline_env = normalized_command_and_env(row)
        normalized.append({
            "index": index,
            "phase": phase,
            "command": command,
            "cwd": row.get("cwd") or "repo",
            "timeout_sec": int(row.get("timeout_sec") or default_timeout),
            "required": command_required(row),
            "env": env_values,
            "inline_env_keys": sorted(inline_env.keys()),
            "inline_env_issues": inline_env_issues,
        })
    return normalized



def normalize_repository_command_for_execution(row: dict[str, Any], command: list[str], repo_path: Path, run_dir: Path) -> tuple[list[str], list[dict[str, str]]]:
    return command, []


def command_environment(command_env: dict[str, str], repo_path: Path, row_env: dict[str, Any]) -> dict[str, str]:
    effective_env = dict(command_env)
    if repo_path.exists():
        repo = str(repo_path.expanduser().resolve())
        existing = str(effective_env.get("PYTHONPATH") or "")
        effective_env["PYTHONPATH"] = repo if not existing else repo + os.pathsep + existing
    effective_env.update({str(key): str(value) for key, value in row_env.items()})
    return effective_env


def _receipt_return_code(receipt: dict[str, Any]) -> int | None:
    try:
        return int(receipt.get("return_code"))
    except Exception:
        return None


def _receipt_phase(receipt: dict[str, Any]) -> str:
    return str(receipt.get("phase") or "").strip().lower()


def _reusable_command_receipt_key(phase: str, command: list[str]) -> tuple[str, str] | None:
    normalized_phase = str(phase or "").strip().lower()
    if not normalized_phase or normalized_phase == "reproduce_full" or conda_command_creates_env(command):
        return None
    try:
        normalized_command = command_text(command)
    except Exception:
        return None
    if not normalized_command.strip():
        return None
    return normalized_phase, normalized_command


def build_reusable_command_receipt_index(previous_rounds: list[dict[str, Any]], run_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for round_record in previous_rounds:
        if not isinstance(round_record, dict):
            continue
        receipts_path_text = str(round_record.get("receipts_path") or "").strip()
        if not receipts_path_text:
            continue
        try:
            receipts_path = ensure_within(Path(receipts_path_text), run_dir)
        except ValueError:
            continue
        receipts = read_json(receipts_path, [])
        if not isinstance(receipts, list):
            continue
        for receipt in receipts:
            if not isinstance(receipt, dict):
                continue
            if receipt.get("reused_receipt") is True:
                continue
            if receipt.get("required") is False:
                continue
            if str(receipt.get("status") or "").strip().lower() != "passed":
                continue
            if _receipt_return_code(receipt) != 0:
                continue
            phase = _receipt_phase(receipt)
            if phase == "reproduce_full":
                continue
            command = str(receipt.get("command") or "").strip()
            if not phase or not command:
                continue
            prefix = str(receipt.get("conda_env_prefix") or "").strip()
            if prefix and conda_command_creates_env(command) and receipt.get("existing_prefix_reused") is not True:
                for key, cached in list(index.items()):
                    if str(cached.get("conda_env_prefix") or "").strip() == prefix:
                        index.pop(key, None)
                continue
            key = _reusable_command_receipt_key(phase, command_tokens(command))
            if key is None:
                continue
            cached = dict(receipt)
            cached["reusable_source_receipts_path"] = str(receipts_path)
            cached["reusable_source_round"] = round_record.get("round")
            index[key] = cached
    return index


GIT_CLONE_TRANSIENT_ERROR_PATTERNS = (
    "gnutls recv error",
    "the tls connection was non-properly terminated",
    "failed to connect to github.com port 443",
    "couldn't connect to server",
    "connection timed out",
    "connection reset by peer",
    "early eof",
    "http/2 stream",
    "rpc failed",
    "remote end hung up unexpectedly",
)
GIT_CLONE_PERMANENT_ERROR_PATTERNS = (
    "repository not found",
    "not found",
    "authentication failed",
    "could not read username",
    "permission denied",
    "access denied",
)


def _is_git_clone_command(command: list[str]) -> bool:
    return len(command) >= 3 and Path(str(command[0])).name == "git" and str(command[1]) == "clone"


def _git_clone_destination(command: list[str]) -> str:
    if not _is_git_clone_command(command) or len(command) < 4:
        return ""
    return str(command[-1])


def _git_clone_transient_failure(receipt: dict[str, Any]) -> bool:
    if _receipt_return_code(receipt) == 0:
        return False
    text = " ".join(str(receipt.get(key) or "") for key in ("stdout_tail", "stdout_head", "stderr_tail")).lower()
    if not text:
        return False
    if any(pattern in text for pattern in GIT_CLONE_PERMANENT_ERROR_PATTERNS):
        return False
    return any(pattern in text for pattern in GIT_CLONE_TRANSIENT_ERROR_PATTERNS)


def _remove_partial_git_clone(destination: str, run_dir: Path) -> str:
    if not destination:
        return ""
    try:
        target = ensure_within(Path(destination), run_dir)
    except Exception:
        return ""
    if target.exists():
        shutil.rmtree(target)
        return str(target)
    return ""


def run_logged_with_git_clone_retries(
    command: list[str],
    *,
    cwd: Path,
    log_path: Path,
    timeout_sec: int | None,
    env: dict[str, str],
    required: bool,
    run_dir: Path,
) -> dict[str, Any]:
    if not _is_git_clone_command(command):
        return run_logged(command, cwd=cwd, log_path=log_path, timeout_sec=timeout_sec, env=env, required=required)
    max_attempts = max(1, int(os.environ.get("TASTE_GIT_CLONE_MAX_ATTEMPTS", "3") or "3"))
    receipts: list[dict[str, Any]] = []
    attempt_commands: list[list[str]] = []
    destination = _git_clone_destination(command)
    for attempt in range(1, max_attempts + 1):
        attempt_log_path = log_path if attempt == 1 else log_path.with_name(f"{log_path.stem}.retry{attempt}{log_path.suffix}")
        attempt_command = list(command)
        if attempt > 1 and "-c" not in attempt_command[:3]:
            attempt_command = [attempt_command[0], "-c", "http.version=HTTP/1.1", *attempt_command[1:]]
        attempt_commands.append(attempt_command)
        if attempt > 1:
            removed = _remove_partial_git_clone(destination, run_dir)
            with attempt_log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"# retry_attempt={attempt}/{max_attempts}; transient git clone failure detected in previous attempt\n")
                if removed:
                    handle.write(f"# removed_partial_clone={removed}\n")
        receipt = run_logged(attempt_command, cwd=cwd, log_path=attempt_log_path, timeout_sec=timeout_sec, env=env, required=required)
        receipt["git_clone_attempt"] = attempt
        receipt["git_clone_max_attempts"] = max_attempts
        receipts.append(receipt)
        if _receipt_return_code(receipt) == 0:
            break
        if not _git_clone_transient_failure(receipt):
            break
        if attempt < max_attempts:
            time.sleep(min(10, attempt * 2))
    final = dict(receipts[-1])
    if len(receipts) > 1:
        final["git_clone_retried"] = True
        final["git_clone_attempt_count"] = len(receipts)
        final["git_clone_attempt_receipts"] = receipts
        final["git_clone_attempt_commands"] = [command_text(item) for item in attempt_commands]
        final["log_path"] = str(log_path)
        summary_lines = [f"$ {command_text(command)}", f"[git clone transient retry summary] attempts={len(receipts)} final_status={final.get('status')} return_code={final.get('return_code')}"]
        for receipt in receipts:
            summary_lines.append(f"attempt {receipt.get('git_clone_attempt')}: status={receipt.get('status')} return_code={receipt.get('return_code')} log={receipt.get('log_path')}")
            tail = str(receipt.get("stdout_tail") or receipt.get("stderr_tail") or "").strip()
            if tail:
                summary_lines.append(tail[-1200:])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(summary_lines).rstrip() + "\n", encoding="utf-8")
    return final


def reusable_command_receipt(source_receipt: dict[str, Any], log_path: Path) -> dict[str, Any]:
    receipt = dict(source_receipt)
    original_log_path = str(source_receipt.get("log_path") or "")
    receipt.update({
        "status": "passed",
        "return_code": 0,
        "log_path": str(log_path),
        "reused_receipt": True,
        "reused_from_round": source_receipt.get("reusable_source_round"),
        "reused_from_receipts_path": source_receipt.get("reusable_source_receipts_path"),
        "reused_from_log_path": original_log_path,
        "started_at": utc_now(),
        "finished_at": utc_now(),
    })
    receipt.pop("reusable_source_round", None)
    receipt.pop("reusable_source_receipts_path", None)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        f"$ {receipt.get('command', '')}\n"
        "[复用同一 run 既有成功回执] 本命令已在前序 round 真实成功执行。\n"
        f"source_receipts_path={receipt.get('reused_from_receipts_path', '')}\n"
        f"source_log_path={original_log_path}\n",
        encoding="utf-8",
    )
    return receipt


def execute_plan_commands(plan: dict[str, Any], repo_path: Path, run_dir: Path, round_dir: Path, include_full: bool, default_timeout: int, command_env: dict[str, str], reusable_receipts: dict[tuple[str, str], dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    conda_exe = find_conda_executable()
    default_env_name = str(plan.get("env_name") or "").strip()
    active_env_name = default_env_name
    receipts: list[dict[str, Any]] = []
    for row in command_rows(plan, include_full, default_timeout=default_timeout):
        requested_env_name = conda_env_name_from_command(row.get("command"))
        effective_env_name = requested_env_name or active_env_name or default_env_name
        env_prefix = env_prefix_for(run_dir, effective_env_name)
        env_prefix.parent.mkdir(parents=True, exist_ok=True)
        command = rewrite_command(row.get("command"), conda_exe, effective_env_name, env_prefix)
        command, repository_migrations = normalize_repository_command_for_execution(row, command, repo_path, run_dir)
        uses_conda_prefix = command_uses_conda_prefix(command, env_prefix)
        issue = command_is_dangerous(command)
        if row.get("inline_env_issues") and not issue:
            issue = "；".join(str(item) for item in row.get("inline_env_issues", []) if str(item).strip())
        phase = slugify(row.get("phase") or "phase")
        log_path = round_dir / "logs" / f"{row['index']:02d}_{phase}.log"
        script_migrations: list[dict[str, str]] = list(repository_migrations)
        try:
            cwd = resolve_command_cwd(row, repo_path, run_dir)
            command, run_repo_migrations = normalize_run_repo_command_paths(command, cwd, run_dir)
            script_migrations.extend(run_repo_migrations)
            missing_script_issue = missing_shell_script_issue(command, cwd, run_dir)
            boundary_issues = command_boundary_issues(command, cwd, run_dir)
            if missing_script_issue:
                boundary_issues.append(missing_script_issue)
            env_boundary_issues = command_env_boundary_issues(row.get("env"), run_dir, cwd=cwd)
        except Exception as exc:
            cwd = run_dir
            boundary_issues = [f"cwd/path 越界或无法解析：{type(exc).__name__}: {exc}"]
            env_boundary_issues = []
        if [*boundary_issues, *env_boundary_issues] and not issue:
            issue = "；".join([*boundary_issues, *env_boundary_issues])
        if issue:
            receipt = {
                "phase": row.get("phase"),
                "command": command_text(command),
                "status": "blocked_by_guard",
                "return_code": 126,
                "stderr_tail": issue,
                "required": row.get("required"),
                "log_path": str(log_path),
                "env_keys": sorted(row.get("env", {}).keys()),
                "inline_env_keys": row.get("inline_env_keys", []),
                "conda_env_prefix": str(env_prefix),
                "uses_conda_prefix": uses_conda_prefix,
                "script_migrations": script_migrations,
            }
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"$ {command_text(command)}\n[命令边界守卫阻止] {issue}\n", encoding="utf-8")
        else:
            reuse_key = _reusable_command_receipt_key(str(row.get("phase") or ""), command)
            if reuse_key and reusable_receipts and reuse_key in reusable_receipts:
                receipt = reusable_command_receipt(reusable_receipts[reuse_key], log_path)
                receipt["phase"] = row.get("phase")
                receipt["conda_env_prefix"] = str(env_prefix)
                receipt["uses_conda_prefix"] = uses_conda_prefix
                receipt["script_migrations"] = script_migrations
                receipt["env_keys"] = sorted((row.get("env") if isinstance(row.get("env"), dict) else {}).keys())
                receipt["inline_env_keys"] = row.get("inline_env_keys", [])
            elif conda_command_creates_env(command) and conda_prefix_has_python(env_prefix):
                receipt = existing_conda_prefix_receipt(command, log_path, env_prefix, row, uses_conda_prefix, script_migrations)
            else:
                row_env = row.get("env") if isinstance(row.get("env"), dict) else {}
                effective_env = command_environment(command_env, repo_path, row_env)
                cwd.mkdir(parents=True, exist_ok=True)
                receipt = run_logged_with_git_clone_retries(
                    command,
                    cwd=cwd,
                    log_path=log_path,
                    timeout_sec=row.get("timeout_sec"),
                    env=effective_env,
                    required=bool(row.get("required")),
                    run_dir=run_dir,
                )
                receipt["phase"] = row.get("phase")
                receipt["conda_env_prefix"] = str(env_prefix)
                receipt["uses_conda_prefix"] = uses_conda_prefix
                receipt["script_migrations"] = script_migrations
                receipt["env_keys"] = sorted(row_env.keys())
                receipt["inline_env_keys"] = row.get("inline_env_keys", [])
        receipts.append(receipt)
        if receipt.get("return_code") == 0 and requested_env_name and conda_command_sets_active_env(row.get("command")):
            active_env_name = requested_env_name
        if receipt.get("return_code") != 0 and row.get("required"):
            break
    write_json(round_dir / "command_receipts.json", receipts)
    return receipts


def _approval_gate_from_decision(decision: dict[str, Any]) -> dict[str, Any]:
    gate = decision.get("approval_gate")
    if isinstance(gate, dict) and gate:
        return gate
    verdict = decision.get("verdict") if isinstance(decision.get("verdict"), dict) else {}
    gate = verdict.get("approval_gate") if isinstance(verdict.get("approval_gate"), dict) else {}
    return gate if isinstance(gate, dict) else {}


def final_decision_payload(run_id: str, run_dir: Path, normalized_plan: dict[str, Any], repo_info: dict[str, Any], machine: dict[str, Any], rounds: list[dict[str, Any]], verdict: dict[str, Any]) -> dict[str, Any]:
    verdict = enforce_reject_evidence(verdict)
    decision = str(verdict.get("decision") or "continue_repair").strip()
    allow_next = bool(decision == "approve" and verdict.get("allow_next_module") is True)
    normalized_decision = "approve" if allow_next else ("reject" if decision == "reject" else "continue_repair")
    exit_code = 0 if allow_next else (20 if decision == "reject" else 30)
    return {
        "schema_version": "environment.deployment_decision.v1",
        "decision_policy_version": DECISION_POLICY_VERSION,
        "created_at": utc_now(),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "decision": normalized_decision,
        "allow_next_module": allow_next,
        "exit_code": exit_code,
        "experiment_plan": {k: normalized_plan.get(k) for k in ["title", "topic", "paper_url", "repo_candidates", "target_metrics"]},
        "repo": repo_info,
        "machine_summary": {
            "hostname": machine.get("hostname"),
            "active_conda_env": machine.get("active_conda_env"),
            "conda_executable": machine.get("conda_executable"),
            "gpu": machine.get("gpu"),
        },
        "rounds": rounds,
        "verdict": verdict,
        "approval_gate": verdict.get("approval_gate") if isinstance(verdict.get("approval_gate"), dict) else {},
        "repair_loop": {
            "mode": "early_exit_or_not_attached",
            "terminal_reached": normalized_decision in {"approve", "reject"},
            "stop_reason": "terminal_decision" if normalized_decision in {"approve", "reject"} else "continue_repair_before_repair_loop_context",
            "rounds_total": len(rounds),
            "resume_command": "",
            "note": "主修复循环结束后会覆盖为详细 repair_loop；早退路径保留这个最小结构以统一输出口径。",
        },
        "approval_contract": {
            "approved": allow_next,
            "policy_version": DECISION_POLICY_VERSION,
            "meaning": "allow_next_module=true 表示本模块确认参考复现达到论文声明；environment_handoff.ready_for_experimenting=true 表示 environment 已交付可运行 repo/data/Conda，可进入 experimenting。",
            "hard_requirements": [
                "真实 GitHub 仓库、仓库 README/文档/配置/入口证据和论文/训练配置证据",
                "Conda 环境 prefix 位于本次 run 目录，且依赖安装和导入/运行验证成功",
                "本机资源适配评估通过，论文硬件/运行要求已与本机 GPU/CPU/CUDA/显存和必要适配动作对齐",
                "必需命令全部成功",
                "reproduce_full 成功回执",
                "论文配置对齐表证明关键数据集/训练/指标配置已按论文实现或有合理本机适配",
                "paper_claims_verified=true 且 reproduction_success=true",
                "success_criteria 必须有指标名、比较符、可解析目标值和论文/README 来源，并逐项绑定到 paper_evidence.target_metrics",
                "本地指标解析通过或结构化 metric_evidence 证据通过",
                "论文上下文必须包含可审计来源、目标指标和训练/结果信号",
                "工作区审计通过",
            ],
        },
    }



def attach_paper_summary(decision: dict[str, Any], run_dir: Path, paper_evidence: dict[str, Any]) -> dict[str, Any]:
    decision["paper_evidence_path"] = str(run_dir / "paper_evidence.json")
    decision["paper_evidence_summary"] = {
        "has_paper_context": bool(paper_evidence.get("has_paper_context")),
        "paper_url": paper_evidence.get("paper_url", ""),
        "target_metric_count": len(paper_evidence.get("target_metrics") or []),
        "claim_signal_count": len(paper_evidence.get("paper_claims_or_training_signals") or []),
        "text_block_count": len(paper_evidence.get("text_blocks") or []),
    }
    return decision

def resolve_repair_loop_settings(dry_run: bool, requested_until_terminal: bool, max_repair_rounds: int | None) -> dict[str, Any]:
    bounded_requested = max_repair_rounds is not None
    try:
        effective_max_repair_rounds = max(1, int(max_repair_rounds if max_repair_rounds is not None else 3))
    except Exception:
        effective_max_repair_rounds = 3
    defaulted_until_terminal = bool(not dry_run and not requested_until_terminal and not bounded_requested)
    return {
        "until_terminal": bool(requested_until_terminal or defaulted_until_terminal),
        "max_repair_rounds": effective_max_repair_rounds,
        "bounded_requested": bounded_requested,
        "defaulted_until_terminal": defaulted_until_terminal,
    }


def main() -> int:
    require_public_entrypoint()
    parser = argparse.ArgumentParser(description="给定实验 plan，让 Claude Code 自主部署环境并裁决参考复现。")
    parser.add_argument("--project", required=True, help="Framework 传入的项目 ID。")
    parser.add_argument("--plan", required=True, help="实验 plan JSON 路径。")
    parser.add_argument("--conda-env", default="", help="Web/Framework 已保存的实验 Conda 环境名；传入后必须固定使用。")
    parser.add_argument("--run-dir", default=os.environ.get("ENVIRONMENT_RUN_DIR", ""), help=argparse.SUPPRESS)
    parser.add_argument("--max-repair-rounds", type=int, default=None, help="显式设置后进入有限修复轮次模式；不设置时非 dry-run 默认持续修复直到 approve/reject。")
    parser.add_argument("--until-terminal", action="store_true", help="持续修复直到 approve/reject；可配合 --max-total-rounds 设总轮数上限。")
    parser.add_argument("--max-total-rounds", type=int, default=0, help="--until-terminal 模式的总轮数上限，0 表示不设轮数上限。")
    parser.add_argument("--claude-timeout-sec", type=int, default=2400, help="单次 Claude Code 调用超时。")
    parser.add_argument("--command-timeout-sec", type=int, default=3600, help="命令默认超时（Claude 未显式给出时使用）。")
    parser.add_argument("--skip-full-reproduction", action="store_true", help="仅用于调试/烟测：跳过 reproduce_full，因此不会批准进入下一模块。")
    parser.add_argument("--dry-run", action="store_true", help="只生成提示词/结构，不调用 Claude、不执行重命令。")
    args = parser.parse_args()
    if args.max_total_rounds < 0:
        raise SystemExit("--max-total-rounds 不能小于 0")
    repair_settings = resolve_repair_loop_settings(
        dry_run=bool(args.dry_run),
        requested_until_terminal=bool(args.until_terminal),
        max_repair_rounds=args.max_repair_rounds,
    )
    effective_until_terminal = bool(repair_settings["until_terminal"])
    effective_max_repair_rounds = int(repair_settings["max_repair_rounds"])
    if args.dry_run and effective_until_terminal and args.max_total_rounds <= 0:
        raise SystemExit("dry-run 与持续修复模式连用时必须设置 --max-total-rounds，避免无限空转。")

    plan_path = Path(args.plan).expanduser().resolve()
    project_root = (REPO_ROOT / "projects" / args.project).resolve()
    try:
        project_root.relative_to((REPO_ROOT / "projects").resolve())
    except ValueError as exc:
        raise SystemExit(f"Environment project escapes projects/: {args.project}") from exc
    if project_root.name != args.project or not project_root.is_dir():
        raise SystemExit(f"Environment project does not exist: {args.project}")
    configured_env_name = str(args.conda_env or "").strip()
    if configured_env_name and not re.fullmatch(r"[A-Za-z0-9_.-]+", configured_env_name):
        raise SystemExit("--conda-env 只能包含字母、数字、点、下划线和连字符")
    raw_plan = load_experiment_plan(plan_path)
    normalized = normalize_plan(raw_plan, plan_path)
    normalized["project"] = args.project
    if configured_env_name:
        normalized["requested_conda_env"] = configured_env_name
    run_dir = resolve_run_dir(args.run_dir)
    run_id = run_dir.name
    claude_env = isolated_runtime_env(run_dir, isolate_home=False)
    command_env = isolated_runtime_env(run_dir, isolate_home=True)
    baseline_outside_paths = _workspace_write_audit_baseline(run_dir)
    logs_dir = run_dir / "logs"
    repos_dir = run_dir / "repos"
    write_json(run_dir / "input_plan.normalized.json", normalized)
    write_json(run_dir / "input_plan.raw.json", raw_plan)
    fixed_env_name = configured_env_name
    write_json(run_dir / "conda_environment.json", {
        "project": args.project,
        "env_name": fixed_env_name,
        "source": "configured_input" if fixed_env_name else "pending_claude_selection",
        "fixed": bool(fixed_env_name),
    })

    emit_progress("evidence", "Collecting machine and paper evidence.")
    machine = detect_machine_profile()
    write_json(run_dir / "machine_profile.json", machine)
    paper_evidence = collect_paper_evidence(normalized, run_dir, allow_network=not args.dry_run, timeout_sec=90)

    repo_candidates = list(normalized.get("repo_candidates") or [])
    repo_spec_lookup = repo_specs_by_url(normalized)
    repo_selection_review: dict[str, Any] = {}
    if repo_candidates and not args.dry_run:
        emit_progress("repository_review", "Environment controller is reviewing repository candidates.")
        review_path = run_dir / "claude_repo_candidate_review.json"
        review_result = run_claude_json(
            prompt_repo_candidate_review(normalized, [str(item) for item in repo_candidates], review_path),
            cwd=run_dir,
            expected_json_path=review_path,
            log_path=logs_dir / "claude_repo_candidate_review.log",
            timeout_sec=args.claude_timeout_sec,
            add_dirs=_claude_add_dirs(run_dir),
            dry_run=False,
            system_prompt="你是 TASTE Environment 仓库候选审阅代理。必须只审阅 prompt 中给出的候选，必须只输出严格 JSON。",
            env=claude_env,
            project=args.project,
            project_root=project_root,
        )
        repo_selection_review = review_result
        reviewed = review_result.get("json") if isinstance(review_result.get("json"), dict) else {}
        review_status = str(reviewed.get("status") or "").strip().lower()
        if review_status == "reject":
            verdict = {
                "decision": "continue_repair",
                "allow_next_module": False,
                "reject_reason": reviewed.get("reject_reason") or "Claude Code 判定 plan 中的 GitHub 仓库候选不可信",
                "failure_taxonomy": [{"category": "repository_code", "evidence": reviewed.get("evidence") or [reviewed.get("reject_reason") or "候选仓库不可信"], "repairable": True}],
            }
            decision = final_decision_payload(run_id, run_dir, normalized, {"repo_candidates": repo_candidates, "repo_selection_review": repo_selection_review}, machine, [], verdict)
            write_json(run_dir / "repo_info.json", decision["repo"])
            return finalize_and_write_decision(decision, run_dir, paper_evidence, baseline_outside_paths)
        selected_candidates, review_issues = repo_candidates_after_review([str(item) for item in repo_candidates], review_result)
        if not selected_candidates:
            verdict = {
                "decision": "continue_repair",
                "allow_next_module": False,
                "reject_reason": "Claude Code 仓库候选审阅结果未通过后端硬校验，且 plan 中没有可直接克隆的 GitHub 候选。",
                "failure_taxonomy": [{"category": "repository_code", "evidence": review_issues or ["没有可克隆 GitHub 候选"], "repairable": True}],
            }
            decision = final_decision_payload(run_id, run_dir, normalized, {"repo_candidates": repo_candidates, "repo_selection_review": repo_selection_review, "repo_selection_validation_issues": review_issues}, machine, [], verdict)
            write_json(run_dir / "repo_info.json", decision["repo"])
            return finalize_and_write_decision(decision, run_dir, paper_evidence, baseline_outside_paths)
        repo_candidates = selected_candidates
        if review_issues:
            repo_selection_review["validation_issues"] = review_issues
    if not repo_candidates:
        emit_progress("repository_discovery", "Environment controller is identifying the evidence-backed repository.")
        discovery_path = run_dir / "claude_repo_discovery.json"
        repo_discovery_result = run_claude_json(
            prompt_repo_discovery(normalized, discovery_path),
            cwd=run_dir,
            expected_json_path=discovery_path,
            log_path=logs_dir / "claude_repo_discovery.log",
            timeout_sec=args.claude_timeout_sec,
            add_dirs=_claude_add_dirs(run_dir),
            dry_run=args.dry_run,
            system_prompt="你是 TASTE Environment 仓库发现代理。必须只基于 prompt 证据选择 GitHub 仓库，必须只输出严格 JSON。",
            env=claude_env,
            project=args.project,
            project_root=project_root,
        )
        discovered = repo_discovery_result.get("json") if isinstance(repo_discovery_result.get("json"), dict) else {}
        discovered_repo_url, discovery_issues = validate_discovered_repo(discovered)
        if not discovery_issues:
            discovered_candidates = [{"url": discovered_repo_url, "source": "repo_url", "evidence": discovered.get("evidence", [])}]
        else:
            discovered_candidates = discovered_repo_candidates(discovered)
        for candidate in discovered_candidates:
            candidate_url = str(candidate.get("url") or "").strip()
            if not candidate_url or not is_github_repo_url(candidate_url):
                continue
            if canonical_repo_url(candidate_url) in {canonical_repo_url(item) for item in repo_candidates}:
                continue
            repo_candidates.append(candidate_url)
            repo_spec_lookup[canonical_repo_url(candidate_url)] = {
                "url": candidate_url,
                "source": f"claude_repo_discovery.{candidate.get('source') or 'candidate'}",
                "confidence": discovered.get("confidence", ""),
                "evidence": candidate.get("evidence") or discovered.get("evidence", []),
                "discovery_validation_issues": discovery_issues,
            }
        if discovery_issues and not args.dry_run:
            verdict = {"decision": "continue_repair", "allow_next_module": False, "reject_reason": discovered.get("reject_reason") or "Claude Code 未能确认可信 GitHub 仓库", "failure_taxonomy": [{"category": "repository_code", "evidence": discovery_issues or [discovered.get("reject_reason") or "缺少可信仓库"], "repairable": True}]}
            decision = final_decision_payload(run_id, run_dir, normalized, {"repo_discovery": repo_discovery_result, "repo_discovery_validation_issues": discovery_issues}, machine, [], verdict)
            write_json(run_dir / "repo_info.json", decision["repo"])
            return finalize_and_write_decision(decision, run_dir, paper_evidence, baseline_outside_paths)

    github_candidates = [url for url in repo_candidates if is_github_repo_url(str(url))]
    rejected_repo_candidates = [url for url in repo_candidates if url not in github_candidates]
    if repo_candidates and not github_candidates:
        decision = github_only_rejection(run_id, run_dir, normalized, str(repo_candidates[0]), machine)
        return finalize_and_write_decision(decision, run_dir, paper_evidence, baseline_outside_paths)

    selected_repo_url = github_candidates[0] if github_candidates else ""
    clone_attempts: list[dict[str, Any]] = []
    repo_info: dict[str, Any]
    if github_candidates and not args.dry_run:
        emit_progress("repository_clone", "Cloning and verifying repository candidates.")
        repo_info = {}
        for candidate_url in github_candidates:
            candidate_spec = repo_spec_for_url(str(candidate_url), repo_spec_lookup)
            branch_or_tag, commit = clone_ref_from_spec(candidate_spec)
            attempt = clone_or_reuse(str(candidate_url), repos_dir=repos_dir, log_dir=logs_dir, branch=branch_or_tag, commit=commit, timeout_sec=900, env=claude_env)
            attempt["repo_candidate_spec"] = candidate_spec
            clone_attempts.append(attempt)
            if attempt.get("exists") and int((attempt.get("clone_receipt") or {}).get("return_code") or 0) == 0 and not repo_info:
                repo_info = dict(attempt)
                selected_repo_url = str(candidate_url)
        if not repo_info:
            verdict = {
                "decision": "continue_repair",
                "allow_next_module": False,
                "reject_reason": "所有 GitHub 候选仓库都未能克隆；可能是网络、权限、仓库不存在或候选错误，需要 Claude Code 继续修复/重新判定仓库。",
                "failure_taxonomy": [{"category": "repository_code", "evidence": ["GitHub clone failed for all candidates"], "repairable": True}],
            }
            decision = final_decision_payload(run_id, run_dir, normalized, {"repo_candidates": github_candidates, "clone_attempts": clone_attempts, "rejected_non_github_candidates": rejected_repo_candidates}, machine, rounds=[], verdict=verdict)
            write_json(run_dir / "repo_info.json", decision["repo"])
            return finalize_and_write_decision(decision, run_dir, paper_evidence, baseline_outside_paths)
    else:
        repo_info = {"repo_url": selected_repo_url, "repo_path": str(repos_dir / "dry_run_repo"), "exists": False, "dry_run": True, "repo_candidate_spec": repo_spec_for_url(selected_repo_url, repo_spec_lookup) if selected_repo_url else {}}
    if repo_selection_review:
        repo_info["repo_selection_review"] = repo_selection_review
    if clone_attempts:
        repo_info["clone_attempts"] = clone_attempts
    if rejected_repo_candidates:
        repo_info["rejected_non_github_candidates"] = rejected_repo_candidates

    repo_path = Path(str(repo_info.get("repo_path") or ""))
    available_repo_paths = successful_clone_repo_rows(clone_attempts, str(repo_path)) if clone_attempts else []
    if available_repo_paths:
        repo_info["available_repo_paths"] = available_repo_paths
        repo_info["auxiliary_repo_paths"] = [row for row in available_repo_paths if row.get("role") == "auxiliary"]
    write_json(run_dir / "repo_info.json", repo_info)

    repo_evidence = collect_repo_evidence(repo_path) if repo_path.exists() else {"repo_path": str(repo_path), "readmes": [], "config_files": [], "dry_run": args.dry_run}
    if available_repo_paths:
        repo_evidence["available_repo_paths"] = available_repo_paths
        repo_evidence["auxiliary_repositories"] = collect_auxiliary_repo_evidence(available_repo_paths, repo_path)
    write_json(run_dir / "repo_evidence.json", repo_evidence)

    rounds: list[dict[str, Any]] = []
    previous_rounds: list[dict[str, Any]] = []
    final_verdict: dict[str, Any] = {"decision": "continue_repair", "allow_next_module": False, "reason": "尚未开始"}

    start_round = len(previous_rounds) + 1
    if effective_until_terminal:
        end_round = (args.max_total_rounds + 1) if args.max_total_rounds > 0 else sys.maxsize
    else:
        end_round = start_round + effective_max_repair_rounds
    for round_index in range(start_round, end_round):
        round_dir = run_dir / f"round_{round_index:02d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        emit_progress("planning", "Environment controller is preparing the deployment and repair plan.", round_index=round_index)
        env_plan_path = round_dir / f"claude_environment_plan_round_{round_index:02d}.json"
        env_plan_result = run_claude_json(
            prompt_environment_plan(normalized, machine, repo_evidence, paper_evidence, previous_rounds, env_plan_path, round_index, fixed_env_name),
            cwd=run_dir,
            expected_json_path=env_plan_path,
            log_path=round_dir / "logs" / "claude_environment_plan.log",
            timeout_sec=args.claude_timeout_sec,
            add_dirs=_claude_add_dirs(run_dir, repo_path) if repo_path.exists() else _claude_add_dirs(run_dir),
            dry_run=args.dry_run,
            system_prompt="你是 TASTE environment 的后端环境部署代理，必须输出严格 JSON。",
            env=claude_env,
            project=args.project,
            project_root=project_root,
        )
        env_plan = env_plan_result.get("json") if isinstance(env_plan_result.get("json"), dict) else {}
        if args.dry_run:
            env_plan = {"status": "dry_run", "commands": [], "success_criteria": [], "env_name": "dry_run_env"}
        proposed_env_name = str(env_plan.get("env_name") or "").strip()
        if not fixed_env_name and re.fullmatch(r"[A-Za-z0-9_.-]+", proposed_env_name):
            fixed_env_name = proposed_env_name
        elif not fixed_env_name and proposed_env_name:
            env_plan["rejected_env_name"] = proposed_env_name
            env_plan["env_name"] = ""
            write_json(run_dir / "conda_environment.json", {
                "project": args.project,
                "env_name": "",
                "source": "pending_claude_selection",
                "fixed": False,
                "rejected_env_name": proposed_env_name,
                "validation_issue": "env_name must match [A-Za-z0-9_.-]+",
            })
        if fixed_env_name:
            env_plan["env_name"] = fixed_env_name
            write_json(run_dir / "conda_environment.json", {
                "project": args.project,
                "env_name": fixed_env_name,
                "source": "configured_input" if configured_env_name else "environment_controller_claude",
                "fixed": True,
            })
        write_json(env_plan_path, env_plan)
        round_record: dict[str, Any] = {"round": round_index, "env_plan_path": str(env_plan_path), "claude_plan_call": env_plan_result, "env_plan_status": env_plan.get("status")}
        if str(env_plan.get("status") or "").strip() == "reject":
            final_verdict = enforce_reject_evidence({
                "decision": "reject",
                "allow_next_module": False,
                "reject_reason": env_plan.get("reject_reason") or "Claude Code 判定仓库/数据/论文/算力不可用",
                "failure_taxonomy": env_plan.get("unreliable_basis") or [{"category": "unknown", "evidence": [env_plan.get("reject_reason") or "拒绝但缺少细分类证据"], "repairable": False}],
            })
            round_record["verdict"] = final_verdict
            rounds.append(round_record)
            if final_verdict.get("decision") == "reject":
                break
            previous_rounds.append(round_record)
            continue

        receipts: list[dict[str, Any]] = []
        plan_validation_issues: list[str] = []
        if not args.dry_run:
            include_full_reproduction = not args.skip_full_reproduction
            plan_validation_issues = validate_environment_plan(env_plan, require_full_reproduction=include_full_reproduction, repo_path=repo_path, run_dir=run_dir)
            if plan_validation_issues:
                receipt = validation_receipt(plan_validation_issues, round_dir)
                receipts = [receipt]
                log_path = Path(str(receipt["log_path"]))
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(receipt["stderr_tail"] + "\n", encoding="utf-8")
            else:
                emit_progress("commands", "Executing the gated Environment command plan.", round_index=round_index)
                reusable_receipts = build_reusable_command_receipt_index(previous_rounds, run_dir)
                receipts = execute_plan_commands(env_plan, repo_path=repo_path, run_dir=run_dir, round_dir=round_dir, include_full=include_full_reproduction, default_timeout=args.command_timeout_sec, command_env=command_env, reusable_receipts=reusable_receipts)
        write_json(round_dir / "command_receipts.json", receipts)
        criteria_passed, metric_evidence = metric_criteria_passed(env_plan.get("success_criteria") if isinstance(env_plan.get("success_criteria"), list) else [], receipts, allowed_phases=APPROVAL_METRIC_PHASES)
        emit_progress("audit", "Environment controller is auditing this round from command receipts.", round_index=round_index)
        audit_judgement_path = round_dir / f"claude_audit_judgement_round_{round_index:02d}.json"
        audit_judgement_result = run_claude_json(
            prompt_audit_judgement(normalized, paper_evidence, env_plan, receipts, metric_evidence, audit_judgement_path),
            cwd=run_dir,
            expected_json_path=audit_judgement_path,
            log_path=round_dir / "logs" / "claude_audit_judgement.log",
            timeout_sec=args.claude_timeout_sec,
            add_dirs=_claude_add_dirs(run_dir, repo_path) if repo_path.exists() else _claude_add_dirs(run_dir),
            dry_run=args.dry_run,
            system_prompt="你是 TASTE Environment 审计裁决代理。必须只输出严格 JSON，必须只依据本轮证据裁决。",
            env=claude_env,
            project=args.project,
            project_root=project_root,
        )
        verdict = audit_judgement_result.get("json") if isinstance(audit_judgement_result.get("json"), dict) else {}
        if args.dry_run:
            verdict = {
                "decision": "continue_repair",
                "allow_next_module": False,
                "paper_claims_verified": False,
                "reproduction_success": False,
                "audit_checks": [{"name": "dry_run_execution", "passed": False, "evidence": ["dry-run 未调用 Claude 裁决实例，也未执行复现命令"]}],
                "failure_taxonomy": [{"category": "unknown", "evidence": ["dry-run 只验证流程结构"], "repairable": True}],
                "repair_plan": ["用非 dry-run 运行真实环境部署和复现命令"],
            }
        verdict = normalize_verdict(verdict)
        verdict = enforce_reject_evidence(verdict)
        verdict_metric_ok, verdict_metric_issues = verdict_metric_evidence_supports_claims(verdict, receipts, env_plan.get("success_criteria") if isinstance(env_plan.get("success_criteria"), list) else [])
        approval_gate = build_approval_gate(
            verdict=verdict,
            receipts=receipts,
            env_plan=env_plan,
            repo_info=repo_info,
            run_dir=run_dir,
            criteria_passed=criteria_passed,
            metric_evidence=metric_evidence,
            verdict_metric_ok=verdict_metric_ok,
            verdict_metric_issues=verdict_metric_issues,
        )
        verdict["approval_gate"] = approval_gate
        approval_checks = {str(row.get("name")): row for row in approval_gate.get("checks", []) if isinstance(row, dict)}
        if not approval_checks.get("metric_evidence", {}).get("passed"):
            verdict["metric_evidence_binding_issues"] = verdict_metric_issues[:10]
        if verdict.get("decision") == "approve" and not approval_gate.get("passed"):
            verdict["decision"] = "continue_repair"
            verdict["allow_next_module"] = False
            verdict.setdefault("repair_plan", []).append("后端降级：批准门槛未通过：" + "、".join(approval_gate.get("missing") or []) + "。")
        round_record.update({
            "receipts_path": str(round_dir / "command_receipts.json"),
            "receipt_count": len(receipts),
            "required_failures": [row for row in receipts if row.get("return_code") != 0 and row.get("required") is not False][:5],
            "metric_evidence": metric_evidence,
            "plan_validation_issues": plan_validation_issues,
            "audit_judgement_path": str(audit_judgement_path),
            "claude_audit_judgement_call": audit_judgement_result,
            "judgement_path": str(audit_judgement_path),
            "claude_judgement_call": audit_judgement_result,
            "verdict": verdict,
        })
        rounds.append(round_record)
        previous_rounds.append(round_record)
        final_verdict = verdict
        if verdict.get("decision") in {"approve", "reject"}:
            break

    decision = final_decision_payload(run_id, run_dir, normalized, repo_info, machine, rounds, final_verdict)
    decision = attach_environment_handoff(decision, run_dir, normalized, repo_info)
    terminal_reached = decision.get("decision") in {"approve", "reject", "environment_ready"}
    if terminal_reached:
        stop_reason = "environment_handoff_ready" if decision.get("decision") == "environment_ready" else "terminal_decision"
    elif effective_until_terminal and args.max_total_rounds > 0 and len(rounds) >= args.max_total_rounds:
        stop_reason = "max_total_rounds_reached"
    elif effective_until_terminal:
        stop_reason = "continue_repair_without_terminal_decision"
    else:
        stop_reason = "max_repair_rounds_reached"
    new_run_command = [
        "conda",
        "run",
        "-n",
        "taste",
        "python",
        "modules/environment/main.py",
        "--action",
        "deploy_from_plan",
        "--project",
        args.project,
        "--plan",
        str(plan_path),
    ]
    if fixed_env_name:
        new_run_command.extend(["--conda-env", fixed_env_name])
    if effective_until_terminal:
        new_run_command.append("--until-terminal")
        if args.max_total_rounds > 0:
            new_run_command.extend(["--max-total-rounds", str(args.max_total_rounds)])
    else:
        new_run_command.extend(["--max-repair-rounds", str(effective_max_repair_rounds)])
    if args.skip_full_reproduction:
        new_run_command.append("--skip-full-reproduction")
    decision["repair_loop"] = {
        "mode": "until_terminal" if effective_until_terminal else "bounded",
        "terminal_reached": terminal_reached,
        "stop_reason": stop_reason,
        "rounds_before_invocation": 0,
        "rounds_this_invocation": len(rounds),
        "rounds_total": len(rounds),
        "max_repair_rounds_this_invocation": None if effective_until_terminal else effective_max_repair_rounds,
        "max_total_rounds": args.max_total_rounds if effective_until_terminal else None,
        "bounded_requested": bool(repair_settings["bounded_requested"]),
        "defaulted_until_terminal": bool(repair_settings["defaulted_until_terminal"]),
        "resume_command": "",
        "new_run_command": " ".join(new_run_command),
        "resume_note": "Environment run 目录不被后续进程复用；需要继续时请按 new_run_command 发起新的 run，并审查上一个 run 的 evidence。",
        "note": "非 dry-run 且未显式设置 --max-repair-rounds 时，本模块默认持续修复直到 approve/reject；continue_repair 表示仍可修复。",
    }
    return finalize_and_write_decision(decision, run_dir, paper_evidence, baseline_outside_paths)


if __name__ == "__main__":
    raise SystemExit(main())
