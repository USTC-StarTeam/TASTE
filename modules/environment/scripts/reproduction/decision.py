from __future__ import annotations

import re
from typing import Any

FAILURE_PATTERNS = [
    (
        "conda_environment",
        [
            "ResolvePackageNotFound", "PackagesNotFoundError", "UnsatisfiableError", "Solving environment",
            "No module named", "ImportError", "ModuleNotFoundError", "pip", "conda", "cuda version", "torch version",
        ],
    ),
    (
        "machine_compute",
        ["CUDA out of memory", "out of memory", "no kernel image", "CUDA error", "NVIDIA", "sm_", "显存", "illegal memory access"],
    ),
    (
        "repository_code",
        ["SyntaxError", "AttributeError", "KeyError", "FileNotFoundError", "bug", "not implemented", "No such file or directory", "Traceback"],
    ),
    (
        "dataset",
        ["dataset", "download", "403", "404", "No such file", "Permission denied", "数据集", "license", "data not found", "file not found", "corrupt"],
    ),
    (
        "paper_config",
        ["metric", "epoch", "hyperparameter", "config", "checkpoint", "论文配置", "batch size", "learning rate", "seed"],
    ),
]
PHASE_CATEGORY_HINTS = {
    "conda": "conda_environment",
    "install": "conda_environment",
    "verify": "conda_environment",
    "dataset": "dataset",
    "data": "dataset",
    "download": "dataset",
    "reproduce_full": "paper_config",
    "reproduce_smoke": "paper_config",
    "train": "paper_config",
    "eval": "paper_config",
    "plan_validation": "paper_config",
}
PHASE_CATEGORY_TOKEN_HINTS = [
    ("conda_environment", {"conda", "mamba", "micromamba", "pip", "install", "dependency", "dependencies", "deps", "requirement", "requirements", "env", "environment", "verify", "import", "setup"}),
    ("dataset", {"dataset", "datasets", "data", "download", "prepare", "preprocess", "preprocessing", "fetch", "extract", "build"}),
    ("paper_config", {"reproduce", "reproduction", "full", "smoke", "train", "training", "eval", "evaluate", "evaluation", "test", "benchmark", "metric", "checkpoint", "ckpt", "config"}),
]
PHASE_CATEGORY_COMPACT_HINTS = [
    ("conda_environment", {"conda", "mamba", "pip", "requirements", "dependency", "environment", "verifyimport"}),
    ("dataset", {"dataset", "downloaddata", "downloaddataset", "preparedata", "preparedataset", "preprocessdata", "preprocessdataset", "fetchdata", "fetchdataset"}),
    ("paper_config", {"reproducefull", "reproducesmoke", "trainfull", "evalfull", "evaluation", "benchmark", "checkpoint", "paperconfig"}),
]
SUPPORTED_CRITERION_OPERATORS = {">=", ">", "<=", "<", "=="}
METRIC_NUMBER_RE = re.compile(r"^[+-]?(?:(?:\d+(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*%?$")
METRIC_NUMBER_FRAGMENT = r"[+-]?(?:(?:\d+(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*%?"
METRIC_NAME_SEPARATORS = r"[-_\s./@]*"
METRIC_ALIAS_PATTERNS = {
    "accuracy": ["acc"],
    "acc": ["accuracy"],
    "f1": ["f1_score", "f1score"],
    "f1score": ["f1", "f1_score"],
    "auc": ["auroc", "roc_auc", "rocauc"],
    "auroc": ["auc", "roc_auc", "rocauc"],
    "meanaverageprecision": ["map", "m_ap"],
    "map": ["mean_average_precision"],
    "meaniou": ["miou", "mean_iou"],
    "miou": ["mean_iou"],
    "top1accuracy": ["top1", "top_1", "top-1", "acc@1", "top1_acc"],
    "top5accuracy": ["top5", "top_5", "top-5", "acc@5", "top5_acc"],
}


def _receipt_output_text(receipt: dict[str, Any]) -> str:
    return "\n".join(str(receipt.get(key) or "") for key in ["stdout_head", "stdout_tail", "stderr_tail"])


