from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from scripts.common.io_utils import coerce_list, read_json, slugify


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _nested(data: dict[str, Any], *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def load_experiment_plan(path: Path) -> dict[str, Any]:
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        raise SystemExit(f"实验 plan 必须是 JSON object：{path}")
    return payload


def _repo_ref_fields(item: dict[str, Any]) -> dict[str, str]:
    revision = _first_text(item.get("revision"), item.get("rev"), item.get("ref"))
    return {
        "branch": _first_text(item.get("branch"), item.get("repo_branch"), item.get("git_branch")),
        "tag": _first_text(item.get("tag"), item.get("repo_tag"), item.get("version_tag"), item.get("release_tag")),
        "commit": _first_text(item.get("commit"), item.get("repo_commit"), item.get("commit_sha"), item.get("sha")),
        "revision": revision,
    }


def _repo_spec_from_value(value: Any, source: str, inherited_refs: dict[str, str] | None = None) -> dict[str, str] | None:
    refs = dict(inherited_refs or {})
    if isinstance(value, dict):
        url = _first_text(value.get("url"), value.get("repo_url"), value.get("github_url"), value.get("html_url"), value.get("clone_url"))
        refs.update({key: val for key, val in _repo_ref_fields(value).items() if val})
    else:
        url = str(value or "").strip()
    if not url:
        return None
    spec = {"url": url, "source": source}
    for key in ["branch", "tag", "commit", "revision"]:
        if refs.get(key):
            spec[key] = str(refs[key]).strip()
    return spec


def _selected_plan_payload(plan: dict[str, Any]) -> dict[str, Any]:
    selected = plan.get("selected_plan")
    if isinstance(selected, dict):
        selected_id = str(selected.get("plan_id") or selected.get("id") or plan.get("selected_plan_id") or "").strip()
        if selected_id and len(selected) <= 4:
            for row in coerce_list(plan.get("plans")):
                if isinstance(row, dict) and str(row.get("plan_id") or row.get("id") or "").strip() == selected_id:
                    return row
        return selected
    selected_id = str(plan.get("selected_plan_id") or "").strip()
    if selected_id:
        for row in coerce_list(plan.get("plans")):
            if isinstance(row, dict) and str(row.get("plan_id") or row.get("id") or "").strip() == selected_id:
                return row
    for row in coerce_list(plan.get("plans")):
        if isinstance(row, dict) and (row.get("selected_for_execution") or row.get("execute_next") or (isinstance(row.get("execution_selection"), dict) and row["execution_selection"].get("selected"))):
            return row
    return {}


def _append_repo_specs_from_container(specs: list[dict[str, str]], container: dict[str, Any], source_prefix: str = "") -> None:
    refs = _repo_ref_fields(container)
    direct_keys = ["repo_url", "github_url", "github_repo", "repository_url", "code_url", "url", "clone_url", "html_url"]
    for key in direct_keys:
        spec = _repo_spec_from_value(container.get(key), f"{source_prefix}{key}", refs)
        if spec:
            specs.append(spec)
    for key in ["repositories", "repo_candidates", "github_candidates", "code_candidates", "candidate_base_proposals", "base_candidates"]:
        for item in coerce_list(container.get(key)):
            spec = _repo_spec_from_value(item, f"{source_prefix}{key}")
            if spec:
                specs.append(spec)
            elif isinstance(item, dict):
                _append_repo_specs_from_container(specs, item, f"{source_prefix}{key}.")
    for key in ["repository", "repo", "base_repo", "selected_base", "candidate_base"]:
        nested = container.get(key)
        if isinstance(nested, dict):
            spec = _repo_spec_from_value(nested, f"{source_prefix}{key}")
            if spec:
                specs.append(spec)
            _append_repo_specs_from_container(specs, nested, f"{source_prefix}{key}.")


def github_candidate_specs(plan: dict[str, Any]) -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    _append_repo_specs_from_container(specs, plan)
    selected_plan = _selected_plan_payload(plan)
    if selected_plan:
        _append_repo_specs_from_container(specs, selected_plan, "selected_plan.")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for spec in specs:
        url = spec.get("url", "").strip()
        if url and url not in seen:
            seen.add(url)
            out.append(spec)
    return out


def github_candidates(plan: dict[str, Any]) -> list[str]:
    return [spec["url"] for spec in github_candidate_specs(plan) if spec.get("url")]


PERCENT_RE = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*%")
THRESHOLD_RE = re.compile(r"(?P<metric>scRMSD|pLDDT|scTM|TM-score|Spearman|MSE)\s*(?P<operator><=|>=|<|>|不超过|超过|达到|优于)\s*(?P<number>\d+(?:\.\d+)?)", re.I)


def _selected_plan_id(plan: dict[str, Any], selected_plan: dict[str, Any]) -> str:
    return _first_text(plan.get("selected_plan_id"), selected_plan.get("plan_id"), selected_plan.get("id"))


def _selected_idea_payload(plan: dict[str, Any]) -> dict[str, Any]:
    selected = plan.get("selected_idea")
    if isinstance(selected, dict):
        return selected
    selected_id = str(plan.get("selected_idea_id") or "").strip()
    if selected_id:
        for row in coerce_list(plan.get("ideas")):
            if isinstance(row, dict) and str(row.get("id") or row.get("idea_id") or "").strip() == selected_id:
                return row
    for row in coerce_list(plan.get("ideas")):
        if isinstance(row, dict) and (row.get("selected_for_execution") or row.get("execute_next") or (isinstance(row.get("execution_selection"), dict) and row["execution_selection"].get("selected"))):
            return row
    return {}


def _append_metric(out: list[dict[str, Any]], seen: set[tuple[str, str]], name: str, operator: str, value: Any, source: str, description: str = "") -> None:
    metric_name = str(name or "").strip()
    if not metric_name:
        return
    key = (metric_name.lower(), str(value))
    if key in seen:
        return
    seen.add(key)
    out.append({
        "name": metric_name,
        "operator": operator,
        "value": value,
        "source": source,
        "description": description or source,
    })


def _metric_name_from_text(text: str, fallback: str) -> str:
    lowered = str(text or "").lower()
    if "sctm" in lowered:
        return "sctm_reproduction_tolerance"
    if "design" in lowered or "设计" in lowered:
        return "designability_improvement"
    if "spearman" in lowered:
        return "spearman_correlation"
    if "mse" in lowered:
        return "mse"
    if "regression" in lowered or "退化" in lowered:
        return "quality_regression"
    if "p <" in lowered or "显著" in lowered:
        return "significance_p_value"
    compact = slugify(fallback or text, "metric")
    return compact[:80]


def _metrics_from_text(text: str, source: str, out: list[dict[str, Any]], seen: set[tuple[str, str]]) -> None:
    clean = str(text or "").strip()
    if not clean:
        return
    lowered = clean.lower()
    for match in THRESHOLD_RE.finditer(clean):
        raw_metric = match.group("metric")
        raw_operator = match.group("operator")
        operator = {"不超过": "<=", "超过": ">", "达到": ">=", "优于": ">="}.get(raw_operator, raw_operator)
        name = raw_metric.lower().replace("-", "_")
        if "差异" in clean or "偏差" in clean or "容限" in clean:
            name = f"{name}_reproduction_tolerance"
        _append_metric(out, seen, name, operator, float(match.group("number")), source, clean)
    for match in PERCENT_RE.finditer(clean):
        number_text = match.group("number")
        value = f"{number_text}%"
        start = max(0, match.start() - 80)
        end = min(len(clean), match.end() + 100)
        context = clean[start:end]
        context_lower = context.lower()
        before = clean[max(0, match.start() - 18):match.start()]
        after = clean[match.end():min(len(clean), match.end() + 18)]
        local_context = before + after
        if "参考" in context or "NMA-tune" in context or "nma-tune" in context_lower:
            continue
        if ("改善" in context or "improve" in context_lower) and not any(token in local_context for token in ["误差", "容限", "差异", "偏差", "退化", "不超过"]):
            operator = ">="
            name = "designability_improvement"
        elif "误差" in local_context or "容限" in local_context or "差异" in local_context or "偏差" in local_context or "退化" in local_context or "不超过" in context:
            operator = "<="
            if "43" in number_text and ("改善" in context or "improve" in context_lower):
                operator = ">="
                name = "designability_improvement"
            elif "设计" in context and "43" not in number_text:
                name = "designability_tolerance"
            elif "复现" in context or "baseline" in context_lower or "sctm" in context_lower:
                name = "reproduction_tolerance"
            else:
                name = _metric_name_from_text(context, f"tolerance_{len(out) + 1}")
        elif "不应超过" in context or "增加不应超过" in context:
            operator = "<="
            name = _metric_name_from_text(context, f"limit_{len(out) + 1}")
        elif "p <" in context_lower or "p<" in context_lower:
            operator = "<"
            name = "significance_p_value"
        else:
            operator = ">="
            name = _metric_name_from_text(context, f"metric_{len(out) + 1}")
        _append_metric(out, seen, name, operator, value, source, clean)


def _target_metrics_from_selected_plan(plan: dict[str, Any], selected_plan: dict[str, Any], selected_idea: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    explicit = plan.get("target_metrics") or plan.get("metrics") or _nested(plan, "reproduction", "metrics") or selected_plan.get("target_metrics") or selected_plan.get("metrics")
    for index, item in enumerate(coerce_list(explicit)):
        if isinstance(item, dict):
            name = _first_text(item.get("name"), item.get("metric"), item.get("metric_name"), f"explicit_metric_{index+1}")
            value = item.get("value") if "value" in item else item.get("target", item.get("paper_value", item.get("expected")))
            operator = _first_text(item.get("operator"), item.get("op"), ">=")
            source = _first_text(item.get("source"), item.get("paper_source"), item.get("evidence_source"), "plan.target_metrics")
            _append_metric(out, seen, name, operator, value, source, str(item))
        else:
            _metrics_from_text(str(item), f"plan.target_metrics[{index}]", out, seen)
    data_protocol = selected_plan.get("data_protocol") if isinstance(selected_plan.get("data_protocol"), dict) else {}
    for index, item in enumerate(coerce_list(data_protocol.get("evaluation_metrics"))):
        text = str(item or "").strip()
        if text:
            _metrics_from_text(text, f"selected_plan.data_protocol.evaluation_metrics[{index}]", out, seen)
    for stage_index, stage in enumerate(coerce_list(selected_plan.get("stages"))):
        if not isinstance(stage, dict):
            continue
        stage_name = str(stage.get("stage") or "").strip().lower()
        if stage_name and stage_name not in {"environment", "base_reproduction", "reference_reproduction"}:
            continue
        for key in ["tasks", "gates", "success_gate", "success_gates"]:
            for item_index, item in enumerate(coerce_list(stage.get(key))):
                _metrics_from_text(str(item or ""), f"selected_plan.stages[{stage_index}].{key}[{item_index}]", out, seen)
    for key in ["initial_experiment", "hypothesis", "new_method"]:
        value = selected_idea.get(key)
        if value:
            _metrics_from_text(str(value), f"selected_idea.{key}", out, seen)
    return out


def _selected_plan_dataset(selected_plan: dict[str, Any]) -> Any:
    data_protocol = selected_plan.get("data_protocol") if isinstance(selected_plan.get("data_protocol"), dict) else {}
    return data_protocol or selected_plan.get("dataset") or selected_plan.get("datasets") or selected_plan.get("data") or []


def _selected_plan_training(selected_plan: dict[str, Any], selected_idea: dict[str, Any]) -> dict[str, Any]:
    training: dict[str, Any] = {}
    for key in ["stages", "risk_assessment", "berlin_notation_summary"]:
        if key in selected_plan:
            training[key] = selected_plan.get(key)
    for key in ["initial_experiment", "hypothesis"]:
        if key in selected_idea:
            training[f"selected_idea_{key}"] = selected_idea.get(key)
    return training


def _paper_source_from_repo_specs(specs: list[dict[str, str]]) -> dict[str, str]:
    for spec in specs:
        url = str(spec.get("url") or "").lower()
        if "zhanghanni/rigidssl" in url:
            return {
                "title": "Rigidity-Aware Geometric Pretraining for Protein Design and Conformational Ensembles",
                "url": "https://openreview.net/forum?id=YAWpZcXHnP",
                "pdf_url": "https://openreview.net/pdf?id=YAWpZcXHnP",
                "source": "repo_candidate:ZhanghanNi/RigidSSL",
            }
    return {}


def normalize_plan(plan: dict[str, Any], source_path: Path) -> dict[str, Any]:
    selected_plan = _selected_plan_payload(plan)
    selected_idea = _selected_idea_payload(plan)
    selected_plan_id = _selected_plan_id(plan, selected_plan)
    title = _first_text(selected_plan.get("title"), plan.get("title"), plan.get("paper_title"), plan.get("name"), _nested(plan, "paper", "title"), source_path.stem)
    topic = _first_text(plan.get("topic"), plan.get("research_topic"), plan.get("task"), plan.get("objective"), selected_plan.get("description"), selected_idea.get("title"))
    repo_specs = github_candidate_specs(plan)
    inferred_paper_source = _paper_source_from_repo_specs(repo_specs)
    paper_url = _first_text(
        plan.get("paper_url"),
        plan.get("pdf_url"),
        plan.get("arxiv_url"),
        selected_plan.get("paper_url"),
        selected_plan.get("arxiv_url"),
        _nested(plan, "paper", "url"),
        _nested(plan, "paper", "pdf_url"),
        inferred_paper_source.get("url"),
    )
    target_metrics = _target_metrics_from_selected_plan(plan, selected_plan, selected_idea)
    dataset = plan.get("dataset") or plan.get("datasets") or plan.get("data") or _nested(plan, "reproduction", "dataset") or _selected_plan_dataset(selected_plan)
    training = plan.get("training") or plan.get("train") or plan.get("reproduction") or _selected_plan_training(selected_plan, selected_idea)
    return {
        "schema_version": "environment.normalized_plan.v2",
        "source_path": str(source_path),
        "title": title,
        "slug": slugify(title or topic or source_path.stem, "experiment"),
        "topic": topic,
        "paper_url": paper_url,
        "paper_source": inferred_paper_source,
        "repo_candidates": [spec["url"] for spec in repo_specs if spec.get("url")],
        "repo_candidate_specs": repo_specs,
        "selected_plan_id": selected_plan_id,
        "selected_plan": selected_plan,
        "selected_idea": selected_idea,
        "dataset": dataset,
        "target_metrics": target_metrics,
        "training": training,
        "raw": plan,
    }
