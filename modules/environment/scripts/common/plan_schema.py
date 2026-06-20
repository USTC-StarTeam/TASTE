from __future__ import annotations

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


def normalize_plan(plan: dict[str, Any], source_path: Path) -> dict[str, Any]:
    title = _first_text(plan.get("title"), plan.get("paper_title"), plan.get("name"), _nested(plan, "paper", "title"), source_path.stem)
    topic = _first_text(plan.get("topic"), plan.get("research_topic"), plan.get("task"), plan.get("objective"))
    paper_url = _first_text(plan.get("paper_url"), plan.get("pdf_url"), plan.get("arxiv_url"), _nested(plan, "paper", "url"), _nested(plan, "paper", "pdf_url"))
    target_metrics = plan.get("target_metrics") or plan.get("metrics") or _nested(plan, "reproduction", "metrics") or []
    dataset = plan.get("dataset") or plan.get("datasets") or plan.get("data") or _nested(plan, "reproduction", "dataset") or []
    training = plan.get("training") or plan.get("train") or plan.get("reproduction") or {}
    return {
        "schema_version": "environment.normalized_plan.v1",
        "source_path": str(source_path),
        "title": title,
        "slug": slugify(title or topic or source_path.stem, "experiment"),
        "topic": topic,
        "paper_url": paper_url,
        "repo_candidates": github_candidates(plan),
        "repo_candidate_specs": github_candidate_specs(plan),
        "dataset": dataset,
        "target_metrics": target_metrics,
        "training": training,
        "raw": plan,
    }