def _receipt_text(receipt: dict[str, Any]) -> str:
    return "\n".join([_receipt_output_text(receipt), *(str(receipt.get(key) or "") for key in ["status", "command", "phase"])])


def _matching_lines(text: str, markers: list[str], limit: int = 4) -> list[str]:
    lowered_markers = [marker.lower() for marker in markers]
    out: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if any(marker in lowered for marker in lowered_markers):
            out.append(line[:500])
        if len(out) >= limit:
            break
    return out


def _phase_tokens(phase: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9一-鿿]+", str(phase or "").strip().lower()) if token}


def _category_for_phase(phase: str) -> str:
    lowered = str(phase or "").strip().lower()
    exact = PHASE_CATEGORY_HINTS.get(lowered)
    if exact:
        return exact
    tokens = _phase_tokens(lowered)
    for category, hints in PHASE_CATEGORY_TOKEN_HINTS:
        if tokens & hints:
            return category
    compact = re.sub(r"[^a-z0-9一-鿿]+", "", lowered)
    for category, hints in PHASE_CATEGORY_COMPACT_HINTS:
        if any(hint in compact for hint in hints):
            return category
    return "unknown"


def classify_failures(receipts: list[dict[str, Any]], extra_text: str = "") -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    failed_receipts = [row for row in receipts if int(row.get("return_code") or 0) != 0]
    combined_extra = str(extra_text or "")
    for receipt in failed_receipts:
        text = _receipt_text(receipt) + "\n" + combined_extra
        lowered = text.lower()
        matched_any = False
        for category, markers in FAILURE_PATTERNS:
            hits = [marker for marker in markers if marker.lower() in lowered]
            if not hits:
                continue
            matched_any = True
            bucket = buckets.setdefault(
                category,
                {"category": category, "markers": [], "evidence": [], "phases": [], "commands": [], "return_codes": [], "repairable": True},
            )
            for marker in hits:
                if marker not in bucket["markers"]:
                    bucket["markers"].append(marker)
            for line in _matching_lines(text, hits):
                if line not in bucket["evidence"]:
                    bucket["evidence"].append(line)
            phase = str(receipt.get("phase") or "").strip()
            command = str(receipt.get("command") or "").strip()
            if phase and phase not in bucket["phases"]:
                bucket["phases"].append(phase)
            if command and command not in bucket["commands"]:
                bucket["commands"].append(command[:500])
            code = receipt.get("return_code")
            if code not in bucket["return_codes"]:
                bucket["return_codes"].append(code)
        if not matched_any:
            category = _category_for_phase(str(receipt.get("phase") or ""))
            bucket = buckets.setdefault(
                category,
                {"category": category, "markers": [], "evidence": [], "phases": [], "commands": [], "return_codes": [], "repairable": True},
            )
            phase = str(receipt.get("phase") or "").strip()
            command = str(receipt.get("command") or "").strip()
            status = str(receipt.get("status") or "").strip()
            if phase and phase not in bucket["phases"]:
                bucket["phases"].append(phase)
            if command and command not in bucket["commands"]:
                bucket["commands"].append(command[:500])
            code = receipt.get("return_code")
            if code not in bucket["return_codes"]:
                bucket["return_codes"].append(code)
            evidence = str(receipt.get("stderr_tail") or _receipt_output_text(receipt) or status or "命令失败但缺少日志摘要").strip()[:500]
            if evidence and evidence not in bucket["evidence"]:
                bucket["evidence"].append(evidence)
    out: list[dict[str, Any]] = []
    for bucket in buckets.values():
        bucket["markers"] = bucket.get("markers", [])[:8]
        bucket["evidence"] = bucket.get("evidence", [])[:8]
        bucket["phases"] = bucket.get("phases", [])[:8]
        bucket["commands"] = bucket.get("commands", [])[:5]
        bucket["return_codes"] = bucket.get("return_codes", [])[:8]
        out.append(bucket)
    if not out and failed_receipts:
        out.append({"category": "unknown", "markers": [], "evidence": ["命令失败但未匹配到已知类别"], "repairable": True})
    return out


