from __future__ import annotations

import re
from typing import Any

SUPPORTED_CRITERION_OPERATORS = {">=", ">", "<=", "<", "=="}
METRIC_NUMBER_RE = re.compile(r"^[+-]?(?:(?:\d+(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*%?$")
METRIC_NUMBER_FRAGMENT = r"[+-]?(?:(?:\d+(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*%?"
METRIC_NAME_SEPARATORS = r"[-_\s./@]*"


def _receipt_output_text(receipt: dict[str, Any]) -> str:
    return "\n".join(str(receipt.get(key) or "") for key in ["stdout_head", "stdout_tail", "stderr_tail"])


def _metric_name_tokens(name: str) -> list[str]:
    return [token for token in re.split(r"[^A-Za-z0-9]+", str(name or "").strip().lower()) if token]


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
    return scope == "environment_gate" or item.get("paper_metric") is False


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
        if op not in SUPPORTED_CRITERION_OPERATORS:
            all_passed = False
            evidence.append({"criterion": item, "metric": name, "passed": False, "reason": f"operator={op} 不受支持"})
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
        }[op]
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


def normalize_verdict(verdict: dict[str, Any]) -> dict[str, Any]:
    decision = str(verdict.get("decision") or "").strip().lower()
    if decision not in {"approve", "reject", "continue_repair"}:
        decision = "continue_repair"
    taxonomy = verdict.get("failure_taxonomy") if isinstance(verdict.get("failure_taxonomy"), list) else []
    allow_next = bool(decision == "approve" and verdict.get("allow_next_module") is True)
    return {
        **verdict,
        "decision": decision,
        "allow_next_module": allow_next,
        "failure_taxonomy": taxonomy,
    }
