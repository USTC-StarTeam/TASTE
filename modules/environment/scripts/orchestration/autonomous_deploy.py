#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import os
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MODULE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = MODULE_ROOT.parents[1]
RUNS_ROOT = MODULE_ROOT / "runs"
DECISION_POLICY_VERSION = "environment.deployment_decision.v77"
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
for candidate in [MODULE_ROOT, MODULE_ROOT / "scripts"]:
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts.common.claude_runner import run_claude_json
from scripts.common.io_utils import ensure_within, read_json, slugify, utc_now, write_json
from scripts.common.plan_schema import load_experiment_plan, normalize_plan
from scripts.common.shell import EXTERNAL_RUNTIME_ENV_KEYS, command_is_dangerous, command_text, command_tokens, isolated_runtime_env, run_logged, runtime_env
from scripts.orchestration.dependency_policy import normalize_environment_plan_commands
from scripts.orchestration.criteria_policy import normalize_success_criteria
from scripts.environment.runtime_probe import detect_machine_profile, find_conda_executable
from scripts.repository.repo_manager import clone_or_reuse, collect_repo_evidence
from scripts.reproduction.decision import classify_failures, compare_metric_values, metric_criteria_passed, normalize_verdict, success_criteria_issues
from scripts.reproduction.paper_evidence import collect_paper_evidence


def default_work_root() -> Path:
    return MODULE_ROOT / "runs"


def resolve_work_root(value: str) -> Path:
    try:
        return ensure_within(Path(value).expanduser(), RUNS_ROOT)
    except ValueError as exc:
        raise SystemExit(f"--work-root 必须位于 {RUNS_ROOT} 内，避免中间产物写到 environment 之外：{exc}") from exc


def make_run_id(plan: dict[str, Any]) -> str:
    return f"{utc_now().replace(':', '').replace('+', 'Z')}_{slugify(plan.get('slug') or plan.get('title') or 'experiment')}"


def normalize_run_id(value: str, plan: dict[str, Any]) -> str:
    if not str(value or "").strip():
        return make_run_id(plan)
    return slugify(str(value), "run")