def _metric_name_tokens(name: str) -> list[str]:
    return [token for token in re.split(r"[^A-Za-z0-9]+", str(name or "").strip().lower()) if token]


def _metric_name_compact(name: str) -> str:
    return "".join(_metric_name_tokens(name))


def _metric_name_patterns(name: str) -> list[tuple[str, str]]:
    tokens = _metric_name_tokens(name)
    if not tokens:
        return []
    variants: list[tuple[str, str]] = []
    raw = str(name or "").strip()
    if raw:
        variants.append((raw, re.escape(raw)))
    token_pattern = METRIC_NAME_SEPARATORS.join(re.escape(token) for token in tokens)
    variants.append((" ".join(tokens), token_pattern))
    compact = "".join(tokens)
    if compact and compact != raw.lower():
        variants.append((compact, re.escape(compact)))
    for alias in METRIC_ALIAS_PATTERNS.get(compact, []):
        alias_tokens = _metric_name_tokens(alias)
        alias_pattern = METRIC_NAME_SEPARATORS.join(re.escape(token) for token in alias_tokens) if alias_tokens else re.escape(alias)
        variants.append((alias, alias_pattern))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, pattern in variants:
        if not pattern or pattern in seen:
            continue
        seen.add(pattern)
        out.append((label, pattern))
    return out


def _metric_patterns(name: str) -> list[tuple[str, re.Pattern[str]]]:
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for label, name_pattern in _metric_name_patterns(name):
        regex = rf"(?<![A-Za-z0-9]){name_pattern}(?![A-Za-z0-9])\s*(?:[:=]|->|=>|is|为|：)?\s*({METRIC_NUMBER_FRAGMENT})"
        patterns.append((label, re.compile(regex, re.I)))
    return patterns


def _metric_value_has_percent(value: Any) -> bool:
    return isinstance(value, str) and value.strip().endswith("%")


def _coerce_metric_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace(",", "").strip()
    if normalized.endswith("%"):
        normalized = normalized[:-1].strip()
    if not METRIC_NUMBER_RE.match(text):
        return None
    try:
        return float(normalized)
    except Exception:
        return None


def _metric_line(name: str, text: str) -> tuple[float | None, str, str, str]:
    patterns = _metric_patterns(name)
    for raw in str(text or "").splitlines():
        for matched_name, pattern in patterns:
            match = pattern.search(raw)
            if not match:
                continue
            raw_value = match.group(1).strip()
            return _coerce_metric_number(raw_value), raw.strip()[:500], raw_value, matched_name
    return None, "", "", ""


def _normalize_metric_scale(observed: float, target: float, observed_has_percent: bool, target_has_percent: bool) -> tuple[float, float, bool]:
    if observed_has_percent == target_has_percent:
        return observed, target, False
    if target_has_percent and not observed_has_percent and abs(observed) <= 1 and abs(target) > 1:
        return observed * 100, target, True
    if observed_has_percent and not target_has_percent and abs(target) <= 1 and abs(observed) > 1:
        return observed, target * 100, True
    return observed, target, False


def compare_metric_values(
    observed: Any,
    target: Any,
    operator: str,
    observed_has_percent: bool | None = None,
    target_has_percent: bool | None = None,
) -> tuple[bool, dict[str, Any]]:
    op = str(operator or ">=").strip()
    observed_f = _coerce_metric_number(observed)
    target_f = _coerce_metric_number(target)
    evidence: dict[str, Any] = {
        "operator": op,
        "observed_raw": observed,
        "target_raw": target,
        "observed": observed_f,
        "target": target_f,
    }
    if observed_f is None or target_f is None:
        evidence["reason"] = "observed 或 target 不能解析为数字/百分比"
        return False, evidence
    if op not in SUPPORTED_CRITERION_OPERATORS:
        evidence["reason"] = f"operator={op} 不受支持"
        return False, evidence
    observed_percent = _metric_value_has_percent(observed) if observed_has_percent is None else bool(observed_has_percent)
    target_percent = _metric_value_has_percent(target) if target_has_percent is None else bool(target_has_percent)
    observed_cmp, target_cmp, scale_normalized = _normalize_metric_scale(observed_f, target_f, observed_percent, target_percent)
    passed = {
        ">=": observed_cmp >= target_cmp,
        ">": observed_cmp > target_cmp,
        "<=": observed_cmp <= target_cmp,
        "<": observed_cmp < target_cmp,
        "==": observed_cmp == target_cmp,
    }[op]
    evidence.update({
        "observed": observed_cmp,
        "target": target_cmp,
        "observed_has_percent": observed_percent,
        "target_has_percent": target_percent,
        "scale_normalized": scale_normalized,
        "passed": bool(passed),
    })
    return bool(passed), evidence


