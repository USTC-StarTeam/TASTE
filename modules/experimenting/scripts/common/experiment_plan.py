#!/usr/bin/env python3
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from file_utils import slugify


@dataclass(slots=True)
class ExperimentPlan:
    path: Path
    raw: Any
    text: str
    experiment_id: str
    title: str
    method: str
    dataset: str
    metric: str
    run_command: str
    conda_env: str
    summary: str


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore
    except Exception as exc:  # pragma: no cover - 只在没有 PyYAML 时触发
        raise RuntimeError("读取 YAML 实验计划需要 PyYAML；请改用 JSON 或安装 pyyaml") from exc
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_plan_payload(path: Path) -> tuple[Any, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(text), text
    if suffix in {".yaml", ".yml"}:
        return _load_yaml(path), text
    return {"plan_text": text}, text


def _first_text(payload: Any, keys: list[str]) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    for container_key in ["experiment", "execution", "runtime", "selected_plan", "plan", "method"]:
        nested = payload.get(container_key)
        if isinstance(nested, dict):
            value = _first_text(nested, keys)
            if value:
                return value
    return ""


def _command_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ["run_command", "validation_command", "experiment_command", "command", "train_command"]:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            return " ".join(str(item) for item in value)
    for container_key in ["execution", "runtime", "experiment", "validation"]:
        nested = payload.get(container_key)
        if isinstance(nested, dict):
            value = _command_from_payload(nested)
            if value:
                return value
    return ""


def normalize_plan(path_text: str) -> ExperimentPlan:
    path = Path(path_text).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"实验计划不存在: {path}")
    raw, text = load_plan_payload(path)
    title = _first_text(raw, ["title", "name", "experiment_name", "hypothesis", "goal"]) or path.stem
    method = _first_text(raw, ["method", "method_slug", "approach", "variant"]) or slugify(title)
    dataset = _first_text(raw, ["dataset", "benchmark", "data", "corpus"])
    metric = _first_text(raw, ["metric", "primary_metric", "target_metric"])
    conda_env = _first_text(raw, ["conda_env", "env_name", "environment"])
    run_command = _command_from_payload(raw)
    experiment_id = _first_text(raw, ["experiment_id", "id", "plan_id", "selected_plan_id"]) or slugify(title)
    summary = _first_text(raw, ["summary", "description", "rationale", "claim", "hypothesis"]) or title
    return ExperimentPlan(
        path=path,
        raw=raw,
        text=text,
        experiment_id=slugify(experiment_id),
        title=title,
        method=slugify(method),
        dataset=dataset,
        metric=metric,
        run_command=run_command,
        conda_env=conda_env,
        summary=summary,
    )


def plan_prompt_block(plan: ExperimentPlan, limit: int = 16000) -> str:
    if isinstance(plan.raw, dict):
        try:
            payload = json.dumps(plan.raw, ensure_ascii=False, indent=2)
        except Exception:
            payload = plan.text
    else:
        payload = plan.text
    return payload[:limit]