def is_github_repo_url(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("https://github.com/") or text.startswith("http://github.com/") or text.startswith("git@github.com:") or text.startswith("ssh://git@github.com/")


def canonical_repo_url(value: str) -> str:
    text = str(value or "").strip()
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


def repo_candidates_after_review(original_candidates: list[str], review_result: dict[str, Any]) -> tuple[list[str], list[str], bool]:
    reviewed = review_result.get("json") if isinstance(review_result.get("json"), dict) else {}
    github_original = [str(item).strip() for item in original_candidates if is_github_repo_url(str(item))]
    if isinstance(reviewed, dict) and str(reviewed.get("status") or "").strip().lower() == "reject":
        return [], [str(reviewed.get("reject_reason") or "Claude Code 判定 plan 中的 GitHub 仓库候选不可信")], False
    clean_ordered, review_issues = validate_repo_candidate_review([str(item) for item in original_candidates], reviewed if isinstance(reviewed, dict) else {})
    if review_result.get("return_code") != 0 or not reviewed:
        review_issues.append("repo candidate review did not produce valid JSON")
    if clean_ordered:
        return clean_ordered, review_issues, False
    if github_original:
        return github_original, review_issues, True
    return [], review_issues, False


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


def _is_framework_runtime_write(path: str) -> bool:
    rel = str(path or "").strip().lstrip("./")
    if not rel:
        return False
    allowed_exact = {
        "runtime/state",
        "runtime/state/web_jobs.json",
    }
    if rel in allowed_exact:
        return True
    if rel.startswith("runtime/state/"):
        return True
    if rel.startswith("framework/workspace/"):
        return True
    parts = rel.split("/")
    if "__pycache__" in parts and (rel.startswith("framework/scripts/") or rel.startswith("web/backend/")):
        return True
    return False


def _filter_framework_runtime_writes(paths: list[str] | set[str]) -> list[str]:
    return sorted(path for path in paths if not _is_framework_runtime_write(path))


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
        if not raw or raw == "modules/environment" or raw.startswith("modules/environment/") or _is_framework_runtime_write(raw):
            continue
        state[raw] = _workspace_path_state(raw, status)
    return state


def _git_status_paths_outside_environment() -> set[str]:
    return set(_git_status_outside_environment())


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
        if not rel or rel == "." or rel.startswith(".git/") or rel == "modules/environment" or rel.startswith("modules/environment/") or _is_framework_runtime_write(rel):
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
    new_paths = _filter_framework_runtime_writes(current_paths - baseline_paths)
    changed_paths = _filter_framework_runtime_writes(path for path in baseline_paths & current_paths if baseline.get(path) != current.get(path))
    resolved_paths = _filter_framework_runtime_writes(baseline_paths - current_paths)
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
    write_json(run_dir / "environment_deployment_decision.json", decision)
    write_json(MODULE_ROOT / "latest_decision.json", decision)
    print(json.dumps(decision, ensure_ascii=False, indent=2))
    return int(decision["exit_code"])

def prompt_repo_candidate_review(normalized_plan: dict[str, Any], repo_candidates: list[str], output_path: Path) -> str:
    return f"""
你是 environment 的 Claude Code 仓库候选审阅代理。实验 plan 已经给出 GitHub 候选，但不能盲信顺序。
请只根据 plan 和候选 URL 判断哪些仓库最可能是论文官方/作者/可信复现仓库，并给出克隆优先顺序；如果候选明显不可信，应拒绝。

硬性要求：
- 只能把 JSON 写入 `{output_path}`，不要写其它 TASTE 模块或前端文件。
- 不要编造候选之外的仓库；如果候选不足以确认，输出 `status=reject`。
- 只输出 JSON，不要 Markdown。
- JSON schema：
  {{
    "status": "ready" | "reject",
    "ordered_repo_urls": ["https://github.com/..."],
    "selected_reason": "为什么这个顺序可信",
    "evidence": ["来自 plan/URL 的证据"],
    "reject_reason": "无法确认时填写"
  }}

候选仓库：
```json
{json.dumps(repo_candidates, ensure_ascii=False, indent=2)}
```

实验 plan：
```json
{json.dumps(normalized_plan, ensure_ascii=False, indent=2)[:30000]}
```
""".strip() + "\n"

def prompt_repo_discovery(normalized_plan: dict[str, Any], output_path: Path) -> str:
    return f"""
你是 environment 的 Claude Code 后端代理。当前实验 plan 没有提供可直接克隆的 GitHub 仓库。
请只根据下面的 plan 和论文线索判断最可信的官方/作者 GitHub 仓库候选；如果无法可靠确认，就输出 reject，不要编造仓库。

硬性要求：
- 只能把 JSON 写入 `{output_path}`，不要写其它 TASTE 模块或前端文件。
- JSON schema：
  {{
    "status": "ready" | "reject",
    "repo_url": "https://github.com/...",
    "confidence": 0.0,
    "evidence": ["为什么这是论文官方/可信仓库"],
    "reject_reason": "无法确认时填写"
  }}

实验 plan：
```json
{json.dumps(normalized_plan, ensure_ascii=False, indent=2)[:30000]}
```
""".strip() + "\n"


def prompt_environment_plan(normalized_plan: dict[str, Any], machine: dict[str, Any], repo_evidence: dict[str, Any], paper_evidence: dict[str, Any], previous_rounds: list[dict[str, Any]], output_path: Path, round_index: int) -> str:
    return f"""
你是 environment 的 Claude Code 环境部署代理。你的任务是根据实验 plan、论文/README 证据和本机画像，制定一个可执行、可审计、适合本机的 Conda 环境与复现计划。仓库 README/docs 必须提供安装/依赖指导和训练/评估/复现/数据运行指导；只有空 README、孤立 requirements.txt、孤立 train.py，或写明 no installation / no training / 未提供复现说明的否定式 README/docs，不能作为自适应部署依据。

必须遵守：
- 这是纯后端模块；不要修改网页前端、不要修改 `modules/environment` 原目录、不要写 `modules/environment` 之外的中间产物。
- 所有克隆仓库、数据、日志、临时脚本、输出都必须留在本次 run 目录或 repo 子目录内。
- `cwd` 只能是 `repo`、`run` 或本次 run 目录内路径；命令参数里的路径值和本地脚本/文件参数必须落在本次 run 目录内。已知输出/数据/配置/依赖文件参数即使写成 `outputs/run1` 这类裸相对值，也会按命令实际 `cwd` 解析；`bash scripts/run.sh`、`python train.py`、`python scripts/train.py` 也会解析真实路径，不能指向 run 外或 run 内 symlink 到外部的文件；`outputs/../../outside`、`--output=.../../..`、`-r.../../..` 这类穿越写法会被拒绝。如果命令本身是 `./script.sh` 或绝对脚本路径，脚本也必须位于本次 run/repo 内，外部系统工具请使用命令名而不是外部临时脚本路径。
- 不要为了通过检查而使用 toy/synthetic/dummy/mock/sample 数据或替代数据集冒充论文数据；数据准备命令或日志必须能看到真实论文数据集名称或来源，不能出现 not using / instead of / replacement / 替代 / 改用 这类说明论文数据集未被使用的上下文，`paper_config_alignment` 也要说明该映射。论文数据准备/下载/预处理阶段必须是必需命令，不能标记为 `required=false` 后再拿它支撑批准。
- HuggingFace Hub 下载必须使用当前可用的 `hf download`；不要使用已废弃不可工作的 `huggingface-cli`，也不要输出 `--resume-download` 参数。数据集仓库用 `hf download <repo_id> --repo-type dataset --local-dir <run内目录>`，模型/checkpoint 仓库用 `hf download <repo_id> --local-dir <run内目录>`。
- `dm-tree` 包的 Python 导入名是 `tree`；验证命令应使用 `import tree as dm_tree` 或 `import tree`，不要写 `import dm_tree`。
- RigidSSL 仓库的 `VelocityNetwork` 不能无参构造；模型验证必须使用 `examples/RigidSSL_Perturb.py` 中的 `model_setup()` 或 `create_model_config()`。skip-full-reproduction/烟测模式下不要运行完整训练 epoch；优先使用 loader/model/单 batch 探针证明数据、模型、CUDA 和旧 PyG pickle 可用。
- Conda/Pip/下载/训练命令必须写成 JSON 数组，后端会受控执行；不要在回答里只写自然语言。不要输出 `rm -rf /`、`rm -rf -- /`、`rm -Rf /*`、`rm -rf ../outside`、`dd if=`、`chmod -R 777 /` 等高危命令片段；危险片段会大小写归一化后检查，`rm` 目标也会结构化解析。
- 不要输出 `conda activate`、`source activate`、`source ...` 或 `.` 这类只对交互 shell 生效的命令；每条命令应直接可执行，Python/训练入口由后端用 run 内 Conda prefix 重写，复杂初始化请写入本次 run 目录脚本后用 `bash <script>` 执行。
- 每条命令必须包含非空字符串 `phase`；`required` 若出现必须是 JSON boolean `true/false`，不能写成字符串或数字；`cwd` 若出现必须是字符串。
- 必须包含成功创建/安装 Conda 环境的阶段，以及 verify/import/smoke/reproduce_full 等导入或运行验证阶段；批准需要独立的 Conda 环境证据，Conda setup 和 verify/import/smoke/运行验证阶段都必须是必需命令，不能标记为 `required=false` 后再拿它支撑批准。setup 阶段命令动作应是 `conda create/install/update -p <prefix>`、`conda env create/update -p <prefix>`、`conda run -p <prefix> python -m pip install ...` 或安装脚本；verify 阶段必须是 `conda run -p <prefix>` 下的 python/pytest/torchrun/训练评估脚本等真实导入或运行验证，不能用第二条 `conda create` 或 `conda install` 冒充 verify。
- 必须输出 `machine_assessment`，说明论文/README 的硬件或运行要求、本机 GPU/CPU/CUDA/显存条件、是否适合本机、以及 batch/precision/device 等本机适配动作；本机资源摘要必须引用后端机器画像里的具体 GPU 型号和显存数值，evidence 应指向 runtime_probe/nvidia-smi/GPU/CUDA 等探测来源，不能只写“GPU ok”；如果本机无法满足且没有合理降级路径，应输出 reject 并给 `machine_compute_unavailable` 证据。
- 不要使用 `bash -c`、`sh -c`、`zsh -c`，也不要使用 `bash --noprofile --norc -c`、`bash -o pipefail -c`、`bash -lc` 等任何内联 shell 变体。你只能写 JSON，不能在输出 JSON 的同时创建辅助脚本；因此不要引用“本计划稍后才生成”的 `setup_scripts`、`write_*.sh`、`download_*.sh`。`["bash", "脚本路径"]` 只能指向仓库里已经存在的脚本，或本次 run 目录里在计划生成前已经存在的脚本；否则后端会拒绝。下载/解压等步骤优先写成直接 JSON 命令，例如 `hf download ...`、`tar -xzf ...`、`python -c ...`。
- `python -c` 仅用于短小导入/打印/探测；其中出现的绝对路径、`~/`、`../` 或包含 `../` 的字符串路径也会按命令 cwd 解析并限制在本次 run 目录内。带解释器选项的 `python -X faulthandler -c ...`、`python -W ignore -c ...` 和 `conda run ... python -X ... -c ...` 也会被检查；需要复杂文件写入时，请把辅助脚本和输出都放在 run/repo 内。下载/解压、依赖安装和构建工具的贴值短路径选项也必须留在 run/repo 内，例如 `curl -oFILE`、`wget -OFILE`、`wget -Pdir`、`tar -Cdir`、`unzip -ddir`、`git -Cdir`、`make -Cdir`、`ninja -Cdir`、`cmake -Bdir`、`pip -tdir`、`7z -odir`、`rsync -Tdir`；即使命令包在 `conda run` / `mamba run` / `micromamba run` 后面，内层命令头和 `python -m pip` 也会被检查。
- 必须输出 `paper_config_alignment`，逐项说明论文里的数据集、指标、训练超参、checkpoint、硬件或本机适配如何映射到实际命令；关键项缺失时不要冒充 ready。
- `success_criteria` 每项必须包含指标名、比较符、可解析的论文目标值和来源：`name/metric`、`operator/op`、`value/target/paper_value`、`source/paper_source/evidence_source`；目标值必须是数字或百分比，且必须能逐项绑定到 `paper_evidence.target_metrics` 中的论文/plan 目标指标，不能是 Claude 为了过 gate 自行编造的数字。
- `paper_config_alignment.command_phase` 必须引用 `commands[].phase` 中真实存在的阶段；至少要覆盖 success_criteria 中的论文指标，并用逐项、具体、有论文值或 README/config 证据的行覆盖不少于 3 类训练/完整复现配置：epoch/steps、batch_size、learning_rate、optimizer/scheduler、seed、checkpoint/pretrained、hardware/precision。不要只写“paper training config handled”。
- 完整复现命令必须包含 `phase=reproduce_full` 且 `required=true`；只跑 smoke/verify 不能作为批准依据。
- 每条命令可以带可选 `env` 对象设置 `CUDA_VISIBLE_DEVICES`、`OMP_NUM_THREADS` 等复现需要的变量；也可以保留 README 原文中的 `KEY=VALUE command` 或 `env KEY=VALUE command` 前置变量，后端会拆成结构化 env；不要使用 `env -i`/`env --unset` 等带选项形式，不要覆盖 HOME/PATH/缓存目录/CONDA_PREFIX/VIRTUAL_ENV/PYTHONHOME 等隔离关键变量；`PYTHONPATH`、`LD_LIBRARY_PATH`、`LD_PRELOAD`、`CONDA_ENVS_PATH`、`PIP_TARGET`、`PIP_PREFIX`、`PIP_INDEX_URL`、`PIP_EXTRA_INDEX_URL`、`PIP_FIND_LINKS`、`PIP_CONSTRAINT`、`PIP_REQUIREMENT`、`UV_INDEX_URL`、`UV_FIND_LINKS`、`PYTHONUSERBASE`、`PIP_CONFIG_FILE`、数据、输出、缓存类路径即使是裸相对路径或 `file://` 本地源，也会按命令 cwd 解析，最终必须在本次 run 目录内；`PIP_FIND_LINKS`、`PIP_CONSTRAINT`、`PIP_REQUIREMENT`、`UV_FIND_LINKS`、`UV_CONSTRAINT` 等多值 env 会按 shell 风格空白分词逐项检查；远程 URL 保持可用。
- 如果 README、论文、仓库、数据源明显不靠谱或不可获得，可以输出 `status=reject`，但必须给不可修证据；可修复的环境、代码、路径或配置问题必须继续修复。
- 如果还可修复，输出 `status=ready_to_execute`，让后端执行；失败后你会看到完整日志继续修。

请写入 `{output_path}`，schema 如下：
```json
{{
  "schema_version": "environment.claude_environment_plan.v1",
  "status": "ready_to_execute | reject",
  "reject_reason": "仅 status=reject 时填写",
  "unreliable_basis": [{{"category": "repository_unreliable|data_unreliable|paper_unreliable|machine_compute_unavailable", "evidence": ["..."]}}],
  "env_name": "建议的 conda 环境名，必须唯一且可读",
  "python_version": "3.10",
  "paper_claims": ["需要复现的论文效果/指标"],
  "machine_assessment": {{
    "paper_hardware_or_runtime_requirement": "论文/README/plan 中的硬件、CUDA、显存、训练时长或并行要求；没有特殊要求也要说明",
    "local_machine_summary": "从本机画像提炼出的 GPU/CPU/CUDA/显存/OS/conda 信息",
    "fit_for_local_machine": true,
    "adaptation_actions": ["例如 batch size、precision、num_workers、CUDA_VISIBLE_DEVICES、单机多卡策略等；无需适配时说明 exact match"],
    "evidence": ["引用 machine_profile、论文/README、命令配置或适配理由"]
  }},
  "paper_config_alignment": [
    {{"paper_item": "dataset|metric|epoch|batch_size|learning_rate|seed|checkpoint|hardware", "paper_value": "论文/README/plan 中的原始要求", "implementation_choice": "本次命令实际采用的实现或本机适配", "command_phase": "dataset|reproduce_full|eval", "evidence_source": "paper/README/plan/log", "match_status": "matched|adapted_for_machine|missing|unknown", "critical": true, "adaptation_reason": "仅 adapted_for_machine 时填写"}}
  ],
  "commands": [
    {{"phase": "conda", "command": ["conda", "create", "-y", "-n", "env", "python=3.10", "pip"], "cwd": "repo", "timeout_sec": 1800, "required": true, "env": {{}}}},
    {{"phase": "install", "command": ["conda", "run", "-n", "env", "python", "-m", "pip", "install", "-r", "requirements.txt"], "cwd": "repo", "timeout_sec": 1800, "required": true}},
    {{"phase": "dataset", "command": ["bash", "scripts/download_data.sh"], "cwd": "repo", "timeout_sec": 7200, "required": true}},
    {{"phase": "verify", "command": ["conda", "run", "-n", "env", "python", "-c", "import torch; print(torch.__version__)"], "cwd": "repo", "timeout_sec": 300, "required": true}},
    {{"phase": "reproduce_smoke", "command": ["conda", "run", "-n", "env", "python", "train.py", "--epochs", "1"], "cwd": "repo", "timeout_sec": 1800, "required": true}},
    {{"phase": "reproduce_full", "command": ["conda", "run", "-n", "env", "python", "train.py"], "cwd": "repo", "timeout_sec": 86400, "required": true}}
  ],
  "success_criteria": [{{"name": "accuracy", "operator": ">=", "value": 0.9, "source": "paper/table/README"}}],
  "metric_extraction_notes": "说明如何从日志中判断论文效果"
}}
```

当前是第 {round_index} 轮。

实验 plan：
```json
{json.dumps(normalized_plan, ensure_ascii=False, indent=2)[:40000]}
```

本机画像：
```json
{json.dumps(machine, ensure_ascii=False, indent=2)[:20000]}
```

仓库 README/配置证据：
```json
{json.dumps(repo_evidence, ensure_ascii=False, indent=2)[:50000]}
```

论文/训练/指标证据：
```json
{json.dumps(paper_evidence, ensure_ascii=False, indent=2)[:50000]}
```

历史失败与修复上下文：
```json
{json.dumps(previous_rounds, ensure_ascii=False, indent=2)[:40000]}
```
""".strip() + "\n"


def prompt_final_judgement(normalized_plan: dict[str, Any], paper_evidence: dict[str, Any], env_plan: dict[str, Any], receipts: list[dict[str, Any]], metric_evidence: list[dict[str, Any]], output_path: Path) -> str:
    return f"""
你是 environment 的 Claude Code 复现裁决代理。请根据实验 plan、你制定的环境计划、后端执行日志和指标证据，输出是否允许进入下一个模块。

裁决必须严格：
- 只有真实仓库、真实数据、论文配置、训练/评估结果都支持论文效果时，才能 `decision=approve` 且 `allow_next_module=true`。
- 如果失败但仍可通过修 Conda、代码、数据路径、配置、训练参数解决，必须 `decision=continue_repair`。
- 必须核对环境计划中的 `paper_config_alignment`；关键论文配置缺失、unknown 或 missing 时，不能批准。
- `metric_evidence` 必须逐项覆盖 `success_criteria` 中每个论文指标，并绑定到本轮后端成功且必需的 `reproduce_full`、`eval/evaluate/evaluation`、`test` 或 `benchmark` 命令回执：必须提供这些阶段 stdout/stderr 中真实出现的指标日志摘录，摘录要包含指标名和 observed 值；`required=false` 的可选指标阶段即使成功也不能支撑批准；`log_path`、phase、command 只能作为来源引用，不能单独支撑批准。后端会重新比较 observed 与论文 target，不能只靠 `passed=true`，也不能用 install/verify/smoke 或失败回执日志冒充论文指标。
- 如果证据证明仓库/数据/论文不靠谱，或本机算力根本不可满足且无合理降级复现路径，才 `decision=reject`。
- `reject` 必须在 failure_taxonomy 中给出不可修终态证据：`repository_unreliable`、`data_unreliable`、`paper_unreliable` 或 `machine_compute_unavailable`，并带可审计 evidence；evidence 应包含 URL、日志路径、命令、状态码、错误码、文件路径或硬件需求对比等来源信号，不能只写“数据不可用/论文不靠谱/算力不足”，也不能只写 `source=manual_review` 这类泛泛来源；普通 `machine_compute` 只能表示可修复算力/显存/配置问题，必须 `continue_repair`。
- `failure_taxonomy[].repairable` 必须使用 JSON boolean；只要是 `true`，或字符串 `"true"`、`"yes"`、`"可修复"`、数字 `1` 这类真值，后端都会视为可修复并禁止 `reject`。
- 必须给 failure_taxonomy，类别可用：conda_environment、machine_compute、repository_code、dataset、paper_config、repository_unreliable、data_unreliable、paper_unreliable、machine_compute_unavailable、unknown。

请写入 `{output_path}`：
```json
{{
  "schema_version": "environment.reproduction_verdict.v1",
  "decision": "approve | reject | continue_repair",
  "allow_next_module": false,
  "paper_claims_verified": false,
  "reproduction_success": false,
  "metric_evidence": [],
  "failure_taxonomy": [{{"category": "dataset", "evidence": ["..."], "repairable": true}}],
  "repair_plan": ["下一轮具体怎么修"],
  "reject_reason": "仅 reject 时填写",
  "approval_summary": "仅 approve 时填写"
}}
```

实验 plan：
```json
{json.dumps(normalized_plan, ensure_ascii=False, indent=2)[:25000]}
```

论文/训练/指标证据：
```json
{json.dumps(paper_evidence, ensure_ascii=False, indent=2)[:30000]}
```

环境/复现计划：
```json
{json.dumps(env_plan, ensure_ascii=False, indent=2)[:30000]}
```

后端命令回执：
```json
{json.dumps(receipts, ensure_ascii=False, indent=2)[:50000]}
```

本地指标解析证据：
```json
{json.dumps(metric_evidence, ensure_ascii=False, indent=2)[:12000]}
```
""".strip() + "\n"



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

def resolve_command_cwd(row: dict[str, Any], repo_path: Path, run_dir: Path) -> Path:
    raw = str(row.get("cwd") or "repo").strip()
    if raw in {"", "repo", "."}:
        return repo_path
    if raw == "run":
        return run_dir
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = repo_path / candidate
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


def _python_inline_string_literals(code: str) -> list[str]:
    try:
        tree = ast.parse(str(code or ""))
    except SyntaxError:
        return []
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    literals.append(part.value)
    return literals


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
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = resolved_cwd / candidate
        try:
            ensure_within(candidate, resolved_run)
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
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = resolved_cwd / candidate
        try:
            ensure_within(candidate, resolved_run)
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


SUCCESS_CRITERIA_METRIC_ALIASES = {
    "accuracy": {"acc"},
    "acc": {"accuracy"},
    "f1": {"f1score", "f1_score"},
    "f1score": {"f1", "f1_score"},
    "auc": {"auroc", "roc_auc", "rocauc"},
    "auroc": {"auc", "roc_auc", "rocauc"},
    "map": {"meanaverageprecision", "mean_average_precision", "m_ap"},
    "meanaverageprecision": {"map", "mean_average_precision"},
    "miou": {"meaniou", "mean_iou"},
    "meaniou": {"miou", "mean_iou"},
    "top1accuracy": {"top1", "top_1", "top-1", "acc@1", "top1_acc"},
    "top5accuracy": {"top5", "top_5", "top-5", "acc@5", "top5_acc"},
}
PAPER_TARGET_NUMBER_RE = re.compile(r"[+-]?(?:(?:\d+(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*%?")
PAPER_TARGET_VALUE_KEYS = ("value", "target", "paper_value", "expected", "score", "result")
PAPER_TARGET_NAME_KEYS = ("name", "metric", "metric_name", "criterion")
PAPER_TARGET_SOURCE_KEYS = ("source", "paper_source", "evidence_source", "table", "section")


def _metric_binding_compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _success_metric_name_variants(name: str) -> set[str]:
    raw = str(name or "").strip().lower()
    compact = _metric_binding_compact(raw)
    variants = {raw, compact}
    variants.update(SUCCESS_CRITERIA_METRIC_ALIASES.get(compact, set()))
    return {item for item in variants if item}


def _metric_name_matches_paper_target(name: str, target: Any) -> bool:
    if not str(name or "").strip():
        return False
    if isinstance(target, dict):
        explicit_names = [str(target.get(key) or "").strip() for key in PAPER_TARGET_NAME_KEYS]
        explicit_names = [item for item in explicit_names if item]
        if explicit_names:
            target_variants: set[str] = set()
            for item in explicit_names:
                target_variants.update(_success_metric_name_variants(item))
            return bool(_success_metric_name_variants(name) & target_variants)
    text = json.dumps(target, ensure_ascii=False, sort_keys=True) if isinstance(target, (dict, list)) else str(target or "")
    lowered = text.lower()
    compact_text = _metric_binding_compact(text)
    for variant in _success_metric_name_variants(name):
        if variant in lowered or _metric_binding_compact(variant) in compact_text:
            return True
    return False


def _target_metric_values(target: Any) -> list[Any]:
    values: list[Any] = []
    if isinstance(target, dict):
        for key in PAPER_TARGET_VALUE_KEYS:
            if key not in target:
                continue
            value = target.get(key)
            if value is not None and value != "":
                values.append(value)
    text = json.dumps(target, ensure_ascii=False, sort_keys=True) if isinstance(target, (dict, list)) else str(target or "")
    for match in PAPER_TARGET_NUMBER_RE.findall(text):
        if match.strip():
            values.append(match.strip())
    out: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = str(value)
        if key not in seen:
            out.append(value)
            seen.add(key)
    return out


def _criterion_target_matches_paper_target(criterion_target: Any, paper_target: Any) -> tuple[bool, Any, dict[str, Any]]:
    last_compare: dict[str, Any] = {}
    for candidate in _target_metric_values(paper_target):
        matched, compare = compare_metric_values(criterion_target, candidate, "==")
        last_compare = compare
        if matched:
            return True, candidate, compare
    return False, None, last_compare


def _paper_target_source(target: Any) -> str:
    if isinstance(target, dict):
        for key in PAPER_TARGET_SOURCE_KEYS:
            value = str(target.get(key) or "").strip()
            if value:
                return value
    return "paper_evidence.target_metrics"


def _success_criteria_paper_binding_gate(env_plan: dict[str, Any], paper_evidence: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    criteria = env_plan.get("success_criteria") if isinstance(env_plan.get("success_criteria"), list) else []
    paper_targets = paper_evidence.get("target_metrics") if isinstance(paper_evidence.get("target_metrics"), list) else []
    issues: list[str] = []
    matches: list[dict[str, Any]] = []
    for index, criterion in enumerate(criteria):
        if not isinstance(criterion, dict):
            issues.append(f"success_criteria[{index}] 不是 object，无法绑定论文目标指标")
            continue
        name = str(criterion.get("name") or criterion.get("metric") or "").strip()
        target_found, target_value, target_key = _criterion_target_value(criterion)
        if not name or not target_found:
            issues.append(f"success_criteria[{index}] 缺少 name/metric 或 target，无法绑定论文目标指标")
            continue
        candidate_matches: list[dict[str, Any]] = []
        for paper_index, paper_target in enumerate(paper_targets):
            if not _metric_name_matches_paper_target(name, paper_target):
                continue
            target_matched, paper_value, compare = _criterion_target_matches_paper_target(target_value, paper_target)
            if not target_matched:
                continue
            candidate_matches.append({
                "success_criteria_index": index,
                "paper_target_index": paper_index,
                "metric": name,
                "criterion_target_key": target_key,
                "criterion_target": target_value,
                "paper_target_value": paper_value,
                "paper_target_source": _paper_target_source(paper_target),
                "compare": compare,
            })
        if candidate_matches:
            matches.append(candidate_matches[0])
        else:
            issues.append(f"success_criteria[{index}] metric={name} target={target_value} 未能绑定到 paper_evidence.target_metrics 中同名且同目标值的论文指标")
    evidence = {
        "criteria_count": len(criteria),
        "paper_target_metric_count": len(paper_targets),
        "matched_count": len(matches),
        "matches": matches[:8],
        "issues": issues[:10],
        "sample_paper_targets": paper_targets[:8],
    }
    return bool(criteria and paper_targets and not issues), evidence


ALIGNMENT_OK_STATUSES = {"matched", "adapted_for_machine", "confirmed", "implemented"}
ALIGNMENT_BAD_STATUSES = {"missing", "unknown", "unmatched", "not_found", "not_applicable"}
ALIGNMENT_TRAINING_TOKENS = {
    "train", "training", "reproduce", "epoch", "epochs", "batch", "batch_size", "learning_rate",
    "lr", "optimizer", "scheduler", "seed", "checkpoint", "ckpt", "hardware", "gpu", "cuda",
    "precision", "训练", "轮次", "批量", "学习率", "优化器", "随机种子", "检查点", "显卡",
}
TRAINING_ALIGNMENT_MIN_CATEGORIES = 3
TRAINING_ALIGNMENT_CATEGORY_LABELS = {
    "epoch_or_steps": "epoch/steps",
    "batch_size": "batch_size",
    "learning_rate": "learning_rate",
    "optimizer_or_scheduler": "optimizer/scheduler",
    "seed": "seed",
    "checkpoint_or_pretrained": "checkpoint/pretrained",
    "hardware_or_precision": "hardware/precision",
}
TRAINING_ALIGNMENT_CATEGORY_PATTERNS = {
    "epoch_or_steps": (r"\bepochs?\b", r"\bsteps?\b", r"\biters?\b", r"\biterations?\b", r"max_steps", r"训练轮次", r"轮次", r"步数", r"迭代"),
    "batch_size": (r"batch[_ -]?size", r"\bbatch\b", r"批量", r"批大小"),
    "learning_rate": (r"learning[_ -]?rate", r"\blr\b", r"学习率"),
    "optimizer_or_scheduler": (r"optimizer", r"optimiser", r"scheduler", r"adamw?", r"sgd", r"cosine", r"warmup", r"decay", r"优化器", r"调度"),
    "seed": (r"\bseed\b", r"random[_ -]?seed", r"随机种子"),
    "checkpoint_or_pretrained": (r"checkpoint", r"\bckpt\b", r"pretrained", r"pre-trained", r"weights", r"resume", r"检查点", r"预训练", r"权重"),
    "hardware_or_precision": (r"hardware", r"\bgpu\b", r"cuda", r"vram", r"显卡", r"显存", r"precision", r"fp16", r"bf16", r"float16", r"精度"),
}
CONCRETE_TRAINING_ALIGNMENT_RE = re.compile(
    r"\d|\.ya?ml\b|\.json\b|\.toml\b|\.cfg\b|\.ini\b|"
    r"\bconfig\b|\bdefault\b|\breadme\b|\badamw?\b|\bsgd\b|\bfp16\b|\bbf16\b|"
    r"\bpretrained\b|\bcheckpoint\b|\bckpt\b|not specified|not reported|not given|not provided|"
    r"默认|配置|未说明|未给出|未报告|预训练|检查点|权重",
    re.IGNORECASE,
)
MACHINE_ALIGNMENT_TOKENS = {
    "hardware", "gpu", "cuda", "vram", "memory", "cpu", "device", "devices", "precision",
    "batch", "batch_size", "num_workers", "workers", "local", "machine", "compute",
    "硬件", "显卡", "显存", "本机", "机器", "算力", "设备", "精度", "批量",
}
MACHINE_FIT_OK_STATUSES = {"fit", "fits", "ok", "true", "yes", "suitable", "supported", "confirmed", "adapted", "adapted_for_machine", "local_fit"}
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
DATASET_NAME_KEYS = {
    "name", "short_name", "title", "value", "paper_value", "expected",
    "dataset", "datasets", "dataset_name", "dataset_names", "dataset_title", "dataset_id", "dataset_key",
    "paper_dataset", "paper_datasets", "benchmark", "benchmark_name", "corpus", "corpus_name",
    "data", "data_name", "hf_dataset", "huggingface_dataset", "kaggle_dataset",
}
GENERIC_DATASET_NAMES = {
    "data", "dataset", "datasets", "benchmark", "corpus", "trainingdata", "testdata",
    "paperdataset", "customdataset", "realdata", "数据", "数据集", "训练数据", "测试数据",
}
SYNTHETIC_DATASET_MARKERS = (
    "toy", "synthetic", "dummy", "fake", "mock", "random data", "random dataset",
    "sample data", "sample dataset", "demo data", "demo dataset", "placeholder",
    "玩具", "合成数据", "伪造", "虚拟", "随机数据", "示例数据", "样例数据",
)
DATASET_NEGATIVE_OR_REPLACEMENT_MARKERS = (
    "not using", "not use", "do not use", "did not use", "without", "instead of",
    "rather than", "replacement", "replaced", "replace", "substitute", "substitution",
    "fallback", "not available", "unavailable", "missing", "cannot access", "can't access",
    "failed to download", "未使用", "不使用", "没有使用", "不用", "替代", "代替", "改用", "换用",
    "不可用", "无法获取", "下载失败",
)


def _command_phase_names(plan: dict[str, Any]) -> set[str]:
    commands = plan.get("commands") if isinstance(plan.get("commands"), list) else []
    return {str(row.get("phase") or "").strip().lower() for row in commands if isinstance(row, dict) and str(row.get("phase") or "").strip()}


def _required_command_phase_names(plan: dict[str, Any]) -> set[str]:
    commands = plan.get("commands") if isinstance(plan.get("commands"), list) else []
    return {
        str(row.get("phase") or "").strip().lower()
        for row in commands
        if isinstance(row, dict) and str(row.get("phase") or "").strip() and row.get("required") is not False
    }


def _optional_command_phase_names(plan: dict[str, Any]) -> set[str]:
    commands = plan.get("commands") if isinstance(plan.get("commands"), list) else []
    return {
        str(row.get("phase") or "").strip().lower()
        for row in commands
        if isinstance(row, dict) and str(row.get("phase") or "").strip() and row.get("required") is False
    }


def _alignment_row_text(row: dict[str, Any], keys: list[str] | None = None) -> str:
    selected = keys or [
        "paper_item", "paper_field", "claim", "setting", "category", "paper_value", "expected",
        "target", "description", "implementation_choice", "implementation", "command_phase",
        "command_ref", "evidence_source", "adaptation_reason",
    ]
    return " ".join(str(row.get(key) or "") for key in selected).lower()


def _alignment_command_phases(row: dict[str, Any]) -> set[str]:
    raw = str(row.get("command_phase") or row.get("phase") or "").strip().lower()
    if not raw:
        return set()
    return {part for part in re.split(r"[,/|;，、\s]+", raw) if part}


def _alignment_row_is_supported(row: dict[str, Any]) -> bool:
    return str(row.get("match_status") or row.get("status") or "").strip().lower() in ALIGNMENT_OK_STATUSES


def _alignment_row_supports_approval(row: dict[str, Any]) -> bool:
    return _alignment_row_is_supported(row) and row.get("critical") is not False


def _alignment_row_mentions_metric(row: dict[str, Any], metric_names: set[str]) -> bool:
    item_text = _alignment_row_text(row, ["paper_item", "paper_field", "claim", "setting", "category", "paper_value", "expected", "target", "description"])
    if any(token in item_text for token in ["metric", "accuracy", "auc", "f1", "loss", "score", "指标"]):
        return True
    return any(name and name in item_text for name in metric_names)


def _alignment_row_mentions_training_config(row: dict[str, Any]) -> bool:
    item_text = _alignment_row_text(row, ["paper_item", "paper_field", "claim", "setting", "category", "paper_value", "expected", "target", "description"])
    return any(token in item_text for token in ALIGNMENT_TRAINING_TOKENS)


def _training_alignment_categories_in_text(text: str) -> set[str]:
    lowered = str(text or "").lower()
    categories: set[str] = set()
    for category, patterns in TRAINING_ALIGNMENT_CATEGORY_PATTERNS.items():
        if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns):
            categories.add(category)
    return categories


def _alignment_row_training_detail_text(row: dict[str, Any]) -> str:
    return _alignment_row_text(row, [
        "paper_value", "expected", "target", "description", "implementation_choice",
        "implementation", "adaptation_reason", "evidence_source",
    ])


def _alignment_row_training_item_text(row: dict[str, Any]) -> str:
    return _alignment_row_text(row, ["paper_item", "paper_field", "claim", "setting", "category"])


def _alignment_row_has_concrete_training_detail(row: dict[str, Any]) -> bool:
    return bool(CONCRETE_TRAINING_ALIGNMENT_RE.search(_alignment_row_training_detail_text(row)))


def _alignment_row_training_categories(row: dict[str, Any]) -> set[str]:
    if not _alignment_row_is_supported(row) or not _alignment_row_has_concrete_training_detail(row):
        return set()
    detail_categories = _training_alignment_categories_in_text(_alignment_row_training_detail_text(row))
    item_categories = _training_alignment_categories_in_text(_alignment_row_training_item_text(row))
    categories = set(detail_categories)
    if len(item_categories) == 1:
        categories.update(item_categories)
    return categories


def _supported_training_alignment_categories(rows: list[dict[str, Any]]) -> set[str]:
    categories: set[str] = set()
    for row in rows:
        categories.update(_alignment_row_training_categories(row))
    return categories


def _training_category_labels(categories: set[str]) -> list[str]:
    return [TRAINING_ALIGNMENT_CATEGORY_LABELS.get(category, category) for category in sorted(categories)]


def _alignment_row_mentions_machine(row: dict[str, Any]) -> bool:
    return any(token in _alignment_row_text(row) for token in MACHINE_ALIGNMENT_TOKENS)


def _textual_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        return [str(item).strip() for item in value.values() if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _machine_assessment_fit_value(assessment: dict[str, Any]) -> bool:
    raw = assessment.get("fit_for_local_machine")
    if isinstance(raw, bool):
        return raw
    status = str(raw or assessment.get("status") or assessment.get("fit_status") or "").strip().lower()
    return status in MACHINE_FIT_OK_STATUSES


def _machine_adaptation_items(assessment: dict[str, Any]) -> list[str]:
    return _textual_items(
        assessment.get("adaptation_actions")
        or assessment.get("adaptations")
        or assessment.get("machine_adaptations")
        or assessment.get("no_adaptation_reason")
        or assessment.get("exact_match_reason")
    )


def _gpu_name_markers(gpu_rows: list[dict[str, Any]]) -> list[str]:
    markers: list[str] = []
    for row in gpu_rows:
        if not isinstance(row, dict):
            continue
        name = re.sub(r"\s+", " ", str(row.get("name") or "").strip().lower())
        if name:
            markers.append(name)
        for token in re.split(r"[^a-z0-9]+", name):
            if len(token) >= 3 and any(char.isdigit() for char in token):
                markers.append(token)
        for match in re.findall(r"(?:rtx|a|h|l)\s*\d{2,5}|\d{4,5}", name):
            markers.append(match.replace(" ", ""))
    unique: list[str] = []
    for marker in markers:
        if marker and marker not in unique:
            unique.append(marker)
    return unique


def _gpu_memory_mb(row: dict[str, Any]) -> float | None:
    for key in ["memory_total_mb", "memory_total", "memory", "vram", "vram_mb"]:
        raw = row.get(key) if isinstance(row, dict) else None
        text = str(raw or "").strip().lower()
        if not text:
            continue
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        if not match:
            continue
        value = float(match.group(1))
        if any(unit in text for unit in ["gb", "gib", "g ", "g显存"]):
            return value * 1024
        return value
    return None


def _text_mentions_detected_gpu_identity(text: str, gpu_rows: list[dict[str, Any]]) -> bool:
    lowered = str(text or "").lower()
    compact = re.sub(r"[^a-z0-9]+", "", lowered)
    for marker in _gpu_name_markers(gpu_rows):
        marker_lower = marker.lower()
        marker_compact = re.sub(r"[^a-z0-9]+", "", marker_lower)
        if marker_lower in lowered or (marker_compact and marker_compact in compact):
            return True
    return False


def _text_mentions_detected_gpu_memory(text: str, gpu_rows: list[dict[str, Any]]) -> bool:
    expected_values = [value for value in (_gpu_memory_mb(row) for row in gpu_rows) if value]
    if not expected_values:
        return True
    lowered = str(text or "").lower()
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(gib|gb|mib|mb|g|m)?", lowered):
        observed = float(match.group(1))
        unit = match.group(2) or ""
        observed_mb = observed * 1024 if unit in {"gib", "gb", "g"} else observed
        for expected_mb in expected_values:
            expected_gb = expected_mb / 1024
            if abs(observed_mb - expected_mb) <= max(1024.0, expected_mb * 0.20):
                return True
            if not unit and abs(observed - expected_gb) <= max(2.0, expected_gb * 0.20):
                return True
    return False


def machine_assessment_issues(env_plan: dict[str, Any], machine: dict[str, Any] | None = None) -> list[str]:
    assessment = env_plan.get("machine_assessment")
    if not isinstance(assessment, dict) or not assessment:
        return ["环境计划缺少 machine_assessment；必须说明论文硬件要求、本机资源、是否适合本机和适配动作"]
    issues: list[str] = []
    if not _machine_assessment_fit_value(assessment):
        issues.append("machine_assessment 未明确 fit_for_local_machine=true/适合本机；若本机不可满足应输出 reject 和 machine_compute_unavailable 证据")
    requirement_items = _textual_items(assessment.get("paper_hardware_or_runtime_requirement") or assessment.get("paper_runtime_requirement") or assessment.get("required_hardware"))
    if not requirement_items:
        issues.append("machine_assessment 缺少论文/README/plan 的硬件或运行要求说明")
    local_items = _textual_items(assessment.get("local_machine_summary") or assessment.get("local_resources") or assessment.get("local_gpu_summary"))
    if not local_items:
        issues.append("machine_assessment 缺少本机 GPU/CPU/CUDA/显存等资源摘要")
    evidence_items = _textual_items(assessment.get("evidence") or assessment.get("evidence_source") or assessment.get("reasoning"))
    adaptation_items = _machine_adaptation_items(assessment)
    if not evidence_items:
        issues.append("machine_assessment 缺少可审计 evidence")
    if not adaptation_items:
        issues.append("machine_assessment 缺少本机适配动作或无需适配理由；必须说明 batch/precision/device/CUDA 等适配，或说明本机配置与论文要求 exact match")
    if machine is not None:
        gpu_rows = machine.get("gpu") if isinstance(machine.get("gpu"), list) else []
        local_text = " ".join(local_items)
        evidence_text = " ".join(evidence_items).lower()
        if gpu_rows and not any(token in local_text.lower() for token in ["gpu", "cuda", "显卡", "显存", "nvidia"]):
            issues.append("machine_assessment 的本机资源摘要没有体现已探测到的 GPU/CUDA 信息")
        if gpu_rows and not _text_mentions_detected_gpu_identity(local_text, gpu_rows):
            issues.append("machine_assessment 的本机资源摘要没有明确写出后端已探测到的 GPU 型号，例如 runtime_probe/nvidia-smi 中的具体型号")
        if gpu_rows and not _text_mentions_detected_gpu_memory(local_text, gpu_rows):
            issues.append("machine_assessment 的本机资源摘要没有明确写出后端已探测到的 GPU 显存数值")
        if gpu_rows and not any(token in evidence_text for token in ["runtime_probe", "nvidia-smi", "machine_profile", "gpu", "cuda", "显存", "显卡"]):
            issues.append("machine_assessment evidence 没有指向 runtime_probe/nvidia-smi/GPU/CUDA/显存等本机探测来源")
    rows = env_plan.get("paper_config_alignment") if isinstance(env_plan.get("paper_config_alignment"), list) else []
    supported_rows = [row for row in rows if isinstance(row, dict) and _alignment_row_supports_approval(row)]
    if not any(_alignment_row_mentions_machine(row) for row in supported_rows):
        issues.append("paper_config_alignment 缺少硬件/GPU/CUDA/显存/batch/precision 等本机适配对齐项")
    return issues


def _machine_fit_gate(env_plan: dict[str, Any], machine: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    assessment = env_plan.get("machine_assessment") if isinstance(env_plan.get("machine_assessment"), dict) else {}
    issues = machine_assessment_issues(env_plan, machine)
    gpu_rows = machine.get("gpu") if isinstance(machine.get("gpu"), list) else []
    evidence = {
        "fit_for_local_machine": _machine_assessment_fit_value(assessment),
        "gpu_count": len(gpu_rows),
        "gpu_names": [str(row.get("name") or "") for row in gpu_rows[:8] if isinstance(row, dict)],
        "paper_hardware_or_runtime_requirement": assessment.get("paper_hardware_or_runtime_requirement") or assessment.get("paper_runtime_requirement") or assessment.get("required_hardware") or "",
        "local_machine_summary": assessment.get("local_machine_summary") or assessment.get("local_resources") or assessment.get("local_gpu_summary") or "",
        "adaptation_actions": _machine_adaptation_items(assessment)[:8],
        "evidence": _textual_items(assessment.get("evidence") or assessment.get("evidence_source") or assessment.get("reasoning"))[:8],
        "issues": issues[:10],
    }
    return not issues, evidence


def _success_criteria_metric_names(plan: dict[str, Any]) -> set[str]:
    rows = plan.get("success_criteria") if isinstance(plan.get("success_criteria"), list) else []
    names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ["name", "metric"]:
            value = str(row.get(key) or "").strip().lower()
            if value:
                names.add(value)
    return names


def paper_config_alignment_passed(plan: dict[str, Any]) -> tuple[bool, list[str]]:
    rows = plan.get("paper_config_alignment")
    if not isinstance(rows, list) or not rows:
        return False, ["环境计划缺少 paper_config_alignment；完整复现必须逐项说明论文配置如何映射到实际命令"]
    issues: list[str] = []
    supported_rows: list[dict[str, Any]] = []
    nonapproval_supported_count = 0
    command_phases = _command_phase_names(plan)
    required_command_phases = _required_command_phase_names(plan)
    optional_command_phases = _optional_command_phase_names(plan)
    metric_names = _success_criteria_metric_names(plan)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            issues.append(f"paper_config_alignment[{index}] 不是 object")
            continue
        status = str(row.get("match_status") or row.get("status") or "").strip().lower()
        critical = row.get("critical") is not False
        has_item = any(str(row.get(key) or "").strip() for key in ["paper_item", "paper_field", "claim", "setting", "category"])
        has_value = any(str(row.get(key) or "").strip() for key in ["paper_value", "expected", "target", "description"])
        has_mapping = any(str(row.get(key) or "").strip() for key in ["implementation_choice", "implementation", "command_phase", "command_ref", "evidence_source"])
        phases = _alignment_command_phases(row)
        if _alignment_row_is_supported(row):
            if _alignment_row_supports_approval(row):
                supported_rows.append(row)
            else:
                nonapproval_supported_count += 1
            if critical and not phases:
                issues.append(f"paper_config_alignment[{index}] 关键支持项缺少 command_phase，无法证明映射到实际命令")
            if phases and command_phases and phases.isdisjoint(command_phases):
                issues.append(f"paper_config_alignment[{index}] command_phase={sorted(phases)} 不在实际 commands phase 中：{sorted(command_phases)}")
            optional_phase_refs = sorted(phase for phase in phases if phase in optional_command_phases and phase not in required_command_phases)
            if critical and optional_phase_refs:
                issues.append(f"paper_config_alignment[{index}] 关键支持项 command_phase={optional_phase_refs} 指向 required=false 的可选命令；支撑论文配置的命令必须是必需命令")
        if critical and status in ALIGNMENT_BAD_STATUSES:
            issues.append(f"paper_config_alignment[{index}] 关键论文配置为 {status}，不能进入完整复现批准路径")
        if not status:
            issues.append(f"paper_config_alignment[{index}] 缺少 match_status")
        if not has_item:
            issues.append(f"paper_config_alignment[{index}] 缺少 paper_item/setting")
        if not has_value:
            issues.append(f"paper_config_alignment[{index}] 缺少 paper_value/expected")
        if not has_mapping:
            issues.append(f"paper_config_alignment[{index}] 缺少 implementation_choice/command_phase/evidence_source")
    if not supported_rows:
        if nonapproval_supported_count:
            issues.append("paper_config_alignment 没有任何可支撑批准的 matched/adapted_for_machine/confirmed/implemented 项；critical=false 的非关键行不会计入批准覆盖")
        else:
            issues.append("paper_config_alignment 没有任何 matched/adapted_for_machine/confirmed/implemented 项")
    if metric_names and not any(_alignment_row_mentions_metric(row, metric_names) for row in supported_rows):
        issues.append("paper_config_alignment 缺少 success_criteria 指标对齐项，不能证明论文指标映射到复现结果")
    if "reproduce_full" in command_phases:
        training_categories = _supported_training_alignment_categories(supported_rows)
        if len(training_categories) < TRAINING_ALIGNMENT_MIN_CATEGORIES:
            covered = ", ".join(_training_category_labels(training_categories)) or "无"
            issues.append(
                "paper_config_alignment 训练/完整复现配置覆盖不足：至少需要逐项覆盖 "
                f"{TRAINING_ALIGNMENT_MIN_CATEGORIES} 类具体配置（epoch/steps、batch_size、learning_rate、"
                "optimizer/scheduler、seed、checkpoint/pretrained、hardware/precision），"
                f"当前可审计覆盖为：{covered}"
            )
    return not issues, issues


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


def _repo_evidence_rows(repo_evidence: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = repo_evidence.get(key) if isinstance(repo_evidence.get(key), list) else []
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict) and str(row.get("relative_path") or row.get("path") or "").strip():
            out.append(row)
        elif isinstance(row, str) and row.strip():
            out.append({"relative_path": row.strip(), "path": row.strip()})
    return out


def _repo_command_lines(rows: list[dict[str, Any]]) -> list[str]:
    commands: list[str] = []
    for row in rows:
        raw_commands = row.get("command_lines") if isinstance(row.get("command_lines"), list) else []
        for command in raw_commands:
            text = str(command or "").strip()
            if text and text not in commands:
                commands.append(text)
    return commands


def _repo_command_line_count(rows: list[dict[str, Any]]) -> int:
    return len(_repo_command_lines(rows))


REPO_INSTALL_COMMAND_RE = re.compile(r"\b(?:pip|pip3)\s+install\b|\b(?:conda|mamba|micromamba)\s+(?:env\s+)?(?:create|install|update)\b|\bpython\s+setup\.py\s+install\b|\buv\s+pip\s+install\b", re.I)
REPO_REPRO_COMMAND_RE = re.compile(r"\b(?:python|python3|torchrun|deepspeed)\b.*\b(?:train|eval|evaluate|test|benchmark|reproduce|run|main|download|prepare|preprocess|finetune|pretrain)\b|\baccelerate\s+launch\b.*\b(?:train|eval|test|benchmark|reproduce|run|finetune|pretrain)\b|\b(?:bash|sh)\b.*\b(?:train|eval|test|benchmark|reproduce|run|download|prepare|preprocess)\b", re.I)
REPO_INSTALL_TEXT_RE = re.compile(r"\b(?:install|installation|setup|dependency|dependencies|requirements|environment|conda|pip|mamba|micromamba|pyproject|setup\.py)\b|安装|依赖|环境", re.I)
REPO_REPRO_TEXT_RE = re.compile(r"\b(?:reproduce|reproduction|train|training|eval|evaluation|evaluate|test|benchmark|dataset|data|download|prepare|preprocess|quickstart|usage|run)\b|复现|训练|评估|测试|数据集|下载|准备|运行", re.I)
REPO_ENTRYPOINT_REPRO_RE = re.compile(r"\b(?:train|eval|evaluate|test|benchmark|run|main|infer|inference|finetune|pretrain|download|prepare|preprocess)\b", re.I)
REPO_INSTALL_GUIDANCE_TOKENS = ("install", "installation", "setup", "dependency", "dependencies", "requirements", "environment", "conda", "pip", "mamba", "micromamba", "安装", "依赖", "环境")
REPO_REPRO_GUIDANCE_TOKENS = ("reproduce", "reproduction", "train", "training", "eval", "evaluation", "evaluate", "test", "benchmark", "dataset", "data", "download", "prepare", "preprocess", "run", "复现", "训练", "评估", "测试", "数据集", "下载", "准备", "运行")
REPO_NEGATIVE_GUIDANCE_RE = re.compile(
    r"\b(?:no|not|none|without|missing|absent|unavailable|unsupported|not\s+provided|not\s+included|not\s+available|does\s+not\s+(?:provide|include)|cannot|can't)\b|"
    r"没有|未提供|未包含|不包含|无|缺少|不可用|无法|不能|不支持",
    re.I,
)


def _repo_document_text(rows: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in rows:
        for key in ["relative_path", "path", "text_excerpt"]:
            value = str(row.get(key) or "").strip()
            if value:
                parts.append(value)
        commands = row.get("command_lines") if isinstance(row.get("command_lines"), list) else []
        parts.extend(str(command or "").strip() for command in commands if str(command or "").strip())
    return "\n".join(parts)


def _repo_entrypoint_has_reproduction_role(row: dict[str, Any]) -> bool:
    text = " ".join(str(row.get(key) or "") for key in ["relative_path", "path", "text_excerpt"])
    return bool(REPO_ENTRYPOINT_REPRO_RE.search(text))


def _repo_guidance_windows(text: str, tokens: tuple[str, ...]) -> list[str]:
    windows: list[str] = []
    for chunk in re.split(r"[\n。；;.!?]+", str(text or "")):
        clean = re.sub(r"\s+", " ", chunk.strip())
        if not clean:
            continue
        lowered = clean.lower()
        if any(token.lower() in lowered for token in tokens):
            windows.append(clean[:500])
    return windows


def _repo_negative_guidance_markers(text: str, tokens: tuple[str, ...]) -> list[str]:
    markers: list[str] = []
    for window in _repo_guidance_windows(text, tokens):
        if REPO_NEGATIVE_GUIDANCE_RE.search(window):
            markers.append(window)
        if len(markers) >= 8:
            break
    return markers


def _repo_evidence_gate(repo_evidence: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    readmes = _repo_evidence_rows(repo_evidence, "readmes")
    docs = _repo_evidence_rows(repo_evidence, "documentation_files")
    configs = _repo_evidence_rows(repo_evidence, "config_files")
    entrypoint_rows = _repo_evidence_rows(repo_evidence, "python_entrypoints")
    doc_rows = [*readmes, *docs]
    doc_commands = _repo_command_lines(doc_rows)
    install_commands = [command for command in doc_commands if REPO_INSTALL_COMMAND_RE.search(command)]
    reproduction_commands = [command for command in doc_commands if REPO_REPRO_COMMAND_RE.search(command)]
    doc_text = _repo_document_text(doc_rows)
    docs_mention_install = bool(REPO_INSTALL_TEXT_RE.search(doc_text))
    docs_mention_reproduction = bool(REPO_REPRO_TEXT_RE.search(doc_text))
    negative_install_markers = _repo_negative_guidance_markers(doc_text, REPO_INSTALL_GUIDANCE_TOKENS)
    negative_reproduction_markers = _repo_negative_guidance_markers(doc_text, REPO_REPRO_GUIDANCE_TOKENS)
    install_text_guidance_ok = bool(docs_mention_install and configs and not negative_install_markers)
    reproduction_text_guidance_ok = bool(docs_mention_reproduction and not negative_reproduction_markers)
    reproduction_entrypoints = [row for row in entrypoint_rows if _repo_entrypoint_has_reproduction_role(row)]
    doc_count = len(readmes) + len(docs)
    has_readme_or_docs = doc_count > 0
    has_install_guidance = bool(install_commands or install_text_guidance_ok)
    has_reproduction_guidance = bool(reproduction_commands or (reproduction_text_guidance_ok and reproduction_entrypoints))
    has_actionable_documentation = bool(has_readme_or_docs and has_install_guidance and has_reproduction_guidance)
    evidence = {
        "repo_path": repo_evidence.get("repo_path", ""),
        "readme_count": len(readmes),
        "documentation_file_count": len(docs),
        "config_file_count": len(configs),
        "python_entrypoint_count": len(entrypoint_rows),
        "command_line_count": len(doc_commands),
        "install_command_count": len(install_commands),
        "reproduction_command_count": len(reproduction_commands),
        "docs_mention_install": docs_mention_install,
        "docs_mention_reproduction": docs_mention_reproduction,
        "negative_install_guidance_markers": negative_install_markers,
        "negative_reproduction_guidance_markers": negative_reproduction_markers,
        "install_text_guidance_ok": install_text_guidance_ok,
        "reproduction_text_guidance_ok": reproduction_text_guidance_ok,
        "has_readme_or_docs": has_readme_or_docs,
        "has_install_guidance": has_install_guidance,
        "has_reproduction_guidance": has_reproduction_guidance,
        "has_actionable_documentation": has_actionable_documentation,
        "evidence_summary": repo_evidence.get("evidence_summary", {}),
        "sample_documents": [row.get("relative_path") or row.get("path") for row in doc_rows[:8]],
        "sample_configs": [row.get("relative_path") or row.get("path") for row in configs[:8]],
        "sample_entrypoints": [row.get("relative_path") or row.get("path") for row in entrypoint_rows[:8]],
        "sample_install_commands": install_commands[:5],
        "sample_reproduction_commands": reproduction_commands[:5],
        "dry_run": repo_evidence.get("dry_run") is True,
    }
    return has_actionable_documentation, evidence


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


def _alignment_has_dataset(plan: dict[str, Any]) -> bool:
    rows = plan.get("paper_config_alignment")
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_text = " ".join(str(row.get(key) or "") for key in ["paper_item", "paper_field", "claim", "setting", "category"]).lower()
        status = str(row.get("match_status") or row.get("status") or "").strip().lower()
        if any(token in item_text for token in ["dataset", "data", "数据集", "数据"]) and _alignment_row_supports_approval(row):
            return True
    return False


def _dataset_compact_text(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def _dataset_spaced_text(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", str(value or "").lower()).strip()


def _split_dataset_candidate_text(text: str) -> list[str]:
    clean = str(text or "").strip()
    if not clean:
        return []
    parts = [part.strip() for part in re.split(r"[,;|，；、]+|\s+/\s+|\s+and\s+", clean) if part.strip()]
    return [clean, *[part for part in parts if part != clean]]


def _dataset_candidate_texts(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in DATASET_NAME_KEYS:
                out.extend(_dataset_candidate_texts(item))
    elif isinstance(value, list):
        for item in value[:20]:
            out.extend(_dataset_candidate_texts(item))
    else:
        out.extend(_split_dataset_candidate_text(str(value or "")))
    return out


def _dataset_name_candidates(paper_dataset: list[Any]) -> list[str]:
    candidates: list[str] = []
    for item in paper_dataset:
        for text in _dataset_candidate_texts(item):
            compact = _dataset_compact_text(text)
            if len(compact) < 3 or compact in GENERIC_DATASET_NAMES:
                continue
            if text not in candidates:
                candidates.append(text)
    return candidates


def _dataset_compact_name_occurs_with_boundary(text: str, dataset_name: str) -> bool:
    compact_name = _dataset_compact_text(dataset_name)
    if len(compact_name) < 3:
        return False
    lowered = str(text or "").lower()
    dataset_pattern = "".join(r"[^a-z0-9\u4e00-\u9fff]*" + re.escape(char) for char in compact_name)
    boundary_pattern = rf"(?<![a-z0-9\u4e00-\u9fff]){dataset_pattern}(?![a-z0-9\u4e00-\u9fff])"
    return bool(re.search(boundary_pattern, lowered, flags=re.IGNORECASE))


def _text_matches_dataset_name(text: str, dataset_name: str) -> bool:
    compact_name = _dataset_compact_text(dataset_name)
    spaced_name = _dataset_spaced_text(dataset_name)
    if len(compact_name) < 3:
        return False
    if _dataset_compact_name_occurs_with_boundary(text, dataset_name):
        return True
    spaced_text = _dataset_spaced_text(text)
    return bool(spaced_name and len(spaced_name) >= 3 and re.search(rf"(?<![a-z0-9\u4e00-\u9fff]){re.escape(spaced_name)}(?![a-z0-9\u4e00-\u9fff])", spaced_text, flags=re.IGNORECASE))


def _dataset_alignment_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    rows = plan.get("paper_config_alignment")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not _alignment_row_supports_approval(row):
            continue
        item_text = " ".join(str(row.get(key) or "") for key in ["paper_item", "paper_field", "claim", "setting", "category"]).lower()
        if any(token in item_text for token in ["dataset", "data", "corpus", "benchmark", "数据集", "数据"]):
            out.append(row)
    return out


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


def _optional_successful_dataset_receipts(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for receipt in receipts:
        phase = str(receipt.get("phase") or "").strip()
        if not phase or int(receipt.get("return_code") or 0) != 0:
            continue
        if receipt.get("required") is False and _phase_indicates_dataset(phase):
            rows.append(receipt)
    return rows


def _receipt_dataset_text(receipt: dict[str, Any]) -> str:
    return "\n".join([str(receipt.get(key) or "") for key in ["phase", "command"]] + [_receipt_output_text(receipt)])


def _dataset_binding_evidence(plan: dict[str, Any], receipts: list[dict[str, Any]], dataset_names: list[str]) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    for row in _dataset_alignment_rows(plan):
        text = _alignment_row_text(row)
        for name in dataset_names:
            if _text_matches_dataset_name(text, name):
                evidence.append({"source": "paper_config_alignment", "dataset": name, "excerpt": text[:300]})
                break
    for receipt in _successful_dataset_receipts(receipts):
        text = _receipt_dataset_text(receipt)
        for name in dataset_names:
            if _text_matches_dataset_name(text, name):
                evidence.append({"source": f"receipt:{receipt.get('phase')}", "dataset": name, "excerpt": text[:300]})
                break
    return evidence[:12]


def _dataset_receipt_binding_evidence(plan: dict[str, Any], receipts: list[dict[str, Any]], dataset_names: list[str]) -> list[dict[str, str]]:
    return [row for row in _dataset_binding_evidence(plan, receipts, dataset_names) if str(row.get("source") or "").startswith("receipt:")]


def _dataset_synthetic_markers(plan: dict[str, Any], receipts: list[dict[str, Any]]) -> list[dict[str, str]]:
    markers: list[dict[str, str]] = []
    texts: list[tuple[str, str]] = [("paper_config_alignment", _alignment_row_text(row)) for row in _dataset_alignment_rows(plan)]
    texts.extend((f"receipt:{receipt.get('phase')}", _receipt_dataset_text(receipt)) for receipt in _successful_dataset_receipts(receipts))
    for source, text in texts:
        lowered = text.lower()
        for marker in SYNTHETIC_DATASET_MARKERS:
            if marker in lowered:
                markers.append({"source": source, "marker": marker, "excerpt": text[:300]})
                break
    return markers[:12]


def _dataset_name_windows(text: str, dataset_name: str, radius: int = 120) -> list[str]:
    lowered = str(text or "").lower()
    variants = [str(dataset_name or "").strip().lower(), _dataset_spaced_text(dataset_name)]
    compact_name = _dataset_compact_text(dataset_name)
    compact_text = _dataset_compact_text(text)
    windows: list[str] = []
    for variant in [item for item in variants if item]:
        start = 0
        while True:
            index = lowered.find(variant, start)
            if index < 0:
                break
            windows.append(lowered[max(0, index - radius): index + len(variant) + radius])
            start = index + max(1, len(variant))
    if compact_name and compact_name in compact_text and not windows:
        windows.append(lowered[: min(len(lowered), radius * 2)])
    return windows


def _dataset_negative_or_replacement_markers(plan: dict[str, Any], receipts: list[dict[str, Any]], dataset_names: list[str]) -> list[dict[str, str]]:
    markers: list[dict[str, str]] = []
    if not dataset_names:
        return markers
    texts: list[tuple[str, str]] = [("paper_config_alignment", _alignment_row_text(row)) for row in _dataset_alignment_rows(plan)]
    texts.extend((f"receipt:{receipt.get('phase')}", _receipt_dataset_text(receipt)) for receipt in _successful_dataset_receipts(receipts))
    for source, text in texts:
        for dataset_name in dataset_names:
            if not _text_matches_dataset_name(text, dataset_name):
                continue
            for window in _dataset_name_windows(text, dataset_name):
                for marker in DATASET_NEGATIVE_OR_REPLACEMENT_MARKERS:
                    if marker in window:
                        markers.append({"source": source, "dataset": dataset_name, "marker": marker, "excerpt": text[:300]})
                        break
                if markers and markers[-1].get("source") == source and markers[-1].get("dataset") == dataset_name:
                    break
    return markers[:12]


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


def _successful_dataset_phases(receipts: list[dict[str, Any]]) -> list[str]:
    phases: list[str] = []
    for receipt in _successful_dataset_receipts(receipts):
        phase = str(receipt.get("phase") or "").strip()
        if phase and phase not in phases:
            phases.append(phase)
    return phases


def _dataset_gate(env_plan: dict[str, Any], paper_evidence: dict[str, Any], receipts: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
    paper_dataset = paper_evidence.get("dataset") if isinstance(paper_evidence.get("dataset"), list) else []
    paper_dataset_items = [item for item in paper_dataset if str(item).strip()]
    has_paper_dataset = bool(paper_dataset_items)
    dataset_names = _dataset_name_candidates(paper_dataset)
    alignment_has_dataset = _alignment_has_dataset(env_plan)
    successful_dataset_phases = _successful_dataset_phases(receipts)
    optional_dataset_receipts = _optional_successful_dataset_receipts(receipts)
    receipts_have_dataset = bool(successful_dataset_phases)
    binding_evidence = _dataset_binding_evidence(env_plan, receipts, dataset_names)
    receipt_binding_evidence = _dataset_receipt_binding_evidence(env_plan, receipts, dataset_names)
    synthetic_markers = _dataset_synthetic_markers(env_plan, receipts)
    negative_or_replacement_markers = _dataset_negative_or_replacement_markers(env_plan, receipts, dataset_names)
    dataset_name_binding_ok = bool(dataset_names and binding_evidence)
    dataset_receipt_binding_ok = bool(dataset_names and receipt_binding_evidence)
    no_synthetic_markers = not synthetic_markers
    no_negative_or_replacement_markers = not negative_or_replacement_markers
    command_phases = [str(row.get("phase") or "") for row in env_plan.get("commands", []) if isinstance(row, dict)] if isinstance(env_plan.get("commands"), list) else []
    return bool(has_paper_dataset and dataset_name_binding_ok and dataset_receipt_binding_ok and alignment_has_dataset and receipts_have_dataset and no_synthetic_markers and no_negative_or_replacement_markers), {
        "paper_dataset_count": len(paper_dataset_items),
        "dataset_name_candidates": dataset_names[:12],
        "dataset_name_binding_passed": dataset_name_binding_ok,
        "dataset_name_binding_evidence": binding_evidence,
        "dataset_receipt_binding_passed": dataset_receipt_binding_ok,
        "dataset_receipt_binding_evidence": receipt_binding_evidence,
        "synthetic_or_toy_markers": synthetic_markers,
        "negative_or_replacement_dataset_markers": negative_or_replacement_markers,
        "alignment_has_dataset": alignment_has_dataset,
        "successful_dataset_phase": receipts_have_dataset,
        "successful_dataset_phases": successful_dataset_phases,
        "ignored_optional_dataset_phase_count": len(optional_dataset_receipts),
        "ignored_optional_dataset_phases": [str(row.get("phase") or "") for row in optional_dataset_receipts[:8]],
        "sample_ignored_optional_dataset_commands": [str(row.get("command") or "") for row in optional_dataset_receipts[:3]],
        "command_phases": command_phases,
        "accepted_phase_examples": sorted(DATASET_PHASE_EXACT | DATASET_PHASE_PHRASES)[:20],
    }


PAPER_CONTEXT_SOURCE_HINTS = (
    "local_file:",
    "url:",
    "plan.paper",
    "plan.paper_text",
    "plan.paper_notes",
    "plan.training",
    "plan.train",
    "plan.reproduction",
    "plan.hyperparameters",
    "plan.expected_results",
    "plan.results",
    "plan.evaluation",
)


def _paper_context_sources(paper_evidence: dict[str, Any]) -> list[str]:
    blocks = paper_evidence.get("text_blocks") if isinstance(paper_evidence.get("text_blocks"), list) else []
    sources: list[str] = []
    for row in blocks:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "").strip()
        text = str(row.get("text") or "").strip()
        if source and text:
            sources.append(source)
    return sources


def _paper_context_source_is_substantive(source: str) -> bool:
    lowered = str(source or "").lower()
    return any(hint in lowered for hint in PAPER_CONTEXT_SOURCE_HINTS)


def _paper_context_gate(paper_evidence: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    target_metrics = paper_evidence.get("target_metrics") if isinstance(paper_evidence.get("target_metrics"), list) else []
    claims = paper_evidence.get("paper_claims_or_training_signals") if isinstance(paper_evidence.get("paper_claims_or_training_signals"), list) else []
    sources = _paper_context_sources(paper_evidence)
    substantive_sources = [source for source in sources if _paper_context_source_is_substantive(source)]
    local_paper = paper_evidence.get("local_paper") if isinstance(paper_evidence.get("local_paper"), dict) else {}
    url_fetch = paper_evidence.get("url_fetch") if isinstance(paper_evidence.get("url_fetch"), dict) else {}
    local_ok = local_paper.get("status") == "passed" and bool(str(local_paper.get("text_excerpt") or "").strip())
    url_ok = url_fetch.get("status") == "passed" and bool(str(url_fetch.get("text_excerpt") or "").strip())
    has_metric_targets = bool([row for row in target_metrics if str(row).strip()])
    has_claim_signals = bool([row for row in claims if str(row).strip()])
    has_substantive_source = bool(substantive_sources or local_ok or url_ok)
    evidence = {
        "has_paper_context": bool(paper_evidence.get("has_paper_context")),
        "target_metric_count": len([row for row in target_metrics if str(row).strip()]),
        "claim_signal_count": len([row for row in claims if str(row).strip()]),
        "text_block_count": len(sources),
        "substantive_source_count": len(substantive_sources) + int(local_ok) + int(url_ok),
        "sample_sources": sources[:8],
        "substantive_sources": substantive_sources[:8],
        "local_paper_status": local_paper.get("status", ""),
        "url_fetch_status": url_fetch.get("status", ""),
    }
    return bool(has_metric_targets and has_claim_signals and has_substantive_source), evidence


def _ensure_approval_gate_for_early_decision(decision: dict[str, Any], paper_evidence: dict[str, Any]) -> dict[str, Any]:
    if _approval_gate_from_decision(decision):
        return decision
    verdict = decision.get("verdict") if isinstance(decision.get("verdict"), dict) else {}
    repo_info = decision.get("repo") if isinstance(decision.get("repo"), dict) else {}
    repo_ok, repo_gate_evidence = _repo_source_gate(repo_info)
    paper_ok, paper_context_evidence = _paper_context_gate(paper_evidence)
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
            paper_ok,
            "存在可审计的论文/训练配置来源、目标指标和训练/结果信号" if paper_ok else "缺少可审计的论文/训练配置来源、目标指标或训练/结果信号",
            paper_context_evidence,
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


def build_approval_gate(
    verdict: dict[str, Any],
    receipts: list[dict[str, Any]],
    paper_evidence: dict[str, Any],
    env_plan: dict[str, Any],
    repo_info: dict[str, Any],
    repo_evidence: dict[str, Any],
    machine: dict[str, Any],
    run_dir: Path,
    criteria_passed: bool,
    metric_evidence: list[dict[str, Any]],
    verdict_metric_ok: bool,
    verdict_metric_issues: list[str],
    paper_alignment_ok: bool,
    paper_alignment_issues: list[str],
) -> dict[str, Any]:
    required_failures = [row for row in receipts if row.get("return_code") != 0 and row.get("required") is not False]
    required_ok = bool(receipts and not required_failures)
    claims_ok = bool(verdict.get("paper_claims_verified") is True and verdict.get("reproduction_success") is True)
    criteria_schema_issues = success_criteria_issues(env_plan.get("success_criteria"))
    criteria_schema_ok = not criteria_schema_issues
    criteria_paper_ok, criteria_paper_evidence = _success_criteria_paper_binding_gate(env_plan, paper_evidence)
    metric_ok = bool(criteria_passed or verdict_metric_ok)
    paper_ok, paper_context_evidence = _paper_context_gate(paper_evidence)
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
    repo_doc_ok, repo_doc_evidence = _repo_evidence_gate(repo_evidence)
    conda_ok, conda_gate_evidence = _conda_environment_gate(env_plan, receipts, run_dir)
    machine_ok, machine_gate_evidence = _machine_fit_gate(env_plan, machine)
    dataset_ok, dataset_gate_evidence = _dataset_gate(env_plan, paper_evidence, receipts)
    checks = [
        _approval_check(
            "repository_source",
            repo_ok,
            "GitHub 仓库已克隆、版本已固定并记录 head_commit" if repo_ok else "缺少可信 GitHub 仓库克隆证据，或指定 commit 未成功 checkout/匹配 HEAD",
            repo_gate_evidence,
        ),
        _approval_check(
            "repository_documentation",
            repo_doc_ok,
            "README/docs 已提供安装/依赖和训练/评估/复现运行指导" if repo_doc_ok else "缺少 README/docs 中可支撑自适应部署的安装/依赖指导或训练/评估/复现运行指导",
            repo_doc_evidence,
        ),
        _approval_check(
            "conda_environment",
            conda_ok,
            "Conda 环境 prefix、依赖安装和导入/运行验证通过" if conda_ok else "缺少可审计的 Conda 环境部署或导入/运行验证证据",
            conda_gate_evidence,
        ),
        _approval_check(
            "machine_fit",
            machine_ok,
            "已证明论文运行要求适合本机或已有合理本机适配" if machine_ok else "缺少本机资源适配评估或硬件/算力对齐证据",
            machine_gate_evidence,
        ),
        _approval_check(
            "dataset_evidence",
            dataset_ok,
            "数据集证据、对齐表和数据准备回执通过" if dataset_ok else "缺少真实数据集证据、数据集对齐项或成功的数据准备阶段",
            dataset_gate_evidence,
        ),
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
        _approval_check(
            "success_criteria_paper_binding",
            criteria_paper_ok,
            "success_criteria 已逐项绑定到论文目标指标" if criteria_paper_ok else "success_criteria 未能逐项绑定到 paper_evidence.target_metrics 中的论文目标指标",
            criteria_paper_evidence,
        ),
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
        _approval_check(
            "paper_context",
            paper_ok,
            "存在可审计的论文/训练配置来源、目标指标和训练/结果信号" if paper_ok else "缺少可审计的论文/训练配置来源、目标指标或训练/结果信号",
            paper_context_evidence,
        ),
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
        _approval_check(
            "paper_config_alignment",
            paper_alignment_ok,
            "论文配置对齐表通过" if paper_alignment_ok else "论文配置对齐表缺失或存在关键问题",
            paper_alignment_issues[:10],
        ),
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


def _drop_deprecated_hf_download_flags(tokens: list[str]) -> list[str]:
    return [str(token) for token in tokens if str(token) != "--resume-download"]


def _normalize_python_inline_code_imports(code: str) -> str:
    updated = re.sub(r"\bfrom\s+dm_tree\b", "from tree", str(code))
    updated = re.sub(r"\bimport\s+dm_tree\s+as\s+([A-Za-z_][A-Za-z0-9_]*)", r"import tree as \1", updated)
    updated = re.sub(r"\bimport\s+dm_tree\b(?!\s+as\b)", "import tree as dm_tree", updated)
    return updated


def normalize_python_inline_imports(tokens: list[str]) -> list[str]:
    if not tokens:
        return tokens
    normalized = [str(token) for token in tokens]
    if Path(str(normalized[0] or "")).name not in {"python", "python3"}:
        return normalized
    for index, token in enumerate(normalized[:-1]):
        if token != "-c":
            continue
        normalized[index + 1] = _normalize_python_inline_code_imports(normalized[index + 1])
        break
    return normalized


def _direct_env_entrypoint_command(tokens: list[str], env_prefix: Path) -> list[str]:
    if not tokens:
        return []
    tokens = _drop_deprecated_hf_download_flags(tokens)
    head = Path(str(tokens[0] or "")).name
    if head in {"pip", "pip3"}:
        return [str(env_prefix / "bin" / "python"), "-m", "pip", *tokens[1:]]
    if head == "huggingface-cli" and len(tokens) > 1 and str(tokens[1]) == "download":
        return [str(env_prefix / "bin" / "hf"), *tokens[1:]]
    if head in RUN_ENV_ENTRYPOINTS:
        executable = "python" if head in {"python", "python3"} else head
        return normalize_python_inline_imports([str(env_prefix / "bin" / executable), *tokens[1:]])
    return []


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
                return normalize_python_inline_imports(direct)
        return normalize_python_inline_imports(tokens)
    if env_name:
        direct = _direct_env_entrypoint_command(tokens, env_prefix)
        if direct:
            return normalize_python_inline_imports(direct)
    return normalize_python_inline_imports(tokens)


def _migrate_deprecated_huggingface_cli_script(text: str) -> str:
    updated = re.sub(r"\bhuggingface-cli\s+download\b", "hf download", text)
    updated = re.sub(r"(?m)^[ \t]*--resume-download[ \t]*\\?[ \t]*(?:#.*)?\n", "", updated)
    updated = re.sub(r"[ \t]+--resume-download(?=[ \t\n]|$)", "", updated)
    return updated


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


def normalize_generated_script_commands_for_command(tokens: list[str], cwd: Path, run_dir: Path) -> list[dict[str, str]]:
    migrations: list[dict[str, str]] = []
    for token in _command_shell_script_tokens(tokens):
        script_path = _resolve_run_script_path(token, cwd, run_dir)
        if script_path is None or not script_path.is_file():
            continue
        try:
            before = script_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        after = _migrate_deprecated_huggingface_cli_script(before)
        if after == before:
            continue
        script_path.write_text(after, encoding="utf-8")
        migrations.append({
            "path": str(script_path),
            "migration": "huggingface-cli download -> hf download; removed --resume-download",
        })
    return migrations




def command_required(row: dict[str, Any]) -> bool:
    return row.get("required") is not False


def validate_environment_plan(plan: dict[str, Any], require_full_reproduction: bool, repo_path: Path | None = None, run_dir: Path | None = None, machine: dict[str, Any] | None = None, paper_evidence: dict[str, Any] | None = None) -> list[str]:
    issues: list[str] = []
    status = str(plan.get("status") or "").strip()
    if status not in {"ready_to_execute", "ready", "execute", "approved"}:
        issues.append(f"环境计划 status 不是 ready_to_execute/ready/execute/approved：{status or 'missing'}")
    if not str(plan.get("env_name") or "").strip():
        issues.append("环境计划缺少 env_name")
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
    if require_full_reproduction and paper_evidence is not None and not criteria_schema_issues:
        criteria_paper_ok, criteria_paper_evidence = _success_criteria_paper_binding_gate(plan, paper_evidence)
        if not criteria_paper_ok:
            binding_issues = [str(item) for item in criteria_paper_evidence.get("issues", []) if str(item).strip()]
            suffix = "；" + "；".join(binding_issues[:5]) if binding_issues else ""
            issues.append("success_criteria 未能逐项绑定到 paper_evidence.target_metrics 中的论文/plan 目标指标；不能执行完整复现计划" + suffix)
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
    issues.extend(machine_assessment_issues(plan, machine))
    if require_full_reproduction:
        alignment_ok, alignment_issues = paper_config_alignment_passed(plan)
        if not alignment_ok:
            issues.extend(alignment_issues)
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

def command_rows(plan: dict[str, Any], include_full: bool, default_timeout: int = 3600) -> list[dict[str, Any]]:
    rows = plan.get("commands") if isinstance(plan.get("commands"), list) else []
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            row = {"phase": "unspecified", "command": row}
        phase = str(row.get("phase") or "unspecified").strip()
        if phase == "reproduce_full" and not include_full:
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


def _rigidssl_model_probe_code(repo_path: Path) -> str:
    repo = str(repo_path.expanduser().resolve())
    examples = str((repo_path / "examples").expanduser().resolve())
    return (
        "import sys; "
        f"sys.path[:0] = [{examples!r}, {repo!r}]; "
        "from RigidSSL_Perturb import model_setup; "
        "model = model_setup(); "
        "print('RigidSSL VelocityNetwork parameters', sum(p.numel() for p in model.parameters()))"
    )


def _rigidssl_loader_probe_code(repo_path: Path, run_dir: Path) -> str:
    repo = str(repo_path.expanduser().resolve())
    examples = str((repo_path / "examples").expanduser().resolve())
    data_dir = str((run_dir / "data" / "RigidSSL_Perturb_data").expanduser().resolve())
    return (
        "import os, sys; "
        f"sys.path[:0] = [{examples!r}, {repo!r}]; "
        "os.environ.setdefault('TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD', '1'); "
        "from config import args; "
        "args.batch_size = 1; args.train_number = 1; args.dataset_portion = 'full'; "
        "import RigidSSL_Perturb as rigidssl_perturb; "
        "from torch_geometric.loader import DataLoader; "
        "rigidssl_perturb.DataLoaderClass = DataLoader; "
        "rigidssl_perturb.dataloader_kwargs = {}; "
        f"loader = rigidssl_perturb.load_dataset({data_dir!r}, '1', args); "
        "batch = next(iter(loader)); "
        "model = rigidssl_perturb.model_setup(); "
        "print('RigidSSL loader probe batch_graphs', getattr(batch, 'num_graphs', 'unknown'), 'model_params', sum(p.numel() for p in model.parameters()))"
    )


def _is_rigidssl_repo(repo_path: Path) -> bool:
    return (repo_path / "examples" / "RigidSSL_Perturb.py").is_file() and (repo_path / "model" / "velocity_network.py").is_file()


def normalize_repository_command_for_execution(row: dict[str, Any], command: list[str], repo_path: Path, run_dir: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not _is_rigidssl_repo(repo_path):
        return command, []
    phase = str(row.get("phase") or "").strip().lower()
    text = command_text(command)
    if phase == "verify_model" and "VelocityNetwork()" in text:
        return [command[0], "-c", _rigidssl_model_probe_code(repo_path)], [{"migration": "RigidSSL VelocityNetwork requires model_conf; replaced no-arg constructor with examples.RigidSSL_Perturb.model_setup probe"}]
    if phase == "reproduce_smoke" and any(Path(str(token)).name == "RigidSSL_Perturb.py" for token in command):
        return [command[0], "-c", _rigidssl_loader_probe_code(repo_path, run_dir)], [{"migration": "RigidSSL smoke uses a bounded loader/model probe instead of a full training epoch in skip-full-reproduction mode"}]
    return command, []


def command_environment(command_env: dict[str, str], repo_path: Path, row_env: dict[str, Any]) -> dict[str, str]:
    effective_env = dict(command_env)
    if repo_path.exists():
        repo = str(repo_path.expanduser().resolve())
        existing = str(effective_env.get("PYTHONPATH") or "")
        effective_env["PYTHONPATH"] = repo if not existing else repo + os.pathsep + existing
    if _is_rigidssl_repo(repo_path):
        effective_env.setdefault("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", "1")
    effective_env.update({str(key): str(value) for key, value in row_env.items()})
    return effective_env


def execute_plan_commands(plan: dict[str, Any], repo_path: Path, run_dir: Path, round_dir: Path, include_full: bool, default_timeout: int, command_env: dict[str, str]) -> list[dict[str, Any]]:
    conda_exe = find_conda_executable()
    env_name = str(plan.get("env_name") or "").strip()
    env_prefix = env_prefix_for(run_dir, env_name)
    env_prefix.parent.mkdir(parents=True, exist_ok=True)
    receipts: list[dict[str, Any]] = []
    for row in command_rows(plan, include_full, default_timeout=default_timeout):
        command = rewrite_command(row.get("command"), conda_exe, env_name, env_prefix)
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
            script_migrations.extend(normalize_generated_script_commands_for_command(command, cwd, run_dir))
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
            row_env = row.get("env") if isinstance(row.get("env"), dict) else {}
            effective_env = command_environment(command_env, repo_path, row_env)
            receipt = run_logged(command, cwd=cwd, log_path=log_path, timeout_sec=row.get("timeout_sec"), env=effective_env, required=bool(row.get("required")))
            receipt["phase"] = row.get("phase")
            receipt["conda_env_prefix"] = str(env_prefix)
            receipt["uses_conda_prefix"] = uses_conda_prefix
            receipt["script_migrations"] = script_migrations
            receipt["env_keys"] = sorted(row_env.keys())
            receipt["inline_env_keys"] = row.get("inline_env_keys", [])
        receipts.append(receipt)
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


def _approval_gate_passed_check_names(gate: dict[str, Any]) -> set[str]:
    checks = gate.get("checks") if isinstance(gate.get("checks"), list) else []
    return {str(row.get("name") or "").strip() for row in checks if isinstance(row, dict) and row.get("passed") is True}


def _approval_gate_checks_all_passed(gate: dict[str, Any]) -> bool:
    checks = gate.get("checks") if isinstance(gate.get("checks"), list) else []
    if not checks:
        return False
    for row in checks:
        if not isinstance(row, dict):
            return False
        if not str(row.get("name") or "").strip():
            return False
        if row.get("passed") is not True:
            return False
    return True


def _decision_policy_versions_current(decision: dict[str, Any]) -> bool:
    contract = decision.get("approval_contract") if isinstance(decision.get("approval_contract"), dict) else {}
    if str(decision.get("decision_policy_version") or "") != DECISION_POLICY_VERSION:
        return False
    contract_version = str(contract.get("policy_version") or "")
    return contract_version == DECISION_POLICY_VERSION


def _approval_gate_workspace_audit_checks_passed(gate: dict[str, Any]) -> bool:
    checks = gate.get("checks") if isinstance(gate.get("checks"), list) else []
    workspace_checks = [
        row for row in checks
        if isinstance(row, dict) and str(row.get("name") or "").strip() == "workspace_write_audit"
    ]
    if not workspace_checks:
        return False
    return all(row.get("passed") is True for row in workspace_checks)


def _approval_gate_computed_missing(gate: dict[str, Any]) -> list[str] | None:
    checks = gate.get("checks") if isinstance(gate.get("checks"), list) else []
    if not checks:
        return None
    missing: list[str] = []
    for row in checks:
        if not isinstance(row, dict):
            return None
        name = str(row.get("name") or "").strip()
        if not name:
            return None
        if row.get("passed") is not True:
            missing.append(str(row.get("reason") or name or "未命名门槛"))
    return missing


def _approval_gate_result_consistent_with_checks(gate: dict[str, Any]) -> bool:
    computed_missing = _approval_gate_computed_missing(gate)
    if computed_missing is None:
        return False
    gate_missing = gate.get("missing")
    if gate_missing is None:
        gate_missing_values: list[str] = []
    elif isinstance(gate_missing, list):
        gate_missing_values = [str(item) for item in gate_missing]
    elif isinstance(gate_missing, str) and not gate_missing.strip():
        gate_missing_values = []
    else:
        return False
    expected_passed = not computed_missing
    return gate.get("passed") is expected_passed and gate_missing_values == computed_missing


def _cached_terminal_approval_gate_policy_current(decision: dict[str, Any]) -> bool:
    gate = _approval_gate_from_decision(decision)
    if not gate:
        return False
    return str(gate.get("policy_version") or "") == DECISION_POLICY_VERSION


def _cached_terminal_approval_gate_result_matches_decision(decision: dict[str, Any]) -> bool:
    gate = _approval_gate_from_decision(decision)
    if not gate:
        return False
    cached_decision = str(decision.get("decision") or "").strip().lower()
    missing = gate.get("missing")
    missing_empty = missing in (None, [], "")
    if cached_decision == "approve":
        return gate.get("passed") is True and missing_empty
    if cached_decision == "reject":
        return gate.get("passed") is False and not missing_empty
    return False


def _cached_terminal_workspace_audit_passed(decision: dict[str, Any]) -> bool:
    audit = decision.get("workspace_write_audit") if isinstance(decision.get("workspace_write_audit"), dict) else {}
    if audit.get("status") != "passed":
        return False
    gate = _approval_gate_from_decision(decision)
    return _approval_gate_workspace_audit_checks_passed(gate)


def _cached_terminal_exit_code_matches(decision: dict[str, Any]) -> bool:
    expected = {"approve": 0, "reject": 20}.get(str(decision.get("decision") or "").strip().lower())
    if expected is None:
        return False
    try:
        return int(decision.get("exit_code")) == expected
    except Exception:
        return False


def _cached_terminal_verdict_matches_top_level(decision: dict[str, Any]) -> bool:
    verdict = decision.get("verdict") if isinstance(decision.get("verdict"), dict) else {}
    if not verdict:
        return False
    top_decision = str(decision.get("decision") or "").strip().lower()
    raw_nested_decision = str(verdict.get("decision") or verdict.get("status") or "").strip().lower()
    if not raw_nested_decision:
        return False
    nested_decision_aliases = {
        "approved": "approve",
        "approval": "approve",
        "pass": "approve",
        "passed": "approve",
        "rejected": "reject",
        "refuse": "reject",
        "refused": "reject",
        "fail_unrecoverable": "reject",
    }
    nested_decision = nested_decision_aliases.get(raw_nested_decision, raw_nested_decision)
    if nested_decision != top_decision:
        return False
    if "allow_next_module" not in verdict:
        return False
    if verdict.get("allow_next_module") is not decision.get("allow_next_module"):
        return False
    return True


def _cached_terminal_approval_gate_matches_top_level(decision: dict[str, Any]) -> bool:
    verdict = decision.get("verdict") if isinstance(decision.get("verdict"), dict) else {}
    if not verdict:
        return True
    top_gate = decision.get("approval_gate") if isinstance(decision.get("approval_gate"), dict) else {}
    nested_gate = verdict.get("approval_gate") if isinstance(verdict.get("approval_gate"), dict) else {}
    top_present = bool(top_gate)
    nested_present = bool(nested_gate)
    if top_present != nested_present:
        return False
    if not top_present:
        return True
    return top_gate == nested_gate


def _cached_terminal_failure_taxonomy_matches_verdict(decision: dict[str, Any]) -> bool:
    if str(decision.get("decision") or "").strip().lower() != "reject":
        return True
    if "failure_taxonomy" not in decision:
        return True
    verdict = decision.get("verdict") if isinstance(decision.get("verdict"), dict) else {}
    return decision.get("failure_taxonomy") == verdict.get("failure_taxonomy")


def _cached_terminal_repair_loop_matches_decision(decision: dict[str, Any]) -> bool:
    if str(decision.get("decision") or "").strip().lower() not in {"approve", "reject"}:
        return False
    repair_loop = decision.get("repair_loop") if isinstance(decision.get("repair_loop"), dict) else {}
    if not repair_loop:
        return False
    if repair_loop.get("terminal_reached") is not True:
        return False
    return str(repair_loop.get("stop_reason") or "").strip() == "terminal_decision"


def _cached_terminal_approve_claims_match_verdict(decision: dict[str, Any]) -> bool:
    if str(decision.get("decision") or "").strip().lower() != "approve":
        return True
    verdict = decision.get("verdict") if isinstance(decision.get("verdict"), dict) else {}
    return verdict.get("paper_claims_verified") is True and verdict.get("reproduction_success") is True


def _cached_terminal_approve_rounds_have_execution(decision: dict[str, Any]) -> bool:
    if str(decision.get("decision") or "").strip().lower() != "approve":
        return True
    rounds = decision.get("rounds") if isinstance(decision.get("rounds"), list) else []
    if not rounds:
        return False
    for row in rounds:
        if not isinstance(row, dict):
            continue
        try:
            receipt_count = int(row.get("receipt_count") or 0)
        except Exception:
            receipt_count = 0
        required_failures = row.get("required_failures")
        verdict = row.get("verdict") if isinstance(row.get("verdict"), dict) else {}
        if receipt_count > 0 and isinstance(required_failures, list) and not required_failures and str(verdict.get("decision") or "").strip().lower() == "approve":
            return True
    return False


def _cached_terminal_reproduce_full_receipt_summary_is_valid(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    if str(row.get("phase") or "").strip().lower() != "reproduce_full":
        return False
    try:
        if int(row.get("return_code")) != 0:
            return False
    except Exception:
        return False
    if row.get("required") is False:
        return False
    command = row.get("command")
    has_command = bool(command) if isinstance(command, list) else bool(str(command or "").strip())
    has_log = bool(str(row.get("log_path") or "").strip())
    return bool(has_command or has_log)


def _cached_terminal_approve_reproduce_full_evidence_present(decision: dict[str, Any]) -> bool:
    if str(decision.get("decision") or "").strip().lower() != "approve":
        return True
    gate = _approval_gate_from_decision(decision)
    checks = gate.get("checks") if isinstance(gate.get("checks"), list) else []
    reproduce_checks = [
        row for row in checks
        if isinstance(row, dict) and str(row.get("name") or "").strip() == "reproduce_full"
    ]
    if not reproduce_checks:
        return False
    for check in reproduce_checks:
        evidence = check.get("evidence") if isinstance(check.get("evidence"), dict) else {}
        receipts = evidence.get("successful_required_reproduce_full_receipts") if isinstance(evidence.get("successful_required_reproduce_full_receipts"), list) else []
        if any(_cached_terminal_reproduce_full_receipt_summary_is_valid(row) for row in receipts):
            return True
    return False


def _cached_terminal_metric_evidence_check_is_valid(check: dict[str, Any]) -> bool:
    if check.get("passed") is not True:
        return False
    evidence = check.get("evidence") if isinstance(check.get("evidence"), dict) else {}
    local_metric_evidence = evidence.get("local_metric_evidence") if isinstance(evidence.get("local_metric_evidence"), list) else []
    local_ok = evidence.get("local_criteria_passed") is True and bool(local_metric_evidence)
    claude_issues = evidence.get("claude_metric_evidence_issues")
    if claude_issues is None:
        claude_issues_values: list[Any] = []
    elif isinstance(claude_issues, list):
        claude_issues_values = claude_issues
    else:
        claude_issues_values = [claude_issues]
    claude_ok = evidence.get("claude_metric_evidence_passed") is True and not claude_issues_values
    return bool(local_ok or claude_ok)


def _cached_terminal_approve_metric_evidence_present(decision: dict[str, Any]) -> bool:
    if str(decision.get("decision") or "").strip().lower() != "approve":
        return True
    gate = _approval_gate_from_decision(decision)
    checks = gate.get("checks") if isinstance(gate.get("checks"), list) else []
    metric_checks = [
        row for row in checks
        if isinstance(row, dict) and str(row.get("name") or "").strip() == "metric_evidence"
    ]
    if not metric_checks:
        return False
    return all(_cached_terminal_metric_evidence_check_is_valid(row) for row in metric_checks)


def cached_approve_decision_is_current(decision: dict[str, Any]) -> bool:
    if str(decision.get("decision") or "").strip().lower() != "approve":
        return False
    if decision.get("allow_next_module") is not True:
        return False
    contract = decision.get("approval_contract") if isinstance(decision.get("approval_contract"), dict) else {}
    if contract.get("approved") is not True:
        return False
    if not _cached_terminal_approve_claims_match_verdict(decision):
        return False
    if not _cached_terminal_approve_rounds_have_execution(decision):
        return False
    if not _cached_terminal_approve_reproduce_full_evidence_present(decision):
        return False
    if not _cached_terminal_approve_metric_evidence_present(decision):
        return False
    gate = _approval_gate_from_decision(decision)
    if not gate or gate.get("passed") is not True:
        return False
    if str(gate.get("policy_version") or "") != DECISION_POLICY_VERSION:
        return False
    if gate.get("missing") not in (None, [], ""):
        return False
    if not _approval_gate_checks_all_passed(gate):
        return False
    passed_check_names = _approval_gate_passed_check_names(gate)
    return set(APPROVAL_GATE_REQUIRED_CHECKS).issubset(passed_check_names)


def cached_reject_decision_is_current(decision: dict[str, Any]) -> bool:
    if not _decision_policy_versions_current(decision):
        return False
    if str(decision.get("decision") or "").strip().lower() != "reject":
        return False
    if decision.get("allow_next_module") is not False:
        return False
    contract = decision.get("approval_contract") if isinstance(decision.get("approval_contract"), dict) else {}
    if contract.get("approved") is not False:
        return False
    verdict = decision.get("verdict") if isinstance(decision.get("verdict"), dict) else {}
    reject_source = dict(verdict if verdict else decision)
    reject_source["decision"] = "reject"
    checked = enforce_reject_evidence(reject_source)
    return checked.get("decision") == "reject" and not reject_evidence_issues(checked)


def cached_terminal_decision_is_current(decision: dict[str, Any]) -> bool:
    if not isinstance(decision, dict) or not _decision_policy_versions_current(decision):
        return False
    gate = _approval_gate_from_decision(decision)
    if not _cached_terminal_approval_gate_policy_current(decision):
        return False
    if not _approval_gate_result_consistent_with_checks(gate):
        return False
    if not _cached_terminal_approval_gate_result_matches_decision(decision):
        return False
    if not _cached_terminal_workspace_audit_passed(decision):
        return False
    if not _cached_terminal_exit_code_matches(decision):
        return False
    if not _cached_terminal_verdict_matches_top_level(decision):
        return False
    if not _cached_terminal_approval_gate_matches_top_level(decision):
        return False
    if not _cached_terminal_failure_taxonomy_matches_verdict(decision):
        return False
    if not _cached_terminal_repair_loop_matches_decision(decision):
        return False
    if not _cached_terminal_approve_claims_match_verdict(decision):
        return False
    if not _cached_terminal_approve_rounds_have_execution(decision):
        return False
    if not _cached_terminal_approve_reproduce_full_evidence_present(decision):
        return False
    if not _cached_terminal_approve_metric_evidence_present(decision):
        return False
    cached_decision = str(decision.get("decision") or "").strip().lower()
    if cached_decision == "approve":
        return cached_approve_decision_is_current(decision)
    if cached_decision == "reject":
        return cached_reject_decision_is_current(decision)
    return False


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
            "meaning": "allow_next_module=true 表示本模块确认参考复现达到论文声明，可进入下一个模块。",
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
    parser = argparse.ArgumentParser(description="给定实验 plan，让 Claude Code 自主部署环境并裁决参考复现。")
    parser.add_argument("--plan", required=True, help="实验 plan JSON 路径。")
    parser.add_argument("--work-root", default=str(default_work_root()), help="运行产物根目录，默认 modules/environment/runs。")
    parser.add_argument("--run-id", default="", help="可选 run_id。")
    parser.add_argument("--max-repair-rounds", type=int, default=None, help="显式设置后进入有限修复轮次模式；不设置时非 dry-run 默认持续修复直到 approve/reject。")
    parser.add_argument("--until-terminal", action="store_true", help="持续修复直到 approve/reject；可配合 --max-total-rounds 设总轮数上限。")
    parser.add_argument("--max-total-rounds", type=int, default=0, help="--until-terminal 模式的总轮数上限，0 表示不设轮数上限。")
    parser.add_argument("--claude-timeout-sec", type=int, default=2400, help="单次 Claude Code 调用超时。")
    parser.add_argument("--command-timeout-sec", type=int, default=3600, help="命令默认超时（Claude 未显式给出时使用）。")
    parser.add_argument("--run-full-reproduction", action="store_true", help="兼容旧参数；当前默认已经执行 reproduce_full。")
    parser.add_argument("--skip-full-reproduction", action="store_true", help="仅用于调试/烟测：跳过 reproduce_full，因此不会批准进入下一模块。")
    parser.add_argument("--dry-run", action="store_true", help="只生成提示词/结构，不调用 Claude、不执行重命令。")
    parser.add_argument("--force-rerun", action="store_true", help="即使 run_id 已有终态裁决，也重新执行。")
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
    raw_plan = load_experiment_plan(plan_path)
    normalized = normalize_plan(raw_plan, plan_path)
    work_root = resolve_work_root(args.work_root)
    requested_run_id = str(args.run_id or "").strip()
    run_id = normalize_run_id(requested_run_id, normalized)
    run_dir = ensure_within(work_root / run_id, work_root)
    run_dir.mkdir(parents=True, exist_ok=True)
    claude_env = isolated_runtime_env(run_dir, isolate_home=False)
    command_env = isolated_runtime_env(run_dir, isolate_home=True)
    baseline_outside_paths = _workspace_write_audit_baseline(run_dir)
    existing_decision_path = run_dir / "environment_deployment_decision.json"
    existing_decision = read_json(existing_decision_path, {}) if existing_decision_path.exists() and not args.force_rerun else {}
    if isinstance(existing_decision, dict) and existing_decision.get("decision") in {"approve", "reject"}:
        if cached_terminal_decision_is_current(existing_decision):
            print(json.dumps(existing_decision, ensure_ascii=False, indent=2))
            return int(existing_decision.get("exit_code") or (0 if existing_decision.get("allow_next_module") else 20))
        write_json(
            run_dir / "stale_terminal_decision_ignored.json",
            {
                "created_at": utc_now(),
                "reason": "已有终态裁决未满足当前 decision policy 和批准/拒绝门槛校验，必须按最新规则重新执行。",
                "current_policy_version": DECISION_POLICY_VERSION,
                "old_decision": existing_decision.get("decision"),
                "old_policy_version": existing_decision.get("decision_policy_version") or (existing_decision.get("approval_contract") or {}).get("policy_version"),
                "old_approval_gate_policy_version": _approval_gate_from_decision(existing_decision).get("policy_version"),
                "old_approval_gate_checks": sorted(_approval_gate_passed_check_names(_approval_gate_from_decision(existing_decision))),
            },
        )
    logs_dir = run_dir / "logs"
    repos_dir = run_dir / "repos"
    if requested_run_id and requested_run_id != run_id:
        normalized["requested_run_id"] = requested_run_id
        normalized["normalized_run_id"] = run_id
    write_json(run_dir / "input_plan.normalized.json", normalized)
    write_json(run_dir / "input_plan.raw.json", raw_plan)

    machine = detect_machine_profile()
    write_json(run_dir / "machine_profile.json", machine)
    paper_evidence = collect_paper_evidence(normalized, run_dir, allow_network=not args.dry_run, timeout_sec=90)

    repo_candidates = list(normalized.get("repo_candidates") or [])
    repo_spec_lookup = repo_specs_by_url(normalized)
    repo_selection_review: dict[str, Any] = {}
    if repo_candidates and not args.dry_run:
        review_path = run_dir / "claude_repo_candidate_review.json"
        review_result = run_claude_json(
            prompt_repo_candidate_review(normalized, [str(item) for item in repo_candidates], review_path),
            cwd=run_dir,
            expected_json_path=review_path,
            log_path=logs_dir / "claude_repo_candidate_review.log",
            timeout_sec=args.claude_timeout_sec,
            add_dirs=[run_dir],
            dry_run=False,
            system_prompt="你是严格 JSON 输出代理，只能审阅 prompt 中给出的仓库候选，不要编造仓库。",
            env=claude_env,
        )
        repo_selection_review = review_result
        reviewed = review_result.get("json") if isinstance(review_result.get("json"), dict) else {}
        if str(reviewed.get("status") or "").strip() == "reject":
            verdict = {
                "decision": "reject",
                "allow_next_module": False,
                "reject_reason": reviewed.get("reject_reason") or "Claude Code 判定 plan 中的 GitHub 仓库候选不可信",
                "failure_taxonomy": [{"category": "repository_unreliable", "evidence": reviewed.get("evidence") or [reviewed.get("reject_reason") or "候选仓库不可信"], "repairable": False}],
            }
            decision = final_decision_payload(run_id, run_dir, normalized, {"repo_candidates": repo_candidates, "repo_selection_review": repo_selection_review}, machine, [], verdict)
            write_json(run_dir / "repo_info.json", decision["repo"])
            return finalize_and_write_decision(decision, run_dir, paper_evidence, baseline_outside_paths)
        selected_candidates, review_issues, used_deterministic_fallback = repo_candidates_after_review([str(item) for item in repo_candidates], review_result)
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
        if used_deterministic_fallback:
            repo_selection_review["deterministic_fallback"] = {
                "used": True,
                "reason": "Claude repo review did not yield validated JSON, but the normalized plan already contains explicit GitHub candidates; continuing with original GitHub order.",
                "ordered_repo_urls": repo_candidates,
            }
    if not repo_candidates:
        discovery_path = run_dir / "claude_repo_discovery.json"
        repo_discovery_result = run_claude_json(
            prompt_repo_discovery(normalized, discovery_path),
            cwd=run_dir,
            expected_json_path=discovery_path,
            log_path=logs_dir / "claude_repo_discovery.log",
            timeout_sec=args.claude_timeout_sec,
            add_dirs=[run_dir],
            dry_run=args.dry_run,
            system_prompt="你是严格的 JSON 输出代理，只能基于证据选择 GitHub 仓库。",
            env=claude_env,
        )
        discovered = repo_discovery_result.get("json") if isinstance(repo_discovery_result.get("json"), dict) else {}
        discovered_repo_url, discovery_issues = validate_discovered_repo(discovered)
        if not discovery_issues:
            repo_candidates.append(discovered_repo_url)
            repo_spec_lookup[canonical_repo_url(discovered_repo_url)] = {
                "url": discovered_repo_url,
                "source": "claude_repo_discovery",
                "confidence": discovered.get("confidence", ""),
                "evidence": discovered.get("evidence", []),
            }
        elif not args.dry_run:
            verdict = {"decision": "reject", "allow_next_module": False, "reject_reason": discovered.get("reject_reason") or "Claude Code 未能确认可信 GitHub 仓库", "failure_taxonomy": [{"category": "repository_unreliable", "evidence": discovery_issues or [discovered.get("reject_reason") or "缺少可信仓库"], "repairable": False}]}
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
        repo_info = {}
        for candidate_url in github_candidates:
            candidate_spec = repo_spec_for_url(str(candidate_url), repo_spec_lookup)
            branch_or_tag, commit = clone_ref_from_spec(candidate_spec)
            attempt = clone_or_reuse(str(candidate_url), repos_dir=repos_dir, log_dir=logs_dir, branch=branch_or_tag, commit=commit, timeout_sec=900, env=claude_env)
            attempt["repo_candidate_spec"] = candidate_spec
            clone_attempts.append(attempt)
            if attempt.get("exists") and int((attempt.get("clone_receipt") or {}).get("return_code") or 0) == 0:
                repo_info = dict(attempt)
                selected_repo_url = str(candidate_url)
                break
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
    write_json(run_dir / "repo_info.json", repo_info)

    repo_path = Path(str(repo_info.get("repo_path") or ""))
    repo_evidence = collect_repo_evidence(repo_path) if repo_path.exists() else {"repo_path": str(repo_path), "readmes": [], "config_files": [], "dry_run": args.dry_run}
    write_json(run_dir / "repo_evidence.json", repo_evidence)

    rounds: list[dict[str, Any]] = []
    previous_rounds: list[dict[str, Any]] = []
    final_verdict: dict[str, Any] = {"decision": "continue_repair", "allow_next_module": False, "reason": "尚未开始"}
    if isinstance(existing_decision, dict) and existing_decision.get("decision") == "continue_repair":
        old_rounds = existing_decision.get("rounds") if isinstance(existing_decision.get("rounds"), list) else []
        rounds = [row for row in old_rounds if isinstance(row, dict)]
        previous_rounds = list(rounds)
        verdict = existing_decision.get("verdict") if isinstance(existing_decision.get("verdict"), dict) else {}
        if verdict:
            final_verdict = verdict

    start_round = len(previous_rounds) + 1
    rounds_before_invocation = len(rounds)
    if effective_until_terminal:
        end_round = (args.max_total_rounds + 1) if args.max_total_rounds > 0 else sys.maxsize
    else:
        end_round = start_round + effective_max_repair_rounds
    for round_index in range(start_round, end_round):
        round_dir = run_dir / f"round_{round_index:02d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        env_plan_path = round_dir / f"claude_environment_plan_round_{round_index:02d}.json"
        env_plan_result = run_claude_json(
            prompt_environment_plan(normalized, machine, repo_evidence, paper_evidence, previous_rounds, env_plan_path, round_index),
            cwd=run_dir,
            expected_json_path=env_plan_path,
            log_path=round_dir / "logs" / "claude_environment_plan.log",
            timeout_sec=args.claude_timeout_sec,
            add_dirs=[run_dir, repo_path] if repo_path.exists() else [run_dir],
            dry_run=args.dry_run,
            system_prompt="你是 TASTE environment 的后端环境部署代理，必须输出严格 JSON。",
            env=claude_env,
        )
        env_plan = env_plan_result.get("json") if isinstance(env_plan_result.get("json"), dict) else {}
        if args.dry_run:
            env_plan = {"status": "dry_run", "commands": [], "success_criteria": [], "env_name": "dry_run_env"}
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
            include_full_reproduction = bool(args.run_full_reproduction or not args.skip_full_reproduction)
            env_plan = normalize_environment_plan_commands(env_plan, machine=machine, policy_version=DECISION_POLICY_VERSION)
            env_plan = normalize_success_criteria(env_plan, paper_evidence=paper_evidence, policy_version=DECISION_POLICY_VERSION)
            write_json(env_plan_path, env_plan)
            plan_validation_issues = validate_environment_plan(env_plan, require_full_reproduction=include_full_reproduction, repo_path=repo_path, run_dir=run_dir, machine=machine, paper_evidence=paper_evidence)
            if plan_validation_issues:
                receipt = validation_receipt(plan_validation_issues, round_dir)
                receipts = [receipt]
                log_path = Path(str(receipt["log_path"]))
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(receipt["stderr_tail"] + "\n", encoding="utf-8")
            else:
                receipts = execute_plan_commands(env_plan, repo_path=repo_path, run_dir=run_dir, round_dir=round_dir, include_full=include_full_reproduction, default_timeout=args.command_timeout_sec, command_env=command_env)
        write_json(round_dir / "command_receipts.json", receipts)
        criteria_passed, metric_evidence = metric_criteria_passed(env_plan.get("success_criteria") if isinstance(env_plan.get("success_criteria"), list) else [], receipts, allowed_phases=APPROVAL_METRIC_PHASES)
        paper_alignment_ok, paper_alignment_issues = paper_config_alignment_passed(env_plan)
        judgement_path = round_dir / f"claude_reproduction_verdict_round_{round_index:02d}.json"
        judgement_result = run_claude_json(
            prompt_final_judgement(normalized, paper_evidence, env_plan, receipts, metric_evidence, judgement_path),
            cwd=run_dir,
            expected_json_path=judgement_path,
            log_path=round_dir / "logs" / "claude_reproduction_verdict.log",
            timeout_sec=args.claude_timeout_sec,
            add_dirs=[run_dir, repo_path] if repo_path.exists() else [run_dir],
            dry_run=args.dry_run,
            system_prompt="你是严格的复现裁决代理。批准必须有真实指标证据。",
            env=claude_env,
        )
        verdict = judgement_result.get("json") if isinstance(judgement_result.get("json"), dict) else {}
        if args.dry_run:
            verdict = {"decision": "continue_repair", "allow_next_module": False, "paper_claims_verified": False, "reproduction_success": False, "repair_plan": ["dry-run 未实际调用 Claude 或执行复现命令"]}
        backend_failure_taxonomy = classify_failures(receipts)
        verdict = normalize_verdict(verdict, receipts)
        verdict = enforce_reject_evidence(verdict)
        if verdict.get("decision") == "continue_repair" and backend_failure_taxonomy:
            verdict.setdefault("backend_failure_taxonomy", backend_failure_taxonomy)
        verdict_metric_ok, verdict_metric_issues = verdict_metric_evidence_supports_claims(verdict, receipts, env_plan.get("success_criteria") if isinstance(env_plan.get("success_criteria"), list) else [])
        approval_gate = build_approval_gate(
            verdict=verdict,
            receipts=receipts,
            paper_evidence=paper_evidence,
            env_plan=env_plan,
            repo_info=repo_info,
            repo_evidence=repo_evidence,
            machine=machine,
            run_dir=run_dir,
            criteria_passed=criteria_passed,
            metric_evidence=metric_evidence,
            verdict_metric_ok=verdict_metric_ok,
            verdict_metric_issues=verdict_metric_issues,
            paper_alignment_ok=paper_alignment_ok,
            paper_alignment_issues=paper_alignment_issues,
        )
        verdict["approval_gate"] = approval_gate
        approval_checks = {str(row.get("name")): row for row in approval_gate.get("checks", []) if isinstance(row, dict)}
        if not approval_checks.get("metric_evidence", {}).get("passed"):
            verdict["metric_evidence_binding_issues"] = verdict_metric_issues[:10]
        if not approval_checks.get("paper_config_alignment", {}).get("passed"):
            verdict["paper_config_alignment_issues"] = paper_alignment_issues[:10]
        if verdict.get("decision") == "approve" and not approval_gate.get("passed"):
            verdict["decision"] = "continue_repair"
            verdict["allow_next_module"] = False
            verdict.setdefault("repair_plan", []).append("后端降级：批准门槛未通过：" + "、".join(approval_gate.get("missing") or []) + "。")
        round_record.update({
            "receipts_path": str(round_dir / "command_receipts.json"),
            "receipt_count": len(receipts),
            "required_failures": [row for row in receipts if row.get("return_code") != 0 and row.get("required") is not False][:5],
            "backend_failure_taxonomy": backend_failure_taxonomy,
            "metric_evidence": metric_evidence,
            "paper_config_alignment_ok": paper_alignment_ok,
            "paper_config_alignment_issues": paper_alignment_issues,
            "plan_validation_issues": plan_validation_issues,
            "judgement_path": str(judgement_path),
            "claude_judgement_call": judgement_result,
            "verdict": verdict,
        })
        rounds.append(round_record)
        previous_rounds.append(round_record)
        final_verdict = verdict
        if verdict.get("decision") in {"approve", "reject"}:
            break

    decision = final_decision_payload(run_id, run_dir, normalized, repo_info, machine, rounds, final_verdict)
    terminal_reached = decision.get("decision") in {"approve", "reject"}
    rounds_this_invocation = max(0, len(rounds) - rounds_before_invocation)
    if terminal_reached:
        stop_reason = "terminal_decision"
    elif effective_until_terminal and args.max_total_rounds > 0 and len(rounds) >= args.max_total_rounds:
        stop_reason = "max_total_rounds_reached"
    elif effective_until_terminal:
        stop_reason = "continue_repair_without_terminal_decision"
    else:
        stop_reason = "max_repair_rounds_reached"
    resume_command = [
        "python",
        "modules/environment/main.py",
        "--action",
        "deploy_from_plan",
        "--plan",
        str(plan_path),
        "--run-id",
        run_id,
    ]
    if effective_until_terminal:
        resume_command.append("--until-terminal")
        if args.max_total_rounds > 0:
            resume_command.extend(["--max-total-rounds", str(args.max_total_rounds)])
    else:
        resume_command.extend(["--max-repair-rounds", str(effective_max_repair_rounds)])
    if args.skip_full_reproduction:
        resume_command.append("--skip-full-reproduction")
    decision["repair_loop"] = {
        "mode": "until_terminal" if effective_until_terminal else "bounded",
        "terminal_reached": terminal_reached,
        "stop_reason": stop_reason,
        "rounds_before_invocation": rounds_before_invocation,
        "rounds_this_invocation": rounds_this_invocation,
        "rounds_total": len(rounds),
        "max_repair_rounds_this_invocation": None if effective_until_terminal else effective_max_repair_rounds,
        "max_total_rounds": args.max_total_rounds if effective_until_terminal else None,
        "bounded_requested": bool(repair_settings["bounded_requested"]),
        "defaulted_until_terminal": bool(repair_settings["defaulted_until_terminal"]),
        "resume_command": " ".join(resume_command),
        "resume_note": "如果 stop_reason=max_total_rounds_reached，需要调大 --max-total-rounds 或去掉该上限后续跑。",
        "note": "非 dry-run 且未显式设置 --max-repair-rounds 时，本模块默认持续修复直到 approve/reject；continue_repair 表示仍可修复。",
    }
    return finalize_and_write_decision(decision, run_dir, paper_evidence, baseline_outside_paths)


if __name__ == "__main__":
    raise SystemExit(main())