def _receipt_phase_allowed(receipt: dict[str, Any], allowed_phases: set[str] | None) -> bool:
    if not allowed_phases:
        return True
    phase = str(receipt.get("phase") or "").strip().lower()
    return phase in allowed_phases


def _ignored_optional_metric_phases(receipts: list[dict[str, Any]], allowed_phases: set[str] | None) -> list[str]:
    phases: list[str] = []
    for receipt in receipts:
        if receipt.get("required") is not False:
            continue
        if int(receipt.get("return_code") or 0) != 0:
            continue
        if not _receipt_phase_allowed(receipt, allowed_phases):
            continue
        phase = str(receipt.get("phase") or "").strip()
        if phase and phase not in phases:
            phases.append(phase)
    return phases


def _criterion_target_value(item: dict[str, Any]) -> tuple[bool, Any, str]:
    for key in ["value", "target", "paper_value"]:
        if key in item:
            return True, item.get(key), key
    return False, None, ""


def success_criteria_issues(criteria: Any) -> list[str]:
    if not isinstance(criteria, list) or not criteria:
        return ["环境计划缺少 success_criteria；没有论文指标/成功标准不能批准"]
    issues: list[str] = []
    for index, item in enumerate(criteria):
        if not isinstance(item, dict):
            issues.append(f"success_criteria[{index}] 不是 object")
            continue
        name = str(item.get("name") or item.get("metric") or "").strip()
        if not name:
            issues.append(f"success_criteria[{index}] 缺少 name/metric，无法对应论文指标和日志指标")
        operator = str(item.get("operator") or item.get("op") or "").strip()
        if not operator:
            issues.append(f"success_criteria[{index}] 缺少 operator/op，必须明确 >=、>、<=、< 或 ==")
        elif operator not in SUPPORTED_CRITERION_OPERATORS:
            issues.append(f"success_criteria[{index}] operator={operator} 不受支持，必须是 {sorted(SUPPORTED_CRITERION_OPERATORS)} 之一")
        target_found, target_value, _target_key = _criterion_target_value(item)
        if not target_found:
            issues.append(f"success_criteria[{index}] 缺少 value/target/paper_value，无法判断是否达到论文效果")
        elif isinstance(target_value, str) and not target_value.strip():
            issues.append(f"success_criteria[{index}] value/target/paper_value 为空")
        elif isinstance(target_value, (list, dict)):
            issues.append(f"success_criteria[{index}] value/target/paper_value 必须是可比较的标量，不能是 list/dict")
        elif _coerce_metric_number(target_value) is None:
            issues.append(f"success_criteria[{index}] value/target/paper_value 必须能解析为数字或百分比，不能是模糊文字")
        source = str(item.get("source") or item.get("paper_source") or item.get("evidence_source") or "").strip()
        if not source:
            issues.append(f"success_criteria[{index}] 缺少 source/paper_source/evidence_source，无法证明成功标准来自论文或 README")
    return issues


def _find_metric_observation(name: str, receipts: list[dict[str, Any]], allowed_phases: set[str] | None) -> tuple[float | None, dict[str, Any]]:
    for receipt in receipts:
        if int(receipt.get("return_code") or 0) != 0:
            continue
        if receipt.get("required") is False:
            continue
        if not _receipt_phase_allowed(receipt, allowed_phases):
            continue
        text = _receipt_output_text(receipt)
        observed, line, raw_value, matched_name = _metric_line(name, text)
        if observed is None:
            continue
        return observed, {
            "phase": receipt.get("phase"),
            "log_path": receipt.get("log_path"),
            "command": receipt.get("command"),
            "log_excerpt": line,
            "observed_raw": raw_value,
            "observed_has_percent": _metric_value_has_percent(raw_value),
            "matched_metric_name": matched_name,
        }
    return None, {}


def _criterion_is_environment_gate(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    scope = str(item.get("approval_scope") or "").strip().lower()
    return scope in {"environment_gate", "environment", "handoff", "runtime_gate", "operational_gate"} or item.get("paper_metric") is False


def metric_criteria_passed(criteria: list[Any], receipts: list[dict[str, Any]], allowed_phases: set[str] | None = None) -> tuple[bool, list[dict[str, Any]]]:
    paper_criteria = [item for item in criteria if not _criterion_is_environment_gate(item)]
    if not paper_criteria:
        return False, []
    evidence: list[dict[str, Any]] = []
    all_passed = True
    allowed_hint = sorted(allowed_phases) if allowed_phases else []
    ignored_optional_phases = _ignored_optional_metric_phases(receipts, allowed_phases)
    for item in paper_criteria:
        if not isinstance(item, dict):
            all_passed = False
            evidence.append({"criterion": item, "passed": False, "reason": "指标条件不是 object"})
            continue
        name = str(item.get("name") or item.get("metric") or "").strip()
        target_found, target, target_key = _criterion_target_value(item)
        op = str(item.get("operator") or item.get("op") or ">=").strip()
        if not name:
            all_passed = False
            evidence.append({"criterion": item, "passed": False, "reason": "缺少指标名 name/metric"})
            continue
        if not target_found:
            all_passed = False
            evidence.append({"criterion": item, "metric": name, "passed": False, "reason": "缺少 value/target/paper_value"})
            continue
        observed, source = _find_metric_observation(name, receipts, allowed_phases)
        observed_f = _coerce_metric_number(observed)
        target_f = _coerce_metric_number(target)
        if observed_f is None or target_f is None:
            all_passed = False
            evidence.append({
                "criterion": item,
                "metric": name,
                "passed": False,
                "reason": "无法从允许且必需的完整复现/评估阶段日志中解析 observed，或 target 不能解析为数字/百分比",
                "allowed_phases": allowed_hint,
                "ignored_optional_metric_phases": ignored_optional_phases,
            })
            continue
        observed_f, target_f, scale_normalized = _normalize_metric_scale(
            observed_f,
            target_f,
            observed_has_percent=bool(source.get("observed_has_percent")),
            target_has_percent=_metric_value_has_percent(target),
        )
        passed = {
            ">=": observed_f >= target_f,
            ">": observed_f > target_f,
            "<=": observed_f <= target_f,
            "<": observed_f < target_f,
            "==": observed_f == target_f,
        }.get(op, observed_f >= target_f)
        evidence.append({
            "metric": name,
            "operator": op,
            "target": target_f,
            "target_key": target_key,
            "observed": observed_f,
            "passed": passed,
            "source": source,
            "allowed_phases": allowed_hint,
            "scale_normalized": scale_normalized,
        })
        all_passed = all_passed and passed
    return all_passed, evidence


def normalize_verdict(verdict: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    decision = str(verdict.get("decision") or verdict.get("status") or "").strip().lower()
    if decision in {"approved", "approval", "pass", "passed"}:
        decision = "approve"
    if decision in {"rejected", "refuse", "refused", "fail_unrecoverable"}:
        decision = "reject"
    if decision not in {"approve", "reject", "continue_repair"}:
        decision = "continue_repair"
    taxonomy = verdict.get("failure_taxonomy") if isinstance(verdict.get("failure_taxonomy"), list) else []
    if not taxonomy:
        taxonomy = classify_failures(receipts, jsonish(verdict))
    allow_next = bool(decision == "approve" and verdict.get("allow_next_module") is True)
    return {
        **verdict,
        "decision": decision,
        "allow_next_module": allow_next,
        "failure_taxonomy": taxonomy,
    }


def jsonish(value: Any) -> str:
    try:
        import json
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)
